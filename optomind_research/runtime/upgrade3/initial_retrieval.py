"""Durable, bounded initial M2 retrieval, before any M3/planning handoff.

The M1 PLAN/outcome remains immutable. ACTIVE_QUERY_PLAN is M2's cumulative
execution artifact. The legacy corpus builder remains a one-pass primitive.
"""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
import math
from pathlib import Path
from types import SimpleNamespace

from ..artifact_store import atomic_write_json
from .candidate_corpus import (
    CORPUS_TARGET_LOW, DEFAULT_PAGE_SIZE, SNIPPET_DISCOVERY_TARGET,
    KEYWORD_SOURCE, QUESTION_SOURCE, Hit, QuerySpec, SnippetTextStore,
    _corpus_input_state, assemble_candidate_corpus, harvest_query, plan_queries, query_feedback,
)
from .module4.runtime import invoke_client

MODEL = "qwen3.5-plus"
MAX_ROUNDS = 3
MAX_NEW_QUERIES = 12
PROMPT_PATH = Path(__file__).resolve().parents[3] / "prompts" / "initial_retrieval_topup.md"
PROFILE = {"model": MODEL, "thinking": True, "thinking_budget": 8192,
           "max_output_tokens": 16384, "json_mode": False,
           "timeout_seconds": 300.0, "max_retries": 0, "max_keys": 1}


class InitialRetrievalError(RuntimeError):
    pass


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode("utf-8")).hexdigest()


def _file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _bind(path, value):
    """A changed/corrupt saved input is an error, never a cache miss."""
    path = Path(path)
    if path.exists():
        if _read(path) != value:
            raise InitialRetrievalError("initial_retrieval_input_changed:" + path.name)
    else:
        atomic_write_json(path, value)


def _fold(text):
    return " ".join(text.split()).casefold()


_METADATA_FIELDS = ("paper_id", "corpus_id", "doi", "title", "authors", "year", "venue", "abstract")


def _metadata_aliases(fields):
    aliases = set()
    if fields.get("paper_id"):
        aliases.add(fields["paper_id"])
    if fields.get("corpus_id") not in (None, ""):
        aliases.add("CorpusId:" + str(fields["corpus_id"]))
    return aliases


def _valid_metadata_fields(fields):
    return (isinstance(fields, dict) and set(fields) == set(_METADATA_FIELDS)
            and all(fields[k] is None or isinstance(fields[k], str)
                    for k in ("paper_id", "doi", "title", "venue", "abstract"))
            and (fields["corpus_id"] is None or type(fields["corpus_id"]) in (str, int))
            and (fields["year"] is None or type(fields["year"]) is int)
            and (fields["authors"] is None or isinstance(fields["authors"], list)
                 and all(isinstance(a, str) for a in fields["authors"]))
            and bool(_metadata_aliases(fields)))


class _MetadataJournalGateway:
    """Only successful enrichment fields, with exact prefix-view replay.

    Records are immutable. Views reference those records and remember missing
    IDs/HTTP outcomes, so later knowledge cannot rewrite an earlier paid input.
    Returning only rows linked to requested IDs adds no provenance-free papers.
    """
    def __init__(self, gateway, path):
        self.gateway, self.path = gateway, Path(path)
        self.prefix_length, self.integrity_error = 0, None
        self.data = {"schema_version": "optomind.initial_metadata.v1", "records": [], "views": {}}
        if self.path.exists():
            try:
                saved = _read(self.path)
                data = saved["data"]
                if saved.get("sha256") != _hash(data) or not self._valid(data):
                    raise ValueError("invalid metadata checksum or structure")
                self.data = data
            except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
                raise InitialRetrievalError("initial_retrieval_metadata_journal_corrupt") from exc

    @staticmethod
    def _valid(data):
        if (not isinstance(data, dict) or set(data) != {"schema_version", "records", "views"}
                or data["schema_version"] != "optomind.initial_metadata.v1"
                or not isinstance(data["records"], list) or not isinstance(data["views"], dict)):
            return False
        for row in data["records"]:
            if (not isinstance(row, dict) or set(row) != {"query_prefix_length", "fields"}
                    or type(row["query_prefix_length"]) is not int or row["query_prefix_length"] < 1
                    or not _valid_metadata_fields(row["fields"])):
                return False
        for key, view in data["views"].items():
            if (not isinstance(view, dict) or set(view) != {
                    "query_prefix_length", "requested_ids", "record_indexes", "unavailable_ids",
                    "provider_status_code", "transport_outcome", "error_type"}
                    or type(view["query_prefix_length"]) is not int or view["query_prefix_length"] < 1
                    or not isinstance(view["requested_ids"], list)
                    or any(not isinstance(i, str) or not i for i in view["requested_ids"])
                    or not isinstance(view["record_indexes"], list)
                    or not isinstance(view["unavailable_ids"], list)
                    or (view["provider_status_code"] is not None and type(view["provider_status_code"]) is not int)
                    or view["transport_outcome"] not in {"not_called", "success", "unsuccessful"}
                    or not isinstance(view["error_type"], str)
                    or key != _hash({"query_prefix_length": view["query_prefix_length"],
                                     "requested_ids": view["requested_ids"]})):
                return False
            available_aliases = set()
            for index in view["record_indexes"]:
                if type(index) is not int or not 0 <= index < len(data["records"]):
                    return False
                row = data["records"][index]
                if (row["query_prefix_length"] > view["query_prefix_length"]
                        or not _metadata_aliases(row["fields"]).intersection(view["requested_ids"])):
                    return False
                available_aliases.update(_metadata_aliases(row["fields"]))
            if view["unavailable_ids"] != [i for i in view["requested_ids"] if i not in available_aliases]:
                return False
        return True

    def check_integrity(self):
        # enrich_papers treats provider exceptions as best effort. Local journal
        # failures must escape that handler before any corpus/request is saved.
        if self.integrity_error is not None:
            raise InitialRetrievalError("initial_retrieval_metadata_journal_write_failed") from self.integrity_error

    def batch_papers(self, ids):
        try:
            return self._batch_papers(list(dict.fromkeys(ids)))
        except Exception as exc:
            self.integrity_error = exc
            raise

    def _batch_papers(self, ids):
        identity = {"query_prefix_length": self.prefix_length, "requested_ids": ids}
        key = _hash(identity)
        view = self.data["views"].get(key)
        if view is None:
            indexes = [i for i, row in enumerate(self.data["records"])
                       if row["query_prefix_length"] <= self.prefix_length
                       and _metadata_aliases(row["fields"]).intersection(ids)]
            aliases = set().union(*(_metadata_aliases(self.data["records"][i]["fields"]) for i in indexes))
            missing = [i for i in ids if i not in aliases]
            status, outcome, error_type = None, "not_called", ""
            if missing:
                try:
                    records, response = self.gateway.batch_papers(missing)
                    status = getattr(response, "status_code", None)
                    success = (type(status) is int and 200 <= status < 300
                               and getattr(response, "ok", True) is not False)
                    outcome = "success" if success else "unsuccessful"
                except Exception as exc:
                    records, success = [], False
                    outcome, error_type = "unsuccessful", type(exc).__name__
                if success:
                    for record in records:
                        fields = {k: getattr(record, k, None) for k in _METADATA_FIELDS}
                        if not _valid_metadata_fields(fields):
                            raise InitialRetrievalError("initial_retrieval_invalid_metadata_record")
                        if not _metadata_aliases(fields).intersection(missing):
                            continue
                        indexes.append(len(self.data["records"]))
                        self.data["records"].append({"query_prefix_length": self.prefix_length, "fields": fields})
                        aliases.update(_metadata_aliases(fields))
            view = {**identity, "record_indexes": indexes,
                    "unavailable_ids": [i for i in ids if i not in aliases],
                    "provider_status_code": status, "transport_outcome": outcome, "error_type": error_type}
            self.data["views"][key] = view
            atomic_write_json(self.path, {"data": self.data, "sha256": _hash(self.data)})
        records = [SimpleNamespace(**self.data["records"][i]["fields"]) for i in view["record_indexes"]]
        return records, SimpleNamespace(status_code=view["provider_status_code"],
                                         metadata_view_replayed=True)


def _original_queries(plan):
    facets = plan.get("facets")
    if not isinstance(facets, list) or not facets:
        raise InitialRetrievalError("initial_retrieval_facets_required")
    facet_ids = []
    for facet in facets:
        if not isinstance(facet, dict) or not isinstance(facet.get("id"), str) or not facet["id"].strip():
            raise InitialRetrievalError("initial_retrieval_invalid_facet")
        facet_ids.append(facet["id"])
        for key in ("keyword_queries", "question_queries"):
            if not isinstance(facet.get(key), list) or any(not isinstance(q, str) or not q.strip() for q in facet[key]):
                raise InitialRetrievalError("initial_retrieval_invalid_query_list:" + key)
    specs = plan_queries(plan)
    if len(set(facet_ids)) != len(facet_ids):
        raise InitialRetrievalError("initial_retrieval_duplicate_facet")
    if len({q.query_id for q in specs}) != len(specs) or len({_fold(q.query_text) for q in specs}) != len(specs):
        raise InitialRetrievalError("initial_retrieval_duplicate_original_query")
    return [{**asdict(q), "supplementary_round": 0, "round_label": "original"} for q in specs]


def build_supplement_request(plan, corpus, *, round_number, earlier_rounds=()):
    """Pure export seam: the real corpus can be inspected/replayed offline."""
    payload = {
        "stage": "initial_retrieval", "supplementary_round": round_number,
        "target_distinct_papers": CORPUS_TARGET_LOW,
        "current_distinct_papers": corpus["statistics"]["distinct_papers"],
        "original_user_question": plan.get("user_question", plan.get("question_en", "")),
        "question_source": "M1 normalized PLAN.question_en and original facets[].ask; not a claim of verbatim user wording",
        "original_user_subquestions": [{"facet_id": f["id"], "ask": f.get("ask", ""),
                                        "must_exclude": f.get("must_exclude", [])}
                                       for f in plan["facets"]],
        "original_plan": plan,
        # Complete measured feedback, including queries that returned zero hits.
        "executed_queries": query_feedback(corpus["query_audit"], corpus["retrieval_hits"]),
        "earlier_supplementary_rounds": list(earlier_rounds),
    }
    messages = [{"role": "system", "content": PROMPT_PATH.read_text(encoding="utf-8")},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]
    return {"model": MODEL, "profile": PROFILE, "payload": payload, "messages": messages}


def _validate_response(response, queries, plan, round_number):
    errors, accepted = [], []
    if response.get("returned_model") not in (None, "", MODEL):
        raise InitialRetrievalError("initial_retrieval_returned_model_mismatch")
    if response.get("complete") is False or response.get("finish_reason", "stop") != "stop":
        return [], ["incomplete_model_response"]
    content = response.get("content")
    try:
        if isinstance(content, str):
            content = content.strip()
            if content.startswith("```json\n") and content.endswith("```"):
                content = content[8:-3].strip()
        obj = json.loads(content) if isinstance(content, str) else content
    except (ValueError, TypeError):
        return [], ["response_not_json"]
    if not isinstance(obj, dict) or set(obj) != {"queries"} or not isinstance(obj["queries"], list):
        return [], ["expected_queries_array"]
    if len(obj["queries"]) > MAX_NEW_QUERIES:
        return [], ["too_many_queries"]
    ids = {q["query_id"] for q in queries}
    texts = {_fold(q["query_text"]) for q in queries}
    facets = {f["id"] for f in plan["facets"]}
    for index, item in enumerate(obj["queries"]):
        reason = ""
        if not isinstance(item, dict) or set(item) != {"query_id", "facet_id", "query_type", "query_text"}:
            reason = "invalid_query_fields"
        elif any(not isinstance(v, str) or not v.strip() for v in item.values()):
            reason = "invalid_query_value_type"
        else:
            item = {k: v.strip() for k, v in item.items()}
        if reason:
            pass
        elif item["facet_id"] not in facets:
            reason = "unknown_facet"
        elif item["query_type"] not in {"keyword", "question"}:
            reason = "invalid_query_type"
        elif item["query_id"] in ids:
            reason = "duplicate_query_id"
        elif _fold(item["query_text"]) in texts:
            reason = "duplicate_query_text"
        if reason:
            errors.append(f"queries[{index}]:{reason}")
            continue
        row = dict(item)
        row.update(retrieval_source=KEYWORD_SOURCE if row["query_type"] == "keyword" else QUESTION_SOURCE,
                   supplementary_round=round_number, round_label=f"supplement round{round_number}")
        ids.add(row["query_id"])
        texts.add(_fold(row["query_text"]))
        accepted.append(row)
    # Valid rows can run; refused rows and the full RAW response remain visible.
    return accepted, errors


def _save_response(path, response):
    atomic_write_json(path, {**response, "saved_response_sha256": _hash(response)})


def _saved_response(round_dir):
    path = round_dir / "RAW_RESPONSE.json"
    if path.exists():
        response = _read(path)
        checksum = response.pop("saved_response_sha256", None)
        if checksum != _hash(response):
            raise InitialRetrievalError("initial_retrieval_raw_response_corrupt")
        return response
    # Direct client saves the complete HTTP receipt before parsing/settlement.
    # Recovering it does not dispatch, settle, or erase an uncertain budget hold.
    receipts = sorted((round_dir / "http").glob("*.raw"))
    if len(receipts) > 1:
        raise InitialRetrievalError("initial_retrieval_multiple_raw_receipts")
    if receipts:
        raw = receipts[0].read_text(encoding="utf-8")
        obj = json.loads(raw)
        choices = obj.get("choices") or []
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise InitialRetrievalError("initial_retrieval_raw_receipt_invalid")
        choice = choices[0]
        response = {"content": choice.get("message", {}).get("content"),
                    "returned_model": obj.get("model"), "finish_reason": choice.get("finish_reason"),
                    "complete": choice.get("finish_reason") == "stop", "usage": obj.get("usage"),
                    "request_id": obj.get("id"), "raw_response": raw,
                    "recovered_from": str(receipts[0]), "budget_reconciliation": "unchanged"}
        _save_response(path, response)
        return response
    return None


def make_topup_client_factory(**config):
    """Lazy existing Qwen transport + supplied persistent project/account caps."""
    def factory(round_dir):
        from .module4.runtime import GlobalBudgetLedger, QwenDirectClient
        from .dual_budget import DualBudgetLedger, open_account_ledger, account_mapping_dir
        key_file, ledger_path = config.get("key_file"), config.get("budget_ledger_path")
        limit = config.get("budget_limit_cny")
        if (not key_file or not ledger_path or isinstance(limit, bool) or not isinstance(limit, (int, float))
                or not math.isfinite(limit) or limit <= 0):
            raise InitialRetrievalError("initial_topup_requires_key_and_existing_budget_with_absolute_cap")
        ledger_path = Path(ledger_path)
        if not ledger_path.is_file():
            raise InitialRetrievalError("initial_topup_existing_project_ledger_required")
        ledger = GlobalBudgetLedger(path=ledger_path, limit_cny=limit)
        if config.get("account_ledger_path"):
            account_path = Path(config["account_ledger_path"])
            if not account_path.is_file():
                raise InitialRetrievalError("initial_topup_existing_account_ledger_required")
            account = open_account_ledger(account_path, config.get("account_limit_cny"))
            ledger = DualBudgetLedger(ledger, account, config.get("account_mapping_directory") or account_mapping_dir(account_path))
            ledger.recover_settlements()
        return QwenDirectClient(**PROFILE, key_file=key_file, key_index=config.get("key_index", 1),
                                budget_ledger=ledger, raw_response_dir=round_dir / "http")
    return factory


def run_initial_retrieval(plan, *, output_dir, gateway, planner_state=None,
                          client_factory=None, qwen_config=None,
                          per_query_limit=DEFAULT_PAGE_SIZE, enrich=True,
                          snippet_discovery_target=SNIPPET_DISCOVERY_TARGET,
                          corpus_path=None, snippets_path=None, initial_topup=True):
    """Execute original queries once, then at most three new-query rounds.

    Same inputs/output directory resume automatically. Completed query harvests
    and paid replies are reused. Unknown dispatched model calls never auto-rebuy.
    """
    if not isinstance(per_query_limit, int) or isinstance(per_query_limit, bool) or not 1 <= per_query_limit <= 400:
        raise InitialRetrievalError("per_query_limit_out_of_range")
    _corpus_input_state(plan, planner_state, False)
    original_queries = _original_queries(plan)
    root = Path(output_dir).resolve()
    corpus_path = Path(corpus_path).resolve() if corpus_path else root / "CANDIDATE_CORPUS.json"
    snippets_path = Path(snippets_path).resolve() if snippets_path else root / "SNIPPET_TEXT.sqlite"
    configuration = {"schema_version": "optomind.initial_retrieval.v1", "target": CORPUS_TARGET_LOW,
                     "max_supplementary_rounds": MAX_ROUNDS, "initial_topup": bool(initial_topup),
                     "per_query_limit": per_query_limit, "enrich": bool(enrich),
                     "metadata_reuse": "durable_success_prefix_views.v1" if enrich else "disabled",
                     "snippet_discovery_target": snippet_discovery_target,
                     "corpus_path": str(corpus_path), "snippets_path": str(snippets_path),
                     "qwen": {**PROFILE, **(qwen_config or {})},
                     "prompt_sha256": _file_hash(PROMPT_PATH)}
    _bind(root / "CONFIG.json", configuration)
    _bind(root / "ORIGINAL_PLAN.json", plan)
    _bind(root / "ORIGINAL_PLANNER_OUTCOME.json", planner_state)
    handoff_path = root / "INITIAL_RETRIEVAL_HANDOFF.json"
    if handoff_path.exists():
        report = _read(handoff_path)
        for artifact in report["artifacts"]:
            if not Path(artifact["path"]).is_file() or _file_hash(artifact["path"]) != artifact["sha256"]:
                raise InitialRetrievalError("initial_retrieval_completed_artifact_corrupt")
        return _read(corpus_path)
    queries, rounds = list(original_queries), []
    skipped_query_ids = set()
    budget_limited = False
    hits, audits = [], []
    metadata = _MetadataJournalGateway(gateway, root / "METADATA_JOURNAL.json") if enrich else None
    store = SnippetTextStore(snippets_path)

    def save_active():
        completed_ids = {a["query_id"] for a in audits}
        atomic_write_json(root / "ACTIVE_QUERY_PLAN.json", {
            "schema_version": "optomind.initial_retrieval_execution_plan.v1",
            "original_plan_sha256": _hash(plan), "original_plan_snapshot": str(root / "ORIGINAL_PLAN.json"),
            "original_m1_plan": plan,
            "queries": [{**q, "execution_status": "complete" if q["query_id"] in completed_ids
                         else "not_executed_target_met" if q["query_id"] in skipped_query_ids else "pending"}
                        for q in queries], "supplementary_rounds": rounds})

    def execute(rows, *, stop_on_target=False):
        current = None
        for index, row in enumerate(rows):
            spec = QuerySpec(**{k: row[k] for k in QuerySpec.__dataclass_fields__})
            # IDs are user/model data; use a digest for safe local journal paths.
            path = root / "queries" / (_hash(asdict(spec)) + ".json")
            if path.exists():
                saved = _read(path)
                if saved.get("query") != row or saved.get("sha256") != _hash(saved.get("harvest")):
                    raise InitialRetrievalError("initial_retrieval_query_checkpoint_corrupt")
                found = [Hit(**h) for h in saved["harvest"]["hits"]]
                audit = saved["harvest"]["audit"]
            else:
                try:
                    found, audit = harvest_query(gateway, spec, per_query_limit=per_query_limit,
                                                 discovery_target=snippet_discovery_target)
                except BaseException as exc:
                    failures = root / "query_failures"
                    sequence = len(list(failures.glob("*.json"))) + 1
                    atomic_write_json(failures / f"{sequence:04d}.json", {
                        "query": row, "status": "interrupted" if not isinstance(exc, Exception) else "exception",
                        "error_type": type(exc).__name__, "error": str(exc),
                        "response_record": getattr(exc, "record", None)})
                    raise
                audit.update(supplementary_round=row["supplementary_round"], round_label=row["round_label"])
                harvest = {"hits": [asdict(h) for h in found], "audit": audit}
                atomic_write_json(path, {"query": row, "harvest": harvest, "sha256": _hash(harvest)})
            hits.extend(found)
            audits.append(audit)
            if stop_on_target:
                # Check the actual unified/enriched corpus after EACH new query.
                # Evidence from a returned query is complete and never trimmed.
                current = assemble()
                if current["statistics"]["distinct_papers"] >= CORPUS_TARGET_LOW:
                    skipped_query_ids.update(q["query_id"] for q in rows[index + 1:])
                    save_active()
                    break
            save_active()
        return current

    def assemble():
        if metadata is not None:
            metadata.check_integrity()
            metadata.prefix_length = len(audits)
        corpus = assemble_candidate_corpus(
            plan, hits=hits, audits=audits, gateway=metadata or gateway, per_query_limit=per_query_limit,
            enrich=enrich, snippet_store=store, planner_state=planner_state,
            snippet_discovery_target=snippet_discovery_target)
        if metadata is not None:
            metadata.check_integrity()
        store.connection.commit()
        atomic_write_json(corpus_path, corpus)
        return corpus

    try:
        save_active()
        execute(original_queries)
        corpus = assemble()
        for number in range(1, MAX_ROUNDS + 1):
            if not initial_topup or corpus["statistics"]["distinct_papers"] >= CORPUS_TARGET_LOW:
                break
            round_dir = root / f"supplement_round{number}"
            request = build_supplement_request(plan, corpus, round_number=number, earlier_rounds=rounds)
            _bind(round_dir / "REQUEST.json", request)
            if (round_dir / "BUDGET_LIMITED.json").exists():
                rounds.append(_read(round_dir / "BUDGET_LIMITED.json"))
                budget_limited = True
                save_active()
                break
            response = _saved_response(round_dir)
            if response is None:
                if (round_dir / "DISPATCHED.json").exists():
                    raise InitialRetrievalError("initial_retrieval_generation_uncertain_no_automatic_rebuy")
                factory = client_factory or make_topup_client_factory(**(qwen_config or {}))
                client = factory(round_dir)  # No setup at all on the >=500 branch.
                call_id = "initial-retrieval-" + _hash({"output_dir": str(root), "request": request})[:24] + f"-r{number}"
                _bind(round_dir / "DISPATCHED.json", {"call_id": call_id,
                                                      "request_sha256": _hash(request)})
                try:
                    response = invoke_client(client, request["messages"],
                                             **{k: v for k, v in PROFILE.items() if k not in {"max_retries", "max_keys"}},
                                             call_id=call_id)
                except BaseException as exc:
                    atomic_write_json(round_dir / "GENERATION_ERROR.json", {
                        "error_type": type(exc).__name__, "error": str(exc),
                        "response_record": getattr(exc, "record", None)})
                    if str(exc) == "global_budget_exceeded":
                        refusal = {"supplementary_round": number, "round_label": f"supplement round{number}",
                                   "status": "budget_limited", "errors": ["global_budget_exceeded"], "accepted_queries": []}
                        atomic_write_json(round_dir / "BUDGET_LIMITED.json", refusal)
                        rounds.append(refusal)
                        budget_limited = True
                        save_active()
                        break
                    raise
                # Paid response is durable BEFORE parsing, validation or queries.
                _save_response(round_dir / "RAW_RESPONSE.json", response)
            added, errors = _validate_response(response, queries, plan, number)
            validation = {"supplementary_round": number, "round_label": f"supplement round{number}",
                          "status": "accepted" if added else "invalid" if errors else "no_new_queries",
                          "errors": errors, "accepted_queries": added}
            _bind(round_dir / "VALIDATION.json", validation)
            rounds.append(validation)
            queries.extend(added)
            save_active()  # Append to the cumulative artifact before execution.
            corpus = execute(added, stop_on_target=True) or assemble()
            atomic_write_json(round_dir / "ROUND_RESULT.json", {
                **validation, "distinct_papers": corpus["statistics"]["distinct_papers"],
                "not_executed_target_met": sorted(skipped_query_ids),
                "query_feedback": corpus["query_audit"]})
        count = corpus["statistics"]["distinct_papers"]
        corpus["initial_retrieval"] = {
            "status": "complete", "target": CORPUS_TARGET_LOW, "target_met": count >= CORPUS_TARGET_LOW,
            "target_unmet": count < CORPUS_TARGET_LOW, "supplementation_enabled": bool(initial_topup),
            "supplementary_rounds_used": len(rounds), "max_supplementary_rounds": MAX_ROUNDS,
            "stop_reason": "legacy_opt_out" if not initial_topup else "target_met" if count >= CORPUS_TARGET_LOW else "budget_limited" if budget_limited else "round_limit",
            "transport_unsuccessful_queries": [a["query_id"] for a in corpus["query_audit"] if a["transport_outcome"] != "success"],
            "original_plan_snapshot": str(root / "ORIGINAL_PLAN.json"),
            "active_query_plan": str(root / "ACTIVE_QUERY_PLAN.json"), "downstream_allowed": True}
        atomic_write_json(corpus_path, corpus)
    finally:
        store.close()
    artifacts = [corpus_path, snippets_path, root / "ACTIVE_QUERY_PLAN.json",
                 root / "ORIGINAL_PLAN.json", root / "ORIGINAL_PLANNER_OUTCOME.json", root / "CONFIG.json"]
    artifacts += sorted((root / "queries").glob("*.json"))
    artifacts += sorted(p for p in root.glob("supplement_round*/*") if p.is_file())
    artifacts += sorted(root.glob("supplement_round*/http/*.raw"))
    if (root / "METADATA_JOURNAL.json").exists():
        artifacts.append(root / "METADATA_JOURNAL.json")
    atomic_write_json(handoff_path, {**corpus["initial_retrieval"],
                                   "corpus_path": str(corpus_path), "snippets_path": str(snippets_path),
                                   "artifacts": [{"path": str(p), "sha256": _file_hash(p)} for p in artifacts]})
    return corpus


def execute_initial_m2(*, plan_path, output_dir, outcome_path=None, run=False,
                       gateway_factory=None, client_factory=None, qwen_config=None, **options):
    """Production adapter for upstream callers; preview never creates a client.

    M1 outcome agreement is checked when provided. M3 consumes corpus_path only
    after INITIAL_RETRIEVAL_HANDOFF.status=complete/downstream_allowed=true.
    The historic external E/preparation driver is intentionally not modified.
    """
    plan = _read(plan_path)
    outcome = _read(outcome_path) if outcome_path else None
    if outcome is not None and outcome.get("plan") != plan:
        raise InitialRetrievalError("selected_plan_does_not_match_complete_m1_outcome")
    _corpus_input_state(plan, outcome, False)
    queries = _original_queries(plan)
    inputs = {"plan_path": str(Path(plan_path).resolve()), "plan_sha256": _file_hash(plan_path),
              "outcome_path": str(Path(outcome_path).resolve()) if outcome_path else None,
              "outcome_sha256": _file_hash(outcome_path) if outcome_path else None}
    preview = {"status": "preview", "entry": "execute_initial_m2", "inputs": inputs,
               "queries": queries, "initial_topup": options.get("initial_topup", True),
               "target": CORPUS_TARGET_LOW, "max_supplementary_rounds": MAX_ROUNDS,
               "qwen_profile": PROFILE, "qwen_config": qwen_config or {},
               "network_starts_only_with_run": True}
    if not run:
        atomic_write_json(Path(output_dir) / "PREVIEW.json", preview)
        return preview
    _bind(Path(output_dir) / "EXECUTION_INPUT.json", inputs)
    if gateway_factory is None:
        from optomind_research.s2_intelligence_gateway import S2IntelligenceGateway
        gateway_factory = S2IntelligenceGateway
    return run_initial_retrieval(plan, output_dir=output_dir, gateway=gateway_factory(),
                                 planner_state=outcome, client_factory=client_factory,
                                 qwen_config=qwen_config, **options)
