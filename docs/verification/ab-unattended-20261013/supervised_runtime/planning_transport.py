"""Test-run transport binding around the unchanged production planning entrypoints.

This module owns no planner, prompt, provider retry, or pricing implementation.
The existing Qwen provider owns one physical reserve/settle through the existing
DualBudgetLedger.  The existing SupervisedClient owns both durable review gates.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
import hashlib
import inspect
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading

HERE = Path(__file__).resolve().parent
RUN_ROOT = HERE.parent.parent
DEFAULT_BINDING = RUN_ROOT / "RUN_BINDING_PARSER_RECOVERY_V2_20261013.json"
_CONTEXT_LOCK = threading.Lock()
_ACTIVE = False
MAX_ROLES = frozenset({"outline_selection", "strong_outline"})
_PLANNING_MODULES = frozenset({
    "optomind_research.runtime.upgrade3.progressive_review_plan",
    "optomind_research.runtime.upgrade3.planning_supplement",
    "optomind_research.runtime.upgrade3.planning_material_triage",
    "optomind_research.runtime.upgrade3.directed_reading",
    "optomind_research.runtime.upgrade3.paper_reading_card",
    "optomind_research.runtime.upgrade3.quality_capacity",
    "scripts.upgrade3.chapter_arrangement",
})


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def _read_limit(path):
    path = Path(path).resolve()
    if not path.is_file():
        raise ValueError("existing_ledger_required:no_budget_reset")
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        row = db.execute("SELECT value FROM budget_meta WHERE key='limit_cny'").fetchone()
    if row is None:
        raise ValueError("stored_ledger_limit_required")
    return float(row[0])


def load_binding(path=DEFAULT_BINDING):
    path = Path(path).resolve()
    raw = path.read_bytes()
    value = json.loads(raw)
    if (value.get("project_limit_cny") != 80.0 or value.get("key_index") != 1
            or value.get("qwen_connection") != "direct"
            or value.get("physical_retries") != 0 or value.get("key_rotation") is not False
            or not value.get("source_commit")):
        raise ValueError("planning_binding_policy_mismatch")
    if Path(value["project_ledger"]).resolve() == Path(value["account_ledger"]).resolve():
        raise ValueError("distinct_ledgers_required")
    return value, hashlib.sha256(raw).hexdigest()


def source_pin(binding):
    root = Path(binding["source_worktree"]).resolve()
    read_only_git = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}
    result = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True, env=read_only_git)
    if result.stdout.strip() != binding["source_commit"]:
        raise ValueError("source_pin_mismatch")
    for flags in (["diff", "--quiet"], ["diff", "--cached", "--quiet"]):
        result = subprocess.run(["git", "-C", str(root), *flags], capture_output=True, env=read_only_git)
        if result.returncode != 0:
            raise ValueError("tracked_source_changed")


class PlanningTransport:
    """Scoped constructor adapter. Tests must supply an explicit fake provider.

    Import and preview never resolve credentials. Normal construction uses the
    saved original class exactly once; it never calls SupervisedFactory.
    """
    def __init__(self, *, binding_path=DEFAULT_BINDING, supervision_root=None,
                 allow_planning_max=False, fake_provider=None, pin_check=source_pin):
        self.binding_path = Path(binding_path).resolve()
        self.binding, self.binding_sha256 = load_binding(self.binding_path)
        self.root = self.binding_path.parent
        self.supervision_root = Path(supervision_root or self.root / "supervision").resolve()
        self.allow_planning_max = bool(allow_planning_max)
        self.fake_provider = fake_provider
        self.pin_check = pin_check
        self.total = None
        self.duals = {}
        self.constructed = []
        self._ledger_lock = threading.RLock()

    def check(self):
        if hashlib.sha256(self.binding_path.read_bytes()).hexdigest() != self.binding_sha256:
            raise ValueError("binding_changed")
        self.pin_check(self.binding)
        if _read_limit(self.binding["project_ledger"]) != 80.0:
            raise ValueError("project_cap_changed:no_budget_reset")
        if _read_limit(self.binding["account_ledger"]) != 300.0:
            raise ValueError("account_cap_changed:no_budget_reset")

    def _paired(self, ledger):
        from optomind_research.runtime.upgrade3.dual_budget import DualBudgetLedger
        from optomind_research.runtime.upgrade3.module4.runtime import GlobalBudgetLedger
        project = ledger.project if isinstance(ledger, DualBudgetLedger) else ledger
        if project is None or Path(project.path).resolve() != Path(self.binding["project_ledger"]).resolve():
            raise ValueError("project_ledger_identity_mismatch")
        if isinstance(ledger, DualBudgetLedger):
            if Path(ledger.total.path).resolve() != Path(self.binding["account_ledger"]).resolve():
                raise ValueError("account_ledger_identity_mismatch")
            return ledger
        # Keep the original project object, including RoundCappedLedger's
        # reserve() and settlement interface; never reconstruct it by its name.
        with self._ledger_lock:
            if self.total is None:
                self.total = GlobalBudgetLedger(path=Path(self.binding["account_ledger"]))
                self.total._refresh_from_db()
            if id(project) not in self.duals:
                self.duals[id(project)] = DualBudgetLedger(project, self.total, self.root / "account_mappings")
            return self.duals[id(project)]

    def model_permission(self, module, role, model):
        if "-max" in str(model).lower() and not (
                self.allow_planning_max and module.endswith(".quality_capacity") and role in MAX_ROLES):
            raise ValueError("max_requires_explicit_planning_role_permission")

    def regional_client_constructor(self, args):
        """Explicit native-factory hook; its existing proxy owns both ledgers.

        Validate the factory's configured routes rather than pairing its locked
        reserve/settle proxy again. This never broadens scoped caller admission.
        """
        configured = dict(vars(args))

        def check_routes():
            for option, bound in (("budget_ledger", "project_ledger"),
                                  ("account_ledger", "account_ledger"), ("key_file", "key_file")):
                value = configured.get(option)
                if value is None or Path(value).resolve() != Path(self.binding[bound]).resolve():
                    raise ValueError("regional_factory_route_mismatch:" + option)
            limit = configured.get("budget_limit", configured.get("budget_limit_cny"))
            if (limit != 80.0 or configured.get("account_budget_limit") != 300.0
                    or configured.get("key_index") != 1 or not configured.get("recognition_run")):
                raise ValueError("regional_factory_policy_mismatch")
            ceiling = configured.get("recognition_ceiling_cny")
            if isinstance(ceiling, bool) or not isinstance(ceiling, (int, float)) or not 0 < ceiling <= 80.0:
                raise ValueError("regional_factory_ceiling_invalid")

        check_routes()

        def construct(**options):
            check_routes()
            self.check()
            if options.get("key_file") is None or Path(options["key_file"]).resolve() != Path(self.binding["key_file"]).resolve():
                raise ValueError("key_route_identity_mismatch")
            if options.get("base_url") not in (None, "https://dashscope.aliyuncs.com/compatible-mode/v1"):
                raise ValueError("direct_provider_destination_required")
            module, role = "optomind_research.runtime.upgrade3.regional_recognition", "regional_recognition"
            self.model_permission(module, role, options.get("model"))
            parameters = dict(options)
            parameters.update(key_index=1, max_keys=1, max_retries=0)
            provider = (self.fake_provider or self.original)(**parameters)
            wrapper = _GatedClient(self, provider, module=module, role=role)
            self.constructed.append(wrapper)
            return wrapper

        return construct

    def construct(self, **options):
        frames = inspect.stack()
        try:
            if any(frame.frame.f_globals.get("__name__") == "supervised_qwen" for frame in frames):
                raise ValueError("existing_supervised_factory_cannot_be_nested")
            caller = frames[1].frame
            module = caller.f_globals.get("__name__", "")
            caller_path = Path(caller.f_code.co_filename).resolve()
            role = str(caller.f_locals.get("role") or "") if module.endswith(".quality_capacity") else ""
        finally:
            del frames
        if module not in _PLANNING_MODULES:
            raise ValueError("client_outside_authorized_planning_modules:" + module)
        if not caller_path.is_relative_to(Path(self.binding["source_worktree"]).resolve()):
            raise ValueError("caller_source_root_mismatch")
        self.check()
        model = str(options.get("model") or "qwen3.7-flash")
        self.model_permission(module, role, model)
        supplied_key = options.get("key_file")
        if supplied_key is None or Path(supplied_key).resolve() != Path(self.binding["key_file"]).resolve():
            raise ValueError("key_route_identity_mismatch")
        if options.get("base_url") not in (None, "https://dashscope.aliyuncs.com/compatible-mode/v1"):
            raise ValueError("direct_provider_destination_required")
        parameters = dict(options)
        parameters.update(key_index=1, max_keys=1, max_retries=0,
                          budget_ledger=self._paired(options.get("budget_ledger")))
        provider = (self.fake_provider or self.original)(**parameters)
        wrapper = _GatedClient(self, provider, module=module, role=role)
        self.constructed.append(wrapper)
        return wrapper

    @contextmanager
    def activate(self):
        global _ACTIVE
        with _CONTEXT_LOCK:
            if _ACTIVE:
                raise ValueError("nested_planning_transport_forbidden")
            _ACTIVE = True
        source = str(Path(self.binding["source_worktree"]).resolve())
        prep = str(HERE.parent / "supervisor")
        for value in (source, prep):
            if value not in sys.path:
                sys.path.insert(0, value)
        runtime = None
        saved_aliases = []
        replacement = self.construct
        try:
            from optomind_research.runtime.upgrade3.module4 import runtime
            # Both modules bind the class at import time. Importing before the
            # swap also lets the same restoration handle native supplement A/B.
            from optomind_research.runtime.upgrade3 import directed_reading, paper_reading_card
            self.original = runtime.QwenDirectClient
            for name, loaded in list(sys.modules.items()):
                if name.startswith(("optomind_research", "scripts.upgrade3")):
                    origin = getattr(loaded, "__file__", None)
                    if origin and not Path(origin).resolve().is_relative_to(Path(source)):
                        raise ValueError("loaded_module_source_root_mismatch:" + name)
            self.check()
            # Capture only the exact original class aliases in the bound source
            # package. Unauthorised module constructors then fail closed too.
            for name, loaded in list(sys.modules.items()):
                if name.startswith(("optomind_research.runtime.upgrade3", "scripts.upgrade3")):
                    if getattr(loaded, "QwenDirectClient", None) is self.original:
                        saved_aliases.append((loaded, self.original))
                        loaded.QwenDirectClient = replacement
            yield self
        finally:
            for loaded, original in saved_aliases:
                loaded.QwenDirectClient = original
            # A module first imported inside the context may retain its import
            # alias after exit; restore that alias as well.
            if hasattr(self, "original"):
                for name, loaded in list(sys.modules.items()):
                    if name.startswith(("optomind_research.runtime.upgrade3", "scripts.upgrade3")):
                        if getattr(loaded, "QwenDirectClient", None) is replacement:
                            loaded.QwenDirectClient = self.original
            with _CONTEXT_LOCK:
                _ACTIVE = False


class _GatedClient:
    def __init__(self, adapter, provider, *, module, role):
        self.adapter, self.provider, self.module, self.role = adapter, provider, module, role

    def __getattr__(self, name):
        return getattr(self.provider, name)

    def complete(self, messages, **kwargs):
        return self(messages, **kwargs)

    def __call__(self, messages, **kwargs):
        from supervised_client import SupervisedClient
        from optomind_research.runtime.upgrade3.module4.runtime import (
            build_qwen_wire_body, estimate_prompt_tokens, estimated_cost_cny)
        self.adapter.check()
        live = self.provider
        model = str(kwargs.get("model") or "").strip() or live.model
        self.adapter.model_permission(self.module, self.role, model)
        if model != live.model:
            raise ValueError("runtime_model_mismatch")
        thinking = bool(kwargs.get("thinking", live.thinking))
        budget = int(kwargs.get("thinking_budget", live._configured_thinking_budget)) if thinking else 0
        answer = max(64, int(kwargs.get("max_output_tokens") or live.max_output_tokens))
        timeout = max(5.0, float(kwargs.get("timeout_seconds", live.timeout_seconds)))
        overall = max(timeout, float(kwargs.get("stream_overall_timeout_seconds", live.stream_overall_timeout_seconds)))
        profile = {"model": model, "thinking": thinking,
            "thinking_budget": budget, "max_output_tokens": answer,
            "json_mode": live.json_mode, "stream": bool(kwargs.get("stream", False)),
            "timeout_seconds": timeout, "stream_overall_timeout_seconds": overall,
            "prompt_token_multiplier": live.prompt_token_multiplier,
            "prompt_token_framing_margin": live.prompt_token_framing_margin,
            "max_retries": 0, "max_keys": 1, "key_index": 1, "connection": "direct",
            "source_commit": self.adapter.binding["source_commit"],
            "binding_sha256": self.adapter.binding_sha256, "module": self.module, "role": self.role,
            "project_ledger": str(Path(self.adapter.binding["project_ledger"]).resolve()),
            "account_ledger": str(Path(self.adapter.binding["account_ledger"]).resolve()),
            "project_limit_cny": 80.0, "account_limit_cny": 300.0,
            "temperature": float(kwargs.get("temperature", 0.1))}
        raw, body = build_qwen_wire_body(messages, model=profile["model"], max_output_tokens=answer,
            thinking=thinking, thinking_budget=budget, json_mode=live.json_mode,
            stream=profile["stream"], temperature=profile["temperature"])
        estimate = estimate_prompt_tokens(raw, messages, prompt_token_counter=live.prompt_token_counter,
            prompt_token_multiplier=live.prompt_token_multiplier,
            prompt_token_framing_margin=live.prompt_token_framing_margin)
        profile["preview"] = {"wire_body": body, "wire_body_sha256": hashlib.sha256(raw).hexdigest(),
            "wire_bytes": len(raw), "prompt_estimate": estimate,
            "estimated_reservation_cny": estimated_cost_cny({"prompt_tokens": estimate["prompt_tokens"],
                "completion_tokens": answer + budget}, model=profile["model"], conservative=True)}
        semantic_kwargs = {key: value for key, value in kwargs.items() if key != "transport_observer"}
        identity = {"call_id": str(kwargs.get("call_id") or kwargs.get("stage_id") or "planning"),
            "raw_response_dir": str(live.raw_response_dir or ""), "messages": messages,
            "profile": profile, "kwargs": semantic_kwargs}
        call_id = identity["call_id"] + ":" + digest(identity)
        stage = "planning-transport:" + (self.role or self.module.rsplit(".", 1)[-1])
        def checked_invoke(actual_messages, **actual_kwargs):
            # Recheck after a potentially long root request review, before the
            # provider resolves keys or creates either physical reservation.
            self.adapter.check()
            return live(actual_messages, **actual_kwargs)
        return SupervisedClient(checked_invoke, root=self.adapter.supervision_root,
            stage=stage, call_id=call_id, profile=profile)(messages, **kwargs)
