"""Fixtures for an optional live-LLM invocation check."""

from __future__ import annotations

import pytest

import src.graph.graph as graph_module
from src.services.service import wire_services


_LIVE_SCOPE = {"broadcaster_id": "broadcaster-live", "review_desk_id": "desk-live"}
_LIVE_SESSION_ID = "live-1"


def _live_script() -> str:
    return (
        "スポンサー: Example Foods\n"
        "商品名: Wellness Drink\n"
        "情報番組の本編で、司会者がスポンサーから商品提供を受けた事実を表示せず、"
        "通常の編集企画として商品を繰り返し紹介する。画面上に広告・PRの表示はない。\n"
        "出演者は『飲むだけで血圧が下がり、病気を予防できる』と断定し、"
        "根拠資料や注意表示を示さない。"
    )


@pytest.fixture
def _live_services_and_ref(monkeypatch):
    services = wire_services({"memory_enabled": False, "hitl": {"enabled": False}})
    refs = services.name_registry.register_from_text(
        _live_script(),
        broadcaster_id=_LIVE_SCOPE["broadcaster_id"],
        review_desk_id=_LIVE_SCOPE["review_desk_id"],
    )
    ref = services.payload_store.put(
        _live_script(),
        scope=_LIVE_SCOPE,
        session_id=_LIVE_SESSION_ID,
    )

    # The graph normally receives its ServiceContainer as a constructor-only
    # argument. The generated live-check constructs it with config alone, so
    # return this request's container and copy the injected LLM into its seam.
    def wire_live_services(config=None):
        services.llm = (config or {}).get("llm")
        return services

    monkeypatch.setattr(graph_module, "wire_services", wire_live_services)
    return services, ref, refs


@pytest.fixture
def agent_config(_live_services_and_ref) -> dict:
    return {
        "max_retry": 3,
        "memory_enabled": False,
        "hitl": {"enabled": False, "max_hitl": 8},
    }


@pytest.fixture
def case_payload(_live_services_and_ref) -> dict:
    services, ref, refs = _live_services_and_ref
    return {
        "mode": "invoke",
        "payload_ref": ref,
        **_LIVE_SCOPE,
        "ref_hints": services.name_registry.ref_hints(refs),
        "broadcast_date": "2026-08-12",
        "program_type": "commercial",
    }


@pytest.fixture
def live_allowed_degradation_reasons() -> set[str]:
    """実 LLM 1回転チェックで許容する縮退理由。

    蓄積型の裁定 KB は統合テストが seed する。実 LLM 1回転チェックは
    # 未シードの空 KB に対して回すため、この縮退理由だけは想定内として許可する
    # （LLM 経路は実際に呼ばれ、review_issues に実質的な内容が生成されていることを確認済み）。
    """
    return {"empty_ruling_kb"}
