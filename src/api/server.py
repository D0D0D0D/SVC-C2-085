"""HTTP entry adapter: authentication, scoped raw payloads, and delivery gate."""

from __future__ import annotations

import base64
import copy
import json
import threading
import weakref
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

import yaml
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field
from langgraph.checkpoint.memory import InMemorySaver

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.context import bound_secrets
from shared.secrets import factory as secrets_factory
from shared.utils.audit_logger import emit_trace_event

from src.entry_adapter import AuthContext, Operation, SCOPE_KEYS, authenticate
from src.ingest_row_pipeline import CheckpointScrubPolicy, scrub_checkpoint_envelope
from src.payload_store.payload_store import mint_reference
from src.ingest_file_sanitizer.file_sanitizer import sanitize_file_bytes
from src.graph.graph import BroadcastStandardsReviewAgent
from src.services.service import ServiceContainer, configured_criterion_matches, contains_sensitive_submission, wire_services


ROOT = Path(__file__).resolve().parents[2]
app = FastAPI(title="Broadcast Standards Review Draft Agent")
_resume_scopes: dict[str, tuple[str, str, str, str, set[str]]] = {}
_RESUME_LOCKS: "weakref.WeakValueDictionary[str, threading.Lock]" = weakref.WeakValueDictionary()
_RESUME_LOCKS_GUARD = threading.Lock()


def _load_runtime_config() -> dict[str, Any]:
    path = ROOT / "config" / "config.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def build_agent(
    *,
    services: ServiceContainer | None = None,
    trusted_scope: dict[str, str] | None = None,
    ingest_session_id: str | None = None,
) -> BroadcastStandardsReviewAgent:
    runtime = _load_runtime_config()
    services = services or wire_services(runtime)
    instance = BroadcastStandardsReviewAgent(
        runtime,
        services=services,
        trusted_scope=trusted_scope,
        ingest_session_id=ingest_session_id,
    )
    # An explicit outer instance is required and intentionally differs from the
    # cached inner GraphNode checkpointer.
    instance.compile(checkpointer=InMemorySaver())
    instance.provision_secrets(secrets_factory(namespace="svc", agent_name="BroadcastStandardsReviewAgent"))
    return instance


# Startup wiring is intentionally eager: a missing service seam fails before a
# first customer invocation rather than producing an opaque 500 later.
services = wire_services(_load_runtime_config())
agent = build_agent(services=services)


class InvokeRequest(BaseModel):
    input: str = Field(min_length=1, max_length=200_000)
    session_id: str = ""
    broadcast_date: str | None = None
    program_type: str = ""


class IngestRequest(BaseModel):
    records: list[dict[str, Any]] | None = None
    file_base64: str | None = None
    filename: str = ""
    session_id: str = ""


class FeedbackRequest(BaseModel):
    """Human ruling to add to the scoped, redacted ruling knowledge base."""

    record_id: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9_-]+$")
    criteria_id: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9_-]+$")
    criteria_version: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9._-]+$")
    axis: Literal["broadcast_standards", "pmd_act", "stealth_marketing"]
    ruling: Literal["approved", "revision_instructed", "rejected"]
    ruling_id: str | None = Field(default=None, max_length=120, pattern=r"^[A-Za-z0-9_-]+$")
    amend_existing: bool = False
    revision_note: str = Field(default="", max_length=512)
    rationale: str = Field(default="", max_length=512)
    escalated: bool = False
    session_id: str = Field(default="", max_length=200)


class ResumeCorrection(BaseModel):
    issue_id: str = Field(min_length=1, max_length=120)
    criteria_id: str = Field(min_length=1, max_length=120)
    note: str = Field(default="", max_length=256)


class ResumeRequest(BaseModel):
    thread_id: str = Field(min_length=1, max_length=200)
    # Omission is accepted as a syntactically valid request but cannot match
    # a stored resume scope, so it is rejected with the same 403 as any other
    # session ownership mismatch.
    session_id: str = Field(default="", max_length=200)
    action: str
    corrections: list[ResumeCorrection] = Field(default_factory=list, max_length=50)


def _permissions(request: Request) -> set[str]:
    raw = getattr(request.state, "permissions", ())
    return {str(item) for item in raw} if isinstance(raw, (list, tuple, set, frozenset)) else set()


def _context(
    request: Request, session_id: str, *, operation: Operation
) -> tuple[AuthContext, InvocationContext]:
    """Authenticate once and preserve that exact context through the request."""

    auth = authenticate(request, operation=operation)
    ctx = InvocationContext(
        session_id=session_id or str(uuid4()),
        caller_trust_level=auth.trust,
        caller_id=auth.principal["caller_id"],
    )
    return auth, ctx


def _resume_lock(thread_id: str) -> threading.Lock:
    """Serialize the resumability check and resume for one thread in this process."""

    with _RESUME_LOCKS_GUARD:
        lock = _RESUME_LOCKS.get(thread_id)
        if lock is None:
            lock = threading.Lock()
            _RESUME_LOCKS[thread_id] = lock
        return lock


def _require_resume_ownership_and_pending(thread_id: str, ctx: InvocationContext) -> None:
    """Reject unknown, foreign, completed, or otherwise non-pending checkpoints.

    The process-local lock in ``resume`` makes this check and ``agent.resume``
    atomic for one worker.  A multi-worker deployment must provide a durable
    per-thread lease/CAS at the shared checkpoint layer.
    """

    compiled = getattr(agent, "_compiled", None)
    if compiled is None:
        raise HTTPException(status_code=503, detail="resume checkpoint is unavailable")
    try:
        snapshot = compiled.get_state({"configurable": {"thread_id": thread_id}})
    except ValueError as exc:
        raise HTTPException(status_code=503, detail="resume checkpoint is unavailable") from exc
    values = getattr(snapshot, "values", None) or {}
    if not values:
        raise HTTPException(status_code=404, detail="resume thread was not found")
    if not ctx.session_id or values.get("session_id") != ctx.session_id:
        raise HTTPException(status_code=403, detail="resume scope is invalid")
    owner_caller = values.get("caller_id")
    if not ctx.caller_id or not owner_caller or owner_caller != ctx.caller_id:
        raise HTTPException(status_code=403, detail="resume scope is invalid")
    if not getattr(snapshot, "next", None) or not values.get("subgraph_thread_id"):
        raise HTTPException(status_code=409, detail="thread is not awaiting human input")


def _envelope(*, mode: str, payload_ref: str, broadcaster_id: str, review_desk_id: str, refs: set[str], **extra: Any) -> str:
    hints = services.name_registry.ref_hints(refs)
    payload = {"mode": mode, "payload_ref": payload_ref, "broadcaster_id": broadcaster_id, "review_desk_id": review_desk_id, "ref_hints": hints, **extra}
    return json.dumps(payload, ensure_ascii=False)


def _delivery_gate(
    graph_output: dict[str, Any], *, trust: TrustLevel, broadcaster_id: str, review_desk_id: str, permissions: set[str], allowed_refs: set[str]
) -> dict[str, Any]:
    """Resolve names only in this ephemeral response copy, after checkpoints."""

    allowed = trust in {TrustLevel.VERIFIED_EXTERNAL, TrustLevel.INTERNAL} and "view_sponsor_names" in permissions
    resolved_count = 0
    failed_count = 0

    def visit(value: Any) -> Any:
        nonlocal resolved_count, failed_count
        if isinstance(value, dict):
            return {key: visit(item) for key, item in value.items()}
        if isinstance(value, list):
            return [visit(item) for item in value]
        if isinstance(value, str) and (value.startswith("sponsor_ref_") or value.startswith("product_ref_")):
            if not allowed or value not in allowed_refs:
                return value
            try:
                resolved = services.name_registry.resolve(value, broadcaster_id=broadcaster_id, review_desk_id=review_desk_id)
            except Exception:  # name resolution must preserve the ref response on all failures
                failed_count += 1
                return value
            if resolved is None:
                failed_count += 1
                return value
            resolved_count += 1
            return resolved
        return value

    delivered = visit(copy.deepcopy(graph_output))
    emit_trace_event("name_resolution", {"resolved_ref_count": resolved_count, "name_resolution_failed": failed_count}, {})
    return delivered


def _delete_payload_ref(ref: str, *, broadcaster_id: str, review_desk_id: str, session_id: str) -> None:
    """Remove a request payload even when the graph rejects or raises.

    ``PayloadStore.delete`` validates the complete scope before removal.  Keep
    that contract at the HTTP integration boundary: the store's TTL is only a
    fallback, never the normal retention path for submitted raw text.
    """

    services.payload_store.delete(
        ref,
        scope={"broadcaster_id": broadcaster_id, "review_desk_id": review_desk_id},
        session_id=session_id,
    )


def _feedback_has_sensitive_text(req: FeedbackRequest) -> bool:
    """Reject the complete feedback request before it can enter State or SQLite."""

    return contains_sensitive_submission(json.dumps(req.model_dump(), ensure_ascii=False))


def _reject_unsafe_submission(value: Any, *, allow_commercial_references: bool = False, detail: str = "unsafe request payload") -> None:
    """Apply the shared PII/confidential-data decision before allocating State input."""

    if contains_sensitive_submission(value, allow_commercial_references=allow_commercial_references):
        raise HTTPException(status_code=422, detail=detail)


@app.post("/invoke")
async def invoke(req: InvokeRequest, request: Request):
    auth, ctx = _context(request, req.session_id, operation=Operation.READ_INVOKE)
    trust = auth.trust
    broadcaster_id, review_desk_id = auth.scope["broadcaster_id"], auth.scope["review_desk_id"]
    _reject_unsafe_submission(json.dumps(req.model_dump(), ensure_ascii=False), allow_commercial_references=True)
    refs = services.name_registry.register_from_text(req.input, broadcaster_id=broadcaster_id, review_desk_id=review_desk_id)
    payload_ref = services.payload_store.put(req.input, scope=auth.scope, session_id=ctx.session_id)
    try:
        envelope = _envelope(
            mode="invoke", payload_ref=payload_ref, broadcaster_id=broadcaster_id, review_desk_id=review_desk_id, refs=refs,
            broadcast_date=req.broadcast_date, program_type=req.program_type,
        )
        with bound_secrets(agent._secrets_provider):
            result = agent.invoke(envelope, session_id=ctx.session_id, ctx=ctx)
        if result.get("status") == "awaiting_human" and result.get("thread_id"):
            _resume_scopes[str(result["thread_id"])] = (broadcaster_id, review_desk_id, ctx.caller_id, ctx.session_id, refs)
        return _delivery_gate(result, trust=trust, broadcaster_id=broadcaster_id, review_desk_id=review_desk_id, permissions=_permissions(request), allowed_refs=refs)
    finally:
        _delete_payload_ref(payload_ref, broadcaster_id=broadcaster_id, review_desk_id=review_desk_id, session_id=ctx.session_id)


@app.post("/ingest")
async def ingest(req: IngestRequest, request: Request):
    auth, ctx = _context(request, req.session_id, operation=Operation.WRITE_INGEST)
    trust = auth.trust
    broadcaster_id, review_desk_id = auth.scope["broadcaster_id"], auth.scope["review_desk_id"]
    if req.file_base64 is not None:
        try:
            raw = base64.b64decode(req.file_base64, validate=True)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid file encoding") from exc
        sanitized = sanitize_file_bytes(raw, declared_name=req.filename)
        if not sanitized.accepted:
            raise HTTPException(status_code=400, detail="unsupported or unsafe ingest file")
        records = sanitized.sanitized_rows or ([{"program_type": "document", "content_summary": sanitized.sanitized_text}] if sanitized.sanitized_text else [])
    else:
        records = req.records or []
    raw_payload = json.dumps({"records": records}, ensure_ascii=False, default=str)
    _reject_unsafe_submission(
        json.dumps({"records": records, "session_id": req.session_id}, ensure_ascii=False, default=str),
        allow_commercial_references=True,
    )
    refs = services.name_registry.register_from_text(raw_payload, broadcaster_id=broadcaster_id, review_desk_id=review_desk_id)
    envelope = scrub_checkpoint_envelope(
        json.dumps({"mode": "ingest", "payload": {"records": records}}, ensure_ascii=False, default=str),
        services.payload_store,
        session_id=ctx.session_id,
        trusted_scope=auth.scope,
        policy=CheckpointScrubPolicy(scope_keys=frozenset(SCOPE_KEYS)),
    )
    payload_ref = str(json.loads(envelope)["payload_ref"])
    try:
        request_agent = build_agent(
            services=services,
            trusted_scope=auth.scope,
            ingest_session_id=ctx.session_id,
        )
        with bound_secrets(request_agent._secrets_provider):
            result = request_agent.invoke(envelope, session_id=ctx.session_id, ctx=ctx)
        return _delivery_gate(result, trust=trust, broadcaster_id=broadcaster_id, review_desk_id=review_desk_id, permissions=_permissions(request), allowed_refs=refs)
    finally:
        _delete_payload_ref(payload_ref, broadcaster_id=broadcaster_id, review_desk_id=review_desk_id, session_id=ctx.session_id)


@app.post("/feedback")
async def feedback(req: FeedbackRequest, request: Request):
    """Persist an operator-confirmed ruling through the normal graph boundary."""

    auth, ctx = _context(request, req.session_id, operation=Operation.WRITE_FEEDBACK)
    trust = auth.trust
    broadcaster_id, review_desk_id = auth.scope["broadcaster_id"], auth.scope["review_desk_id"]
    if "submit_ruling" not in _permissions(request):
        raise HTTPException(status_code=403, detail="submit_ruling permission is required")
    if _feedback_has_sensitive_text(req):
        raise HTTPException(status_code=422, detail="unsafe feedback payload")
    if not configured_criterion_matches(
        services.review_criteria, criteria_id=req.criteria_id, criteria_version=req.criteria_version, axis=req.axis
    ):
        raise HTTPException(status_code=422, detail="feedback criterion is not configured")
    # Check ownership before allocating a raw payload, and retain the graph
    # node's identical check as defense in depth for non-HTTP callers.
    if not services.repository.record_belongs_to_scope(req.record_id, broadcaster_id=broadcaster_id, review_desk_id=review_desk_id):
        raise HTTPException(status_code=403, detail="feedback record scope is invalid")
    existing_ruling = bool(
        req.ruling_id and services.repository.ruling_belongs_to_scope(req.ruling_id, broadcaster_id=broadcaster_id, review_desk_id=review_desk_id)
    )
    if existing_ruling and not req.amend_existing:
        raise HTTPException(status_code=409, detail="explicit amendment intent is required for ruling_id")
    if req.amend_existing and not existing_ruling:
        raise HTTPException(status_code=403, detail="feedback ruling scope is invalid")

    feedback_payload = req.model_dump(exclude={"session_id"})
    # Each operator submission is a separate ruling by default.  A caller can
    # supply an existing opaque ID only when it intentionally amends that
    # ruling; silently reusing a record-level ID would overwrite KB history.
    if not feedback_payload.get("ruling_id"):
        # Digit-free suffix on purpose: a `uuid4().hex` suffix contains digit
        # runs that `_PAYMENT_CARD_CANDIDATE` / `_MY_NUMBER` match for ~3.4% of
        # generated IDs.  The sanitiser would then rewrite part of the ID to
        # "[個人情報非開示]" and the S-3 provenance gate would reject the
        # resulting unsafe source id, failing the whole request.  This ID is
        # minted here and never carries PII, so it must not be shaped like it.
        feedback_payload["ruling_id"] = f"ruling_feedback_{mint_reference()}"
    raw_payload = json.dumps(feedback_payload, ensure_ascii=False)
    payload_ref = services.payload_store.put(raw_payload, scope=auth.scope, session_id=ctx.session_id)
    try:
        envelope = _envelope(mode="feedback", payload_ref=payload_ref, broadcaster_id=broadcaster_id, review_desk_id=review_desk_id, refs=set())
        with bound_secrets(agent._secrets_provider):
            result = agent.invoke(envelope, session_id=ctx.session_id, ctx=ctx)
        return _delivery_gate(result, trust=trust, broadcaster_id=broadcaster_id, review_desk_id=review_desk_id, permissions=_permissions(request), allowed_refs=set())
    finally:
        _delete_payload_ref(payload_ref, broadcaster_id=broadcaster_id, review_desk_id=review_desk_id, session_id=ctx.session_id)


@app.post("/resume")
def resume(req: ResumeRequest, request: Request):
    auth, ctx = _context(request, req.session_id, operation=Operation.RESUME)
    trust = auth.trust
    broadcaster_id, review_desk_id = auth.scope["broadcaster_id"], auth.scope["review_desk_id"]
    if req.action not in {"approve", "correct", "reject"}:
        raise HTTPException(status_code=422, detail="unsupported review action")
    if contains_sensitive_submission(json.dumps(req.model_dump(), ensure_ascii=False)):
        raise HTTPException(status_code=422, detail="unsafe correction payload")
    feedback = {"action": req.action, "corrections": [item.model_dump() for item in req.corrections]}
    with _resume_lock(req.thread_id):
        _require_resume_ownership_and_pending(req.thread_id, ctx)
        known = _resume_scopes.get(req.thread_id)
        if known is None or known[:4] != (broadcaster_id, review_desk_id, ctx.caller_id, ctx.session_id):
            raise HTTPException(status_code=403, detail="resume scope is invalid")
        with bound_secrets(agent._secrets_provider):
            result = agent.resume(req.thread_id, feedback)
        if result.get("status") != "awaiting_human":
            _resume_scopes.pop(req.thread_id, None)
    return _delivery_gate(result, trust=trust, broadcaster_id=broadcaster_id, review_desk_id=review_desk_id, permissions=_permissions(request), allowed_refs=known[4])


@app.get("/health")
def health():
    return {"status": "ok", "agent": "SVC-C2-085"}
