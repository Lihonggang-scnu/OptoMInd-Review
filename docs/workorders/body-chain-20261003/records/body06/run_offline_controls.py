"""Bounded offline control selection. Run from repository root; not a full chain."""
import socket, pytest

def deny(*args, **kwargs):
    raise AssertionError("Network disabled for WO06 bounded checks")
socket.create_connection = deny
socket.socket.connect = deny
files = [
    "test_directed_reading_real_store_contract.py",
    "test_directed_reading_adapter_contract.py",
    "test_progressive_review_plan_material_reuse.py",
    "test_progressive_review_plan_owner_recovery_contract.py",
    "test_progressive_review_plan_batch_recovery_contract.py",
    "test_progressive_review_plan_chapter_cache_contract.py",
    "test_progressive_review_plan_stage_cache_contract.py",
    "test_progressive_review_plan_transport_retry.py",
    "test_progressive_review_plan_chapter_capacity.py",
    "test_progressive_review_plan_chapter_recovery.py",
    "test_progressive_review_plan_case_chain.py",
    "test_planning_feedback_scope.py",
    "test_planning_retrieval_query_initialization.py",
    "test_planning_supplement_attempt_contract.py",
    "test_body03_retrieval_adapter_contract.py",
    "test_body03_local_lookup_reading.py",
    "test_body03_local_lookup_contract.py",
"test_body03_local_lookup_r2.py",
"test_body03_local_lookup_r2_boundaries.py",
"test_body04_owner_material_handoff.py",
"test_body04_writer_tool_handoff.py",
"test_body04_late_source_routes.py",
"test_review_unit_writer_completion.py",
"test_progressive_review_plan_body_boundary.py",
"test_body05_argument_cli_handoff.py",
"test_body05_feedback_text_reuse.py",
"test_body05_global_argument_handoff.py",
"test_body05_unit_identity_continuity.py",
"test_body06_writer_output_consumption.py",
"test_body06_feedback_arrangement_gate.py",
]
raise SystemExit(pytest.main([
    "-q", *["tests/upgrade3/" + name for name in files],
    "-k", "not test_run_consumes and not test_revision_chain_runs",
]))