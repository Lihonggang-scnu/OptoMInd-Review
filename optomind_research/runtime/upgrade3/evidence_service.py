"""Configured R04/R05 host over existing index, snapshot, reader and repair APIs.

Preview never instantiates a model or gateway. Replay only uses explicit recorded
transport responses. Run is opt-in and uses existing finite reserve-before-HTTP
reader clients; no new reader/card/parser or scientific compiler is introduced.
"""
from __future__ import annotations
from dataclasses import replace
import json
import math
from pathlib import Path
from types import SimpleNamespace
from .article_evidence import EvidenceRequest, collect_evidence, source_unit_from_snapshot, read_evidence_source, request_binding
from .planning_pool_adapter import read, resolve, digest
from .offline_replay import canonical_hash, RecordedClient
from .evidence_assessment import validate_assessed_unit

SCHEMA = "review.evidence_service.v1"


class RecordedGateway:
    def __init__(self, path):
        self.data = read(path)
        if self.data.get("schema_version") != "review.gateway_replay.v1":
            raise ValueError("gateway_replay_version_invalid")

    def _call(self, method, query, kwargs):
        binding = canonical_hash({"method": method, "query": query, "params": kwargs})
        row = self.data.get("responses", {}).get(binding)
        if not row or row.get("request_sha256") != binding:
            raise ValueError("gateway_replay_request_missing:" + binding)
        items = [SimpleNamespace(**item) for item in row.get("items", [])]
        return items, SimpleNamespace(**row.get("response", {"ok": True, "status_code": 200}))

    def search_papers(self, query, **kwargs): return self._call("search_papers", query, kwargs)
    def search_snippets(self, query, **kwargs): return self._call("search_snippets", query, kwargs)


class EvidenceService:
    def __init__(self, config, *, root=".", reader_client=None, gateway=None):
        from .progressive_review_plan import load_planning_pool
        self.config = dict(config)
        if self.config.get("schema_version", SCHEMA) != SCHEMA:
            raise ValueError("evidence_service_version_invalid")
        self.mode = self.config.get("mode", "preview")
        if self.mode not in {"preview", "replay", "run"}:
            raise ValueError("evidence_service_mode_invalid")
        if self.mode == "run" and (reader_client is not None or gateway is not None):
            raise ValueError("injected_transports_require_replay_mode")
        self.root = Path(root).resolve()
        self.pool = resolve(config["planning_pool_path"], self.root)
        self.output = resolve(config["output_dir"], self.root)
        self.rows = load_planning_pool(self.pool) if self.pool.read_text().strip() else []
        self.by_id = {}
        for row in self.rows:
            if row.get("schema_version") != "planning_pool.v2":
                # Legacy metadata remains searchable but cannot become trusted
                # evidence until an explicit versioned snapshot map is supplied.
                continue
            for key in (row["paper_id"], row["canonical_paper_id"]):
                if key in self.by_id and self.by_id[key]["canonical_paper_id"] != row["canonical_paper_id"]:
                    raise ValueError("ambiguous_service_source_identity")
                self.by_id[key] = row
        self.reader_client = reader_client
        self.reading_store = None
        self.shared_ledger = None
        self.query_reviser = None
        self.repair_not_run_reason = None
        self.gateway = gateway
        self.versions = [{k: row.get(k) for k in ("paper_id", "canonical_paper_id", "snapshot_id", "snapshot_sha256", "plan_sha256", "card_sha256")}
                         for row in self.rows]
        self.input_hashes = {}
        for container, names in ((config, ("reader_replay_path", "assessments_path", "identity_map_path", "coverage_plan_path")),
                                 (config.get("repair") or {}, ("plan_path", "gateway_replay_path", "source_records_path", "revision_replay_path"))):
            for name in names:
                if container.get(name):
                    file = resolve(container[name], self.root)
                    self.input_hashes[str(file)] = digest(file)
        self.config_hash = canonical_hash(config)
        self.pool_hash = digest(self.pool)
        self.protocol_id = SCHEMA + ":" + canonical_hash({"config": config, "pool": self.pool_hash, "versions": self.versions, "input_hashes": self.input_hashes})
        self.assessments = read(resolve(config["assessments_path"], self.root)) if config.get("assessments_path") else {}
        self.repair_config = dict(config.get("repair") or {})
        self.source_records = []
        self.source_record_versions = {}
        if self.repair_config.get("source_records_path"):
            self.source_records = read(resolve(self.repair_config["source_records_path"], self.root)).get("records", [])
            for row in self.source_records:
                ident = row["canonical_paper_id"]
                if ident in self.source_record_versions:
                    raise ValueError("duplicate_registered_source_identity")
                if row.get("snapshot_dir"):
                    unit = source_unit_from_snapshot(snapshot_dir=resolve(row["snapshot_dir"], self.root), roles=["problem_boundary"])
                    if unit["canonical_paper_id"] != ident:
                        raise ValueError("registered_snapshot_identity_mismatch")
                    self.source_record_versions[ident] = {"snapshot_id": unit["snapshot_id"], "source_hash": unit["source_hash"]}
                elif row.get("local_xml"):
                    file = resolve(row["local_xml"], self.root)
                    self.input_hashes[str(file)] = digest(file)
                    self.source_record_versions[ident] = {"local_xml_sha256": digest(file)}
                else:
                    self.source_record_versions[ident] = {"status": "not_acquired"}
        self.protocol_id = SCHEMA + ":" + canonical_hash({"config": config, "pool": self.pool_hash,
            "versions": self.versions, "registered_versions": self.source_record_versions, "input_hashes": self.input_hashes})

    def preview(self, request):
        if not isinstance(request, EvidenceRequest): request = EvidenceRequest.from_mapping(request)
        return {"schema_version": SCHEMA, "mode": self.mode, "protocol_id": self.protocol_id,
            "config_sha256": self.config_hash, "request_binding": request_binding(request),
            "selected_pool_sha256": self.pool_hash, "source_versions": self.versions, "registered_source_versions": self.source_record_versions,
            "adapters": {"local_search": "PlanningMaterialIndex", "source": "PreparedSnapshotProvider",
                "reader": "run_directed_reading" if self.config.get("reader_enabled", True) else "explicit_assessments_only",
                "repair": "CoverageRepairRunner" if self.repair_config else None},
            "limits": {"request": dict(request.budget), "repair": self.repair_config.get("budget", {}),
                       "reader": {k: v for k, v in (self.config.get("reader_options") or {}).items() if k != "key_file"}},
            "network_calls": 0}

    def _unit(self, row, request):
        unit = source_unit_from_snapshot(snapshot_dir=row["snapshot_dir"], roles=request.material_roles)
        unit["provider_identifiers"] = dict(row.get("provider_identifiers") or {})
        unit["title"] = row.get("title", "")
        return unit

    def _read(self, *, request, source_unit):
        unit = source_unit
        if self.config.get("reader_enabled", True):
            from .directed_reading import DirectedReadingStore
            if self.mode == "replay" and self.reader_client is None:
                if not self.config.get("reader_replay_path"):
                    raise ValueError("reader_replay_path_required")
                self.reader_client = RecordedClient(resolve(self.config["reader_replay_path"], self.root))
            options = dict(self.config.get("reader_options") or {})
            if set(options) - {"key_file", "budget_ledger_path", "budget_limit_cny", "model", "max_output_tokens", "thinking_budget", "max_input_tokens"}:
                raise ValueError("reader_options_not_supported")
            for key in ("key_file", "budget_ledger_path"):
                if options.get(key): options[key] = str(resolve(options[key], self.root))
            if self.mode == "run":
                cap = options.get("budget_limit_cny")
                if not options.get("budget_ledger_path") or cap is None or not math.isfinite(float(cap)) or float(cap) <= 0:
                    raise ValueError("live_service_requires_shared_finite_reader_ledger")
            if self.reading_store is None:
                self.reading_store = DirectedReadingStore(self.output / "DIRECTED_READING.sqlite")
            unit = read_evidence_source(request=request, source_unit=unit,
                store=self.reading_store,
                output_dir=self.output / "readings" / request_binding(request) / canonical_hash(unit["canonical_paper_id"]),
                client=self.reader_client, **options)
        key = canonical_hash({"request_binding": request_binding(request), "source_hash": unit["source_hash"],
                              "snapshot_id": unit["snapshot_id"], "canonical_paper_id": unit["canonical_paper_id"]})
        assessment = self.assessments.get("assessments", {}).get(key)
        if assessment is not None:
            unit = validate_assessed_unit(request, unit, assessment)
        return unit

    def _resolve_hit(self, *, hit, request):
        ids = {str(v) for v in hit.identifiers.values() if v}
        candidates = [row for row in self.rows if row.get("canonical_paper_id") in ids or
                      ids.intersection(str(v) for v in (row.get("provider_identifiers") or {}).values())]
        candidates += [row for row in self.source_records if row.get("canonical_paper_id") in ids or
                       ids.intersection(str(v) for v in (row.get("provider_identifiers") or {}).values())]
        if not candidates:
            if self.mode != "run" or not self.repair_config.get("acquire_unregistered", False):
                return None
            ident = hit.identifiers.get("doi") or hit.identifiers.get("s2") or hit.identifiers.get("corpus")
            if not ident: return None
            candidates = [{**hit.fields, "canonical_paper_id": str(ident),
                "doi": hit.identifiers.get("doi", ""), "semantic_scholar_paper_id": hit.identifiers.get("s2", ""),
                "corpus_id": hit.identifiers.get("corpus", ""), "provider_identifiers": dict(hit.identifiers)}]
        canonical = {row["canonical_paper_id"] for row in candidates}
        if len(canonical) != 1:
            raise ValueError("repair_source_identity_ambiguous")
        row = dict(candidates[0])
        if row.get("snapshot_dir"):
            row["snapshot_dir"] = str(resolve(row["snapshot_dir"], self.root))
        else:
            from .material_acquisition import MaterialAcquirer, AcquisitionConfig
            acquirer = MaterialAcquirer(self.output / "acquired", config=AcquisitionConfig(
                cache_root=self.output / "acquisition_cache", rescue_enabled=False))
            if row.get("local_xml"):
                path = resolve(row["local_xml"], self.root)
                result = acquirer.acquire_local(row, xml_bytes=path.read_bytes(), source_name=path.name)
            elif self.mode == "run":
                result = acquirer.acquire(row)
            else:
                return None
            if result.snapshot is None: return None
            if result.identity.canonical_paper_id != row["canonical_paper_id"]:
                raise ValueError("repair_acquisition_identity_mismatch")
            row["snapshot_dir"] = str(result.snapshot.root)
        unit = self._unit(row, request)
        expected = self.source_record_versions.get(row["canonical_paper_id"], {})
        if expected.get("source_hash") and any(unit.get(k) != expected[k] for k in ("source_hash", "snapshot_id")):
            raise ValueError("registered_source_version_changed")
        return unit

    def _live_ledger(self):
        if self.mode != "run": return None
        if self.shared_ledger is None:
            options = self.config.get("reader_options") or {}
            cap = options.get("budget_limit_cny")
            if not options.get("budget_ledger_path") or cap is None or not math.isfinite(float(cap)) or float(cap) <= 0:
                raise ValueError("live_service_requires_shared_finite_reader_ledger")
            from .module4.runtime import GlobalBudgetLedger
            self.shared_ledger = GlobalBudgetLedger(limit_cny=float(cap), path=resolve(options["budget_ledger_path"], self.root))
        return self.shared_ledger

    def _repair(self, request):
        self.repair_not_run_reason = None
        if not self.repair_config: return None
        if not request.budget.get("max_supplementary_calls", 0):
            self.repair_not_run_reason = "max_supplementary_calls_reached"
            return None
        if not request.budget.get("max_papers", 8):
            self.repair_not_run_reason = "max_papers_reached"
            return None
        from .coverage_repair import CoverageRepairRunner
        from .candidate_corpus import plan_queries
        from .plan_contract import load_plan
        cfg = self.repair_config
        for key in ("max_rounds", "max_queries", "max_reads"):
            if (cfg.get("budget") or {}).get(key) == 0:
                self.repair_not_run_reason = key + "_reached"
                return None
        plan, _ = load_plan(resolve(cfg["plan_path"], self.root))
        gateway = self.gateway
        if gateway is None:
            if self.mode == "replay":
                gateway = RecordedGateway(resolve(cfg["gateway_replay_path"], self.root))
            else:
                from optomind_research.s2_intelligence_gateway import S2IntelligenceGateway
                gateway = S2IntelligenceGateway()
        parents = {s.query_id: s for s in plan_queries(plan)}
        revisions = cfg.get("query_revisions", [])
        def revise(**kwargs):
            rows = []
            for entry in revisions:
                parent = parents.get(entry["parent_query_id"])
                if parent and parent.facet_id in kwargs["unmet_facets"]:
                    rows.append({"parent_query_id": parent.query_id,
                        "spec": replace(parent, query_id=entry["query_id"], query_text=entry["query_text"])})
            return rows
        reviser = revise if revisions else None
        if cfg.get("revision_options") is not None:
            if revisions: raise ValueError("choose_static_or_model_query_revision")
            from .evidence_query_revision import EvidenceQueryReviser
            options = dict(cfg["revision_options"])
            if options.get("key_file"): options["key_file"] = str(resolve(options["key_file"], self.root))
            elif self.mode == "run" and (self.config.get("reader_options") or {}).get("key_file"):
                options["key_file"] = str(resolve(self.config["reader_options"]["key_file"], self.root))
            reviser = EvidenceQueryReviser(mode=self.mode, options=options, output_dir=self.output / "query_revision",
                ledger=self._live_ledger(), replay_path=resolve(cfg["revision_replay_path"], self.root) if cfg.get("revision_replay_path") else None)
            self.query_reviser = reviser
        paid = self.mode == "run" and (self.config.get("reader_enabled", True) or cfg.get("revision_options") is not None)
        return CoverageRepairRunner(plan=plan, facet_outputs=cfg["facet_outputs"], gateway=gateway,
            source_loader=self._resolve_hit, reader=self._read, query_reviser=reviser,
            budget=cfg.get("budget"), state_path=self.output / "repair" / (request_binding(request) + ".json"),
            paid_adapters=paid, budget_ledger=self._live_ledger() if paid else None)

    def request(self, request):
        if self.mode == "preview": raise ValueError("explicit_preview_only")
        if not isinstance(request, EvidenceRequest): request = EvidenceRequest.from_mapping(request)
        # Revalidate selected pool and source inputs each time, before model/network work.
        from .progressive_review_plan import load_planning_pool
        if self.pool.read_text().strip(): load_planning_pool(self.pool)
        if any(digest(path) != expected for path, expected in self.input_hashes.items()):
            raise ValueError("service_input_changed")
        if digest(self.pool) != self.pool_hash: raise ValueError("service_pool_changed")
        self.output.mkdir(parents=True, exist_ok=True)
        binding_file = self.output / "SERVICE_BINDING.json"
        binding = {"protocol_id": self.protocol_id, "pool_sha256": self.pool_hash}
        if binding_file.exists() and read(binding_file) != binding:
            raise ValueError("service_binding_changed_use_new_output")
        binding_file.write_text(json.dumps(binding, indent=2))
        from .planning_material_search import SearchIndexConfig, PlanningMaterialIndex, build_index
        index_path = resolve(self.config.get("index_path", str(self.output / "MATERIAL_INDEX.sqlite")), self.root)
        if self.config.get("identity_map_path"):
            identity_path = resolve(self.config["identity_map_path"], self.root)
        else:
            identity_path = self.output / "SOURCE_IDENTITIES.json"
            identity_path.write_text(json.dumps({"source_identity_map": {f"P{i:04d}": {
                "paper_id": row["paper_id"], "canonical_paper_id": row.get("canonical_paper_id"),
                "card_path": row.get("card_path"), "title": row.get("title", "")}
                for i, row in enumerate(self.rows, 1)}}, indent=2))
        index_binding_path = index_path.with_suffix(index_path.suffix + ".service_binding.json")
        index_binding = {"protocol_id": self.protocol_id, "pool_sha256": self.pool_hash,
                         "identity_map_sha256": digest(identity_path)}
        if index_binding_path.exists():
            if read(index_binding_path) != index_binding:
                raise ValueError("service_index_binding_mismatch")
        elif index_path.exists():
            raise ValueError("existing_index_requires_explicit_service_binding")
        if self.rows:
            from .planning_material_search import load_identity_map
            identities = load_identity_map(identity_path)
            if {str(row.get("paper_id")) for row in identities.values()} - {row["paper_id"] for row in self.rows}:
                raise ValueError("service_identity_map_foreign_paper")
        index_binding_path.parent.mkdir(parents=True, exist_ok=True)
        index_binding_path.write_text(json.dumps(index_binding, indent=2))
        if self.rows:
            build_index(SearchIndexConfig(topic_id=self.protocol_id, index_path=index_path, pool_path=self.pool,
                identity_map_path=identity_path, snapshot_roots=tuple(Path(row["snapshot_dir"]) for row in self.rows if row.get("snapshot_dir"))))
        else:
            empty = PlanningMaterialIndex(index_path)
            if empty._conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0]:
                empty.close()
                raise ValueError("empty_pool_index_contains_foreign_papers")
            empty.close()
        index = PlanningMaterialIndex(index_path, readonly=True)
        try:
            repair = self._repair(request)
            bundle = collect_evidence(request, index=index,
                source_resolver=lambda hit: self._unit(self.by_id[hit.paper_id], request) if hit.paper_id in self.by_id else None,
                directed_reader=self._read, supplementary_search=repair)
        finally:
            index.close()
        if self.repair_not_run_reason:
            bundle = replace(bundle, diagnostics=tuple(bundle.diagnostics) + ({"stage": "coverage_repair",
                "stop_reason": self.repair_not_run_reason},))
        receipt = {**self.preview(request), "mode": self.mode,
            "network_calls": 0 if self.mode == "replay" else None, "evidence_bundle": bundle.to_dict(),
            "repair_state": repair.state if repair else None, "repair_not_run_reason": self.repair_not_run_reason, "scientific_quality_certified": False,
            "revision_messages": self.query_reviser.last_messages if self.query_reviser else None}
        if self.repair_config or self.config.get("coverage_plan_path"):
            from .plan_contract import load_plan
            _, plan_receipt = load_plan(resolve(self.repair_config.get("plan_path") or self.config["coverage_plan_path"], self.root))
            receipt.update(plan_hash=plan_receipt["plan_sha256"], facet_outputs=self.repair_config.get("facet_outputs") or self.config.get("facet_outputs"))
        path = self.output / ("EVIDENCE_" + request_binding(request) + "_" + canonical_hash(receipt)[:24] + ".json")
        path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2))
        self.last_receipt_path = str(path)
        return bundle


def create_evidence_service(config, *, root=".", reader_client=None, gateway=None):
    return EvidenceService(config, root=root, reader_client=reader_client, gateway=gateway)
