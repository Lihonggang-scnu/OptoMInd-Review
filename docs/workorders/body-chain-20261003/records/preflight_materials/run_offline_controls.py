"""Bounded second-stop controls; network denied, not the full test suite."""
import socket
import pytest

def deny(*args, **kwargs):
    raise AssertionError("Network disabled for second-stop material controls")
socket.create_connection = deny
socket.socket.connect = deny
files = ['test_body_preflight_f1_arrangement_cache_consumer.py',
 'test_progressive_review_plan_tool_consumer_recovery.py',
 'test_preflight_material_recovery_context.py',
 'test_body05_global_argument_handoff.py',
 'test_progressive_review_plan_stage_cache_contract.py',
 'test_planning_retrieval_query_initialization.py',
 'test_body03_retrieval_adapter_contract.py',
 'test_body07_retrieval_short_chains.py',
 'test_body07_arrangement_writer_short_chain.py',
 'test_body06_feedback_arrangement_gate.py',
 'test_citation_prefix_consumption.py',
 'test_feedback_loop_chapter_tool_materials.py',
 'test_preflight_material_identity_compatibility.py',
 'test_preflight_case_material_cache_consumer.py',
 'test_preflight_material_review_regressions.py',
 'test_directed_reading_real_store_contract.py',
 'test_directed_reading_adapter_contract.py',
 'test_progressive_review_plan_material_reuse.py',
 'test_body04_owner_material_handoff.py',
 'test_progressive_review_plan_case_chain.py']
raise SystemExit(pytest.main(["-q", *["tests/upgrade3/" + name for name in files]]))
