"""Versioned bid/ofr facts and deterministic whole-lot group coverage."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Any, Mapping, Sequence

from .prices import InvalidFact, Price, comparison, iso_day, number


@dataclass(frozen=True)
class Quote:
    side: str
    price: Price | None = None
    volume_wan: str | None = None
    assertion: str = "QUOTE"
    validity: str = "ACTIVE"
    finality: str = "UNKNOWN"
    confirmed_day: str | None = None
    revision: int = 0
    evidence: dict[str, list[str]] = field(default_factory=dict)

    def __post_init__(self):
        if self.side not in ("bid", "ofr"):
            raise InvalidFact("Quote side must be bid or ofr")
        if self.price is not None and not isinstance(self.price, Price):
            raise InvalidFact("Invalid price object")
        if self.volume_wan is not None and number(self.volume_wan, "volume") < 0:
            raise InvalidFact("Volume must be nonnegative")
        if self.assertion not in ("QUOTE", "INDICATIVE", "QUESTION", "REPORTED"):
            raise InvalidFact("Unknown quotation assertion")
        if self.validity not in ("ACTIVE", "WITHDRAWN", "UNCONFIRMED"):
            raise InvalidFact("Unknown quote validity")
        if self.finality not in ("UNKNOWN", "NEGOTIABLE", "FINAL"):
            raise InvalidFact("Unknown negotiation-space state")
        if self.confirmed_day is not None:
            iso_day(self.confirmed_day)
        if type(self.revision) is not int or self.revision < 0:
            raise InvalidFact("Invalid quote revision")
        if not isinstance(self.evidence, dict):
            raise InvalidFact("Quote evidence must be a field/message mapping")

    def dump(self) -> dict:
        return asdict(self)

    @classmethod
    def load(cls, value: dict) -> "Quote":
        if not isinstance(value, dict) or set(value) - set(cls.__dataclass_fields__):
            raise InvalidFact("Invalid quote object")
        data = dict(value)
        if data.get("price") is not None:
            data["price"] = Price.load(data["price"])
        try:
            return cls(**data)
        except TypeError as exc:
            raise InvalidFact("Incomplete quote object") from exc


def apply_patch(previous: Quote, changes: Mapping[str, Any], *,
                evidence: Mapping[str, Sequence[str]]) -> Quote:
    """Absent fields stay unchanged; explicit null clears one field.

    Evidence is checked structurally here. The repository must also verify
    the messages actually exist and belong to the same counterparty/bond.
    New economics cannot inherit a previous 'final' label without evidence.
    """
    allowed = {"price", "volume_wan", "assertion", "validity", "finality", "confirmed_day"}
    if not isinstance(changes, dict) or not changes or set(changes) - allowed:
        raise InvalidFact("Empty or unknown quote patch fields")
    if not isinstance(evidence, dict) or set(evidence) != set(changes):
        raise InvalidFact("Each changed field requires source message references")
    refs = {}
    for key, values in evidence.items():
        if not isinstance(values, (tuple, list)) or not 1 <= len(values) <= 30:
            raise InvalidFact("Evidence must cite one to thirty actual messages")
        if any(not isinstance(ref, str) or not 1 <= len(ref) <= 160 for ref in values):
            raise InvalidFact("Invalid source message reference")
        refs[key] = list(dict.fromkeys(values))
    old = previous.dump()
    updated = previous.dump()
    updated.update(changes)
    if "price" in changes and changes["price"] is not None:
        updated["price"] = Price.load(changes["price"]).dump()
    if "volume_wan" in changes and changes["volume_wan"] is not None:
        updated["volume_wan"] = format(number(changes["volume_wan"], "volume"), "f")
    economics_changed = any(key in changes and updated[key] != old[key]
                            for key in ("price", "volume_wan", "assertion"))
    confirmation_changed = ("confirmed_day" in changes and
                            updated["confirmed_day"] != old["confirmed_day"])
    reactivated = ("validity" in changes and updated["validity"] == "ACTIVE"
                   and old["validity"] != "ACTIVE")
    if (economics_changed or confirmation_changed or reactivated) and "finality" not in changes:
        updated["finality"] = "UNKNOWN"
        updated["evidence"].pop("finality", None)
    updated["revision"] = previous.revision + 1
    updated["evidence"].update(refs)
    return Quote.load(updated)


def group_summary(*, our_side: str, target_volume: Any, trade_day: str,
                  boundary: Mapping[str, str] | None,
                  quotes: Sequence[tuple[str, Quote]]) -> dict:
    """Aggregate current independent counterparty lots, never split or execute.

    A quantity contributes to final coverage only if the quote is current,
    active, self-quoted, price eligible AND contextually final.
    Silence, revoked or yesterday's quotes never become coverage.
    """
    iso_day(trade_day)
    if our_side not in ("BUY", "SELL"):
        raise InvalidFact("Unknown group direction")
    target = number(target_volume, "target volume")
    if target <= 0:
        raise InvalidFact("Group target must be positive")
    relevant_side = "ofr" if our_side == "BUY" else "bid"
    reported = eligible = final = Decimal(0)
    whole_lots = []
    seen = set()
    for member, quote in quotes:
        key = (member, quote.side)
        if key in seen:
            raise InvalidFact("Multiple current versions for one member and side")
        seen.add(key)
        if quote.side != relevant_side:
            continue
        volume = number(quote.volume_wan) if quote.volume_wan is not None else Decimal(0)
        current = (quote.confirmed_day == trade_day and quote.validity == "ACTIVE"
                   and quote.assertion == "QUOTE")
        result = comparison(quote.price, boundary, our_side=our_side) if quote.price and boundary else None
        if current:
            reported += volume
        if current and result and result["price_eligible"]:
            eligible += volume
            if quote.finality == "FINAL":
                final += volume
                if volume > 0:
                    whole_lots.append({"member_id": member, "quote_revision": quote.revision,
                                       "volume_wan": format(volume, "f"), "comparison": result})
    return {
        "reported_volume_wan": format(reported, "f"),
        "price_eligible_volume_wan": format(eligible, "f"),
        "final_confirmed_volume_wan": format(final, "f"),
        "target_volume_wan": format(target, "f"),
        "reported_shortfall_wan": format(max(Decimal(0), target - reported), "f"),
        "final_shortfall_wan": format(max(Decimal(0), target - final), "f"),
        "quantity_covered": final >= target,
        "goal_ready": boundary is not None and final >= target,
        "whole_lots": whole_lots,
        "allocation_is_manual": True,
    }
