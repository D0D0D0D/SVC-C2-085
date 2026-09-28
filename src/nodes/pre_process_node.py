"""S-2 input boundary for the three review modes."""

from __future__ import annotations

import json
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.payload_store import PayloadStoreError
from src.services.service import ServiceContainer, contains_sensitive_submission, sanitize_sensitive_text, wire_services


class PreProcessNode(FunctionNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL
    _allowed_modes = frozenset({"ingest", "invoke", "feedback"})

    def __init__(self, *, services: ServiceContainer | None = None) -> None:
        super().__init__()
        self._services = services or wire_services()

    def execute(self, state: dict) -> dict:
        from shared.utils.audit_logger import emit_trace_event

        # Retry/resume must never spend the single-use raw payload a second time.
        if isinstance(state.get("validated_input"), str) and state["validated_input"]:
            emit_trace_event("broadcast_preprocess_reused_validated_input", {"mode": state.get("mode", "")}, state)
            return {"status": AgentStatus.SUCCESS.value}

        envelope = self._parse_envelope(state.get("user_input"))
        mode = envelope.get("mode")
        broadcaster_id = envelope.get("broadcaster_id")
        review_desk_id = envelope.get("review_desk_id")
        session_id = state.get("session_id", "")
        if mode not in self._allowed_modes or not isinstance(broadcaster_id, str) or not isinstance(review_desk_id, str):
            emit_trace_event("broadcast_preprocess_rejected", {"reason": "invalid_envelope"}, state)
            return {"status": AgentStatus.ERROR.value, "error_log": ["invalid_mode_or_scope"]}
        if not broadcaster_id or not review_desk_id or not session_id:
            emit_trace_event("broadcast_preprocess_rejected", {"reason": "missing_scope"}, state)
            return {"status": AgentStatus.ERROR.value, "error_log": ["scope_missing_fail_closed"]}

        payload_ref = envelope.get("payload_ref")
        if not isinstance(payload_ref, str) or not payload_ref:
            emit_trace_event("broadcast_preprocess_rejected", {"reason": "payload_ref_missing"}, state)
            return {"status": AgentStatus.ERROR.value, "error_log": ["payload_ref_missing"]}
        if mode == "ingest":
            # The v0.3.1 row pipeline owns the single scoped consume.  It is
            # constructed from the authenticated request context, not State;
            # pre-process must therefore leave this reference untouched.
            emit_trace_event("broadcast_preprocess_deferred_ingest_payload", {"mode": mode}, state)
            return {
                "mode": mode,
                "broadcaster_id": broadcaster_id,
                "review_desk_id": review_desk_id,
                "payload_ref": payload_ref,
                "status": AgentStatus.SUCCESS.value,
            }
        try:
            raw = self._services.payload_store.resolve(
                payload_ref,
                scope={"broadcaster_id": broadcaster_id, "review_desk_id": review_desk_id},
                session_id=session_id,
                consume=True,
            )
        except PayloadStoreError as exc:
            event = "payload_expired" if str(exc) == "payload_ref_expired" else "payload_deleted"
            emit_trace_event(event, {"payload_ref": payload_ref}, state)
            return {"status": AgentStatus.ERROR.value, "error_log": [str(exc)]}

        if not isinstance(raw, str) or raw.startswith(("%PDF-", "RIFF", "\x1aE\xdf\xa3")):
            emit_trace_event("broadcast_preprocess_rejected", {"reason": "non_text_input"}, state)
            return {"status": AgentStatus.ERROR.value, "error_log": ["non_text_input_rejected"]}
        sanitized = sanitize_sensitive_text(raw, ref_hints=envelope.get("ref_hints") or {})
        if contains_sensitive_submission(raw, allow_commercial_references=mode in {"invoke", "ingest"}):
            emit_trace_event("broadcast_preprocess_sanitized_sensitive_input", {"mode": mode}, state)
        emit_trace_event("broadcast_preprocess_complete", {"mode": mode, "payload_ref": payload_ref}, state)
        return {
            "mode": mode,
            "broadcaster_id": broadcaster_id,
            "review_desk_id": review_desk_id,
            "payload_ref": payload_ref,
            "validated_input": sanitized,
            # These envelope fields are non-sensitive request metadata.  They
            # must survive S-2 so Stage1 selects criteria by broadcast date,
            # rather than silently falling back to today's date.
            "broadcast_date": envelope.get("broadcast_date"),
            "program_type": envelope.get("program_type", ""),
            "status": AgentStatus.SUCCESS.value,
        }

    def _extra_security_gate_input(self, state: dict) -> dict:
        # The envelope itself is checkpoint-safe.  Reject attempts to smuggle a
        # raw body directly into it rather than falling back to that body.
        raw = state.get("user_input", "")
        if isinstance(raw, str) and raw.startswith(("%PDF-", "RIFF")):
            return {**state, "status": AgentStatus.ERROR.value, "error_log": ["non_text_input_rejected"]}
        return state

    @staticmethod
    def _parse_envelope(value: Any) -> dict[str, Any]:
        if not isinstance(value, str):
            return {}
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
