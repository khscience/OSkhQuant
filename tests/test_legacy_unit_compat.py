"""Portable storage regressions shared by desktop and OS distributions."""
import pandas as pd
import pytest

from duckdb_storage.stock_db import StockDB
from duckdb_storage.units import VolumeUnitError


@pytest.mark.parametrize("stock_code,period", [
    ("600309.SH", "1d"), ("510300.SH", "1m"), ("000300.SH", "5m"),
])
def test_unmarked_history_accepts_values_without_claiming_verified_units(tmp_path, stock_code, period):
    db = StockDB(stock_code, str(tmp_path))
    table = f"kline_{period}"
    try:
        db._ensure_period_table(period)
        db.conn.execute(f"ALTER TABLE {table} ALTER COLUMN volume TYPE BIGINT")
        db.conn.execute(f"INSERT INTO {table} (time, open, high, low, close, volume) "
                        "VALUES ('2026-09-14 09:30', 10, 11, 9, 10, 123)")
        frame = pd.DataFrame({"time": [pd.Timestamp("2026-09-15 09:30")],
                              "open": [10.0], "high": [11.0], "low": [9.0], "close": [10.0],
                              "volume": [123.45], "amount": [123450.0]})
        assert db.save_kline(frame, period, overwrite=False) == 1
        assert db.conn.execute(f"SELECT volume, amount FROM {table} ORDER BY time").fetchall() == [
            (123.0, None), (123.45, 123450.0),
        ]
        frame["time"] += pd.Timedelta(days=1)
        assert db.save_kline(frame, period, overwrite=False) == 1
        markers = dict(db.conn.execute("SELECT key, value FROM stock_info").fetchall())
        assert markers[f"{table}.legacy_units_unverified"] == "1"
        assert f"{table}.volume_unit" not in markers
        assert f"{table}.amount_unit" not in markers

        # 旧标记也不再阻断；保留它供以后核验，不虚标已统一。
        db.conn.execute("INSERT INTO stock_info (key, value) VALUES (?, 'shares')",
                        [f"{table}.volume_unit"])
        frame["time"] += pd.Timedelta(days=1)
        assert db.save_kline(frame, period, overwrite=False) == 1
        assert db.conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 4
        assert db.conn.execute("SELECT value FROM stock_info WHERE key=?", [f"{table}.volume_unit"]).fetchone() == ('shares',)
        # 兼容旧库不允许适配器遗漏新数据的单位转换。
        frame["time"] += pd.Timedelta(days=1)
        frame.attrs["volume_unit"] = "shares"
        with pytest.raises(VolumeUnitError, match="输入声明"):
            db.save_kline(frame, period, overwrite=False)
        assert db.conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 4
    finally:
        db.close()


@pytest.mark.parametrize('period', ['1d', '1m', '5m'])
@pytest.mark.parametrize('stock_code', ['600309.SH', '510300.SH'])
def test_share_evidence_and_old_markers_do_not_block_append_or_reopen(tmp_path, period, stock_code):
    db = StockDB(stock_code, str(tmp_path))
    table = f'kline_{period}'
    try:
        db._ensure_period_table(period)
        db.conn.execute(f"INSERT INTO {table} (time,open,high,low,close,volume,amount) VALUES ('2026-09-14 09:30',10,11,9,10,12345,123450)")
        frame = pd.DataFrame(dict(time=[pd.Timestamp('2026-09-15 09:30')],
                                  open=[10.], high=[11.], low=[9.], close=[10.],
                                  volume=[123.45], amount=[123450.]))
        assert db.save_kline(frame, period, overwrite=False) == 1
        assert db.conn.execute(f'SELECT volume,amount FROM {table} ORDER BY time').fetchall() == [(12345.,123450.),(123.45,123450.)]
        db.conn.execute("INSERT INTO stock_info (key,value) VALUES (?, 'shares'), (?, 'USD')", [table+'.volume_unit', table+'.amount_unit'])
        db.close()
        db = StockDB(stock_code, str(tmp_path))
        frame['time'] += pd.Timedelta(days=1)
        assert db.save_kline(frame, period, overwrite=False) == 1
        markers = dict(db.conn.execute('SELECT key,value FROM stock_info').fetchall())
        assert markers[table+'.legacy_units_unverified'] == '1'
        assert markers[table+'.volume_unit'] == 'shares'
        assert markers[table+'.amount_unit'] == 'USD'
        assert db.conn.execute(f'SELECT volume,amount FROM {table} ORDER BY time').fetchall() == [(12345.,123450.),(123.45,123450.),(123.45,123450.)]
    finally:
        db.close()


def test_legacy_compatibility_metadata_and_rows_roll_back_together(tmp_path):
    db = StockDB('600309.SH', str(tmp_path))
    try:
        db._ensure_period_table('1m')
        db.conn.execute('ALTER TABLE kline_1m ALTER COLUMN volume TYPE BIGINT')
        db.conn.execute("INSERT INTO kline_1m (time,volume,amount) VALUES ('2026-09-14 09:30',12345,123450)")
        frame = pd.DataFrame(dict(time=[pd.Timestamp('2026-09-15 09:30')],
                                  open=[10.], high=[11.], low=[9.], close=[10.],
                                  volume=[123.45], amount=[123450.]))
        db.conn.execute('BEGIN TRANSACTION')
        db.save_kline(frame, '1m', overwrite=False, manage_transaction=False)
        db.conn.execute('ROLLBACK')
        assert db.conn.execute('SELECT volume,amount FROM kline_1m').fetchall() == [(12345,123450.)]
        assert db.conn.execute("SELECT data_type FROM information_schema.columns WHERE table_name='kline_1m' AND column_name='volume'").fetchone() == ('BIGINT',)
        assert not db.conn.execute("SELECT value FROM stock_info WHERE key='kline_1m.legacy_units_unverified'").fetchone()
    finally:
        db.close()
