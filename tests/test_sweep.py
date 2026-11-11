"""Checks the markout convention in tools/sweep.py without needing the SDK or data."""
import os
import sys
from types import SimpleNamespace as NS

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
from sweep import H, markout  # noqa: E402


def top(bid, ask):
    return {"BNKY": NS(bid_px=bid, ask_px=ask)}


def fill(batch, side, qty, price):
    return NS(batch=batch, side=NS(name=side), qty=qty, price=price, symbol="BNKY")


def test_uses_fill_batch_plus_horizon():
    tops = [top(100, 102)] * (H + 20)
    tops[10 + H] = top(110, 112)  # mid 111
    assert markout(tops, fill(10, "BUY", 10, 100)) == 10 * (111 - 100) / 100.0
    assert markout(tops, fill(10, "SELL", 10, 100)) == -10 * (111 - 100) / 100.0


def test_incomplete_window_excluded_not_clamped():
    tops = [top(100, 102)] * (H + 5)
    assert markout(tops, fill(6, "BUY", 5, 100)) is None


def test_one_sided_mark_excluded():
    tops = [top(100, 102)] * (H + 5)
    tops[3 + H] = top(None, 102)
    assert markout(tops, fill(3, "BUY", 5, 100)) is None
