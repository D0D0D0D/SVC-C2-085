"""AGENTIC STAR Marketplace entry adapter for SVC-C2-085."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

from langgraph.checkpoint.memory import InMemorySaver

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.base import SecretProvider
from framework.secrets.context import current_secrets
from shared.services.llm.base_llm import BaseLLM

try:
    from framework.utils.config_loader import load_agent_config
except ImportError:  # AgentCore 1.0.0 / 1.0.1 compatibility
    from framework.utils.config_loader import load_config

    def load_agent_config(agent_dir: Path) -> dict[str, Any]:
        config_path = agent_dir / "config" / "config.yaml"
        return load_config(str(config_path)) if config_path.exists() else {}


try:
    from shared.bootstrap import run_agent_marketplace
except ModuleNotFoundError as exc:  # AgentCore 1.0.0 / 1.0.1 compatibility
    if exc.name != "shared.bootstrap":
        raise
    _BOOTSTRAP_IMPORT_ERROR: ModuleNotFoundError | None = exc

    def run_agent_marketplace(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise RuntimeError(
            "The installed AgentCore wheel does not provide Marketplace bootstrap support"
        ) from _BOOTSTRAP_IMPORT_ERROR
else:
    _BOOTSTRAP_IMPORT_ERROR = None


try:
    from shared.secrets.env_provider import EnvProvider
except ModuleNotFoundError as exc:  # AgentCore 1.0.0 / 1.0.1 compatibility
    if exc.name != "shared.secrets.env_provider":
        raise

    class EnvProvider(SecretProvider):  # type: ignore[no-redef]
        """Minimal process-environment provider for AgentCore 1.0.1."""

        def __init__(self, namespace: str = "", agent_name: str = "") -> None:
            self._namespace = namespace
            self._agent_name = agent_name

        def get(self, key: str, default: str | None = None) -> str | None:
            return os.environ.get(key, default)


from src.entry_adapter import SCOPE_KEYS, STG_DEFAULT_SCOPE_ENVS
from src.graph.graph import BroadcastStandardsReviewAgent
from src.services.service import ServiceContainer, contains_sensitive_submission

_NAMESPACE = "svc"
_AGENT_NAME = "SVC-C2-085"
_PROVIDER_AGENT_NAME = "svc_c2_085"

_AZURE_OPENAI_SECRET_KEYS = (
    "AZURE_OPENAI_API_KEY",
    "AZURE_OPENAI_ENDPOINT",
    "AZURE_OPENAI_DEPLOYMENT",
)


def _validate_scope_declarations() -> None:
    """Validate the reusable entry-adapter declaration before env lookup."""

    if not isinstance(SCOPE_KEYS, tuple):
        raise RuntimeError("Marketplace SCOPE_KEYS must be a tuple")
    if len(set(SCOPE_KEYS)) != len(SCOPE_KEYS):
        raise RuntimeError("Marketplace SCOPE_KEYS must not contain duplicates")
    if any(not isinstance(key, str) or not key or any(char.isspace() for char in key) for key in SCOPE_KEYS):
        raise RuntimeError("Marketplace SCOPE_KEYS must contain clean non-empty strings")
    if not isinstance(STG_DEFAULT_SCOPE_ENVS, Mapping) or set(STG_DEFAULT_SCOPE_ENVS) != set(SCOPE_KEYS):
        raise RuntimeError("Marketplace STG_DEFAULT_SCOPE_ENVS must exactly match SCOPE_KEYS")
    if any(
        not isinstance(env_name, str) or not env_name or any(char.isspace() for char in env_name)
        for env_name in STG_DEFAULT_SCOPE_ENVS.values()
    ):
        raise RuntimeError("Marketplace scope environment names must be clean non-empty strings")


def _allowed_environment_keys() -> frozenset[str]:
    return frozenset((*_AZURE_OPENAI_SECRET_KEYS, *STG_DEFAULT_SCOPE_ENVS.values()))


class _AllowlistedEnvironmentProvider(EnvProvider):  # type: ignore[misc]
    """Expose only this single-agent container's declared environment values."""

    def __init__(self) -> None:
        super().__init__(namespace=_NAMESPACE, agent_name=_PROVIDER_AGENT_NAME)

    def get(self, key: str, default: str | None = None) -> str | None:
        if key not in _allowed_environment_keys():
            return default
        value = super().get(key)
        return value if value is not None else default


def _require_clean(provider: SecretProvider, key: str) -> str:
    """Resolve a required deployment value without trimming or repairing it."""

    value = provider.require(key)
    if not value or any(char.isspace() for char in value):
        raise ValueError(f"Marketplace environment variable {key!r} must be a clean non-empty string")
    return value


def _require_azure_endpoint(provider: SecretProvider) -> str:
    """Reject ``AZURE_OPENAI_ENDPOINT`` values that are not a bare Azure resource endpoint.

    ``AzureOpenAIClient`` appends its own ``/openai/v1/`` path, so anything
    beyond a scheme and host silently produces a broken request URL, and the
    failure surfaces inside ``complete()``. A node that swallows that exception
    degrades quietly while the invocation still reports ``status: success``,
    which is why this has to be a startup check. The value is reported here,
    never repaired.
    """

    endpoint = _require_clean(provider, "AZURE_OPENAI_ENDPOINT")
    parts = urlsplit(endpoint)
    try:
        port = parts.port
    except ValueError:
        port = -1
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or port == -1
        or parts.path.strip("/")
        or parts.query
        or parts.fragment
        or any(character.isspace() for character in endpoint)
    ):
        raise ValueError(
            "Marketplace environment variable 'AZURE_OPENAI_ENDPOINT' must be a bare Azure "
            "resource endpoint -- scheme and host only, with no path (such as "
            "'/openai'), query or fragment"
        )
    return endpoint


def _scope_from_environment(provider: SecretProvider) -> dict[str, str]:
    """Build exactly the scope declared by ``src.entry_adapter``."""

    _validate_scope_declarations()
    return {scope_key: _require_clean(provider, STG_DEFAULT_SCOPE_ENVS[scope_key]) for scope_key in SCOPE_KEYS}


def _validate_startup_environment() -> None:
    provider = _AllowlistedEnvironmentProvider()
    for key in _AZURE_OPENAI_SECRET_KEYS:
        _require_clean(provider, key)
    _require_azure_endpoint(provider)
    _scope_from_environment(provider)


class _AzureOpenAIClientCompat(BaseLLM):
    """Small Azure chat-completion client for AgentCore 1.0.1."""

    def __init__(self, config: dict[str, str]) -> None:
        self._config = dict(config)

    def complete(self, messages: list[Any]) -> dict[str, Any]:
        endpoint = self._config["azure_endpoint"].rstrip("/")
        deployment = quote(self._config["azure_deployment"], safe="")
        request = Request(
            f"{endpoint}/openai/deployments/{deployment}/chat/completions?api-version=2024-10-21",
            data=json.dumps({"messages": messages}).encode("utf-8"),
            headers={"api-key": self._config["api_key"], "content-type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=30) as response:  # noqa: S310 - deployment-owned endpoint
            payload = json.loads(response.read().decode("utf-8"))
        choices = payload.get("choices", []) if isinstance(payload, dict) else []
        message = choices[0].get("message", {}) if choices else {}
        content = message.get("content", "") if isinstance(message, dict) else ""
        return {
            "content": content,
            "tool_calls": message.get("tool_calls", []) if isinstance(message, dict) else [],
            "model": payload.get("model", self._config["azure_deployment"]),
            "usage": payload.get("usage", {}),
        }

    def stream(self, messages: list[Any]) -> Iterator[str]:
        content = self.complete(messages).get("content", "")
        if content:
            yield str(content)

    def bind_tools(self, tools: list[Any]) -> BaseLLM:
        if tools:
            raise NotImplementedError("Azure compatibility client does not support tool binding")
        return self


class _LazyAzureLLM(BaseLLM):
    """Resolve Azure credentials and create a fresh client for every operation."""

    def __init__(self, *, tools: tuple[Any, ...] = ()) -> None:
        if tools:
            self._tools = tools

    def _new_client(self) -> BaseLLM:
        provider = current_secrets()
        values = {key: _require_clean(provider, key) for key in _AZURE_OPENAI_SECRET_KEYS}

        try:
            from shared.services.llm.azure_openai_client import AzureOpenAIClient
        except ModuleNotFoundError as exc:  # AgentCore 1.0.0 / 1.0.1 compatibility
            if exc.name != "shared.services.llm.azure_openai_client":
                raise
            AzureOpenAIClient = _AzureOpenAIClientCompat  # type: ignore[misc,assignment]

        client = cast(
            BaseLLM,
            AzureOpenAIClient(
                {
                    "api_key": values["AZURE_OPENAI_API_KEY"],
                    "azure_endpoint": values["AZURE_OPENAI_ENDPOINT"],
                    "azure_deployment": values["AZURE_OPENAI_DEPLOYMENT"],
                }
            ),
        )
        tools = getattr(self, "_tools", ())
        return client.bind_tools(list(tools)) if tools else client

    def complete(self, messages: list[Any]) -> dict[str, Any]:
        return cast(dict[str, Any], self._new_client().complete(messages))

    def stream(self, messages: list[Any]) -> Iterator[str]:
        yield from self._new_client().stream(messages)

    def bind_tools(self, tools: list[Any]) -> BaseLLM:
        return type(self)(tools=tuple(tools))


def _load_file_config() -> dict[str, Any]:
    """Load runtime configuration relative to this adapter, never the CWD."""

    return dict(load_agent_config(Path(__file__).resolve().parent))


def _absolute_criteria_path(config: Mapping[str, Any]) -> str:
    value = config.get("review_criteria_path", "config/review_criteria.yaml")
    path = Path(value)
    if not path.is_absolute():
        path = Path(__file__).resolve().parent / path
    return str(path.resolve())


def _merged_config(config: Mapping[str, Any] | None = None) -> dict[str, Any]:
    merged = _load_file_config()
    merged.update(config or {})
    merged["review_criteria_path"] = _absolute_criteria_path(merged)
    if merged.get("llm") is None:
        merged["llm"] = _LazyAzureLLM()
    return merged


def _delete_payload_refs(
    services: ServiceContainer,
    refs: list[str],
    *,
    scope: dict[str, str],
    session_id: str,
) -> None:
    for ref in refs:
        try:
            services.payload_store.delete(ref, scope=scope, session_id=session_id)
        except ValueError:
            continue


class MarketplaceGraph(BroadcastStandardsReviewAgent):
    """Marketplace-only read boundary, deployment scope, and Azure wiring."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        _validate_startup_environment()
        merged = _merged_config(config)
        super().__init__(config=merged)

    def compile(self, checkpointer=None) -> None:
        super().compile(checkpointer=checkpointer or InMemorySaver())

    def invoke(
        self,
        user_input: str,
        session_id: str = "",
        ctx: InvocationContext | None = None,
        input_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        del input_context
        if ctx is None:
            ctx = InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)
        ctx = replace(ctx, hitl_allowed=False)
        scope = _scope_from_environment(_AllowlistedEnvironmentProvider())
        text = user_input if isinstance(user_input, str) else str(user_input)
        if contains_sensitive_submission(text, allow_commercial_references=True):
            return {
                "status": "error",
                "error_code": "unsafe_request_payload",
                "correlation_id": ctx.correlation_id,
            }
        ref = self._services.payload_store.put(text, scope=scope, session_id=ctx.session_id)
        try:
            envelope = json.dumps(
                {
                    "mode": "invoke",
                    "payload_ref": ref,
                    **scope,
                    "ref_hints": {},
                    "broadcast_date": None,
                    "program_type": "",
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            return cast(
                dict[str, Any],
                super().invoke(
                    envelope,
                    session_id=session_id,
                    ctx=ctx,
                    input_context=scope,
                ),
            )
        finally:
            _delete_payload_refs(
                self._services,
                [ref],
                scope=scope,
                session_id=ctx.session_id,
            )

    def provision_secrets(self, provider: SecretProvider) -> None:
        del provider
        environment_provider = _AllowlistedEnvironmentProvider()
        for key in _AZURE_OPENAI_SECRET_KEYS:
            _require_clean(environment_provider, key)
        _scope_from_environment(environment_provider)
        super().provision_secrets(environment_provider)


def main() -> None:
    _validate_startup_environment()
    run_agent_marketplace(
        MarketplaceGraph,
        agent_name=_AGENT_NAME,
        namespace=_NAMESPACE,
        config=_merged_config(),
    )


if __name__ == "__main__":
    main()
