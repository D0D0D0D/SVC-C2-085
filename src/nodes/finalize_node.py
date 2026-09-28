"""Safe finalizer for routes where post_process is intentionally skipped."""

from __future__ import annotations

from typing import ClassVar

from framework.nodes.defaults.finalize_node import FinalizeNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel


class SafeFinalizeNode(FinalizeNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def on_finalize(self, state: dict) -> dict:
        from shared.utils.audit_logger import emit_trace_event

        status = state.get("status")
        if status != AgentStatus.SUCCESS.value:
            emit_trace_event("broadcast_finalize_error", {"status": str(status)}, state)
            return {
                "formatted_output": {
                    "status": AgentStatus.ERROR.value,
                    "error_code": "review_processing_failed",
                    "correlation_id": state.get("correlation_id", ""),
                },
                "status": AgentStatus.ERROR.value,
            }
        emit_trace_event("broadcast_finalize_success", {"mode": state.get("mode", "")}, state)
        return {}
