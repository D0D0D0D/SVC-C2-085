"""Declaration-driven proof that the prompt contract reaches public output."""

from __future__ import annotations

import json

from src.graph.domain_workflow_graph import Stage3DraftNode, Stage4DisclosureNode
from src.nodes.post_process_node import PostProcessNode


DESIGN_REVIEW_ISSUE_FIELDS = {
    "issue_id",
    "input_span_id",
    "criteria_id",
    "criteria_version",
    "axis",
    "requires_human_review",
    "policy_source",
    "tenant_policy_id",
    "issue_text",
}
DESIGN_PUBLIC_INVOKE_FIELDS = {
    "review_issues",
    "disclosure_map",
    "escalation_flags",
    "evidence_map",
    "cited_source_ids",
    "retrieved_records",
    "criteria_records",
    "requires_human_review",
}


class ContractFollowingLlm:
    """Build a response using only the contract serialized in the prompt."""

    def complete(self, messages):
        prompt = json.loads(messages[0]["content"])
        contract = prompt["response_contract"]
        item_contract = contract["review_issues"]["item"]
        criterion = contract["criteria_options"][0]
        spans = item_contract["field_contracts"]["input_span_id"]["values"]
        response = {}
        for root_key in contract["required"]:
            assert root_key == "review_issues"
            issue = {}
            for field in item_contract["required"]:
                if field in criterion:
                    issue[field] = criterion[field]
                elif field == "issue_id":
                    issue[field] = "contract_issue_1"
                elif field == "input_span_id":
                    issue[field] = spans[0]
                elif field == "requires_human_review":
                    issue[field] = True
                elif field == "policy_source":
                    issue[field] = "baseline"
                elif field == "tenant_policy_id":
                    issue[field] = None
                elif field == "issue_text":
                    issue[field] = "規則と開示方法を考査担当者が確認する論点です。"
                else:  # pragma: no cover - a new required field must teach the declaration how to fill it
                    raise AssertionError(f"undeclared construction rule for {field}")
            response[root_key] = [issue]
        return {"content": json.dumps(response, ensure_ascii=False)}


def _state() -> dict:
    return {
        "status": "success",
        "mode": "invoke",
        "broadcaster_id": "broadcaster-a",
        "review_desk_id": "cm-review",
        "validated_input": "健康表現とスポンサー開示の確認",
        "input_span_ids": ["span_1"],
        "criteria_records": [
            {
                "id": "PMD-ACT-AD-STANDARD",
                "source_id": "PMD-ACT-AD-STANDARD",
                "criteria_id": "PMD-ACT-AD-STANDARD",
                "criteria_version": "2026.1",
                "axis": "pmd_act",
            }
        ],
        "retrieved_records": [],
    }


def test_prompt_declaration_mechanically_builds_a_response_that_reaches_projection() -> None:
    state = _state()
    stage3 = Stage3DraftNode(config={"llm": ContractFollowingLlm()}).execute(state)
    assert stage3["status"] == "success"
    assert set(stage3["review_issues"][0]) == DESIGN_REVIEW_ISSUE_FIELDS

    stage4 = Stage4DisclosureNode().execute({**state, **stage3})
    assert stage4["status"] == "success"

    post_state = {**state, **stage3, **stage4, "status": "success"}
    projected = PostProcessNode().execute(post_state)
    gated = PostProcessNode()._extra_security_gate_output(projected)
    formatted = gated["formatted_output"]
    assert DESIGN_PUBLIC_INVOKE_FIELDS.issubset(formatted)
    assert formatted["review_issues"][0]["issue_id"] == "contract_issue_1"


def test_closed_response_contract_rejects_an_unknown_issue_field() -> None:
    class ExtraFieldLlm(ContractFollowingLlm):
        def complete(self, messages):
            response = super().complete(messages)
            body = json.loads(response["content"])
            body["review_issues"][0]["model_invented_verdict"] = "ok"
            return {"content": json.dumps(body, ensure_ascii=False)}

    result = Stage3DraftNode(config={"llm": ExtraFieldLlm()}).execute(_state())
    assert result["status"] == "error"
    assert result["error_code"] == "llm_output_contract_failed"
    assert "model_invented_verdict" not in str(result)
