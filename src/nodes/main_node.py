"""Cat2 main-slot GraphNode for the broadcast-review domain workflow."""

from __future__ import annotations

import json
from typing import Any, ClassVar, Mapping

from framework.errors import SecurityViolationError
from framework.schemas.trust_level import TrustLevel

from src.cat2_graph_skeleton import DomainWorkflowGraphNode, ModeSpec
from src.evidence_gate import ProvenanceContract, validate_provenance
from src.services.service import ServiceContainer, wire_services


class MainNode(DomainWorkflowGraphNode):
    """Adapted cat2-graph-skeleton; inner graph remains cached for HITL resume."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL
    propagate_hitl: ClassVar[bool] = True
    mode_specs: ClassVar[dict[str, ModeSpec]] = {
        "ingest": ModeSpec("ingest", {"ingest_summary": "ingest_summary", "error_log": "error_log"}),
        "feedback": ModeSpec("feedback", {"feedback_result": "feedback_result", "error_log": "error_log"}),
        "invoke": ModeSpec(
            "draft",
            {
                "review_issues": "review_issues", "criteria_records": "criteria_records",
                "retrieved_records": "retrieved_records", "disclosure_map": "disclosure_map",
                "escalation_flags": "escalation_flags", "evidence_map": "evidence_map",
                "cited_source_ids": "cited_source_ids", "missing_data_flags": "missing_data_flags",
                "requires_human_review": "requires_human_review", "error_log": "error_log",
            },
        ),
    }
    _contract = ProvenanceContract(
        passthrough_keys=(
            "review_issues", "criteria_records", "disclosure_map", "escalation_flags", "requires_human_review",
            "missing_data_flags", "degradation_reason",
        )
    )

    def __init__(
        self,
        *,
        services: ServiceContainer | None = None,
        config: dict[str, Any] | None = None,
        trusted_scope: Mapping[str, str] | None = None,
        ingest_session_id: str | None = None,
    ) -> None:
        self._services = services or wire_services(config)
        self._trusted_scope = dict(trusted_scope) if trusted_scope is not None else None
        self._ingest_session_id = ingest_session_id
        super().__init__(config=config)

    def _build_subgraph(self):
        from src.graph.domain_workflow_graph import DomainWorkflowGraph

        return DomainWorkflowGraph(
            config=self._parent_config(),
            services=self._services,
            trusted_scope=self._trusted_scope,
            ingest_session_id=self._ingest_session_id,
        )

    def _parent_config(self) -> dict[str, Any]:
        forwarded = super()._parent_config()
        forwarded["llm"] = self._services.llm  # injected seam; no model/client is constructed in a node
        return forwarded

    def extract_input(self, state: dict) -> str:
        # The inner graph receives a JSON-safe envelope rather than an ambiguous
        # raw string so its first node can restore the domain fields.
        keys = (
            "mode", "broadcaster_id", "review_desk_id", "payload_ref", "validated_input", "broadcast_date",
            "program_type", "session_id", "hitl_draft", "hitl_feedback", "hitl_status",
        )
        return json.dumps({key: state.get(key) for key in keys if key in state}, ensure_ascii=False, default=str)

    def _extra_security_gate_output(self, result: dict) -> dict:
        if result.get("status") == "error" or result.get("gate_kind") != "draft":
            return result
        required = ("review_issues", "criteria_records", "retrieved_records", "evidence_map", "cited_source_ids")
        if any(key not in result for key in required):
            raise SecurityViolationError("S-3: incomplete draft provenance boundary")
        validation = dict(result)
        validation["retrieved_records"] = list(result["retrieved_records"]) + list(result["criteria_records"])
        validate_provenance(validation, self._contract)
        return result
