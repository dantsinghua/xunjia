from dify.negotiation_v22.cases import cases,review_cases,reference_snapshot
from server.negotiation.slots import collect_candidates
import pytest

def test_review_corpus_preserves_original_cases_and_adds_unique_inputs():
    original=cases();extra=review_cases()
    assert len(original)==47 and len(extra)==6
    assert len({c["id"] for c in original+extra})==53

@pytest.mark.parametrize("case",review_cases(),ids=lambda c:c["id"])
def test_review_valuation_fixture_and_lexical_roles(case):
    ref=reference_snapshot(case["reference_case_id"])
    assert next(r for r in ref.bonds if r["code"]=="SYN-A")["valuation_yield_pct"]=="2.5818"
    candidates=collect_candidates({"bond_code":"SYN-A","messages":[{"message_id":"review","text":case["text"],"direction":"in"}]})
    bp=[r for r in candidates if "CHINABOND_BP" in r["roles"]]
    assert len(bp)==(0 if case["id"]=="review-negative-yield" else 2 if case["id"]=="review-bp-two-sided" else 1)
