"""交易日历缓存：读取合并用户缓存与随包日历，写入只落到可写目录。

打包版装在 Program Files 时，程序目录（随包 data/）不可写；以前 trade_days.csv
固定写在模块旁边，补过的新年份存不下来，每次启动都要重新联网取日历。
"""
import os

import pytest

import khQTTools


def _write(path, dates):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("trade_date\n" + "\n".join(dates) + "\n")


@pytest.fixture
def isolated_calendar(monkeypatch, tmp_path):
    user_dir = tmp_path / "localappdata" / "data"
    bundled_dir = tmp_path / "Program Files" / "_internal" / "data"
    monkeypatch.setattr(
        khQTTools, "get_stock_pool_read_dirs",
        lambda include_missing=True: [
            str(path) for path in (user_dir, bundled_dir)
            if include_missing or path.is_dir()
        ],
    )
    monkeypatch.setattr(
        khQTTools, "get_stock_pool_path",
        lambda name, for_write=False: str((user_dir if for_write else bundled_dir) / name),
    )
    for name, value in (("_td_memory", set()), ("_td_min", ""), ("_td_max", ""),
                        ("_td_covered_years", set()), ("_td_csv_loaded", False)):
        monkeypatch.setattr(khQTTools, name, value)
    return user_dir, bundled_dir


def test_load_merges_user_cache_and_bundled(isolated_calendar):
    user_dir, bundled_dir = isolated_calendar
    _write(str(bundled_dir / "trade_days.csv"), ["20261230", "20261231"])
    _write(str(user_dir / "trade_days.csv"), ["20270104", "20270105"])

    khQTTools._td_load_csv()

    assert khQTTools._td_memory == {"20261230", "20261231", "20270104", "20270105"}
    assert khQTTools._td_covered_years == {"2026", "2027"}
    assert (khQTTools._td_min, khQTTools._td_max) == ("20261230", "20270105")


def test_load_uses_bundled_when_no_user_cache(isolated_calendar):
    _, bundled_dir = isolated_calendar
    _write(str(bundled_dir / "trade_days.csv"), ["20250102"])

    khQTTools._td_load_csv()

    assert khQTTools._td_memory == {"20250102"}


def test_save_writes_user_dir_and_leaves_bundled_untouched(isolated_calendar, monkeypatch):
    user_dir, bundled_dir = isolated_calendar
    bundled = bundled_dir / "trade_days.csv"
    _write(str(bundled), ["20261231"])
    before = bundled.read_bytes()
    user_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(khQTTools, "_td_memory", {"20261231", "20270104"})
    khQTTools._td_save_csv()

    assert bundled.read_bytes() == before
    saved = (user_dir / "trade_days.csv").read_text(encoding="utf-8").split()
    assert saved == ["trade_date", "20261231", "20270104"]
