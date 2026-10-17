"""R25-R29: source decode and real SQLite output under replay/failure."""
from copy import deepcopy
from datetime import datetime, timedelta
import struct

import pytest

from collector.qb_apollo import parse_push_quote
from server import market_cache as db, qb_source


def varint(value):
    out = bytearray()
    while value >= 128:
        out.append((value & 127) | 128)
        value >>= 7
    out.append(value)
    return bytes(out)


def field(number, value, *, floating=False):
    return varint((number << 3) | (1 if floating else 0)) + (struct.pack("<d", value) if floating else varint(value))


@pytest.mark.parametrize("side,type_field,raw_field,result_field", [
    ("bid", 16, 14, "bid_yield"), ("ofr", 30, 28, "ofr_yield"),
])
def test_net_price_without_conversion_is_never_treated_as_yield(side, type_field, raw_field, result_field):
    decoded = parse_push_quote(field(type_field, 1) + field(raw_field, 99.85, floating=True))
    assert decoded[result_field] is None, "Net price 99.85 must not enter yield comparisons"
    assert decoded[side + "_net_price"] == 99.85


@pytest.mark.parametrize("quote_type", [0, 99])
def test_absent_or_unknown_quote_type_cannot_authorize_a_yield(quote_type):
    decoded = parse_push_quote(field(16, quote_type) + field(14, 2.15, floating=True))
    assert decoded["bid_yield"] is None


def source_row(index, *, bid=2.10, ofr=2.20, broker=2, valuation=2.15, when=None):
    when = when or datetime(2026, 10, 13, 10, 0, 0) + timedelta(seconds=index)
    return {
        "id": index, "bond_uni_code": 100, "broker_id": broker, "seq_ns": index,
        "bid_yield": bid, "bid_vol": 1000 if bid is not None else None,
        "bid_pending": 0, "bid_vol_str": "+1", "bid_quote_type": 3 if bid is not None else 0,
        "ofr_yield": ofr, "ofr_vol": 2000 if ofr is not None else None,
        "ofr_pending": 0, "ofr_vol_str": "+1", "ofr_quote_type": 3 if ofr is not None else 0,
        "update_time": when, "capture_time": when,
        "cnbd_yield_mat": valuation, "cnbd_net_price": 100.0,
        "cnbd_duration": 1.5, "tenor": "1.50Y", "remain_days": 540,
        "coupon_rate": 2.00,
    }


@pytest.fixture
def market(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_conn", None)
    connection = db.init_db(path=str(tmp_path / "quotes.db"))
    monkeypatch.setattr(qb_source, "DIRTY", set())
    monkeypatch.setattr(qb_source, "INTENT", {})
    monkeypatch.setattr(qb_source, "REPLAY", 0)
    source = qb_source.QBSource()
    source.dict = {100: ("000100.IB", "合成债券")}
    source.code2uni = {"000100.IB": 100}
    source.brokers = {2: "合成经纪甲", 9: "合成经纪乙"}
    monkeypatch.setattr(source, "_refresh_dict", lambda *_, **__: None)
    incoming = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def execute(self, query, params):
            self.after = params[0]

        def fetchall(self):
            return [tuple(row.get(key) for key in qb_source._COLS) for row in incoming if row["id"] > self.after]

    class Feed:
        def cursor(self):
            return Cursor()

    monkeypatch.setattr(source, "_c", lambda: Feed())
    try:
        yield source, connection, incoming
    finally:
        connection.close()


def latest(connection):
    row = connection.execute("SELECT * FROM quote_latest WHERE bond_uni_code='100'").fetchone()
    return dict(row) if row else None


def test_full_withdrawal_clears_both_persisted_sides(market):
    source, connection, incoming = market
    incoming.append(source_row(1))
    assert source.poll()
    incoming.append(source_row(2, bid=None, ofr=None))
    source.poll()
    result = latest(connection)
    assert result is None or (result["best_bid"] is None and result["best_ofr"] is None)


def test_one_side_withdrawal_preserves_the_other_side(market):
    source, connection, incoming = market
    incoming.append(source_row(1))
    source.poll()
    incoming.append(source_row(2, bid=None))
    source.poll()
    assert latest(connection)["best_bid"] is None
    assert latest(connection)["best_ofr"] == 2.20


def test_same_price_new_observation_advances_freshness(market):
    source, connection, incoming = market
    incoming.append(source_row(1))
    source.poll()
    incoming.append(source_row(2))
    source.poll()
    assert latest(connection)["issue_time"] == int(incoming[-1]["update_time"].timestamp() * 1000)


def test_valuation_update_reaches_the_persisted_fact(market):
    source, connection, incoming = market
    incoming.append(source_row(1, valuation=1.80))
    source.poll()
    incoming.append(source_row(2, valuation=2.50))
    source.poll()
    value = connection.execute("SELECT val_yield FROM bond_static WHERE bond_uni_code='100'").fetchone()[0]
    assert value == 2.50


def test_missing_source_valuation_date_is_not_replaced_by_today(market):
    source, connection, incoming = market
    incoming.append(source_row(1))
    source.poll()
    value = connection.execute("SELECT val_date FROM bond_static WHERE bond_uni_code='100'").fetchone()[0]
    assert value in (None, ""), "The valuation publication date must come from its source"


def test_explicit_negative_yield_remains_a_valid_quote(market):
    source, connection, incoming = market
    incoming.append(source_row(1, bid=-0.10, ofr=-0.05))
    source.poll()
    result = latest(connection)
    assert result is not None
    assert result["best_bid"] == -0.10
    assert result["best_ofr"] == -0.05


def test_new_day_does_not_keep_yesterdays_better_quote(market):
    source, connection, incoming = market
    incoming.append(source_row(1, broker=2, bid=1.5, when=datetime(2026, 10, 12, 16)))
    source.poll()
    incoming.append(source_row(2, broker=9, bid=2.0, when=datetime(2026, 10, 13, 9)))
    source.poll()
    assert latest(connection)["best_bid"] == 2.0


@pytest.mark.parametrize("stage", ["static", "quote"])
def test_database_failure_does_not_consume_input_or_prevent_replay(market, monkeypatch, stage):
    source, connection, incoming = market
    incoming.append(source_row(1))
    target = source if stage == "static" else db
    name = "_upsert_static" if stage == "static" else "upsert_quotes"
    original = getattr(target, name)
    failures = []

    def fail_once(*args, **kwargs):
        if not failures:
            failures.append(1)
            raise OSError("synthetic database write failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(target, name, fail_once)
    with pytest.raises(OSError, match="synthetic database write failure"):
        source.poll()
    source.poll()
    result = latest(connection)
    assert result is not None, "A failed write must remain replayable"
    assert result["best_bid"] == 2.10
    assert connection.execute("SELECT count(*) FROM quote_flow").fetchone()[0] == 1


def test_lower_source_version_cannot_restore_withdrawn_price(market):
    source, connection, incoming = market
    incoming.append(source_row(1))
    source.poll()
    incoming.append(source_row(3, bid=None))
    source.poll()
    old = deepcopy(source_row(2))
    old["id"] = 4  # Delivery order is newer but source sequence is older.
    incoming.append(old)
    source.poll()
    assert latest(connection)["best_bid"] is None
