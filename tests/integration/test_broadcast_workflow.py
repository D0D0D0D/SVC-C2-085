"""Integration coverage for the outer graph, real SQL, HITL resume, and delivery gate."""

from __future__ import annotations

import json
import threading

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel

from src.api import server
from src.entry_adapter import AuthContext, AuthSource
from src.graph.graph import BroadcastStandardsReviewAgent
from src.graph.domain_workflow_graph import HitlRefineNode, IngestNode
from src.scoped_retrieval import ScopedRetrievalNode
from src.services.service import AxisBoundRetrievalBackend, wire_services


def _agent(*, hitl: bool):
    config = {"memory_enabled": True, "max_retry": 3, "hitl": {"enabled": hitl, "max_hitl": 8}}
    services = wire_services(config)
    agent = BroadcastStandardsReviewAgent(config, services=services)
    agent.compile(checkpointer=InMemorySaver())
    return agent, services


def _invoke(agent, services, *, text: str, hitl_allowed: bool, thread_id: str = "thread"):
    ctx = InvocationContext(session_id="session", thread_id=thread_id, caller_trust_level=TrustLevel.VERIFIED_EXTERNAL, hitl_allowed=hitl_allowed)
    ref = services.payload_store.put(text, scope={"broadcaster_id": "tenant", "review_desk_id": "org"}, session_id="session")
    return agent.invoke(json.dumps({"mode": "invoke", "payload_ref": ref, "broadcaster_id": "tenant", "review_desk_id": "org"}), ctx=ctx)


def test_bl01_bl04_cold_start_produces_review_issues_and_envelope() -> None:
    agent, services = _agent(hitl=False)
    result = _invoke(agent, services, text="スポンサー: Example Sponsor\n健康表現", hitl_allowed=False)
    assert result["status"] == "success"
    assert result["output"]["review_issues"]
    assert result["output"]["evidence_map"]
    assert result["output"]["degradation_reason"] == "LLM not configured — deterministic fallback returned."
    assert "Example Sponsor" not in str(result)


def test_pb3_real_sql_retrieval_is_scope_and_axis_bound() -> None:
    agent, services = _agent(hitl=False)
    services.repository.write_ingest_record({"record_id": "record1", "broadcaster_id": "tenant", "review_desk_id": "org", "program_type": "drama", "content_summary": "健康表現"})
    services.repository.write_ruling({"ruling_id": "ruling1", "record_id": "record1", "broadcaster_id": "tenant", "review_desk_id": "org", "criteria_id": "PMD-ACT-AD-STANDARD", "criteria_version": "2026.1", "axis": "pmd_act"})
    result = _invoke(agent, services, text="健康表現", hitl_allowed=False)
    retrieved = result["output"]["retrieved_records"]
    assert any(record["ruling_id"] == "ruling1" for record in retrieved)
    assert all(record["axis"] == "pmd_act" for record in retrieved)


def test_r1_r5_real_sql_returns_stored_scope_and_rejects_an_adapter_that_drops_it() -> None:
    services = wire_services()
    services.repository.write_ingest_record(
        {
            "record_id": "record-a",
            "broadcaster_id": "tenant-a",
            "review_desk_id": "org-a",
            "program_type": "drama",
            "content_summary": "health review",
        }
    )
    services.repository.write_ruling(
        {
            "ruling_id": "ruling-a",
            "record_id": "record-a",
            "broadcaster_id": "tenant-a",
            "review_desk_id": "org-a",
            "criteria_id": "PMD-ACT-AD-STANDARD",
            "criteria_version": "2026.1",
            "axis": "pmd_act",
        }
    )
    backend = AxisBoundRetrievalBackend(services.repository, "pmd_act")
    node = ScopedRetrievalNode(
        scope_keys=("broadcaster_id", "review_desk_id"),
        backend=backend,
        normalize_query=lambda value: str(value),
    )
    state = {"broadcaster_id": "tenant-a", "review_desk_id": "org-a", "validated_input": "health"}
    accepted = node.execute(state)
    assert accepted["status"] == "success"
    assert accepted["retrieved_records"][0]["metadata"]["broadcaster_id"] == "tenant-a"
    assert accepted["retrieved_records"][0]["metadata"]["review_desk_id"] == "org-a"

    class ScopeDroppingAdapter:
        def search(self, *, scope, query, top_k):
            hits = backend.search(scope=scope, query=query, top_k=top_k)
            for hit in hits:
                hit.pop("broadcaster_id", None)
                hit.pop("review_desk_id", None)
                hit["metadata"].pop("broadcaster_id", None)
                hit["metadata"].pop("review_desk_id", None)
            return hits

    rejected = ScopedRetrievalNode(
        scope_keys=("broadcaster_id", "review_desk_id"),
        backend=ScopeDroppingAdapter(),
        normalize_query=lambda value: str(value),
    ).execute(state)
    assert rejected["status"] == "success"
    assert rejected["retrieved_records"] == []
    assert rejected["degradation_reason"] == "no scoped retrieval hits"


def test_i8_state_scope_cannot_replace_missing_authenticated_constructor_context() -> None:
    services = wire_services()
    ref = services.payload_store.put(
        json.dumps({"records": [{"program_type": "show", "content_summary": "review"}]}),
        scope={"broadcaster_id": "trusted", "review_desk_id": "trusted-org"},
        session_id="trusted-session",
    )
    with pytest.raises(ValueError, match="trusted_scope must be passed explicitly"):
        IngestNode(services=services, trusted_scope=None, session_id=None).execute(
            {
                "payload_ref": ref,
                "broadcaster_id": "trusted",
                "review_desk_id": "trusted-org",
                "session_id": "trusted-session",
            }
        )


def test_pb7_http_resume_reaches_success_and_replay_is_rejected(monkeypatch) -> None:
    agent, services = _agent(hitl=True)
    auth = AuthContext(
        trust=TrustLevel.INTERNAL,
        scope={"broadcaster_id": "tenant", "review_desk_id": "org"},
        principal={"caller_id": "operator"},
        auth_source=AuthSource.MIDDLEWARE,
    )

    def fixed_context(_request, session_id, *, operation):
        return auth, InvocationContext(
            session_id=session_id,
            thread_id="resumethread",
            caller_trust_level=TrustLevel.INTERNAL,
            caller_id="operator",
            hitl_allowed=True,
        )

    monkeypatch.setattr(server, "services", services)
    monkeypatch.setattr(server, "agent", agent)
    monkeypatch.setattr(server, "_context", fixed_context)
    server._resume_scopes.clear()

    with TestClient(server.app, raise_server_exceptions=False) as client:
        first = client.post("/invoke", json={"input": "健康表現", "session_id": "sessionalpha"})
        pending = agent._compiled.get_state({"configurable": {"thread_id": "resumethread"}})
        resumed = client.post(
            "/resume",
            json={"thread_id": "resumethread", "session_id": "sessionalpha", "action": "approve"},
        )
        completed = agent._compiled.get_state({"configurable": {"thread_id": "resumethread"}})
        replayed = client.post(
            "/resume",
            json={"thread_id": "resumethread", "session_id": "sessionalpha", "action": "approve"},
        )

    assert first.status_code == 200
    assert first.json()["status"] == "awaiting_human"
    assert pending.next == ("main",)
    assert resumed.status_code == 200
    assert resumed.json()["status"] == "success"
    assert resumed.json()["output"]["review_issues"]
    assert completed.next == ()
    assert "resumethread" not in server._resume_scopes
    assert replayed.status_code == 409
    assert replayed.json() == {"detail": "thread is not awaiting human input"}


def test_pb7_concurrent_http_resume_applies_feedback_once(monkeypatch) -> None:
    agent, services = _agent(hitl=True)
    auth = AuthContext(
        trust=TrustLevel.INTERNAL,
        scope={"broadcaster_id": "tenant", "review_desk_id": "org"},
        principal={"caller_id": "operator"},
        auth_source=AuthSource.MIDDLEWARE,
    )

    def fixed_context(_request, session_id, *, operation):
        return auth, InvocationContext(
            session_id=session_id,
            thread_id="concurrent-resume-thread",
            caller_trust_level=TrustLevel.INTERNAL,
            caller_id="operator",
            hitl_allowed=True,
        )

    original_pending_check = server._require_resume_ownership_and_pending
    pending_checks = 0
    pending_checks_guard = threading.Lock()
    both_pending_checks_reached = threading.Event()

    def coordinate_after_pending_check(thread_id, ctx):
        nonlocal pending_checks
        original_pending_check(thread_id, ctx)
        with pending_checks_guard:
            pending_checks += 1
            check_number = pending_checks
            if pending_checks == 2:
                both_pending_checks_reached.set()
        if check_number == 1:
            both_pending_checks_reached.wait(timeout=0.5)

    feedback_applications = 0
    feedback_applications_guard = threading.Lock()
    original_apply_corrections = HitlRefineNode._apply_corrections

    def counted_apply_corrections(issues, corrections):
        nonlocal feedback_applications
        with feedback_applications_guard:
            feedback_applications += 1
        return original_apply_corrections(issues, corrections)

    monkeypatch.setattr(server, "services", services)
    monkeypatch.setattr(server, "agent", agent)
    monkeypatch.setattr(server, "_context", fixed_context)
    monkeypatch.setattr(server, "_require_resume_ownership_and_pending", coordinate_after_pending_check)
    monkeypatch.setattr(HitlRefineNode, "_apply_corrections", staticmethod(counted_apply_corrections))
    server._resume_scopes.clear()

    with TestClient(server.app, raise_server_exceptions=False) as client:
        first = client.post("/invoke", json={"input": "健康表現", "session_id": "concurrent-session"})
        pending = agent._compiled.get_state({"configurable": {"thread_id": "concurrent-resume-thread"}})
        inner_pending = agent._nodes["main"]._subgraph._compiled.get_state(
            {"configurable": {"thread_id": pending.values["subgraph_thread_id"]}}
        )
        issue = inner_pending.values["review_issues"][0]
        correction = {
            "issue_id": issue["issue_id"],
            "criteria_id": issue["criteria_id"],
            "note": "並行フィードバックは一度だけ適用されます。",
        }
        resume_payload = {
            "thread_id": "concurrent-resume-thread",
            "session_id": "concurrent-session",
            "action": "correct",
            "corrections": [correction],
        }
        start = threading.Barrier(2)
        responses = []

        def post_resume() -> None:
            start.wait()
            responses.append(client.post("/resume", json=resume_payload))

        threads = [threading.Thread(target=post_resume) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

    assert first.status_code == 200
    assert first.json()["status"] == "awaiting_human"
    assert all(not thread.is_alive() for thread in threads)
    assert sorted(response.status_code for response in responses) == [200, 409]
    succeeded = next(response for response in responses if response.status_code == 200)
    corrected = next(
        item for item in succeeded.json()["output"]["review_issues"] if item["issue_id"] == issue["issue_id"]
    )
    assert corrected["issue_text"] == correction["note"]
    assert feedback_applications == 1


def test_sec20_ingest_keeps_structured_sponsor_name_out_of_real_sql() -> None:
    _unused_agent, services = _agent(hitl=False)
    ctx = InvocationContext(session_id="ingest-session", thread_id="ingest-thread", caller_trust_level=TrustLevel.VERIFIED_EXTERNAL, hitl_allowed=False)
    agent = BroadcastStandardsReviewAgent(
        {"memory_enabled": True, "max_retry": 3, "hitl": {"enabled": False, "max_hitl": 8}},
        services=services,
        trusted_scope={"broadcaster_id": "tenant", "review_desk_id": "org"},
        ingest_session_id=ctx.session_id,
    )
    agent.compile(checkpointer=InMemorySaver())
    raw = json.dumps(
        {"records": [{"record_id": "record-ingest", "program_type": "drama", "sponsor_name": "Secret Sponsor", "content_summary": "開示表現"}]},
        ensure_ascii=False,
    )
    ref = services.payload_store.put(raw, scope={"broadcaster_id": "tenant", "review_desk_id": "org"}, session_id=ctx.session_id)
    result = agent.invoke(json.dumps({"mode": "ingest", "payload_ref": ref, "broadcaster_id": "tenant", "review_desk_id": "org"}), ctx=ctx)
    stored = services.repository._connection.execute("SELECT sponsor_ref, content_summary FROM broadcast_review_records").fetchone()
    assert result["status"] == "success"
    assert "Secret Sponsor" not in str(tuple(stored))
    assert stored["sponsor_ref"].startswith("sponsor_ref_")
