"""下载进程在结果送达前被结束时，停在「数据已就绪」的任务要能被发现并重试。

长回测补 5 分钟线时实测卡死：主进程为同一进程上一个任务的网络错误换进程重登，
把它结束时它已经领了下一个任务、报了 data_ready，结果随进程丢失；这个阶段不受
运行超时约束，调度线程一直在等。
"""
import time

from duckdb_storage import baostock_import_worker as worker


class _FakeProcess:
    def __init__(self, pid, alive):
        self.pid = pid
        self._alive = alive

    def is_alive(self):
        return self._alive


def _importer(tasks, workers):
    imp = worker.MultiProcessBaoStockImporter(num_workers=1, timeout_per_task=120.0, max_task_retries=2)
    imp._is_running = True
    imp.workers = list(workers)
    now = time.monotonic()
    for task_id, (stage, pid, age) in tasks.items():
        imp._task_meta[task_id] = {
            "payload": {"stock_code": f"00000{task_id}.SZ", "period": "5m", "task_kind": "kline"},
            "submitted_at": now - age, "started_at": now - age, "stage_at": now - age,
            "pid": pid, "stage": stage, "attempt": 0, "retry_at": None,
        }
    return imp


def test_result_lost_with_dead_worker_is_retried_after_grace():
    imp = _importer({1: ("data_ready", 111, 60.0), 2: ("result_sent", 111, 60.0)}, workers=[])
    imp._check_worker_health_and_timeouts()
    lost = {r["task_id"]: r for r in imp._synthetic_results}
    assert set(lost) == {1, 2}
    assert all(r["error_type"] == "worker_exit" and not r["success"] for r in lost.values())
    assert imp._task_meta[1]["stage"] == "failed_pending_delivery"


def test_live_worker_or_within_grace_is_left_alone():
    imp = _importer(
        {1: ("data_ready", 222, 600.0), 2: ("data_ready", 111, 5.0)},
        workers=[_FakeProcess(222, alive=True)],
    )
    imp._check_worker_health_and_timeouts()
    assert list(imp._synthetic_results) == []
    assert imp._task_meta[1]["stage"] == "data_ready" and imp._task_meta[2]["stage"] == "data_ready"


def test_lost_result_is_classified_as_retryable():
    from duckdb_storage.baostock_import_worker import _is_transient_data_source_error

    assert _is_transient_data_source_error("工作进程在结果送达前退出（PID 111）", "worker_exit")
