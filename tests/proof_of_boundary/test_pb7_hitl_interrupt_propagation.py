"""PB-7: HITL is mandatory for this template and must propagate interrupts."""

from __future__ import annotations

from pathlib import Path

import yaml
from langgraph.checkpoint.memory import InMemorySaver

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from src.graph.graph import BroadcastStandardsReviewAgent
from src.services.service import wire_services


ROOT = Path(__file__).parents[2]

#: Static manifest fallback for installations without a runtime HITL setting.
#: `_hitl_enabled()` consults config/config.yaml first because that file owns
#: runtime settings, then falls back to this manifest for compatibility.
_CONFIG_PATH = ROOT / "config" / "agent.yaml"


def _hitl_enabled() -> bool:
    """Read runtime config first; a malformed config is a failing test, not skip."""

    config_path = ROOT / "config" / "config.yaml"
    agent_path = _CONFIG_PATH
    try:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
        if not isinstance(data, dict):
            raise AssertionError("config/config.yaml must be a YAML mapping")
        runtime = data.get("hitl", {})
        if isinstance(runtime, dict) and "enabled" in runtime:
            return bool(runtime["enabled"])
        manifest = yaml.safe_load(agent_path.read_text(encoding="utf-8")) if agent_path.exists() else {}
        return bool((manifest or {}).get("hitl", {}).get("enabled", False))
    except Exception as exc:  # fail closed: HITL test must never auto-waive on parse failure
        raise AssertionError("unable to determine HITL configuration") from exc


def _agent(*, hitl_allowed: bool) -> tuple[BroadcastStandardsReviewAgent, InvocationContext, object]:
    config = yaml.safe_load((ROOT / "config" / "config.yaml").read_text(encoding="utf-8"))
    services = wire_services(config)
    agent = BroadcastStandardsReviewAgent(config, services=services)
    agent.compile(checkpointer=InMemorySaver())
    ctx = InvocationContext(
        session_id="pb7-session", thread_id="pb7-thread", caller_trust_level=TrustLevel.VERIFIED_EXTERNAL,
        hitl_allowed=hitl_allowed,
    )
    return agent, ctx, services


def test_pb7_hitl_interrupt_propagates() -> None:
    assert _hitl_enabled() is True, "this HITL template must not be auto-waived"
    agent, ctx, services = _agent(hitl_allowed=True)
    ref = services.payload_store.put("スポンサー: Example Sponsor", scope={"broadcaster_id": "tenant", "review_desk_id": "org"}, session_id=ctx.session_id)
    result = agent.invoke(
        '{"mode":"invoke","payload_ref":"%s","broadcaster_id":"tenant","review_desk_id":"org"}' % ref, ctx=ctx
    )
    # A GraphInterrupt reached the LangGraph engine: BaseGraph converted it to
    # its resumable awaiting-human envelope instead of an error response.
    assert result["status"] == "awaiting_human"
    assert result["status"] != "error"


def test_pb7_hitl_allowed_false_skips_interrupt() -> None:
    agent, ctx, services = _agent(hitl_allowed=False)
    ref = services.payload_store.put("スポンサー: Example Sponsor", scope={"broadcaster_id": "tenant", "review_desk_id": "org"}, session_id=ctx.session_id)
    result = agent.invoke(
        '{"mode":"invoke","payload_ref":"%s","broadcaster_id":"tenant","review_desk_id":"org"}' % ref, ctx=ctx
    )
    assert result["status"] == "success"
