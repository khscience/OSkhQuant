"""增量优化必须保留字段、事务和单位约束，重复写入应真正不落盘。"""
import pandas as pd
import pytest

from duckdb_storage.stock_db import StockDB
from duckdb_storage.units import VolumeUnitError


def bars(times):
    return pd.DataFrame({
        'time': pd.to_datetime(times), 'open': 10., 'high': 11., 'low': 9.,
        'close': 10.5, 'volume': 123.45, 'amount': 123450.,
        'open_front': 8., 'open_back': 12.,
    })


@pytest.mark.parametrize('period', ['1d', '1m', '5m'])
def test_repeat_preserves_entire_database_bytes_and_unit_timestamps(tmp_path, period):
    db = StockDB('600000.SH', str(tmp_path))
    frame = bars(['2026-09-14 09:30', '2026-09-15 09:30'])
    try:
        db.save_kline(frame, period)
        db.conn.execute(f'ALTER TABLE kline_{period} ADD COLUMN custom_signal DOUBLE DEFAULT 1.25')
        markers = db.conn.execute('SELECT * FROM stock_info ORDER BY key').fetchall()
        db.close()
        from pathlib import Path
        before = Path(db.db_path).read_bytes()
        db = StockDB('600000.SH', str(tmp_path))
        # 不同的非空新值也不得覆盖已有值；全空来源列不能擦除已有复权列。
        incoming = frame.copy()
        incoming['open'] = 10.2
        incoming['open_back'] = float('nan')
        db.save_kline(incoming, period, overwrite=False, merge_missing=True)
        assert db.conn.execute('SELECT * FROM stock_info ORDER BY key').fetchall() == markers
        db.close()
        assert Path(db.db_path).read_bytes() == before
    finally:
        db.close(skip_checkpoint=True)


@pytest.mark.parametrize('period', ['1d', '1m', '5m'])
def test_merge_fills_only_nulls_appends_and_preserves_custom_columns(tmp_path, period):
    db = StockDB('600000.SH', str(tmp_path))
    table = f'kline_{period}'
    try:
        original = bars(['2026-09-14 09:30', '2026-09-15 09:30'])
        db.save_kline(original, period)
        db.conn.execute(f'ALTER TABLE {table} ADD COLUMN custom_signal DOUBLE')
        db.conn.execute(f'UPDATE {table} SET custom_signal=42, open_front=NULL')
        before_times = db.conn.execute(f'SELECT update_time FROM {table} ORDER BY time').fetchall()
        incoming = bars(['2026-09-15 09:30', '2026-09-16 09:30'])
        incoming['open_front'] = [8.1, 8.2]
        incoming['open'] = 10.2
        assert db.save_kline(incoming, period, overwrite=False, merge_missing=True) == 2
        rows = db.conn.execute(f'SELECT open, open_front, open_back, volume, amount, custom_signal FROM {table} ORDER BY time').fetchall()
        assert rows == [
            (10., None, 12., 123.45, 123450., 42.),
            (10., 8.1, 12., 123.45, 123450., 42.),
            (10.2, 8.2, 12., 123.45, 123450., None),
        ]
        assert db.conn.execute(f'SELECT update_time FROM {table} ORDER BY time LIMIT 2').fetchall() == before_times
    finally:
        db.close(skip_checkpoint=True)


def test_insert_failure_rolls_back_preceding_null_fills(tmp_path):
    db = StockDB('600000.SH', str(tmp_path))
    try:
        db.save_kline(bars(['2026-09-14 09:30']), '1m')
        # 扩展列无默认值，新行插入会失败；前面的补空 UPDATE 必须一起回滚。
        db.conn.execute('ALTER TABLE kline_1m ADD COLUMN custom_required INTEGER')
        db.conn.execute('UPDATE kline_1m SET custom_required=1, open_front=NULL')
        db.conn.execute('ALTER TABLE kline_1m ALTER COLUMN custom_required SET NOT NULL')
        before = db.conn.execute('SELECT * FROM kline_1m').fetchall()
        incoming = bars(['2026-09-14 09:30', '2026-09-15 09:30'])
        with pytest.raises(Exception, match='NOT NULL'):
            db.save_kline(incoming, '1m', overwrite=False, merge_missing=True)
        assert db.conn.execute('SELECT * FROM kline_1m').fetchall() == before
    finally:
        db.close(skip_checkpoint=True)


def test_noop_still_rejects_conflicting_unit_marker(tmp_path):
    db = StockDB('600000.SH', str(tmp_path))
    try:
        frame = bars(['2026-09-14 09:30'])
        db.save_kline(frame, '1m')
        db.conn.execute("UPDATE stock_info SET value='shares' WHERE key='kline_1m.volume_unit'")
        before = db.conn.execute('SELECT * FROM kline_1m').fetchall()
        with pytest.raises(VolumeUnitError):
            db.save_kline(frame, '1m', overwrite=False, merge_missing=True)
        assert db.conn.execute('SELECT * FROM kline_1m').fetchall() == before
        assert db.conn.execute("SELECT value FROM stock_info WHERE key='kline_1m.volume_unit'").fetchone() == ('shares',)
    finally:
        db.close(skip_checkpoint=True)


@pytest.mark.parametrize('period', ['1d', '1m', '5m'])
def test_empty_range_append_keeps_history_constraints_and_units(tmp_path, period):
    db = StockDB('688001.SH', str(tmp_path))
    table = f'kline_{period}'
    try:
        db.save_kline(bars(['2026-09-14 09:30']), period)
        db.conn.execute(f'ALTER TABLE {table} ADD COLUMN custom_signal DOUBLE DEFAULT 7.25')
        original = db.conn.execute(f'SELECT * FROM {table}').fetchall()
        incoming = bars(['2026-09-17 09:30', '2026-09-18 09:30'])
        assert db.save_kline(incoming, period, overwrite=False, merge_missing=True) == 2
        assert db.conn.execute(f"SELECT * FROM {table} WHERE time < '2026-09-15'").fetchall() == original
        assert db.conn.execute(f'SELECT count(*), min(volume), max(custom_signal) FROM {table}').fetchone() == (3, 123.45, 7.25)
        assert db.conn.execute('SELECT value FROM stock_info WHERE key=?', [table+'.volume_unit']).fetchone() == ('lots',)
        with pytest.raises(Exception, match='Duplicate key'):
            db.conn.execute(f"INSERT INTO {table} (time) VALUES ('2026-09-17 09:30')")
        db.close()
        reopened = StockDB('688001.SH', str(tmp_path), read_only=True)
        try:
            assert reopened.conn.execute(f'SELECT count(*) FROM {table}').fetchone() == (3,)
        finally:
            reopened.close()
    finally:
        db.close(skip_checkpoint=True)


def test_empty_range_still_rejects_unit_conflict_without_appending(tmp_path):
    db = StockDB('600000.SH', str(tmp_path))
    try:
        db.save_kline(bars(['2026-09-14 09:30']), '1m')
        db.conn.execute("UPDATE stock_info SET value='shares' WHERE key='kline_1m.volume_unit'")
        before = db.conn.execute('SELECT * FROM kline_1m').fetchall()
        with pytest.raises(VolumeUnitError):
            db.save_kline(bars(['2026-09-17 09:30']), '1m', overwrite=False, merge_missing=True)
        assert db.conn.execute('SELECT * FROM kline_1m').fetchall() == before
    finally:
        db.close(skip_checkpoint=True)
