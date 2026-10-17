"""报价渠道:最优、单家经纪商、未接入的 QT / 做市商。"""
from __future__ import annotations

import datetime as dt
import unittest

from server.qb_source import QBSource
from server.quote_channel import classify


def _row(broker_id, bid, bid_vol, ofr, ofr_vol, when):
    return {
        "broker_id": broker_id,
        "bid_yield": bid, "bid_vol": bid_vol, "bid_pending": 0, "bid_vol_str": "+1",
        "ofr_yield": ofr, "ofr_vol": ofr_vol, "ofr_pending": 0, "ofr_vol_str": "+1",
        "update_time": when,
    }


class ChannelTest(unittest.TestCase):
    def test_empty_channels(self):
        kind, note = classify("qt", "")
        self.assertEqual(kind, "empty")
        self.assertIn("QT", note)
        kind, note = classify("mm", "9")
        self.assertEqual(kind, "empty")
        self.assertIn("做市商", note)

    def test_best_and_broker_best_are_all(self):
        self.assertEqual(classify("best", "")[0], "all")
        self.assertEqual(classify("broker", "")[0], "all")
        self.assertEqual(classify("broker", "best")[0], "all")

    def test_one_broker(self):
        self.assertEqual(classify("broker", "9")[0], "one")

    def test_single_broker_keeps_its_own_price(self):
        src = QBSource()
        src.brokers = {2: "中诚", 9: "国利"}
        when = dt.datetime(2026, 10, 12, 11, 0, 0)
        rows = [
            _row(2, 1.90, 1000, 1.80, 2000, when),
            _row(9, 1.50, 3000, 1.60, 500, when),
        ]
        cross = src._best_of(rows)
        one = src._best_of([rows[0]])
        self.assertEqual(cross["best_bid"], 1.5)
        self.assertIn("国利", cross["best_bid_side"])
        self.assertEqual(one["best_bid"], 1.9)
        self.assertEqual(one["best_bid_vol"], 1000)
        self.assertEqual(one["best_bid_side"], "中诚")
        self.assertEqual(one["best_ofr"], 1.8)
        self.assertEqual(one["best_ofr_vol"], 2000)
        self.assertEqual(one["best_ofr_side"], "中诚")

    def test_page_lists_only_selected_broker(self):
        src = QBSource()
        src.brokers = {2: "中诚", 9: "国利"}
        when = dt.datetime(2026, 10, 12, 11, 0, 0)
        src.dict = {100: ("230210.IB", "23国开10"), 200: ("102280109.IB", "22中原高速MTN001")}
        src.book = {
            100: {
                2: _row(2, 1.90, 1000, 1.80, 2000, when),
                9: _row(9, 1.50, 3000, 1.60, 500, when),
            },
            200: {9: _row(9, 2.10, 4000, 2.00, 1000, when)},
        }
        page, total, _more, _ms, _uni = src.page_one_broker(2, 0, 0, 50, ["bond-all"])
        self.assertEqual(total, 1)
        self.assertEqual(page[0]["static"]["sec_name"], "23国开10")
        self.assertEqual(page[0]["best"]["best_bid"], 1.9)
        self.assertEqual(page[0]["best"]["best_bid_side"], "中诚")
        names = [x["name"] for x in src.broker_choices()]
        self.assertEqual(names, ["国际", "中诚", "平安", "上田", "信唐", "国利"])
