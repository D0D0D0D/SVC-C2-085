"""Unit coverage for the Cat2 GraphNode adaptation contract."""

from framework.schemas.agent_status import AgentStatus

from src.nodes.main_node import MainNode


def test_invoke_mode_spec_preserves_review_output_fields() -> None:
    node = MainNode()
    delta = node.merge_output(
        {"mode": "invoke"},
        {
            "status": AgentStatus.SUCCESS.value,
            "review_issues": [{"issue_id": "issue_1"}],
            "criteria_records": [{"criteria_id": "JBA-BC-STD-ART-152"}],
            "retrieved_records": [],
            "disclosure_map": {"issue_1": {"disclosure_review_required": True}},
            "escalation_flags": [],
            "evidence_map": [],
            "cited_source_ids": [],
            "requires_human_review": False,
        },
    )
    assert delta["status"] == AgentStatus.SUCCESS.value
    assert delta["gate_kind"] == "draft"
    assert "review_issues" in delta
    assert "criteria_records" in delta
    assert "disclosure_map" in delta
    assert "escalation_flags" in delta


def test_main_node_uses_json_serializable_input_envelope() -> None:
    node = MainNode()
    envelope = node.extract_input(
        {"mode": "invoke", "broadcaster_id": "t", "review_desk_id": "o", "validated_input": "sanitized", "payload_ref": "ref"}
    )
    assert '"mode": "invoke"' in envelope
    assert "sanitized" in envelope


def test_execute_method_signature() -> None:
    import inspect

    signature = inspect.signature(MainNode.execute)
    assert list(signature.parameters)[1] == "state"
    assert "_invoke_impl" not in MainNode.__dict__
