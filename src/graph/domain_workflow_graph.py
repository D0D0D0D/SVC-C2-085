"""Inner four-stage deterministic/LLM broadcast-review workflow."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any, ClassVar

from langgraph.graph import END, START
from langgraph.types import interrupt

from framework.errors import SecurityViolationError
from framework.graph.base_graph import BaseGraph
from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.hitl_status import HitlStatus
from framework.schemas.trust_level import TrustLevel

from src.llm_draft_stage import DraftStageHooks, LlmDraftStageNode
from src.ingest_row_pipeline import IngestRowPipelineNode, RowPipelineHooks
from src.scoped_retrieval import ScopedRetrievalNode
from src.schemas.state import State
from src.services.service import (
    AXES,
    AxisBoundRetrievalBackend,
    ServiceContainer,
    configured_criterion_matches,
    effective_criteria,
    normalize_review_text,
    sanitize_sensitive_text,
    stable_ref,
)


RULINGS = frozenset({"approved", "revision_instructed", "rejected"})
POLICY_SOURCES = frozenset({"baseline", "tenant_override"})
REVIEW_ISSUE_REQUIRED_FIELDS = (
    "issue_id",
    "input_span_id",
    "criteria_id",
    "criteria_version",
    "axis",
    "requires_human_review",
    "policy_source",
    "tenant_policy_id",
    "issue_text",
)
REVIEW_ISSUE_OPTIONAL_FIELDS = ("ruling_id",)
MAX_REVIEW_ISSUES = 50
MAX_ISSUE_TEXT_CHARS = 512
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,120}$")


def _response_contract(state: dict[str, Any]) -> dict[str, Any]:
    """Build the exact LLM response declaration from validator-owned constants."""

    criteria_options = [
        {
            "criteria_id": str(item["criteria_id"]),
            "criteria_version": str(item["criteria_version"]),
            "axis": str(item["axis"]),
        }
        for item in state.get("criteria_records", [])
        if isinstance(item, dict)
        and item.get("criteria_id")
        and item.get("criteria_version")
        and item.get("axis") in AXES
    ]
    return {
        "type": "object",
        "required": ["review_issues"],
        "additional_properties": False,
        "review_issues": {
            "type": "array",
            "min_items": 1,
            "max_items": MAX_REVIEW_ISSUES,
            "item": {
                "type": "object",
                "required": list(REVIEW_ISSUE_REQUIRED_FIELDS),
                "optional": list(REVIEW_ISSUE_OPTIONAL_FIELDS),
                "additional_properties": False,
                "field_contracts": {
                    "issue_id": {"type": "safe_id", "max_length": 120},
                    "input_span_id": {"type": "enum", "values": list(state.get("input_span_ids") or ["span_1"])},
                    "criteria_id": {"type": "criteria_option"},
                    "criteria_version": {"type": "criteria_option"},
                    "axis": {"type": "enum", "values": sorted(AXES)},
                    "requires_human_review": {"type": "boolean"},
                    "policy_source": {"type": "enum", "values": sorted(POLICY_SOURCES)},
                    "tenant_policy_id": {"type": "safe_id_or_null"},
                    "issue_text": {"type": "string", "min_length": 1, "max_length": MAX_ISSUE_TEXT_CHARS},
                    "ruling_id": {"type": "safe_id"},
                },
            },
        },
        "criteria_options": criteria_options,
        "rules": [
            "criteria_id, criteria_version, and axis must come from one criteria_options object",
            "baseline requires tenant_policy_id=null; tenant_override requires a non-empty tenant_policy_id",
            "return JSON only",
        ],
    }


def _valid_review_issues(draft: Any, state: dict[str, Any]) -> bool:
    """Validate the same closed contract declared by ``_response_contract``."""

    if not isinstance(draft, dict) or set(draft) != {"review_issues"}:
        return False
    issues = draft.get("review_issues")
    if not isinstance(issues, list) or not 1 <= len(issues) <= MAX_REVIEW_ISSUES:
        return False
    criteria = {
        (str(item.get("criteria_id")), str(item.get("criteria_version")), item.get("axis"))
        for item in state.get("criteria_records", [])
        if isinstance(item, dict)
    }
    spans = set(state.get("input_span_ids") or ["span_1"])
    allowed_fields = set(REVIEW_ISSUE_REQUIRED_FIELDS) | set(REVIEW_ISSUE_OPTIONAL_FIELDS)
    seen_ids: set[str] = set()
    for issue in issues:
        if not isinstance(issue, dict) or not set(REVIEW_ISSUE_REQUIRED_FIELDS).issubset(issue):
            return False
        if not set(issue).issubset(allowed_fields):
            return False
        issue_id = issue.get("issue_id")
        text = issue.get("issue_text")
        if not isinstance(issue_id, str) or not _SAFE_ID.fullmatch(issue_id) or issue_id in seen_ids:
            return False
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_ISSUE_TEXT_CHARS:
            return False
        if issue.get("input_span_id") not in spans or not isinstance(issue.get("requires_human_review"), bool):
            return False
        criterion = (str(issue.get("criteria_id")), str(issue.get("criteria_version")), issue.get("axis"))
        if criterion not in criteria:
            return False
        policy_source = issue.get("policy_source")
        tenant_policy_id = issue.get("tenant_policy_id")
        if policy_source == "baseline":
            if tenant_policy_id is not None:
                return False
        elif policy_source == "tenant_override":
            if not isinstance(tenant_policy_id, str) or not _SAFE_ID.fullmatch(tenant_policy_id):
                return False
        else:
            return False
        ruling_id = issue.get("ruling_id")
        if ruling_id is not None and (not isinstance(ruling_id, str) or not _SAFE_ID.fullmatch(ruling_id)):
            return False
        seen_ids.add(issue_id)
    return True


class _DomainNode(FunctionNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL


class ModeRouterNode(_DomainNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: dict) -> dict:
        from shared.utils.audit_logger import emit_trace_event

        try:
            envelope = json.loads(state.get("user_input", ""))
        except (TypeError, json.JSONDecodeError):
            envelope = {}
        if not isinstance(envelope, dict) or envelope.get("mode") not in {"ingest", "invoke", "feedback"}:
            emit_trace_event("broadcast_inner_mode_rejected", {"reason": "invalid_mode"}, state)
            return {"status": AgentStatus.ERROR.value, "error_log": ["inner_mode_invalid"]}
        missing_payload = envelope.get("mode") == "ingest" and not envelope.get("payload_ref")
        missing_validated = envelope.get("mode") != "ingest" and not envelope.get("validated_input")
        if not envelope.get("broadcaster_id") or not envelope.get("review_desk_id") or missing_payload or missing_validated:
            emit_trace_event("broadcast_inner_mode_rejected", {"reason": "missing_scope_or_input"}, state)
            return {"status": AgentStatus.ERROR.value, "error_log": ["inner_scope_or_input_missing"]}
        emit_trace_event("broadcast_inner_mode_routed", {"mode": envelope["mode"]}, state)
        return {
            "mode": envelope["mode"], "broadcaster_id": envelope["broadcaster_id"], "review_desk_id": envelope["review_desk_id"],
            "payload_ref": envelope.get("payload_ref"), "validated_input": envelope.get("validated_input", ""),
            "broadcast_date": envelope.get("broadcast_date"), "program_type": envelope.get("program_type", ""),
            "status": AgentStatus.SUCCESS.value,
        }


class Stage1CriteriaNode(_DomainNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, *, services: ServiceContainer) -> None:
        super().__init__()
        self._services = services

    def execute(self, state: dict) -> dict:
        from shared.utils.audit_logger import emit_trace_event

        if state.get("status") == AgentStatus.ERROR.value:
            return {}
        criteria = effective_criteria(self._services.review_criteria, state.get("broadcast_date"))
        if not criteria:
            emit_trace_event("broadcast_stage1_failed", {"reason": "no_effective_criteria"}, state)
            return {"status": AgentStatus.ERROR.value, "error_log": ["no_effective_criteria"]}
        spans = [f"span_{index + 1}" for index, _ in enumerate(filter(None, state["validated_input"].splitlines()))]
        spans = spans or ["span_1"]
        records = [
            {
                "id": item["criteria_id"], "source_id": item["criteria_id"], "criteria_id": item["criteria_id"],
                "criteria_version": item["criteria_version"], "axis": item["axis"], "kind": "review_criteria",
            }
            for item in criteria
        ]
        emit_trace_event("broadcast_stage1_complete", {"criteria_count": len(records)}, state)
        return {"criteria_records": records, "input_span_ids": spans, "status": AgentStatus.SUCCESS.value}


class Stage2ScopedRetrievalNode(_DomainNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, *, services: ServiceContainer) -> None:
        super().__init__()
        self._nodes = tuple(
            ScopedRetrievalNode(
                scope_keys=("broadcaster_id", "review_desk_id"),
                backend=AxisBoundRetrievalBackend(services.repository, axis),
                normalize_query=normalize_review_text,
                top_k=5,
            )
            for axis in sorted(AXES)
        )

    def execute(self, state: dict) -> dict:
        from shared.utils.audit_logger import emit_trace_event

        if state.get("status") == AgentStatus.ERROR.value:
            return {}
        records: list[dict[str, Any]] = []
        for node in self._nodes:
            outcome = node.execute(state)
            if outcome.get("status") == AgentStatus.ERROR.value:
                return outcome
            records.extend(outcome.get("retrieved_records", []))
        reason = None if records else "empty_ruling_kb"
        emit_trace_event("broadcast_stage2_complete", {"retrieved_count": len(records)}, state)
        result: dict[str, Any] = {"retrieved_records": records, "status": AgentStatus.SUCCESS.value}
        if reason:
            result["degradation_reason"] = reason
        return result


class Stage3DraftNode(_DomainNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, *, config: dict[str, Any]) -> None:
        super().__init__()
        hooks = DraftStageHooks(build_prompt=self._prompt, build_fallback=self._fallback, build_evidence=self._evidence)
        self._draft_node = LlmDraftStageNode(config={"llm": config.get("llm")}, hooks=hooks)

    def execute(self, state: dict) -> dict:
        from shared.utils.audit_logger import emit_trace_event

        if state.get("status") == AgentStatus.ERROR.value:
            return {}
        result = self._draft_node.execute(state)
        if result.get("status") == AgentStatus.ERROR.value:
            return result
        draft = result.get("draft") or {}
        if not _valid_review_issues(draft, state):
            return {
                "status": AgentStatus.ERROR.value,
                "error_code": "llm_output_contract_failed",
                "error_log": ["stage3_response_contract_invalid"],
            }
        issues = draft["review_issues"]
        emit_trace_event("broadcast_stage3_complete", {"issue_count": len(issues)}, state)
        return {
            "review_issues": issues, "evidence_map": result.get("evidence_map", []),
            "cited_source_ids": result.get("cited_source_ids", []), "status": AgentStatus.SUCCESS.value,
            **({"degradation_reason": result["degradation_reason"]} if result.get("degradation_reason") else {}),
        }

    @staticmethod
    def _prompt(state: dict) -> str:
        # State was sanitised at the outer boundary; no sponsor/product real name is included.
        return json.dumps(
            {
                "task": "規則別の確認対象論点をJSONで返す。可否判断は禁止。",
                "response_contract": _response_contract(state),
                "input": state["validated_input"],
                "criteria": state.get("criteria_records", []),
                "past_rulings": state.get("retrieved_records", []),
            },
            ensure_ascii=False,
        )

    @staticmethod
    def _fallback(state: dict) -> dict[str, Any]:
        span_ids = state.get("input_span_ids") or ["span_1"]
        issues = []
        for index, criterion in enumerate(state.get("criteria_records", [])):
            axis = criterion["axis"]
            issues.append(
                {
                    "issue_id": f"issue_{index + 1}", "input_span_id": span_ids[index % len(span_ids)],
                    "criteria_id": criterion["criteria_id"], "criteria_version": criterion["criteria_version"], "axis": axis,
                    "requires_human_review": axis in {"pmd_act", "stealth_marketing"}, "policy_source": "baseline",
                    "tenant_policy_id": None,
                    "issue_text": "当該表現と開示方法について、規則・自局基準の確認対象として考査担当者が確認します。",
                }
            )
        return {"review_issues": issues}

    @staticmethod
    def _evidence(state: dict, draft: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
        evidence: list[dict[str, Any]] = []
        cited: list[str] = []
        for issue in draft.get("review_issues", []):
            if not isinstance(issue, dict) or not issue.get("criteria_id"):
                continue
            source_id = str(issue["criteria_id"])
            cited.append(source_id)
            evidence.append({"claim_id": issue.get("issue_id", source_id), "claim": issue.get("issue_text", ""), "source_ids": [source_id]})
        return evidence, cited


class Stage4DisclosureNode(_DomainNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: dict) -> dict:
        from shared.utils.audit_logger import emit_trace_event

        issues = state.get("review_issues")
        criteria = {item.get("criteria_id"): item for item in state.get("criteria_records", []) if isinstance(item, dict)}
        spans = set(state.get("input_span_ids", []))
        rulings = [item for item in state.get("retrieved_records", []) if isinstance(item, dict)]
        if not isinstance(issues, list):
            return {"status": AgentStatus.ERROR.value, "error_log": ["stage4_issues_missing"]}
        disclosure: dict[str, dict[str, Any]] = {}
        escalations: list[dict[str, Any]] = []
        for issue in issues:
            if not isinstance(issue, dict) or not self._valid_issue(issue, criteria, spans, rulings, state):
                return {"status": AgentStatus.ERROR.value, "error_log": ["stage4_issue_contract_invalid"]}
            issue_id = issue["issue_id"]
            disclosure[issue_id] = {"axis": issue["axis"], "disclosure_review_required": True}
            if issue["requires_human_review"]:
                escalations.append({"issue_id": issue_id, "axis": issue["axis"], "reason": "human_review_required"})
        emit_trace_event("broadcast_stage4_complete", {"escalation_count": len(escalations)}, state)
        return {
            "disclosure_map": disclosure, "escalation_flags": escalations,
            "requires_human_review": any(item["requires_human_review"] for item in issues),
            "status": AgentStatus.SUCCESS.value,
        }

    @staticmethod
    def _valid_issue(
        issue: dict[str, Any], criteria: dict[str, dict], spans: set[str], rulings: list[dict[str, Any]], state: dict
    ) -> bool:
        required = {"issue_id", "input_span_id", "criteria_id", "criteria_version", "axis", "requires_human_review", "policy_source", "tenant_policy_id"}
        if not required.issubset(issue) or issue["input_span_id"] not in spans or issue["axis"] not in AXES:
            return False
        if not isinstance(issue["requires_human_review"], bool) or issue["criteria_id"] not in criteria:
            return False
        criterion = criteria[issue["criteria_id"]]
        if criterion.get("axis") != issue["axis"] or criterion.get("criteria_version") != issue["criteria_version"]:
            return False
        ruling_id = issue.get("ruling_id")
        if ruling_id and not Stage4DisclosureNode._ruling_matches_issue(str(ruling_id), issue, rulings, state):
            return False
        return (issue["policy_source"] == "baseline" and issue["tenant_policy_id"] is None) or (
            issue["policy_source"] == "tenant_override" and bool(issue["tenant_policy_id"])
        )

    @staticmethod
    def _ruling_matches_issue(ruling_id: str, issue: dict[str, Any], rulings: list[dict[str, Any]], state: dict) -> bool:
        matches = [item for item in rulings if str(item.get("ruling_id", "")) == ruling_id]
        if len(matches) != 1:
            return False
        ruling = matches[0]
        metadata = ruling.get("metadata") if isinstance(ruling.get("metadata"), dict) else {}
        return (
            ruling.get("axis") == issue["axis"]
            and ruling.get("criteria_id") == issue["criteria_id"]
            and ruling.get("criteria_version") == issue["criteria_version"]
            and metadata.get("broadcaster_id") == state.get("broadcaster_id")
            and metadata.get("review_desk_id") == state.get("review_desk_id")
        )


class HitlRefineNode(_DomainNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, *, enabled: bool) -> None:
        super().__init__()
        self._enabled = enabled

    def execute(self, state: dict) -> dict:
        from shared.utils.audit_logger import emit_trace_event

        if state.get("status") == AgentStatus.ERROR.value:
            return {}
        if not self._enabled or not state.get("hitl_allowed", True):
            emit_trace_event("broadcast_hitl_skipped", {"enabled": self._enabled}, state)
            return {"status": AgentStatus.SUCCESS.value}
        draft = [
            {key: issue.get(key) for key in ("issue_id", "criteria_id", "input_span_id", "issue_text", "policy_source")}
            for issue in state.get("review_issues", []) if isinstance(issue, dict)
        ]
        feedback = interrupt({"draft": draft, "actions": ["approve", "correct", "reject"]})
        action = feedback.get("action") if isinstance(feedback, dict) else None
        if action not in {"approve", "correct", "reject"}:
            raise SecurityViolationError("invalid_hitl_feedback")
        status = {"approve": HitlStatus.APPROVED, "correct": HitlStatus.CORRECTED, "reject": HitlStatus.REJECTED}[action]
        emit_trace_event("broadcast_hitl_resumed", {"action": action}, state)
        result = {"hitl_feedback": feedback, "hitl_status": status, "status": AgentStatus.SUCCESS.value}
        if action == "correct":
            result["review_issues"] = self._apply_corrections(state.get("review_issues"), feedback.get("corrections", []))
        return result

    @staticmethod
    def _apply_corrections(issues: Any, corrections: Any) -> list[dict[str, Any]]:
        """Apply each accepted structured correction to its exact draft issue.

        A correction is not merely audit metadata: the corrected issue text is
        what reaches post-process and the human caller after resume.  IDs and
        criterion IDs must match the interrupted draft so feedback cannot
        rewrite a different issue.
        """

        if not isinstance(issues, list) or not all(isinstance(issue, dict) for issue in issues):
            raise SecurityViolationError("invalid_hitl_review_issues")
        if not isinstance(corrections, list):
            raise SecurityViolationError("invalid_hitl_corrections")

        updated = [dict(issue) for issue in issues]
        by_issue_id = {str(issue.get("issue_id")): issue for issue in updated}
        seen: set[str] = set()
        for correction in corrections:
            if not isinstance(correction, dict):
                raise SecurityViolationError("invalid_hitl_correction")
            issue_id = correction.get("issue_id")
            criteria_id = correction.get("criteria_id")
            note = correction.get("note")
            if (
                not isinstance(issue_id, str)
                or not isinstance(criteria_id, str)
                or not isinstance(note, str)
                or not note
                or len(note) > 256
                or issue_id in seen
            ):
                raise SecurityViolationError("invalid_hitl_correction")
            issue = by_issue_id.get(issue_id)
            if issue is None or issue.get("criteria_id") != criteria_id:
                raise SecurityViolationError("hitl_correction_issue_mismatch")
            issue["issue_text"] = note
            issue["operator_edited"] = True
            seen.add(issue_id)
        return updated


class IngestNode(IngestRowPipelineNode):
    """Domain adaptation of the request-scoped v0.3.1 row pipeline."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL
    required_fields: ClassVar[tuple[str, ...]] = ("program_type", "content_summary")
    required_any_fields: ClassVar[tuple[tuple[str, ...], ...]] = ()

    def __init__(
        self,
        *,
        services: ServiceContainer,
        trusted_scope: Mapping[str, str] | None,
        session_id: str | None,
    ) -> None:
        self._services = services
        self._scope = dict(trusted_scope) if isinstance(trusted_scope, Mapping) else None
        super().__init__(
            payload_store=services.payload_store,
            trusted_scope=trusted_scope,
            session_id=session_id,
            hooks=RowPipelineHooks(
                sanitize=self._sanitize_row,
                normalize=self._normalize_row,
                write=self._write_row,
            ),
        )

    def _sanitize_row(self, _state: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
        sanitized = sanitize_sensitive_text(json.dumps(row, ensure_ascii=False, default=str))
        parsed = json.loads(sanitized)
        if not isinstance(parsed, dict):
            raise ValueError("sanitized_row_not_object")
        return parsed

    def _normalize_row(self, _state: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
        if self._scope is None:
            raise ValueError("trusted_ingest_scope_not_configured")
        normalized = dict(row)
        normalized.update(self._scope)
        normalized["content_summary"] = normalize_review_text(row.get("content_summary"))
        normalized["record_id"] = str(
            row.get("record_id")
            or stable_ref("record", json.dumps(row, ensure_ascii=False, sort_keys=True, default=str))
        )
        return normalized

    def _write_row(self, _state: dict[str, Any], row: dict[str, Any]) -> str:
        record_id = self._services.repository.write_ingest_record(
            {
                "record_id": row["record_id"],
                "broadcaster_id": row["broadcaster_id"],
                "review_desk_id": row["review_desk_id"],
                "program_type": row["program_type"],
                "sponsor_ref": row.get("sponsor_ref"),
                "content_summary": row["content_summary"],
                "reviewed_at": row.get("reviewed_at"),
                "criteria_version": row.get("criteria_version"),
            }
        )
        for index, ruling in enumerate(row.get("rulings", [])):
            if isinstance(ruling, dict):
                self._services.repository.write_ruling(
                    {
                        **ruling,
                        "ruling_id": ruling.get("ruling_id", f"ruling_{record_id}_{index}"),
                        "record_id": record_id,
                        "broadcaster_id": row["broadcaster_id"],
                        "review_desk_id": row["review_desk_id"],
                    }
                )
        return record_id


class FeedbackNode(_DomainNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, *, services: ServiceContainer) -> None:
        super().__init__()
        self._services = services

    def execute(self, state: dict) -> dict:
        from shared.utils.audit_logger import emit_trace_event

        try:
            feedback = json.loads(state["validated_input"])
        except (TypeError, json.JSONDecodeError):
            feedback = {}
        if (
            not isinstance(feedback, dict)
            or feedback.get("axis") not in AXES
            or feedback.get("ruling") not in RULINGS
            or not feedback.get("criteria_id")
            or not feedback.get("criteria_version")
        ):
            return {"status": AgentStatus.ERROR.value, "error_log": ["feedback_contract_invalid"]}
        record_id = str(feedback.get("record_id", ""))
        if not record_id:
            return {"status": AgentStatus.ERROR.value, "error_log": ["feedback_record_id_missing"]}
        if not configured_criterion_matches(
            self._services.review_criteria,
            criteria_id=feedback["criteria_id"],
            criteria_version=feedback["criteria_version"],
            axis=feedback["axis"],
        ):
            return {"status": AgentStatus.ERROR.value, "error_log": ["feedback_criterion_invalid"]}
        if not self._services.repository.record_belongs_to_scope(
            record_id, broadcaster_id=state["broadcaster_id"], review_desk_id=state["review_desk_id"]
        ):
            return {"status": AgentStatus.ERROR.value, "error_log": ["feedback_record_scope_invalid"]}
        supplied_ruling_id = feedback.get("ruling_id")
        existing_ruling = bool(
            supplied_ruling_id
            and self._services.repository.ruling_belongs_to_scope(
                str(supplied_ruling_id), broadcaster_id=state["broadcaster_id"], review_desk_id=state["review_desk_id"]
            )
        )
        if existing_ruling:
            if not feedback.get("amend_existing"):
                return {"status": AgentStatus.ERROR.value, "error_log": ["feedback_ruling_overwrite_not_explicit"]}
        elif supplied_ruling_id and feedback.get("amend_existing"):
            return {"status": AgentStatus.ERROR.value, "error_log": ["feedback_ruling_scope_invalid"]}
        try:
            ruling_id = self._services.repository.write_ruling(
                {"ruling_id": supplied_ruling_id or f"ruling_feedback_{record_id}", "record_id": record_id,
                 "broadcaster_id": state["broadcaster_id"], "review_desk_id": state["review_desk_id"], "criteria_id": feedback["criteria_id"],
                 "criteria_version": feedback.get("criteria_version", "unknown"), "axis": feedback["axis"],
                 "ruling": feedback.get("ruling"), "revision_note": feedback.get("revision_note", ""),
                 "rationale": feedback.get("rationale", ""), "escalated": feedback.get("escalated", False), "operator_edited": True},
                allow_overwrite=bool(existing_ruling and feedback.get("amend_existing")),
            )
        except ValueError as exc:
            return {"status": AgentStatus.ERROR.value, "error_log": [str(exc)]}
        emit_trace_event("broadcast_feedback_recorded", {"ruling_count": 1}, state)
        return {"feedback_result": {"ruling_id": ruling_id, "record_id": record_id}, "status": AgentStatus.SUCCESS.value}


class DomainWorkflowGraph(BaseGraph):
    def __init__(
        self,
        *,
        config: dict[str, Any] | None = None,
        services: ServiceContainer,
        trusted_scope: Mapping[str, str] | None = None,
        ingest_session_id: str | None = None,
    ) -> None:
        super().__init__(config=config)
        self._services = services
        self._trusted_scope = dict(trusted_scope) if trusted_scope is not None else None
        self._ingest_session_id = ingest_session_id

    @property
    def name(self) -> str:
        return "broadcast_standards_review_domain_workflow"

    @property
    def state_schema(self) -> type:
        return State

    def _validate_config(self) -> None:
        if not isinstance(self.config.get("hitl", {}), dict):
            raise ValueError("hitl configuration must be a mapping")

    def register_nodes(self) -> None:
        self._nodes = {
            "route_mode": ModeRouterNode(), "stage1": Stage1CriteriaNode(services=self._services),
            "stage2": Stage2ScopedRetrievalNode(services=self._services), "stage3": Stage3DraftNode(config=self.config),
            "stage4": Stage4DisclosureNode(), "hitl_refine": HitlRefineNode(enabled=bool(self.config.get("hitl", {}).get("enabled"))),
            "ingest": IngestNode(
                services=self._services,
                trusted_scope=self._trusted_scope,
                session_id=self._ingest_session_id,
            ),
            "feedback": FeedbackNode(services=self._services),
        }

    def add_edges(self) -> None:
        self._sg.add_edge(START, "route_mode")
        self._sg.add_conditional_edges("route_mode", self.route, {"invoke": "stage1", "ingest": "ingest", "feedback": "feedback", "error": END})
        self._sg.add_edge("stage1", "stage2")
        self._sg.add_edge("stage2", "stage3")
        self._sg.add_edge("stage3", "stage4")
        self._sg.add_edge("stage4", "hitl_refine")
        self._sg.add_edge("hitl_refine", END)
        self._sg.add_edge("ingest", END)
        self._sg.add_edge("feedback", END)

    def route(self, state: State) -> str:
        return state.get("mode", "error") if state.get("status") != AgentStatus.ERROR.value else "error"

    def get_output(self, state: State) -> dict:
        keys = (
            "mode", "status", "criteria_records", "retrieved_records", "review_issues", "disclosure_map",
            "escalation_flags", "evidence_map", "cited_source_ids", "requires_human_review", "missing_data_flags",
            "ingest_summary", "feedback_result", "degradation_reason", "hitl_draft", "hitl_feedback", "hitl_status", "error_log",
        )
        return {key: state[key] for key in keys if key in state}
