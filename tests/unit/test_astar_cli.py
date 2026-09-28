from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator

import pytest
import yaml

import cli
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.context import bound_secrets
from shared.services.llm.base_llm import BaseLLM
from src.graph.graph import BroadcastStandardsReviewAgent
from src.payload_store import PayloadStoreError


ROOT = Path(__file__).resolve().parents[2]
REQUIRED_ENV = {
    "AZURE_OPENAI_API_KEY": "test-key",
    "AZURE_OPENAI_ENDPOINT": "https://azure.invalid",
    "AZURE_OPENAI_DEPLOYMENT": "test-deployment",
    "STG_DEFAULT_BROADCASTER_ID": "broadcaster-test",
    "STG_DEFAULT_REVIEW_DESK_ID": "review-desk-test",
}
FAKE_MARKER = "FAKE_LLM_MARKER"


def _runtime_config() -> dict[str, Any]:
    loaded = yaml.safe_load((ROOT / "config" / "config.yaml").read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


class FakeLLM(BaseLLM):
    def __init__(self) -> None:
        self.complete_calls = 0

    def complete(self, messages: list[Any]) -> dict[str, Any]:
        self.complete_calls += 1
        prompt = json.loads(messages[0]["content"])
        criterion = prompt["criteria"][0]
        return {
            "content": json.dumps(
                {
                    "review_issues": [
                        {
                            "issue_id": "fake_issue",
                            "input_span_id": "span_1",
                            "criteria_id": criterion["criteria_id"],
                            "criteria_version": criterion["criteria_version"],
                            "axis": criterion["axis"],
                            "requires_human_review": False,
                            "policy_source": "baseline",
                            "tenant_policy_id": None,
                            "issue_text": FAKE_MARKER,
                        }
                    ]
                },
                ensure_ascii=False,
            )
        }

    def stream(self, messages: list[Any]) -> Iterator[str]:
        yield str(self.complete(messages)["content"])

    def bind_tools(self, tools: list[Any]) -> BaseLLM:
        del tools
        return self


@pytest.fixture(autouse=True)
def marketplace_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)


def _runner_graph(fake: FakeLLM | None = None) -> tuple[cli.MarketplaceGraph, FakeLLM]:
    llm = fake or FakeLLM()
    config = _runtime_config()
    config["llm"] = llm
    agent = cli.MarketplaceGraph(config=config)
    agent.compile()
    agent.provision_secrets(cli._AllowlistedEnvironmentProvider())
    return agent, llm


def _verified_context() -> InvocationContext:
    return InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)


def _invoke(
    agent: cli.MarketplaceGraph,
    text: str = "番組内で提供表示の方法を考査してください。",
    *,
    input_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    with bound_secrets(agent._secrets_provider):
        return agent.invoke(text, ctx=_verified_context(), input_context=input_context)


def test_main_wires_lazy_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: dict[str, Any] = {}

    def recorder(*args: Any, **kwargs: Any) -> None:
        recorded["args"] = args
        recorded["kwargs"] = kwargs

    monkeypatch.setattr(cli, "run_agent_marketplace", recorder)
    cli.main()

    assert recorded["args"] == (cli.MarketplaceGraph,)
    assert recorded["kwargs"]["agent_name"] == "SVC-C2-085"
    assert recorded["kwargs"]["namespace"] == "svc"
    assert isinstance(recorded["kwargs"]["config"]["llm"], cli._LazyAzureLLM)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("AZURE_OPENAI_API_KEY", None),
        ("AZURE_OPENAI_API_KEY", ""),
        ("AZURE_OPENAI_API_KEY", " padded"),
        ("STG_DEFAULT_BROADCASTER_ID", None),
        ("STG_DEFAULT_BROADCASTER_ID", ""),
        ("STG_DEFAULT_BROADCASTER_ID", "padded "),
    ],
)
def test_main_fails_closed_before_runner(
    monkeypatch: pytest.MonkeyPatch, key: str, value: str | None
) -> None:
    called = False

    def recorder(*args: Any, **kwargs: Any) -> None:
        nonlocal called
        del args, kwargs
        called = True

    monkeypatch.setattr(cli, "run_agent_marketplace", recorder)
    if value is None:
        monkeypatch.delenv(key, raising=False)
    else:
        monkeypatch.setenv(key, value)

    with pytest.raises(ValueError):
        cli.main()
    assert called is False


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://example.cognitiveservices.azure.com/openai/responses?api-version=2025-04-01-preview",
        "https://example.services.ai.azure.example/openai",
        "https://example.services.ai.azure.example/v1",
        "https://example.services.ai.azure.example?api-version=2024-10-21",
        "example.services.ai.azure.example",
    ],
    ids=["api-path", "openai-path", "base-path", "query-only", "missing-scheme"],
)
def test_unusable_azure_endpoint_shape_fails_before_runner(
    monkeypatch: pytest.MonkeyPatch, endpoint: str
) -> None:
    """The client appends its own path, so anything but a bare endpoint breaks.

    The resulting error surfaces only at call time; a node that swallows it
    degrades silently while the invocation still reports status: success, so
    both startup paths reject the shape instead.
    """

    called = False

    def recorder(*args: Any, **kwargs: Any) -> None:
        nonlocal called
        del args, kwargs
        called = True

    monkeypatch.setattr(cli, "run_agent_marketplace", recorder)
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", endpoint)

    with pytest.raises(ValueError, match="bare"):
        cli.main()
    assert called is False

    with pytest.raises(ValueError, match="bare"):
        cli.MarketplaceGraph().provision_secrets(cli._AllowlistedEnvironmentProvider())


def test_runner_order_invokes_real_llm_seam_and_returns_marker() -> None:
    agent, fake = _runner_graph()

    result = _invoke(agent, "この番組原稿の提供表示を確認してください。")

    assert result["status"] == "success"
    assert isinstance(result["output"], dict) and result["output"]
    assert fake.complete_calls >= 1
    assert any(FAKE_MARKER in issue["issue_text"] for issue in result["output"]["review_issues"])
    serialized = json.dumps(result, ensure_ascii=False, default=str)
    assert "llm_not_configured" not in serialized


def test_runner_conversation_history_canary_is_discarded(monkeypatch: pytest.MonkeyPatch) -> None:
    agent, _ = _runner_graph()
    observed_context: dict[str, Any] = {}
    original_invoke = BroadcastStandardsReviewAgent.invoke

    def record_context(
        self: BroadcastStandardsReviewAgent,
        user_input: str,
        session_id: str = "",
        ctx: InvocationContext | None = None,
        input_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        observed_context.update(input_context or {})
        return original_invoke(
            self,
            user_input,
            session_id=session_id,
            ctx=ctx,
            input_context=input_context,
        )

    monkeypatch.setattr(BroadcastStandardsReviewAgent, "invoke", record_context)

    result = _invoke(
        agent,
        input_context={"conversation_history": [{"role": "user", "content": "CANARY_XYZ"}]},
    )

    assert observed_context == {
        "broadcaster_id": REQUIRED_ENV["STG_DEFAULT_BROADCASTER_ID"],
        "review_desk_id": REQUIRED_ENV["STG_DEFAULT_REVIEW_DESK_ID"],
    }
    assert "CANARY_XYZ" not in json.dumps(result, ensure_ascii=False, default=str)


def test_pasted_ingest_json_remains_opaque_review_text() -> None:
    agent, fake = _runner_graph()
    submitted = json.dumps({"mode": "ingest", "payload": {"records": []}})

    result = _invoke(agent, submitted)

    assert result["status"] == "success"
    assert result["output"]["mode"] == "invoke"
    assert result["output"]["gate_kind"] == "draft"
    assert "ingest_summary" not in result["output"]
    assert fake.complete_calls >= 1


def test_payload_ref_is_deleted_after_success(monkeypatch: pytest.MonkeyPatch) -> None:
    agent, _ = _runner_graph()
    scope = cli._scope_from_environment(cli._AllowlistedEnvironmentProvider())
    refs: list[tuple[str, str]] = []
    original_put = agent._services.payload_store.put

    def capture_put(value: Any, *, scope: dict[str, str], session_id: str) -> str:
        ref = original_put(value, scope=scope, session_id=session_id)
        refs.append((ref, session_id))
        return ref

    monkeypatch.setattr(agent._services.payload_store, "put", capture_put)
    assert _invoke(agent)["status"] == "success"

    assert len(refs) == 1
    with pytest.raises(PayloadStoreError, match="payload_ref_unknown"):
        agent._services.payload_store.resolve(refs[0][0], scope=scope, session_id=refs[0][1])


def test_payload_ref_is_deleted_when_graph_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    agent, _ = _runner_graph()
    scope = cli._scope_from_environment(cli._AllowlistedEnvironmentProvider())
    refs: list[tuple[str, str]] = []
    original_put = agent._services.payload_store.put

    def capture_put(value: Any, *, scope: dict[str, str], session_id: str) -> str:
        ref = original_put(value, scope=scope, session_id=session_id)
        refs.append((ref, session_id))
        return ref

    def raise_from_graph(self: BroadcastStandardsReviewAgent, *args: Any, **kwargs: Any) -> dict[str, Any]:
        del self, args, kwargs
        raise RuntimeError("forced graph failure")

    monkeypatch.setattr(agent._services.payload_store, "put", capture_put)
    monkeypatch.setattr(BroadcastStandardsReviewAgent, "invoke", raise_from_graph)
    with bound_secrets(agent._secrets_provider), pytest.raises(RuntimeError, match="forced graph failure"):
        agent.invoke("例外経路でも削除する原稿", ctx=_verified_context())

    assert len(refs) == 1
    with pytest.raises(PayloadStoreError, match="payload_ref_unknown"):
        agent._services.payload_store.resolve(refs[0][0], scope=scope, session_id=refs[0][1])


def test_review_criteria_path_is_absolute_and_exists() -> None:
    agent = cli.MarketplaceGraph(config=_runtime_config())
    criteria_path = Path(agent.config["review_criteria_path"])

    assert criteria_path.is_absolute()
    assert criteria_path.is_file()


def test_default_and_explicit_llm_configuration() -> None:
    default_config = _runtime_config()
    default_config["llm"] = None
    default_agent = cli.MarketplaceGraph(config=default_config)
    sentinel = object()
    explicit_agent = cli.MarketplaceGraph(config={**_runtime_config(), "llm": sentinel})

    assert isinstance(default_agent.config["llm"], cli._LazyAzureLLM)
    assert explicit_agent.config["llm"] is sentinel


def test_lazy_azure_llm_retains_no_credentials() -> None:
    assert vars(cli._LazyAzureLLM()) == {}


def test_bind_tools_returns_a_new_lazy_llm() -> None:
    llm = cli._LazyAzureLLM()

    bound = llm.bind_tools([object()])

    assert isinstance(bound, cli._LazyAzureLLM)
    assert bound is not llm
    assert not isinstance(bound, cli._AzureOpenAIClientCompat)


def test_bind_tools_does_not_resolve_azure_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in cli._AZURE_OPENAI_SECRET_KEYS:
        monkeypatch.delenv(key, raising=False)

    bound = cli._LazyAzureLLM().bind_tools([object()])

    assert isinstance(bound, cli._LazyAzureLLM)
