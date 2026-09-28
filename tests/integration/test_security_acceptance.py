"""Focused acceptance evidence for the security contracts (SEC-05b..30)."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from langgraph.checkpoint.memory import InMemorySaver

from src.api import server
from src.entry_adapter import AuthContext, AuthSource
from src.entry_adapter import Operation
from src.graph.domain_workflow_graph import (
    HitlRefineNode,
    IngestNode,
    Stage3DraftNode,
    Stage4DisclosureNode,
)
from src.graph.graph import BroadcastStandardsReviewAgent
from src.nodes.pre_process_node import PreProcessNode
from src.nodes.finalize_node import SafeFinalizeNode
from src.payload_store import PayloadStore, PayloadStoreError
from src.services.service import NameRegistry, wire_services


def _issue(**overrides):
    value = {
        "issue_id": "issue-1", "input_span_id": "span_1", "criteria_id": "criterion-1",
        "criteria_version": "1", "axis": "pmd_act", "requires_human_review": False,
        "policy_source": "baseline", "tenant_policy_id": None,
    }
    value.update(overrides)
    return value


def _stage4_state(issue):
    return {"review_issues": [issue], "input_span_ids": ["span_1"], "criteria_records": [
        {"criteria_id": "criterion-1", "criteria_version": "1", "axis": "pmd_act"}
    ]}


def _checkpoint_states(*savers):
    """Return persisted state values from every outer/inner checkpoint saver."""

    config = {"configurable": {"thread_id": "checkpoint-thread"}}
    states = []
    for saver in savers:
        assert saver is not None, "the HITL subgraph must have its own checkpointer"
        states.extend(item.checkpoint.get("channel_values", {}) for item in saver.list(config))
    return states


def _assert_no_sensitive_hitl_value(states, markers):
    """SEC-10 deliberately examines only the persisted HITL fields."""

    hitl_values = [
        state[field]
        for state in states
        for field in ("hitl_draft", "hitl_feedback")
        if state.get(field) is not None
    ]
    assert hitl_values, "HITL checkpoint fields were not persisted"
    serialized = "\n".join(json.dumps(value, ensure_ascii=False, default=str) for value in hitl_values)
    assert all(marker not in serialized for marker in markers)


@pytest.mark.parametrize(
    "field,value",
    [("axis", None), ("axis", "other"), ("requires_human_review", None), ("requires_human_review", "false")],
    ids=["missing-axis", "invalid-axis", "missing-review-flag", "non-bool-review-flag"],
)
def test_sec05b_stage4_contract_rejects_missing_or_invalid_fields(field, value):
    issue = _issue()
    if value is None:
        issue.pop(field)
    else:
        issue[field] = value
    result = Stage4DisclosureNode().execute(_stage4_state(issue))
    assert result == {"status": AgentStatus.ERROR.value, "error_log": ["stage4_issue_contract_invalid"]}


def test_sec06_baseline_policy_without_tenant_override_is_valid():
    result = Stage4DisclosureNode().execute(_stage4_state(_issue(policy_source="baseline", tenant_policy_id=None)))
    assert result["status"] == "success"
    assert result["disclosure_map"] == {"issue-1": {"axis": "pmd_act", "disclosure_review_required": True}}


def test_sec11_hitl_disallowed_continues_without_interrupt(monkeypatch):
    monkeypatch.setattr("src.graph.domain_workflow_graph.interrupt", lambda _payload: pytest.fail("must not interrupt"))
    result = HitlRefineNode(enabled=True).execute({"status": "success", "hitl_allowed": False})
    assert result == {"status": "success"}


@pytest.mark.parametrize("action, expected", [("approve", "approved"), ("correct", "corrected"), ("reject", "rejected")])
def test_sec12_all_hitl_feedback_actions_are_resumed(monkeypatch, action, expected):
    monkeypatch.setattr("src.graph.domain_workflow_graph.interrupt", lambda _payload: {"action": action, "corrections": []})
    result = HitlRefineNode(enabled=True).execute({"status": "success", "hitl_allowed": True, "review_issues": []})
    assert result["status"] == "success"
    assert result["hitl_feedback"]["action"] == action
    assert result["hitl_status"].value == expected


@pytest.mark.parametrize("raw", [b"%PDF-1.7", b"RIFF....WAVE", b"\x1aE\xdf\xa3binary"])
def test_sec13_non_text_magic_bytes_are_rejected(raw):
    services = wire_services()
    ref = services.payload_store.put(raw, scope={"broadcaster_id": "t", "review_desk_id": "o"}, session_id="s")
    result = PreProcessNode(services=services).execute({
        "session_id": "s", "user_input": json.dumps({"mode": "invoke", "payload_ref": ref, "broadcaster_id": "t", "review_desk_id": "o"}),
    })
    assert result["status"] == "error"
    assert result["error_log"] == ["non_text_input_rejected"]


def test_sec14_error_envelope_does_not_disclose_inner_review_state():
    result = SafeFinalizeNode().on_finalize({
        "status": "error", "correlation_id": "c-1", "review_issues": [{"issue_text": "secret issue"}],
        "retrieved_records": [{"summary": "secret ruling"}], "validated_input": "secret script",
    })
    assert result["formatted_output"] == {
        "status": "error", "error_code": "review_processing_failed", "correlation_id": "c-1",
    }


def _delivery_input(ref="sponsor_ref_demo"):
    return {"status": "success", "output": {"review_issues": [{"sponsor": ref}]}}


def _wire_http_agent(monkeypatch, *, broadcaster_id="tenant", review_desk_id="org", hitl=False):
    """Use a real graph behind the HTTP adapter with deterministic services."""

    services = wire_services()
    http_agent = BroadcastStandardsReviewAgent(
        {"memory_enabled": True, "max_retry": 3, "hitl": {"enabled": hitl, "max_hitl": 8}}, services=services
    )
    http_agent.compile(checkpointer=InMemorySaver())
    monkeypatch.setattr(server, "services", services)
    monkeypatch.setattr(server, "agent", http_agent)
    monkeypatch.setattr(
        server,
        "authenticate",
        lambda _request, *, operation: AuthContext(
            trust=TrustLevel.INTERNAL,
            scope={"broadcaster_id": broadcaster_id, "review_desk_id": review_desk_id},
            principal={"caller_id": "operator"},
            auth_source=AuthSource.MIDDLEWARE,
        ),
    )
    return services, http_agent


def _wire_pending_http_agent(monkeypatch, *, thread_id, session_id, caller_id):
    """Create a real suspended checkpoint for resume boundary tests."""

    services = wire_services()
    http_agent = BroadcastStandardsReviewAgent(
        {"memory_enabled": True, "max_retry": 3, "hitl": {"enabled": True, "max_hitl": 8}}, services=services
    )
    http_agent.compile(checkpointer=InMemorySaver())
    context = InvocationContext(
        session_id=session_id,
        thread_id=thread_id,
        caller_trust_level=TrustLevel.INTERNAL,
        caller_id=caller_id,
        hitl_allowed=True,
    )
    ref = services.payload_store.put(
        "健康表現",
        scope={"broadcaster_id": "tenant", "review_desk_id": "org"},
        session_id=session_id,
    )
    first = http_agent.invoke(
        json.dumps(
            {
                "mode": "invoke",
                "payload_ref": ref,
                "broadcaster_id": "tenant",
                "review_desk_id": "org",
            }
        ),
        ctx=context,
    )
    assert first["status"] == "awaiting_human"
    monkeypatch.setattr(server, "services", services)
    monkeypatch.setattr(server, "agent", http_agent)
    server._resume_scopes.clear()
    server._resume_scopes[thread_id] = ("tenant", "org", caller_id, session_id, set())
    return http_agent


def _http_request(caller_id="operator", permissions=()):
    return SimpleNamespace(state=SimpleNamespace(caller_id=caller_id, permissions=list(permissions)))


def _capture_payload_put(monkeypatch, services):
    refs = []
    original_put = services.payload_store.put

    def capture(value, *, scope, session_id):
        ref = original_put(value, scope=scope, session_id=session_id)
        refs.append(ref)
        return ref

    monkeypatch.setattr(services.payload_store, "put", capture)
    return refs


def test_sec15_17_18_22_name_resolution_is_scope_and_reference_bound(monkeypatch):
    registry = NameRegistry()
    allowed = registry.register_from_text("スポンサー: Allowed Name\n未公開企画名: Secret Project", broadcaster_id="t", review_desk_id="o")
    allowed_ref = next(iter(allowed))
    foreign_ref = registry.register_from_text("スポンサー: Foreign Name", broadcaster_id="other", review_desk_id="o").pop()
    monkeypatch.setattr(server.services, "name_registry", registry)
    monkeypatch.setattr(server, "emit_trace_event", lambda *_args: None)
    output = {"allowed": allowed_ref, "foreign": foreign_ref, "unknown": "sponsor_ref_not_registered", "project": "Secret Project"}
    delivered = server._delivery_gate(output, trust=TrustLevel.INTERNAL, broadcaster_id="t", review_desk_id="o", permissions={"view_sponsor_names"}, allowed_refs=allowed)
    assert delivered["allowed"] == "Allowed Name"
    assert delivered["foreign"] == foreign_ref
    assert delivered["unknown"] == "sponsor_ref_not_registered"
    assert delivered["project"] == "Secret Project"  # only ref tokens can ever be resolved
    assert output["allowed"] == allowed_ref  # response-only copy, never state mutation


def test_sec19_23_24_25_delivery_gate_never_persists_or_leaks_names(monkeypatch):
    registry = NameRegistry()
    ref = registry.register_from_text("スポンサー: Resolved Name", broadcaster_id="t", review_desk_id="o").pop()
    events = []
    monkeypatch.setattr(server.services, "name_registry", registry)
    monkeypatch.setattr(server, "emit_trace_event", lambda event, payload, _state: events.append((event, payload)))
    denied = server._delivery_gate(_delivery_input(ref), trust=TrustLevel.INTERNAL, broadcaster_id="t", review_desk_id="o", permissions=set(), allowed_refs={ref})
    assert str(denied) == str(_delivery_input(ref))
    delivered = server._delivery_gate(_delivery_input(ref), trust=TrustLevel.INTERNAL, broadcaster_id="t", review_desk_id="o", permissions={"view_sponsor_names"}, allowed_refs={ref})
    assert "Resolved Name" in str(delivered)
    assert all("Resolved Name" not in str(payload) for _event, payload in events)
    assert events[-1][1] == {"resolved_ref_count": 1, "name_resolution_failed": 0}

    def unavailable(*_args, **_kwargs):
        raise TimeoutError("resolver unavailable")

    monkeypatch.setattr(registry, "resolve", unavailable)
    failed = server._delivery_gate(_delivery_input(ref), trust=TrustLevel.INTERNAL, broadcaster_id="t", review_desk_id="o", permissions={"view_sponsor_names"}, allowed_refs={ref})
    assert failed["output"]["review_issues"][0]["sponsor"] == ref
    assert "Resolved Name" not in str(failed)
    assert events[-1][1] == {"resolved_ref_count": 0, "name_resolution_failed": 1}


def test_sec20_ingest_persists_only_refs_not_registered_names():
    services = wire_services()
    raw = json.dumps({"records": [{"record_id": "r1", "program_type": "show", "sponsor": "Actual Sponsor", "content_summary": "商品名: Actual Product", "rulings": []}]}, ensure_ascii=False)
    refs = services.name_registry.register_from_text(raw, broadcaster_id="t", review_desk_id="o")
    sanitized = PreProcessNode(services=services).execute({
        "session_id": "s", "user_input": json.dumps({"mode": "ingest", "payload_ref": services.payload_store.put(raw, scope={"broadcaster_id": "t", "review_desk_id": "o"}, session_id="s"), "broadcaster_id": "t", "review_desk_id": "o", "ref_hints": services.name_registry.ref_hints(refs)}),
    })
    result = IngestNode(
        services=services,
        trusted_scope={"broadcaster_id": "t", "review_desk_id": "o"},
        session_id="s",
    ).execute(sanitized)
    dump = " ".join(" ".join(map(str, row)) for row in services.repository._connection.execute("SELECT * FROM broadcast_review_records").fetchall())
    assert result["status"] == "success"
    assert "Actual Sponsor" not in dump and "Actual Product" not in dump
    assert "_ref_" in dump


def test_sec21_llm_prompt_contains_refs_but_not_real_names():
    captured = []

    class Llm:
        def complete(self, messages):
            captured.extend(messages)
            return {"content": json.dumps({"review_issues": []})}

    Stage3DraftNode(config={"llm": Llm()}).execute({"status": "success", "validated_input": "スポンサー: sponsor_ref_x", "criteria_records": [], "retrieved_records": [], "input_span_ids": ["span_1"]})
    prompt = captured[0]["content"]
    assert "sponsor_ref_x" in prompt
    assert "Actual Sponsor" not in prompt


def test_sec27_payload_scope_and_replay_are_rejected():
    services = wire_services()
    ref = services.payload_store.put("raw", scope={"broadcaster_id": "t", "review_desk_id": "o"}, session_id="s")
    with pytest.raises(Exception, match="scope_mismatch"):
        services.payload_store.resolve(ref, scope={"broadcaster_id": "other", "review_desk_id": "o"}, session_id="s", consume=True)
    assert services.payload_store.resolve(ref, scope={"broadcaster_id": "t", "review_desk_id": "o"}, session_id="s", consume=True) == "raw"
    with pytest.raises(Exception, match="replayed"):
        services.payload_store.resolve(ref, scope={"broadcaster_id": "t", "review_desk_id": "o"}, session_id="s", consume=True)


def test_sec26_request_payload_is_deleted_after_normal_completion(monkeypatch):
    services, _http_agent = _wire_http_agent(monkeypatch)
    refs = _capture_payload_put(monkeypatch, services)

    result = asyncio.run(server.invoke(server.InvokeRequest(input="健康表現", session_id="request-session"), _http_request()))

    assert result["status"] == "success"
    assert len(refs) == 1
    with pytest.raises(PayloadStoreError, match="^payload_ref_unknown$"):
        services.payload_store.resolve(refs[0], scope={"broadcaster_id": "tenant", "review_desk_id": "org"}, session_id="request-session", consume=True)


def test_invoke_rejects_a_credit_card_before_payload_or_outer_checkpoint(monkeypatch):
    _services, http_agent = _wire_http_agent(monkeypatch)
    invoked = []
    monkeypatch.setattr(http_agent, "invoke", lambda *_args, **_kwargs: invoked.append(True))

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            server.invoke(
                server.InvokeRequest(input="スポンサー開示の連絡先: 4111 1111 1111 1111", session_id="card-session"),
                _http_request(),
            )
        )

    assert exc.value.status_code == 422
    assert invoked == []


def test_sec26_request_payload_is_deleted_after_s2_rejection(monkeypatch):
    services, http_agent = _wire_http_agent(monkeypatch)
    refs = _capture_payload_put(monkeypatch, services)
    pre_process = http_agent._nodes["pre_process"]
    monkeypatch.setattr(
        pre_process,
        "_extra_security_gate_input",
        lambda state: {**state, "status": AgentStatus.ERROR.value, "error_log": ["test_s2_rejected"]},
    )

    result = asyncio.run(server.invoke(server.InvokeRequest(input="健康表現", session_id="s2-session"), _http_request()))

    assert result["status"] == "error"
    with pytest.raises(PayloadStoreError, match="^payload_ref_unknown$"):
        services.payload_store.resolve(refs[0], scope={"broadcaster_id": "tenant", "review_desk_id": "org"}, session_id="s2-session", consume=True)


def test_sec26_request_payload_is_deleted_when_graph_raises(monkeypatch):
    services, http_agent = _wire_http_agent(monkeypatch)
    refs = _capture_payload_put(monkeypatch, services)
    monkeypatch.setattr(http_agent, "invoke", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("test graph failure")))

    with pytest.raises(RuntimeError, match="test graph failure"):
        asyncio.run(server.invoke(server.InvokeRequest(input="健康表現", session_id="error-session"), _http_request()))
    with pytest.raises(PayloadStoreError, match="^payload_ref_unknown$"):
        services.payload_store.resolve(refs[0], scope={"broadcaster_id": "tenant", "review_desk_id": "org"}, session_id="error-session", consume=True)


def test_ingest_payload_is_deleted_when_request_scoped_agent_build_fails(monkeypatch):
    services, _http_agent = _wire_http_agent(monkeypatch)
    refs = _capture_payload_put(monkeypatch, services)
    monkeypatch.setattr(server, "build_agent", lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("build failed")))

    with pytest.raises(RuntimeError, match="build failed"):
        asyncio.run(
            server.ingest(
                server.IngestRequest(
                    records=[{"record_id": "r", "program_type": "show", "content_summary": "review"}],
                    session_id="ingest-build-session",
                ),
                _http_request(),
            )
        )
    assert len(refs) == 1
    with pytest.raises(PayloadStoreError, match="^payload_ref_unknown$"):
        services.payload_store.resolve(
            refs[0],
            scope={"broadcaster_id": "tenant", "review_desk_id": "org"},
            session_id="ingest-build-session",
            consume=True,
        )


def test_feedback_persists_operator_ruling_and_stage2_recalls_it(monkeypatch):
    services, _http_agent = _wire_http_agent(monkeypatch)
    services.repository.write_ingest_record(
        {
            "record_id": "record-health", "broadcaster_id": "tenant", "review_desk_id": "org", "program_type": "drama",
            "content_summary": "健康表現のスポンサー開示",
        }
    )
    request = _http_request(permissions={"submit_ruling"})

    feedback_result = asyncio.run(
        server.feedback(
            server.FeedbackRequest(
                record_id="record-health", criteria_id="PMD-ACT-AD-STANDARD", criteria_version="2026.1",
                axis="pmd_act", ruling="revision_instructed", revision_note="根拠表示を補足して再確認します。",
                rationale="過去の考査記録に基づく修正指示です。", session_id="feedback-session",
            ),
            request,
        )
    )

    assert feedback_result["status"] == "success"
    stored = services.repository._connection.execute(
        "SELECT ruling, axis, operator_edited FROM ruling_records WHERE broadcaster_id=? AND review_desk_id=?",
        ("tenant", "org"),
    ).fetchone()
    assert tuple(stored) == ("revision_instructed", "pmd_act", 1)

    invoke_result = asyncio.run(
        server.invoke(server.InvokeRequest(input="健康表現のスポンサー開示", session_id="invoke-session"), request)
    )
    recalled = invoke_result["output"]["retrieved_records"]
    assert any(
        record["record_id"] == "record-health"
        and record["criteria_id"] == "PMD-ACT-AD-STANDARD"
        and record["axis"] == "pmd_act"
        for record in recalled
    )


def test_feedback_rejects_record_from_another_scope_at_http_boundary(monkeypatch):
    services, _http_agent = _wire_http_agent(monkeypatch)
    services.repository.write_ingest_record(
        {
            "record_id": "foreign-record", "broadcaster_id": "other-tenant", "review_desk_id": "other-org",
            "program_type": "drama", "content_summary": "健康表現",
        }
    )

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            server.feedback(
                server.FeedbackRequest(
                    record_id="foreign-record", criteria_id="PMD-ACT-AD-STANDARD", criteria_version="2026.1",
                    axis="pmd_act", ruling="approved", session_id="feedback-session",
                ),
                _http_request(),
            )
        )
    assert exc.value.status_code == 403


def test_feedback_requires_submit_ruling_permission_before_writing(monkeypatch):
    services, _http_agent = _wire_http_agent(monkeypatch)
    services.repository.write_ingest_record(
        {"record_id": "record-auth", "broadcaster_id": "tenant", "review_desk_id": "org", "program_type": "drama", "content_summary": "健康表現"}
    )

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            server.feedback(
                server.FeedbackRequest(
                    record_id="record-auth", criteria_id="PMD-ACT-AD-STANDARD", criteria_version="2026.1",
                    axis="pmd_act", ruling="approved", session_id="feedback-session",
                ),
                _http_request(),
            )
        )

    assert exc.value.status_code == 403
    assert services.repository._connection.execute("SELECT count(*) FROM ruling_records").fetchone()[0] == 0


def test_feedback_rejects_unconfigured_or_axis_mismatched_criterion(monkeypatch):
    services, _http_agent = _wire_http_agent(monkeypatch)
    services.repository.write_ingest_record(
        {"record_id": "record-criterion", "broadcaster_id": "tenant", "review_desk_id": "org", "program_type": "drama", "content_summary": "健康表現"}
    )

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            server.feedback(
                server.FeedbackRequest(
                    record_id="record-criterion", criteria_id="PMD-ACT-AD-STANDARD", criteria_version="2026.1",
                    axis="broadcast_standards", ruling="approved", session_id="feedback-session",
                ),
                _http_request(permissions={"submit_ruling"}),
            )
        )

    assert exc.value.status_code == 422
    assert services.repository._connection.execute("SELECT count(*) FROM ruling_records").fetchone()[0] == 0


def test_feedback_rejects_existing_ruling_id_without_explicit_amendment(monkeypatch):
    services, _http_agent = _wire_http_agent(monkeypatch)
    services.repository.write_ingest_record(
        {"record_id": "record-ruling", "broadcaster_id": "tenant", "review_desk_id": "org", "program_type": "drama", "content_summary": "健康表現"}
    )
    services.repository.write_ruling(
        {
            "ruling_id": "existing-ruling", "record_id": "record-ruling", "broadcaster_id": "tenant", "review_desk_id": "org",
            "criteria_id": "PMD-ACT-AD-STANDARD", "criteria_version": "2026.1", "axis": "pmd_act", "ruling": "approved",
        }
    )

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            server.feedback(
                server.FeedbackRequest(
                    record_id="record-ruling", criteria_id="PMD-ACT-AD-STANDARD", criteria_version="2026.1",
                    axis="pmd_act", ruling="rejected", ruling_id="existing-ruling", session_id="feedback-session",
                ),
                _http_request(permissions={"submit_ruling"}),
            )
        )

    assert exc.value.status_code == 409
    stored = services.repository._connection.execute("SELECT ruling FROM ruling_records WHERE ruling_id='existing-ruling'").fetchone()
    assert stored[0] == "approved"


def test_sec28_retry_reuses_validated_input_without_second_payload_read(monkeypatch):
    services = wire_services()
    ref = services.payload_store.put("スポンサー: Actual Sponsor", scope={"broadcaster_id": "t", "review_desk_id": "o"}, session_id="s")
    node = PreProcessNode(services=services)
    first = node.execute({"session_id": "s", "user_input": json.dumps({"mode": "invoke", "payload_ref": ref, "broadcaster_id": "t", "review_desk_id": "o"})})
    monkeypatch.setattr(services.payload_store, "resolve", lambda *_a, **_kw: pytest.fail("retry must not resolve raw payload"))
    retry = node.execute({**first, "status": "retry", "session_id": "s"})
    assert retry == {"status": "success"}


def test_sec26_expired_payload_fails_closed_without_raw_text_in_audit(monkeypatch):
    now = [0.0]
    services = wire_services()
    services.payload_store = PayloadStore(scope_keys=("broadcaster_id", "review_desk_id"), ttl_seconds=1, clock=lambda: now[0])
    events = []
    monkeypatch.setattr("shared.utils.audit_logger.emit_trace_event", lambda event, payload, _state: events.append((event, payload)))
    raw = "スポンサー: SEC26_EXPIRED_SPONSOR\nSEC26_RAW_SCRIPT"
    ref = services.payload_store.put(raw, scope={"broadcaster_id": "t", "review_desk_id": "o"}, session_id="s")
    now[0] = 1.0

    result = PreProcessNode(services=services).execute({
        "session_id": "s", "user_input": json.dumps({"mode": "invoke", "payload_ref": ref, "broadcaster_id": "t", "review_desk_id": "o"}),
    })

    assert result == {"status": "error", "error_log": ["payload_ref_expired"]}
    assert events == [("payload_expired", {"payload_ref": ref})]
    assert raw not in repr(events) and "SEC26_RAW_SCRIPT" not in repr(result)


def test_sec29_hitl_resume_never_rereads_an_expired_payload():
    now = [0.0]
    services = wire_services()
    services.payload_store = PayloadStore(scope_keys=("broadcaster_id", "review_desk_id"), ttl_seconds=1, clock=lambda: now[0])
    saver = InMemorySaver()
    agent = BroadcastStandardsReviewAgent({"memory_enabled": True, "hitl": {"enabled": True, "max_hitl": 8}}, services=services)
    agent.compile(checkpointer=saver)
    from framework.schemas.invocation_context import InvocationContext

    context = InvocationContext(session_id="s", thread_id="sec29-thread", caller_trust_level=TrustLevel.VERIFIED_EXTERNAL, hitl_allowed=True)
    ref = services.payload_store.put("スポンサー: SEC29 Sponsor\nSEC29 script body", scope={"broadcaster_id": "t", "review_desk_id": "o"}, session_id="s")
    first = agent.invoke(json.dumps({"mode": "invoke", "payload_ref": ref, "broadcaster_id": "t", "review_desk_id": "o"}), ctx=context)
    assert first["status"] == "awaiting_human"

    now[0] = 1.0  # The already-consumed raw body has expired before human feedback arrives.
    resumed = agent.resume("sec29-thread", {"action": "approve", "corrections": []})
    assert resumed["status"] == "success"


def test_sec30_raw_view_is_na_when_no_view_endpoint_exists():
    assert not any(route.path in {"/payload", "/payload/{payload_ref}", "/raw-payload"} for route in server.app.routes)
    pytest.skip("N/A: design (i) has no raw-payload viewing API, so viewing cannot consume invoke single-use payloads.")


def test_sec09_checkpoint_excludes_sensitive_markers_but_keeps_sanitized_script_text():
    services = wire_services()
    saver = InMemorySaver()
    agent = BroadcastStandardsReviewAgent({"memory_enabled": True, "hitl": {"enabled": True, "max_hitl": 8}}, services=services)
    agent.compile(checkpointer=saver)
    from framework.schemas.invocation_context import InvocationContext
    context = InvocationContext(session_id="s", thread_id="checkpoint-thread", caller_trust_level=TrustLevel.VERIFIED_EXTERNAL, hitl_allowed=True)
    markers = ("SEC09_DUMMY_PERSON", "SEC09_DUMMY_SPONSOR", "SEC09_DUMMY_PRODUCT", "SEC09_UNRELEASED_PROJECT")
    raw = (
        "スポンサー: SEC09_DUMMY_SPONSOR（担当者 SEC09_DUMMY_PERSON）\n"
        "商品名: SEC09_DUMMY_PRODUCT\n"
        "未公開企画名: SEC09_UNRELEASED_PROJECT\n"
        "SEC09_SCRIPT_BODY_REMAINS_REVIEWABLE"
    )
    refs = services.name_registry.register_from_text(raw, broadcaster_id="t", review_desk_id="o")
    ref = services.payload_store.put(raw, scope={"broadcaster_id": "t", "review_desk_id": "o"}, session_id="s")
    first = agent.invoke(json.dumps({"mode": "invoke", "payload_ref": ref, "broadcaster_id": "t", "review_desk_id": "o", "ref_hints": services.name_registry.ref_hints(refs)}), ctx=context)
    assert first["status"] == "awaiting_human"

    inner_saver = agent._nodes["main"]._checkpointer
    states = _checkpoint_states(saver, inner_saver)
    checkpoint_text = "\n".join(repr(state) for state in states)
    assert all(marker not in checkpoint_text for marker in markers)
    # SEC-09 does not prohibit ordinary script text in validated_input.
    assert any(state.get("validated_input", "").find("SEC09_SCRIPT_BODY_REMAINS_REVIEWABLE") >= 0 for state in states)


def test_sec10_hitl_fields_exclude_sensitive_values_during_interrupt_and_after_resume():
    services = wire_services()
    saver = InMemorySaver()
    agent = BroadcastStandardsReviewAgent({"memory_enabled": True, "hitl": {"enabled": True, "max_hitl": 8}}, services=services)
    agent.compile(checkpointer=saver)
    from framework.schemas.invocation_context import InvocationContext

    context = InvocationContext(session_id="s", thread_id="checkpoint-thread", caller_trust_level=TrustLevel.VERIFIED_EXTERNAL, hitl_allowed=True)
    markers = ("SEC10_SPONSOR", "SEC10_PRODUCT", "SEC10_UNRELEASED_PROJECT", "SEC10_SCRIPT_BODY")
    raw = "スポンサー: SEC10_SPONSOR\n商品名: SEC10_PRODUCT\n未公開企画名: SEC10_UNRELEASED_PROJECT\nSEC10_SCRIPT_BODY"
    refs = services.name_registry.register_from_text(raw, broadcaster_id="t", review_desk_id="o")
    ref = services.payload_store.put(raw, scope={"broadcaster_id": "t", "review_desk_id": "o"}, session_id="s")
    first = agent.invoke(json.dumps({"mode": "invoke", "payload_ref": ref, "broadcaster_id": "t", "review_desk_id": "o", "ref_hints": services.name_registry.ref_hints(refs)}), ctx=context)
    assert first["status"] == "awaiting_human"

    inner_saver = agent._nodes["main"]._checkpointer
    suspended_states = _checkpoint_states(saver, inner_saver)
    _assert_no_sensitive_hitl_value(suspended_states, markers)

    resumed = agent.resume("checkpoint-thread", {
        "action": "correct",
        "corrections": [{"issue_id": "issue_1", "criteria_id": "JBA-BC-STD-ART-152", "note": "safe structured correction"}],
    })
    assert resumed["status"] == "success"
    resumed_states = _checkpoint_states(saver, inner_saver)
    _assert_no_sensitive_hitl_value(resumed_states, markers)
    assert any(state.get("hitl_feedback", {}).get("action") == "correct" for state in resumed_states if isinstance(state.get("hitl_feedback"), dict))


def test_hitl_correct_updates_review_issue_in_resumed_output():
    services = wire_services()
    saver = InMemorySaver()
    agent = BroadcastStandardsReviewAgent(
        {"memory_enabled": True, "hitl": {"enabled": True, "max_hitl": 8}}, services=services
    )
    agent.compile(checkpointer=saver)
    from framework.schemas.invocation_context import InvocationContext

    context = InvocationContext(
        session_id="correct-session", thread_id="correct-thread", caller_trust_level=TrustLevel.VERIFIED_EXTERNAL, hitl_allowed=True
    )
    ref = services.payload_store.put("健康表現", scope={"broadcaster_id": "tenant", "review_desk_id": "org"}, session_id=context.session_id)
    first = agent.invoke(
        json.dumps({"mode": "invoke", "payload_ref": ref, "broadcaster_id": "tenant", "review_desk_id": "org"}), ctx=context
    )
    assert first["status"] == "awaiting_human"

    correction = "効能表現の根拠と開示方法を確認対象として追記します。"
    resumed = agent.resume(
        "correct-thread",
        {"action": "correct", "corrections": [{"issue_id": "issue_1", "criteria_id": "JBA-BC-STD-ART-152", "note": correction}]},
    )

    assert resumed["status"] == "success"
    corrected = next(issue for issue in resumed["output"]["review_issues"] if issue["issue_id"] == "issue_1")
    assert corrected["issue_text"] == correction
    assert corrected["operator_edited"] is True


def test_sec10b_and_sec10c_resume_rejects_unsafe_and_unknown_thread(monkeypatch):
    # Auth is checked at the same entry boundary as /invoke, before any resume state is read.
    server._resume_scopes.clear()
    with TestClient(server.app, raise_server_exceptions=False) as client:
        unauthenticated = client.post("/resume", json={"thread_id": "unknown", "action": "approve", "corrections": []})
    assert unauthenticated.status_code in {401, 403}

    monkeypatch.setattr(
        server,
        "authenticate",
        lambda _request, *, operation: AuthContext(
            trust=TrustLevel.INTERNAL,
            scope={"broadcaster_id": "t", "review_desk_id": "o"},
            principal={"caller_id": "operator"},
            auth_source=AuthSource.MIDDLEWARE,
        ),
    )
    with TestClient(server.app, raise_server_exceptions=False) as client:
        unsafe = client.post("/resume", json={"thread_id": "unknown", "action": "approve", "corrections": [{"issue_id": "i", "criteria_id": "c", "note": "test@example.com"}]})
        unknown_thread = client.post("/resume", json={"thread_id": "unknown", "action": "approve", "corrections": []})
    assert unsafe.status_code == 422
    assert unknown_thread.status_code == 404


@pytest.mark.parametrize(
    "note",
    ["スポンサー: Actual Sponsor", "ＳＰＯＮＳＯＲ：Full Width Sponsor", "商品名: Actual Product", "未公開企画名: Future Show"],
    ids=["sponsor", "full-width-sponsor", "product", "unreleased-project"],
)
def test_resume_rejects_confidential_note_before_it_can_be_checkpointed(monkeypatch, note):
    monkeypatch.setattr(
        server,
        "authenticate",
        lambda _request, *, operation: AuthContext(
            trust=TrustLevel.INTERNAL,
            scope={"broadcaster_id": "tenant", "review_desk_id": "org"},
            principal={"caller_id": "caller"},
            auth_source=AuthSource.MIDDLEWARE,
        ),
    )
    request = SimpleNamespace(state=SimpleNamespace(caller_id="caller", permissions=[]))
    payload = server.ResumeRequest(
        thread_id="pending", session_id="session", action="correct",
        corrections=[{"issue_id": "issue", "criteria_id": "criterion", "note": note}],
    )

    with pytest.raises(HTTPException) as exc:
        server.resume(payload, request)
    assert exc.value.status_code == 422


def test_resume_requires_original_caller_and_session(monkeypatch):
    http_agent = _wire_pending_http_agent(
        monkeypatch,
        thread_id="pending",
        session_id="session-a",
        caller_id="caller-a",
    )
    monkeypatch.setattr(
        server,
        "authenticate",
        lambda request, *, operation: AuthContext(
            trust=TrustLevel.INTERNAL,
            scope={"broadcaster_id": "tenant", "review_desk_id": "org"},
            principal={"caller_id": request.state.caller_id},
            auth_source=AuthSource.MIDDLEWARE,
        ),
    )
    def call(caller_id, session_id):
        request = SimpleNamespace(state=SimpleNamespace(caller_id=caller_id, permissions=[]))
        return server.resume(server.ResumeRequest(thread_id="pending", session_id=session_id, action="approve"), request)

    with pytest.raises(HTTPException) as caller_mismatch:
        call("caller-b", "session-a")
    with pytest.raises(HTTPException) as session_mismatch:
        call("caller-a", "session-b")
    assert caller_mismatch.value.status_code == 403
    assert session_mismatch.value.status_code == 403
    assert call("caller-a", "session-a")["status"] == "success"
    assert http_agent._compiled.get_state({"configurable": {"thread_id": "pending"}}).next == ()


def test_resume_rejects_empty_caller_id_even_when_the_saved_scope_is_also_empty(monkeypatch):
    _wire_pending_http_agent(
        monkeypatch,
        thread_id="pending-empty-caller",
        session_id="session-a",
        caller_id="",
    )
    monkeypatch.setattr(
        server,
        "authenticate",
        lambda _request, *, operation: AuthContext(
            trust=TrustLevel.INTERNAL,
            scope={"broadcaster_id": "tenant", "review_desk_id": "org"},
            principal={"caller_id": ""},
            auth_source=AuthSource.MIDDLEWARE,
        ),
    )
    with pytest.raises(HTTPException) as exc:
        server.resume(
            server.ResumeRequest(thread_id="pending-empty-caller", session_id="session-a", action="approve"),
            _http_request(caller_id=""),
        )

    assert exc.value.status_code == 403


def test_http_all_modes_declare_their_real_operation_and_run_serially(monkeypatch):
    services, http_agent = _wire_http_agent(monkeypatch, hitl=True)
    operations = []

    def authenticate_all(_request, *, operation):
        operations.append(operation)
        return AuthContext(
            trust=TrustLevel.INTERNAL,
            scope={"broadcaster_id": "tenant", "review_desk_id": "org"},
            principal={"caller_id": "operator"},
            auth_source=AuthSource.MIDDLEWARE,
        )

    def context_all(request, session_id, *, operation):
        auth = authenticate_all(request, operation=operation)
        thread_id = "httpresumethread" if operation in {Operation.READ_INVOKE, Operation.RESUME} else f"{operation.value}thread"
        return auth, InvocationContext(
            session_id=session_id,
            thread_id=thread_id,
            caller_trust_level=auth.trust,
            caller_id=auth.principal["caller_id"],
            hitl_allowed=True,
        )

    monkeypatch.setattr(server, "_context", context_all)
    monkeypatch.setattr(server, "_permissions", lambda _request: {"submit_ruling"})
    server._resume_scopes.clear()

    with TestClient(server.app, raise_server_exceptions=False) as client:
        invoked = client.post("/invoke", json={"input": "健康表現", "session_id": "invoke-session"})
        ingested = client.post(
            "/ingest",
            json={
                "records": [{"record_id": "http-record", "program_type": "show", "content_summary": "健康表現"}],
                "session_id": "ingest-session",
            },
        )
        feedback = client.post(
            "/feedback",
            json={
                "record_id": "http-record",
                "criteria_id": "PMD-ACT-AD-STANDARD",
                "criteria_version": "2026.1",
                "axis": "pmd_act",
                "ruling": "approved",
                "session_id": "feedback-session",
            },
        )
        resumed = client.post(
            "/resume",
            json={"thread_id": "httpresumethread", "session_id": "invoke-session", "action": "approve"},
        )

    assert [invoked.status_code, ingested.status_code, feedback.status_code, resumed.status_code] == [200, 200, 200, 200]
    assert invoked.json()["status"] == "awaiting_human"
    assert resumed.json()["status"] == "success"
    assert http_agent._compiled.get_state({"configurable": {"thread_id": "httpresumethread"}}).next == ()
    assert operations == [
        Operation.READ_INVOKE,
        Operation.WRITE_INGEST,
        Operation.WRITE_FEEDBACK,
        Operation.RESUME,
    ]


def test_http_standalone_token_cannot_reach_write_or_resume(monkeypatch):
    monkeypatch.setenv("INVOKE_AUTH_TOKEN", "mock-entry-token")
    monkeypatch.setattr(
        server.app.state,
        "verify_scope",
        lambda _request: {"broadcaster_id": "tenant", "review_desk_id": "org"},
        raising=False,
    )
    monkeypatch.setattr(server.agent, "invoke", lambda *_args, **_kwargs: {"status": "success", "output": {}})
    headers = {"authorization": "Bearer mock-entry-token"}
    with TestClient(server.app, raise_server_exceptions=False) as client:
        invoked = client.post("/invoke", headers=headers, json={"input": "read", "session_id": "read-session"})
        ingested = client.post("/ingest", headers=headers, json={"records": [], "session_id": "write-session"})
        feedback = client.post(
            "/feedback",
            headers=headers,
            json={
                "record_id": "record",
                "criteria_id": "PMD-ACT-AD-STANDARD",
                "criteria_version": "2026.1",
                "axis": "pmd_act",
                "ruling": "approved",
            },
        )
        resumed = client.post(
            "/resume",
            headers=headers,
            json={"thread_id": "thread", "session_id": "session", "action": "approve"},
        )
    assert invoked.status_code == 200
    assert [ingested.status_code, feedback.status_code, resumed.status_code] == [403, 403, 403]


def test_legacy_uuid_ruling_ids_survive_sanitisation_for_amend():
    """A ruling minted before this fix must still be amendable.

    Legacy IDs are `ruling_feedback_{uuid4().hex}`; ~3.4% of them contain a
    12+ digit run that `_MY_NUMBER` / `_PAYMENT_CARD_CANDIDATE` match. Masking
    one rewrites the identifier, so the amend could never match stored history.
    Identifier-shaped fields are passed through structurally instead.
    """
    import json as _json

    from src.services.service import sanitize_sensitive_text

    legacy = "ruling_feedback_a464271ea8964c5b842368339002774d"  # contains 842368339002
    payload = _json.dumps({"ruling_id": legacy, "rationale": "連絡先は 090-1234-5678"}, ensure_ascii=False)
    sanitised = _json.loads(sanitize_sensitive_text(payload))

    assert sanitised["ruling_id"] == legacy          # identifier survives intact
    assert "090-1234-5678" not in sanitised["rationale"]  # real PII still masked


@pytest.mark.parametrize(
    "smuggled",
    ["090-1234-5678", "123456789012", "taro@example.com"],
)
def test_identifier_passthrough_is_not_a_pii_channel(smuggled):
    """The passthrough must not become a way to carry PII through the gate."""
    import json as _json

    from src.services.service import sanitize_sensitive_text

    out = _json.loads(sanitize_sensitive_text(_json.dumps({"ruling_id": smuggled}, ensure_ascii=False)))
    assert out["ruling_id"] != smuggled


def test_minted_ruling_ids_are_never_mangled_by_the_sanitiser():
    """Self-minted IDs must not be shaped like the PII the sanitiser masks.

    A `uuid4().hex` suffix contains digit runs that `_MY_NUMBER` /
    `_PAYMENT_CARD_CANDIDATE` match for ~3.4% of generated IDs.  The sanitiser
    then rewrote part of the ID to "[個人情報非開示]", and the S-3 provenance
    gate rejected the resulting unsafe source id — failing the whole request at
    that rate.  The ID carries no PII, so it must not look like PII.
    """
    from src.payload_store.payload_store import mint_reference
    from src.services.service import _redact_free_text

    for _ in range(2000):
        ruling_id = f"ruling_feedback_{mint_reference()}"
        assert _redact_free_text(ruling_id, lambda match: match.group()) == ruling_id
