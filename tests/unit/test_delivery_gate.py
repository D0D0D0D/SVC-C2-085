"""Delivery-gate tests for graph-external sponsor/product name resolution."""

from __future__ import annotations

from framework.schemas.trust_level import TrustLevel
from src.api import server
from src.services.service import wire_services


def _setup():
    services = wire_services()
    ref = next(iter(services.name_registry.register_from_text("スポンサー: Example Sponsor", broadcaster_id="t", review_desk_id="o")))
    return services, ref


def test_sec15_to_sec18_delivery_gate_requires_permission_scope_and_allowlist(monkeypatch) -> None:
    services, ref = _setup()
    monkeypatch.setattr(server, "services", services)
    output = {"output": {"sponsor_ref": ref, "unreleased_project": "[企画名非開示]", "other": "sponsor_ref_unknown"}}
    denied = server._delivery_gate(output, trust=TrustLevel.VERIFIED_EXTERNAL, broadcaster_id="t", review_desk_id="o", permissions=set(), allowed_refs={ref})
    assert denied["output"]["sponsor_ref"] == ref
    allowed = server._delivery_gate(output, trust=TrustLevel.VERIFIED_EXTERNAL, broadcaster_id="t", review_desk_id="o", permissions={"view_sponsor_names"}, allowed_refs={ref})
    assert allowed["output"]["sponsor_ref"] == "Example Sponsor"
    assert allowed["output"]["unreleased_project"] == "[企画名非開示]"
    assert allowed["output"]["other"] == "sponsor_ref_unknown"
    assert output["output"]["sponsor_ref"] == ref  # state/cache source copy remains ref-only
    foreign = server._delivery_gate(output, trust=TrustLevel.VERIFIED_EXTERNAL, broadcaster_id="other", review_desk_id="o", permissions={"view_sponsor_names"}, allowed_refs={ref})
    assert foreign["output"]["sponsor_ref"] == ref


def test_sec22_to_sec25_resolution_failure_is_ref_only_and_non_throwing(monkeypatch) -> None:
    services, ref = _setup()
    monkeypatch.setattr(server, "services", services)
    monkeypatch.setattr(services.name_registry, "resolve", lambda *_args, **_kwargs: (_ for _ in ()).throw(TimeoutError()))
    output = {"output": {"sponsor_ref": ref}}
    delivered = server._delivery_gate(output, trust=TrustLevel.VERIFIED_EXTERNAL, broadcaster_id="t", review_desk_id="o", permissions={"view_sponsor_names"}, allowed_refs={ref})
    assert delivered == output


def test_delivery_gate_never_reexpands_pii_embedded_in_a_sponsor_value(monkeypatch) -> None:
    services = wire_services()
    ref = next(iter(services.name_registry.register_from_text("スポンサー: Acme／jane@example.com", broadcaster_id="t", review_desk_id="o")))
    monkeypatch.setattr(server, "services", services)

    delivered = server._delivery_gate(
        {"output": {"sponsor_ref": ref}},
        trust=TrustLevel.INTERNAL,
        broadcaster_id="t",
        review_desk_id="o",
        permissions={"view_sponsor_names"},
        allowed_refs={ref},
    )

    assert delivered["output"]["sponsor_ref"] == "Acme"
    assert "jane@example.com" not in str(delivered)
