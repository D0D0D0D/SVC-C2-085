"""Domain security acceptance tests (SEC-01..13, SEC-26..29)."""

from __future__ import annotations

import json

import pytest

from framework.errors import SecurityViolationError
from framework.schemas.agent_status import AgentStatus
from src.graph.domain_workflow_graph import FeedbackNode, Stage1CriteriaNode, Stage4DisclosureNode
from src.graph.graph import BroadcastStandardsReviewAgent
from src.nodes.post_process_node import PostProcessNode
from src.nodes.pre_process_node import PreProcessNode
from src.services.service import (
    NameRegistry,
    SQLiteReviewRepository,
    effective_criteria,
    sanitize_sensitive_text,
    stable_ref,
    wire_services,
)


def _valid_formatted() -> dict:
    criteria = {"id": "JBA-BC-STD-ART-152", "source_id": "JBA-BC-STD-ART-152", "criteria_id": "JBA-BC-STD-ART-152", "criteria_version": "2026.1", "axis": "broadcast_standards"}
    issue = {
        "issue_id": "issue_1", "input_span_id": "span_1", "criteria_id": "JBA-BC-STD-ART-152",
        "criteria_version": "2026.1", "axis": "broadcast_standards", "requires_human_review": False,
        "policy_source": "baseline", "tenant_policy_id": None, "issue_text": "確認対象として考査担当者が確認します。",
    }
    return {
        "mode": "invoke", "gate_kind": "draft", "review_issues": [issue], "criteria_records": [criteria],
        "retrieved_records": [], "evidence_map": [{"claim_id": "issue_1", "claim": issue["issue_text"], "source_ids": [criteria["id"]]}],
        "cited_source_ids": [criteria["id"]], "disclosure_map": {}, "escalation_flags": [], "requires_human_review": False,
    }


def test_sec01_authoritative_fields_are_structurally_rejected() -> None:
    formatted = _valid_formatted()
    formatted["verdict"] = True
    with pytest.raises(SecurityViolationError, match="authoritative"):
        PostProcessNode()._extra_security_gate_output({"formatted_output": formatted})


@pytest.mark.parametrize(
    "word",
    ["放送してよい", "問題ない", "薬機法上クリア", "違反する", "問題ＮＡＩ", "放送して　よい", "問題ありません", "基準を充足する"],
)
def test_sec02_decisive_lexicon_is_nfkc_casefolded(word: str) -> None:
    formatted = _valid_formatted()
    formatted["review_issues"][0]["issue_text"] = word
    with pytest.raises(SecurityViolationError, match="decisive"):
        PostProcessNode()._extra_security_gate_output({"formatted_output": formatted})


def test_sec03_nonexistent_citation_fails_closed() -> None:
    formatted = _valid_formatted()
    formatted["cited_source_ids"] = ["invented"]
    with pytest.raises(SecurityViolationError, match="non-existent"):
        PostProcessNode()._extra_security_gate_output({"formatted_output": formatted})


@pytest.mark.parametrize(
    "mutate",
    [
        lambda state: state["review_issues"][0].update({"input_span_id": "unknown"}),
        lambda state: state["review_issues"][0].update({"axis": "not_an_axis"}),
        lambda state: state["review_issues"][0].pop("criteria_id"),
        lambda state: state["review_issues"][0].update({"requires_human_review": "yes"}),
    ],
)
def test_sec04_and_sec05_stage4_contract_is_fail_closed(mutate) -> None:
    formatted = _valid_formatted()
    state = {"review_issues": formatted["review_issues"], "criteria_records": formatted["criteria_records"], "input_span_ids": ["span_1"]}
    mutate(state)
    result = Stage4DisclosureNode().execute(state)
    assert result["status"] == AgentStatus.ERROR.value


def test_sec05c_requires_human_review_is_or_aggregate() -> None:
    formatted = _valid_formatted()
    issue = dict(formatted["review_issues"][0], issue_id="issue_2", requires_human_review=True)
    state = {"review_issues": [formatted["review_issues"][0], issue], "criteria_records": formatted["criteria_records"], "input_span_ids": ["span_1"]}
    result = Stage4DisclosureNode().execute(state)
    assert result["requires_human_review"] is True


@pytest.mark.parametrize(
    "mutate",
    [
        lambda state: state["review_issues"][0].update({"criteria_version": "other"}),
        lambda state: state["retrieved_records"][0].update({"axis": "broadcast_standards"}),
        lambda state: state["retrieved_records"][0]["metadata"].update({"broadcaster_id": "other"}),
        lambda state: state["retrieved_records"][0]["metadata"].update({"review_desk_id": "other"}),
    ],
    ids=["criteria-version", "ruling-axis", "ruling-tenant", "ruling-org"],
)
def test_stage4_rejects_mismatched_criteria_or_ruling_provenance(mutate) -> None:
    criteria = {"criteria_id": "criterion-1", "criteria_version": "1", "axis": "pmd_act"}
    issue = {
        "issue_id": "issue-1", "input_span_id": "span_1", "criteria_id": "criterion-1", "criteria_version": "1",
        "axis": "pmd_act", "requires_human_review": False, "policy_source": "baseline", "tenant_policy_id": None,
        "ruling_id": "ruling-1",
    }
    state = {
        "broadcaster_id": "tenant", "review_desk_id": "org", "review_issues": [issue], "criteria_records": [criteria],
        "input_span_ids": ["span_1"],
        "retrieved_records": [{
            "ruling_id": "ruling-1", "criteria_id": "criterion-1", "criteria_version": "1", "axis": "pmd_act",
            "metadata": {"broadcaster_id": "tenant", "review_desk_id": "org"},
        }],
    }
    mutate(state)
    result = Stage4DisclosureNode().execute(state)
    assert result == {"status": AgentStatus.ERROR.value, "error_log": ["stage4_issue_contract_invalid"]}


def test_sec07_and_sec08_axis_and_scope_are_pre_filters() -> None:
    repository = SQLiteReviewRepository()
    repository.write_ingest_record({"record_id": "r1", "broadcaster_id": "t1", "review_desk_id": "o1", "program_type": "drama", "content_summary": "health review"})
    repository.write_ruling({"ruling_id": "rule1", "record_id": "r1", "broadcaster_id": "t1", "review_desk_id": "o1", "criteria_id": "c", "criteria_version": "1", "axis": "pmd_act"})
    assert repository.search_axis(broadcaster_id="t1", review_desk_id="o1", axis="broadcast_standards", query="health", top_k=5) == []
    assert repository.search_axis(broadcaster_id="", review_desk_id="o1", axis="pmd_act", query="health", top_k=5) == []
    assert repository.search_axis(broadcaster_id="t1", review_desk_id="o2", axis="pmd_act", query="health", top_k=5) == []


def test_repository_composite_scope_keys_prevent_cross_scope_overwrite_and_join() -> None:
    repository = SQLiteReviewRepository()
    repository.write_ingest_record({"record_id": "shared", "broadcaster_id": "tenant-a", "review_desk_id": "org-a", "program_type": "a", "content_summary": "scope a"})
    repository.write_ingest_record({"record_id": "shared", "broadcaster_id": "tenant-b", "review_desk_id": "org-b", "program_type": "b", "content_summary": "scope b"})
    repository.write_ruling({"ruling_id": "shared-ruling", "record_id": "shared", "broadcaster_id": "tenant-a", "review_desk_id": "org-a", "criteria_id": "ca", "criteria_version": "1", "axis": "pmd_act"})
    repository.write_ruling({"ruling_id": "shared-ruling", "record_id": "shared", "broadcaster_id": "tenant-b", "review_desk_id": "org-b", "criteria_id": "cb", "criteria_version": "1", "axis": "pmd_act"})

    a_rows = repository.search_axis(broadcaster_id="tenant-a", review_desk_id="org-a", axis="pmd_act", query="scope", top_k=5)
    b_rows = repository.search_axis(broadcaster_id="tenant-b", review_desk_id="org-b", axis="pmd_act", query="scope", top_k=5)
    assert a_rows[0]["criteria_id"] == "ca" and a_rows[0]["title"] == "a"
    assert b_rows[0]["criteria_id"] == "cb" and b_rows[0]["title"] == "b"

    repository.write_ingest_record({"record_id": "only-b", "broadcaster_id": "tenant-b", "review_desk_id": "org-b", "program_type": "b", "content_summary": "foreign"})
    with pytest.raises(ValueError, match="record_scope_invalid"):
        repository.write_ruling({"ruling_id": "foreign", "record_id": "only-b", "broadcaster_id": "tenant-a", "review_desk_id": "org-a", "criteria_id": "ca", "criteria_version": "1", "axis": "pmd_act"})


def test_feedback_rejects_a_record_owned_by_another_scope() -> None:
    services = wire_services()
    services.repository.write_ingest_record(
        {"record_id": "foreign-record", "broadcaster_id": "tenant-b", "review_desk_id": "org-b", "program_type": "show", "content_summary": "review"}
    )
    result = FeedbackNode(services=services).execute(
        {
            "broadcaster_id": "tenant-a", "review_desk_id": "org-a",
            "validated_input": json.dumps(
                {
                    "record_id": "foreign-record", "axis": "pmd_act", "criteria_id": "PMD-ACT-AD-STANDARD",
                    "criteria_version": "2026.1", "ruling": "approved",
                }
            ),
        }
    )
    assert result == {"status": AgentStatus.ERROR.value, "error_log": ["feedback_record_scope_invalid"]}


def test_sec09_sec26_to_sec29_payload_is_sanitised_and_single_use() -> None:
    services = wire_services()
    raw = "スポンサー: Secret Sponsor\n固有原文マーカー"
    ref = services.payload_store.put(raw, scope={"broadcaster_id": "t", "review_desk_id": "o"}, session_id="s")
    node = PreProcessNode(services=services)
    result = node.execute({"user_input": json.dumps({"mode": "invoke", "payload_ref": ref, "broadcaster_id": "t", "review_desk_id": "o"}), "session_id": "s"})
    assert result["status"] == "success"
    assert "Secret Sponsor" not in result["validated_input"]
    assert "固有原文マーカー" not in result["validated_input"]
    retry = node.execute({**result, "session_id": "s"})
    assert retry["status"] == "success"  # no second payload resolve
    with pytest.raises(Exception, match="replayed"):
        services.payload_store.resolve(ref, scope={"broadcaster_id": "t", "review_desk_id": "o"}, session_id="s", consume=True)


def test_preprocess_carries_envelope_broadcast_date_to_stage1() -> None:
    services = wire_services()
    services.review_criteria = [
        {"criteria_id": "past", "criteria_version": "1", "axis": "pmd_act", "effective_date": "2020-01-01"},
        {"criteria_id": "future", "criteria_version": "1", "axis": "pmd_act", "effective_date": "2099-01-01"},
    ]
    ref = services.payload_store.put("review text", scope={"broadcaster_id": "t", "review_desk_id": "o"}, session_id="s")
    preprocessed = PreProcessNode(services=services).execute(
        {"session_id": "s", "user_input": json.dumps({"mode": "invoke", "payload_ref": ref, "broadcaster_id": "t", "review_desk_id": "o", "broadcast_date": "2026-07-22", "program_type": "drama"})}
    )
    stage1 = Stage1CriteriaNode(services=services).execute(preprocessed)
    assert preprocessed["broadcast_date"] == "2026-07-22"
    assert preprocessed["program_type"] == "drama"
    assert [item["criteria_id"] for item in stage1["criteria_records"]] == ["past"]


def test_outer_error_output_omits_diagnostics_and_history() -> None:
    result = BroadcastStandardsReviewAgent.get_output(
        object(),
        {
            "status": "error", "correlation_id": "corr-1", "error_log": ["sensitive detail"],
            "node_history": [{"node": "sensitive"}],
            "formatted_output": {"status": "error", "error_code": "review_processing_failed", "correlation_id": "corr-1"},
        },
    )
    assert result == {"status": "error", "error_code": "review_processing_failed", "correlation_id": "corr-1"}


@pytest.mark.parametrize(
    "raw, sponsor_values, product_value",
    [
        ("ＳＰＯＮＳＯＲ：Full Sponsor\nスポンサー: Second Sponsor\n商品名：Full Product\n連絡先 alice@example.com", ["Full Sponsor", "Second Sponsor"], "Full Product"),
        ("SPONSOR: Half Sponsor\nsponsor: Another Sponsor\nPRODUCT: Half Product\ncontact bob@example.com", ["Half Sponsor", "Another Sponsor"], "Half Product"),
    ],
    ids=["full-width-labels", "half-width-labels"],
)
def test_s2_redacts_full_or_half_width_labels_multiple_names_and_email(raw, sponsor_values, product_value) -> None:
    registry = NameRegistry()
    refs = registry.register_from_text(raw, broadcaster_id="tenant", review_desk_id="org")
    sanitized = sanitize_sensitive_text(raw, ref_hints=registry.ref_hints(refs))

    assert all(value not in sanitized for value in [*sponsor_values, product_value, "alice@example.com", "bob@example.com"])
    assert all(stable_ref("sponsor", value) in sanitized for value in sponsor_values)
    assert stable_ref("product", product_value) in sanitized
    assert len(refs) == len(sponsor_values) + 1


def test_bl03_effective_date_excludes_future_rule() -> None:
    criteria = [{"criteria_id": "future", "axis": "pmd_act", "effective_date": "2099-01-01"}]
    assert effective_criteria(criteria, "2026-07-22") == []
