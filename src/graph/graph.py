"""Outer fixed backbone for BroadcastStandardsReviewAgent."""

from __future__ import annotations

from typing import Any, Mapping

from framework.graph.agent_base_graph import AgentBaseGraph

from src.nodes.finalize_node import SafeFinalizeNode
from src.nodes.main_node import MainNode
from src.nodes.post_process_node import PostProcessNode
from src.nodes.pre_process_node import PreProcessNode
from src.schemas.state import State
from src.services.service import ServiceContainer, wire_services


class BroadcastStandardsReviewAgent(AgentBaseGraph):
    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        services: ServiceContainer | None = None,
        trusted_scope: Mapping[str, str] | None = None,
        ingest_session_id: str | None = None,
    ) -> None:
        super().__init__(config=config)
        self._services = services or wire_services(config)
        self._trusted_scope = dict(trusted_scope) if trusted_scope is not None else None
        self._ingest_session_id = ingest_session_id

    @property
    def name(self) -> str:
        return "SVC-C2-085"

    @property
    def state_schema(self) -> type:
        return State

    def register_nodes(self) -> None:
        super().register_nodes()
        self._nodes["pre_process"] = PreProcessNode(services=self._services)
        self._nodes["main"] = MainNode(
            services=self._services,
            config=self.config,
            trusted_scope=self._trusted_scope,
            ingest_session_id=self._ingest_session_id,
        )
        self._nodes["post_process"] = PostProcessNode()
        self._nodes["finalize"] = SafeFinalizeNode()

    def get_output(self, state) -> dict:
        if state.get("status") == "error":
            # Error diagnostics remain in audit/checkpoint storage.  The
            # client contract intentionally exposes only this fixed envelope.
            formatted = state.get("formatted_output")
            error_code = formatted.get("error_code") if isinstance(formatted, dict) else None
            return {
                "status": "error",
                "error_code": error_code or "review_processing_failed",
                "correlation_id": state.get("correlation_id", ""),
            }
        return {
            "output": state.get("formatted_output"), "status": state.get("status"),
            "trace_id": state.get("trace_id", ""), "correlation_id": state.get("correlation_id", ""),
            "node_history": state.get("node_history", []), "error_log": state.get("error_log", []),
        }


# AgentRegistry imports Graph in legacy manifests; keep the alias concrete.
Graph = BroadcastStandardsReviewAgent
