"""Independent read-only tool contracts and analytical golden fixtures."""
from copy import deepcopy
from datetime import datetime
from decimal import Decimal
import json

import pytest


def fact(value):
    return {"value": value, "quality": "verified" if value is not None else "missing",
            "source_ref": "acceptance-source", "as_of": "2026-10-12", "missing_reason": None if value is not None else "not_supplied"}


def records():
    return [
        {"bond_id": identifier, "code": code, "name": "合成同名券", "market": "IB", "aliases": [],
         "master": {"issuer_id": fact(issuer), "maturity_date": fact("2027-10-13"),
                    "bond_type": fact("fixed"), "balance": fact(None)},
         "valuation": {"yield_pct": fact("2.15"), "valuation_date": fact(None)}, "quotes": []}
        for identifier, code, issuer in [("bond-a", "000001.IB", "issuer-a"),
                                         ("bond-b", "000002.IB", "issuer-a"),
                                         ("bond-c", "000003.IB", None),
                                         ("bond-d", "000004.IB", "issuer-b")]
    ]


def tools(rows=None):
    from server.bond_tools import BondTools
    from server.reference_data import ReferenceSnapshot

    snapshot_rows = deepcopy(records() if rows is None else rows)

    class SyntheticProvider:
        def snapshot(self):
            return ReferenceSnapshot(snapshot_id="acceptance-snapshot", as_of="2026-10-13",
                                     sources=[{"id": "acceptance-source", "origin": "synthetic",
                                               "transport": "fixture", "dataset_version": "1", "as_of": "2026-10-12"}],
                                     bonds=deepcopy(snapshot_rows), issuers=[], terms={})
    return BondTools(SyntheticProvider())


def invoke(client, name, **arguments):
    return client.invoke(name, {"schema_version": "1", **arguments})


@pytest.mark.parametrize("name,arguments", [
    ("bond_info", {"mode": "search", "query": "合成"}),
    ("issuer_info", {"mode": "search", "query": "合成"}),
    ("similar_bonds", {"anchor_bond_id": "bond-a", "tenor_basis": "maturity",
                       "remaining_days": {"min": 0, "max": 1000}, "issuer_scope": "same", "compare_fields": ["valuation_yield_pct"]}),
])
def test_unconfigured_lookup_is_explicitly_unavailable(name, arguments):
    from server.bond_tools import BondTools

    result = invoke(BondTools(), name, **arguments)
    assert result["status"] == "unavailable"
    assert any(warning["code"] == "NOT_CONFIGURED" for warning in result["warnings"])
    assert result["usage"] == "analysis_only"
    assert datetime.fromisoformat(result["generated_at"].replace("Z", "+00:00")).tzinfo is not None


def test_search_pagination_cannot_turn_ambiguous_name_into_unique_resolution():
    client = tools()
    first = invoke(client, "bond_info", mode="search", query="合成同名券", limit=1)
    assert first["status"] == "ambiguous"
    assert first["data"]["total"] == 4
    assert first["data"]["resolved_bond_id"] is None
    assert first["data"]["has_more"] is True
    second = invoke(client, "bond_info", mode="search", query="合成同名券", limit=1, cursor=first["data"]["cursor"])
    assert second["data"]["candidates"][0]["bond_id"] != first["data"]["candidates"][0]["bond_id"]
    assert second["data"]["resolved_bond_id"] is None


def test_exact_code_retains_leading_zeros_and_missing_facts_stay_missing():
    client = tools()
    found = invoke(client, "bond_info", mode="search", query="000001.IB")
    assert found["data"]["resolved_bond_id"] == "bond-a"
    result = invoke(client, "bond_info", mode="get", bond_id="bond-a")
    assert result["data"]["identity"]["code"] == "000001.IB"
    assert result["data"]["master"]["balance"]["value"] is None
    assert result["data"]["valuation"]["valuation_date"]["value"] is None
    assert result["status"] == "partial"


def test_public_tool_output_omits_private_intents_and_raw_payloads():
    rows = records()
    rows[0].update(ib="SECRET-OUR-PRICE", instructions=[{"text": "SECRET-INSTRUCTION"}], raw_json={"password": "SECRET-PASSWORD"})
    rows[0]["master"]["internal_credit"] = fact("SECRET-CREDIT")
    result = invoke(tools(rows), "bond_info", mode="get", bond_id="bond-a")
    assert "SECRET-" not in json.dumps(result)
    assert "raw_json" not in json.dumps(result)


def test_same_issuer_filter_excludes_other_and_unknown_issuers_without_relaxing():
    result = invoke(tools(), "similar_bonds", anchor_bond_id="bond-a", tenor_basis="maturity",
                    remaining_days={"min": 350, "max": 380}, issuer_scope="same", compare_fields=["valuation_yield_pct"])
    assert [candidate["bond_id"] for candidate in result["data"]["candidates"]] == ["bond-b"]
    assert result["data"]["unknown_counts"]["issuer_id"] == 1
    assert result["data"]["criteria_applied"]["issuer_scope"] == "same"


@pytest.mark.parametrize("extra", [{"sql": "SELECT private"}, {"user_id": "other-owner"}, {"url": "https://private.invalid"}, {"limit": True}])
def test_model_cannot_choose_sql_identity_urls_or_boolean_page_sizes(extra):
    result = invoke(tools(), "bond_info", mode="search", query="合成", **extra)
    assert result["status"] == "invalid_input"


def calculation(operation, **inputs):
    from server.bond_tools import BondTools
    return invoke(BondTools(), "bond_calc", operation=operation, inputs=inputs)


def hypothetical_terms(**overrides):
    return {"hypothetical": True, "bond_type": "zero_coupon", "settlement_date": "2025-01-01",
            "cashflows": [{"payment_date": "2027-01-01", "amount_per_100": "100"}],
            "convention": "ACT/365F-compounded-v1", "compounding_frequency": 1,
            "yield_basis": "maturity", "settlement_cashflow": "exclude",
            "accrued_interest_per_100": "0", "accrued_interest_settlement_date": "2025-01-01", **overrides}


def test_zero_coupon_hypothetical_matches_independent_arithmetic_and_round_trip():
    expected = Decimal(100) / Decimal("1.05") ** 2
    priced = calculation("price_from_yield", yield_pct="5", **hypothetical_terms())
    assert priced["status"] == "ok"
    actual = Decimal(priced["data"]["result"]["dirty_price_per_100"])
    assert abs(actual - expected) <= Decimal("0.0001")
    inverse = calculation("yield_from_price", price_type="dirty", price_per_100=str(actual), **hypothetical_terms())
    assert inverse["status"] == "ok"
    assert abs(Decimal(inverse["data"]["result"]["yield_pct"]) - Decimal(5)) <= Decimal("0.00001")


def test_clean_to_dirty_uses_supplied_accrual_for_the_same_settlement():
    result = calculation("clean_dirty", hypothetical=True, clean_price_per_100="99.2",
                         accrued_interest_per_100="1.3", settlement_date="2026-10-13",
                         accrued_interest_settlement_date="2026-10-13")
    assert Decimal(result["data"]["result"]["dirty_price_per_100"]) == Decimal("100.5")


def test_other_settlement_accrual_is_rejected_instead_of_reused():
    result = calculation("clean_dirty", hypothetical=True, clean_price_per_100="99.2",
                         accrued_interest_per_100="1.3", settlement_date="2026-10-13",
                         accrued_interest_settlement_date="2026-10-12")
    assert result["status"] == "invalid_input"


def test_missing_cashflows_never_generates_an_assumed_schedule():
    result = calculation("price_from_yield", hypothetical=True, yield_pct="5", settlement_date="2025-01-01")
    assert result["status"] == "partial"
    assert "cashflows" in result["data"]["missing_inputs"]
    assert not result["data"].get("result")


@pytest.mark.parametrize("bond_type", ["callable", "perpetual", "floating"])
def test_unsupported_terms_never_fall_back_to_the_fixed_rate_formula(bond_type):
    result = calculation("price_from_yield", yield_pct="5", **hypothetical_terms(bond_type=bond_type))
    assert result["status"] == "unsupported"


def test_tiny_positive_gap_is_not_rounded_into_target_reached():
    result = calculation("yield_gap", our_side="SELL", peer_side="bid", limit_yield_pct="2.10",
                         peer_yield_pct="2.100001", same_basis=True)
    assert Decimal(result["data"]["result"]["raw_gap_bp"]) == Decimal("0.0001")


def test_current_yield_is_not_reported_as_yield_to_maturity():
    result = calculation("current_yield", hypothetical=True, annual_coupon_per_100="3", clean_price_per_100="95", coupon_basis="fixed_annual")
    assert result["status"] == "ok"
    values = result["data"]["result"]
    assert "yield_pct" not in values
    assert abs(Decimal(values["current_yield_pct"]) - Decimal(300) / Decimal(95)) <= Decimal("0.00001")


def test_current_yield_does_not_assume_a_missing_coupon_basis():
    result = calculation("current_yield", hypothetical=True, annual_coupon_per_100="3", clean_price_per_100="95")
    assert result["status"] == "partial"
    assert "coupon_basis" in result["data"]["missing_inputs"]
    assert result["data"]["result"] is None


def test_real_bond_calculation_without_terms_source_is_not_configured():
    result = calculation("price_from_yield", bond_id="bond-a", terms_snapshot_id="unconfigured-terms", yield_pct="5")
    assert result["status"] == "unavailable"
    assert any(warning["code"] == "NOT_CONFIGURED" for warning in result["warnings"])
    assert result["data"] is None


def test_provider_failure_is_reported_without_exposing_private_exception():
    from server.bond_tools import BondTools

    class BrokenProvider:
        def snapshot(self):
            raise OSError("private-db-password-and-host")

    result = invoke(BondTools(BrokenProvider()), "bond_info", mode="search", query="合成")
    assert result["status"] == "unavailable"
    assert "private-db-password-and-host" not in json.dumps(result)


def test_malformed_source_snapshot_is_unavailable_instead_of_crashing():
    from server.bond_tools import BondTools
    from server.reference_data import ReferenceSnapshot

    class MalformedProvider:
        def snapshot(self):
            return ReferenceSnapshot(snapshot_id="bad-source-shape", as_of="2026-10-13", sources=[None], bonds=records())

    result = invoke(BondTools(MalformedProvider()), "bond_info", mode="get", bond_id="bond-a")
    assert result["status"] == "unavailable"
    assert result["data"] is None
