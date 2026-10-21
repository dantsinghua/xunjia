"""Deterministic quote math. Language interpretation stays in the Chatflow."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_UP
import re
from typing import Any, Mapping


class InvalidFact(ValueError):
    pass


class StaleDecision(RuntimeError):
    pass


class AmbiguousPrice(InvalidFact):
    pass


def number(value: Any, name: str = "number") -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise InvalidFact(f"{name}: expected decimal text, integer or Decimal")
    raw = str(value).strip()
    if not re.fullmatch(r"-?\d{1,12}(?:\.\d{1,8})?", raw):
        raise InvalidFact(f"{name}: invalid decimal")
    return Decimal(raw)


def iso_day(value: str) -> str:
    if not isinstance(value, str):
        raise InvalidFact("A confirmed ISO date is required")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise InvalidFact("Invalid ISO date") from exc
    if parsed.isoformat() != value:
        raise InvalidFact("Date must be YYYY-MM-DD")
    return value


def rounded_yield(value: Any) -> Decimal:
    return number(value, "yield").quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def valuation_quote(valuation: Any, offset_bp: Any) -> Decimal:
    # Round only AFTER adding the offset to the full valuation precision.
    return rounded_yield(number(valuation, "valuation") + number(offset_bp, "BP") / 100)


def resolve_yield_suffix(suffix: str, *, contextual_anchor: Any) -> Decimal:
    """Require a same-bond/counterparty anchor, not the price that reaches goal.

    The nearest uniquely supported integer part handles carry (e.g. 04 around
    2.9872 -> 3.04); an exact tie is unresolved rather than silently guessed.
    """
    if not isinstance(suffix, str) or not re.fullmatch(r"\d{2}", suffix):
        raise InvalidFact("Yield suffix must contain exactly two digits")
    anchor = number(contextual_anchor, "contextual anchor")
    integer = int(anchor.to_integral_value(rounding=ROUND_FLOOR))
    fraction = Decimal(suffix) / 100
    options = [Decimal(integer + delta) + fraction for delta in (-1, 0, 1)]
    distance = min(abs(candidate - anchor) for candidate in options)
    matches = [candidate for candidate in options if abs(candidate - anchor) == distance]
    if len(matches) != 1:
        raise AmbiguousPrice("Two integer parts are equally supported by this anchor")
    return rounded_yield(matches[0])


def volume_wan(token: str, *, contextual_unit: str | None = None) -> Decimal:
    """Normalize an already classified QUANTITY; never scan a price for volume."""
    if not isinstance(token, str):
        raise InvalidFact("Volume token must preserve its textual number/unit")
    match = re.fullmatch(r"\s*(\d{1,12}(?:\.\d{1,8})?)\s*(万|[wW]|亿)?\s*", token)
    if not match:
        raise InvalidFact("Unrecognized volume token")
    unit = match.group(2) or contextual_unit
    if unit not in ("万", "w", "W", "亿"):
        raise InvalidFact("Unqualified quantity needs a contextual unit")
    return number(match.group(1), "volume") * (10000 if unit == "亿" else 1)


@dataclass(frozen=True)
class Price:
    kind: str
    value: str
    settlement_date: str | None = None
    computed_yield: str | None = None
    calculation_ref: str | None = None

    def __post_init__(self):
        if self.kind not in ("YIELD", "NET_PRICE", "DIRTY_PRICE"):
            raise InvalidFact("Unknown quotation price type")
        parsed = number(self.value, "price")
        if self.kind != "YIELD" and parsed < 0:
            raise InvalidFact("Cash price cannot be negative")
        canonical = format(rounded_yield(parsed), ".2f") if self.kind == "YIELD" else format(parsed, "f")
        object.__setattr__(self, "value", canonical)
        if self.settlement_date is not None:
            iso_day(self.settlement_date)
        if self.kind == "YIELD" and (self.computed_yield is not None or self.calculation_ref is not None):
            raise InvalidFact("Yield quotes cannot carry a redundant cash-price conversion")
        if self.computed_yield is not None:
            if not self.settlement_date or not isinstance(self.calculation_ref, str) or not self.calculation_ref:
                raise InvalidFact("Cash conversion requires confirmed settlement and calculator evidence")
            object.__setattr__(self, "computed_yield", format(rounded_yield(self.computed_yield), ".2f"))
        elif self.calculation_ref is not None:
            raise InvalidFact("Calculator evidence without a conversion result")

    @classmethod
    def load(cls, value: Mapping[str, Any]) -> "Price":
        if not isinstance(value, dict) or set(value) - set(cls.__dataclass_fields__):
            raise InvalidFact("Invalid price object")
        try:
            return cls(**value)
        except TypeError as exc:
            raise InvalidFact("Incomplete price object") from exc

    def dump(self) -> dict:
        return asdict(self)

    def as_yield(self) -> Decimal | None:
        if self.kind == "YIELD":
            return number(self.value)
        return number(self.computed_yield) if self.computed_yield is not None else None


@dataclass(frozen=True)
class Valuation:
    bond_code: str
    trading_date: str | None
    yield_pct: str
    snapshot_id: str
    provider: str = "CHINABOND"
    source: str | None = None

    def __post_init__(self):
        # The source date is advisory; never substitute today's/quote's date.
        if self.trading_date is not None:
            try:iso_day(self.trading_date)
            except InvalidFact:object.__setattr__(self, "trading_date", None)
        number(self.yield_pct, "valuation")
        if self.provider != "CHINABOND" or not self.snapshot_id or not self.bond_code:
            raise InvalidFact("A sourced ChinaBond valuation snapshot is required")
        if self.source is None:
            # Existing persisted JSON predates the explicit origin field.
            object.__setattr__(self, "source", "innodealing" if self.snapshot_id.startswith("innodealing-cnbd:") else "reference")
        if self.source not in ("innodealing", "reference"):
            raise InvalidFact("Unknown ChinaBond valuation origin")

    def authorization_key(self):
        # Date/republication metadata does not change the authorized economics.
        return self.bond_code, self.provider, self.source, number(self.yield_pct)


@dataclass(frozen=True)
class LimitPolicy:
    mode: str
    value: str
    price_kind: str = "YIELD"

    def __post_init__(self):
        if self.mode not in ("ABSOLUTE", "CHINABOND_BP"):
            raise InvalidFact("Unknown internal limit mode")
        amount = number(self.value, "limit or BP offset")
        if self.mode == "ABSOLUTE" and self.price_kind != "YIELD" and amount < 0:
            raise InvalidFact("Absolute cash-price limit cannot be negative")
        if self.price_kind not in ("YIELD", "NET_PRICE", "DIRTY_PRICE"):
            raise InvalidFact("Unknown internal price type")
        if self.mode == "CHINABOND_BP" and self.price_kind != "YIELD":
            raise InvalidFact("ChinaBond BP offsets are yield based")

    def resolve(self, *, bond_code: str, expected_valuation_date: str | None = None,
                valuation: Valuation | None = None) -> dict[str, str] | None:
        if self.mode == "ABSOLUTE":
            # Keep the authorized absolute value unchanged across trading days.
            return {"kind": self.price_kind, "value": self.value}
        if valuation is None:
            return None
        if valuation.bond_code != bond_code:
            raise InvalidFact("Valuation does not match the required bond")
        return {"kind": "YIELD", "value": format(valuation_quote(valuation.yield_pct, self.value), ".2f")}


def comparison(price: Price, boundary: Mapping[str, str], *, our_side: str) -> dict | None:
    if our_side not in ("BUY", "SELL"):
        raise InvalidFact("Our side must be explicitly BUY or SELL")
    kind = boundary["kind"]
    limit = number(boundary["value"], "limit")
    if kind == "YIELD":
        peer = price.as_yield()
        if peer is None:
            return None
        gap = (limit - peer if our_side == "BUY" else peer - limit) * 100
        unit = "BP"
    elif kind in ("NET_PRICE", "DIRTY_PRICE") and price.kind == kind and price.settlement_date:
        peer = number(price.value)
        gap = peer - limit if our_side == "BUY" else limit - peer
        unit = "PRICE_PER_100"
    else:
        return None
    return {"raw_gap": format(gap, "f"), "remaining_gap": format(max(Decimal(0), gap), "f"),
            "improvement": format(max(Decimal(0), -gap), "f"), "unit": unit,
            "price_eligible": gap <= 0}
