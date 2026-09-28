"""S-3 output boundary and mode-specific public envelope."""

from __future__ import annotations

import unicodedata
from typing import Any, ClassVar

from framework.errors import SecurityViolationError
from framework.schemas.trust_level import TrustLevel
from shared.security.pii_detector import detect_pii

from src.output_envelope import OutputEnvelopeNode
from src.evidence_gate import ProvenanceContract, validate_provenance


class PostProcessNode(OutputEnvelopeNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL
    required_fields_by_mode: ClassVar[dict[str, tuple[str, ...]]] = {
        "invoke": ("mode", "gate_kind", "review_issues", "evidence_map"),
        "ingest": ("mode", "gate_kind", "ingest_summary"),
        "feedback": ("mode", "gate_kind", "feedback_result"),
    }
    _forbidden_keys = frozenset({"broadcastok", "reviewpassed", "iscompliant", "verdict"})
    _decisive_terms = (
        "放送してよい", "問題ない", "問題ありません", "問題nai", "薬機法上クリア", "違反する", "抵触する",
        "基準を満たす", "基準を充足する", "で可", "対応で可",
    )
    _contract = ProvenanceContract(
        passthrough_keys=(
            "review_issues", "disclosure_map", "escalation_flags", "criteria_records", "degradation_reason",
            "requires_human_review", "missing_data_flags",
        )
    )

    def execute(self, state: dict) -> dict:
        from shared.utils.audit_logger import emit_trace_event

        emit_trace_event("broadcast_postprocess_requested", {"mode": state.get("mode", "")}, state)
        return super().execute(state)

    def format_invoke(self, state: dict) -> dict[str, Any]:
        return {
            "review_issues": list(state.get("review_issues") or []),
            "disclosure_map": dict(state.get("disclosure_map") or {}),
            "escalation_flags": list(state.get("escalation_flags") or []),
            "evidence_map": list(state.get("evidence_map") or []),
            "cited_source_ids": list(state.get("cited_source_ids") or []),
            "retrieved_records": list(state.get("retrieved_records") or []),
            "criteria_records": list(state.get("criteria_records") or []),
            "requires_human_review": bool(state.get("requires_human_review", False)),
        }

    def format_ingest(self, state: dict) -> dict[str, Any]:
        return {"ingest_summary": dict(state.get("ingest_summary") or {})}

    def format_feedback(self, state: dict) -> dict[str, Any]:
        return {"feedback_result": dict(state.get("feedback_result") or {})}

    def on_gated_invoke_output(self, state: dict, formatted: dict[str, Any]) -> None:
        # Intentionally no persistence: post-process state is already checkpointed.
        return None

    def _sanitize_client_echoes(self, formatted: dict[str, Any]) -> dict[str, Any]:
        # The envelope part supplies shape/depth guarding; no name resolution is
        # permitted here because this dict is checkpointed state.
        return super()._sanitize_client_echoes(formatted)

    def _extra_security_gate_output(self, result: dict) -> dict:
        result = super()._extra_security_gate_output(result)
        formatted = result["formatted_output"]
        if formatted.get("gate_kind") != "draft":
            return result
        self._assert_non_authoritative(formatted)
        self._assert_no_decisive_lexicon(formatted)
        # Include Stage1 criteria only for validation, then preserve the public
        # retrieved-records projection unchanged.
        validation = dict(formatted)
        validation["retrieved_records"] = list(formatted.get("retrieved_records", [])) + list(
            formatted.get("criteria_records", [])
        )
        validate_provenance(validation, self._contract)
        self._assert_no_pii_or_confidential_echo(formatted)
        return result

    def _assert_non_authoritative(self, value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                normalized = "".join(ch for ch in unicodedata.normalize("NFKC", str(key)).casefold() if ch.isalnum())
                if normalized in self._forbidden_keys:
                    raise SecurityViolationError("S-3: authoritative decision field is forbidden")
                self._assert_non_authoritative(item)
        elif isinstance(value, list):
            for item in value:
                self._assert_non_authoritative(item)

    def _assert_no_decisive_lexicon(self, value: Any) -> None:
        if isinstance(value, str):
            # Whitespace cannot be used to split an authoritative phrase.
            normalized = "".join(unicodedata.normalize("NFKC", value).casefold().split())
            if any(term in normalized for term in self._decisive_terms):
                raise SecurityViolationError("S-3: decisive legal/compliance wording is forbidden")
        elif isinstance(value, dict):
            for item in value.values():
                self._assert_no_decisive_lexicon(item)
        elif isinstance(value, list):
            for item in value:
                self._assert_no_decisive_lexicon(item)

    def _assert_no_pii_or_confidential_echo(self, value: Any) -> None:
        if isinstance(value, str):
            if detect_pii(value) or "スポンサー:" in value or "商品名:" in value:
                raise SecurityViolationError("S-3: PII or confidential business text detected")
        elif isinstance(value, dict):
            for item in value.values():
                self._assert_no_pii_or_confidential_echo(item)
        elif isinstance(value, list):
            for item in value:
                self._assert_no_pii_or_confidential_echo(item)
