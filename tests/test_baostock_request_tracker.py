from __future__ import annotations

import json

from duckdb_storage.viewer import BaoStockRequestTracker


def test_baostock_request_tracker_default_daily_limit_is_30000(tmp_path):
    tracker = BaoStockRequestTracker(str(tmp_path))

    assert tracker.daily_limit == 30_000
    assert tracker.display_limit == 30_000
    assert "30000" in tracker.limit_message


def test_baostock_request_tracker_stops_at_daily_limit_and_persists(tmp_path):
    tracker = BaoStockRequestTracker(str(tmp_path))
    tracker._count = 29_999

    assert tracker.consume() is True
    assert tracker.get_count() == 30_000
    assert tracker.is_limit_reached() is True
    assert tracker.consume() is False

    saved = json.loads(
        (tmp_path / "baostock_request_usage.json").read_text(encoding="utf-8")
    )
    assert saved["count"] == 30_000
