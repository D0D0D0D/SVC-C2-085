"""Flat, checkpoint-safe state for BroadcastStandardsReviewAgent."""

from typing import NotRequired, TypedDict

from framework.schemas.agent_state import AgentState


class ReviewIssue(TypedDict):
    issue_id: str
    input_span_id: str
    criteria_id: str
    criteria_version: str
    axis: str
    requires_human_review: bool
    policy_source: str
    tenant_policy_id: str | None
    issue_text: str
    ruling_id: NotRequired[str]


class State(AgentState, total=False):
    """Only JSON-serialisable domain fields; raw payloads stay out of State."""

    mode: str
    broadcaster_id: str
    review_desk_id: str
    payload_ref: str | None
    criteria_records: list[dict]
    retrieved_records: list[dict]
    review_issues: list[ReviewIssue]
    disclosure_map: dict
    escalation_flags: list[dict]
    evidence_map: list[dict]
    cited_source_ids: list[str]
    requires_human_review: bool
    missing_data_flags: list[str]
    ingest_summary: dict
    feedback_result: dict
    degradation_reason: str | None
    input_span_ids: list[str]
    gate_kind: str
