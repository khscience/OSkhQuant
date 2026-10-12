"""BaoStock 下载进程登录遇到网络类错误时先退避重试，不马上判整批失败。

服务器限流时会直接断开登录连接；超时补丁让登录很快返回“网络接收错误”，
而主进程把登录失败当致命错误、让整批任务失败（沙盒外长回测补数时 63 秒内
206 个任务全部失败）。
"""
from types import SimpleNamespace

from duckdb_storage import baostock_import_worker as worker


class _FakeBaoStock:
    def __init__(self, results):
        self.results = list(results)
        self.calls = 0

    def login(self):
        self.calls += 1
        code, msg = self.results.pop(0)
        return SimpleNamespace(error_code=code, error_msg=msg)


class _Queue:
    def __init__(self):
        self.items = []

    def put(self, item):
        self.items.append(item)


def test_network_login_error_retries_with_backoff_then_succeeds():
    bs = _FakeBaoStock([("10002007", "网络接收错误。"), ("10002004", "网络接收时连接断开"), ("0", "success")])
    slept, queue = [], _Queue()
    result = worker._login_with_backoff(bs, queue, delays=(10.0, 30.0, 60.0), sleep=slept.append)
    assert result.error_code == "0"
    assert slept == [10.0, 30.0] and bs.calls == 3
    assert all(item["type"] == "status" for item in queue.items) and len(queue.items) == 2


def test_non_network_login_error_is_returned_immediately():
    bs = _FakeBaoStock([("10001001", "用户名或密码错误")])
    slept = []
    result = worker._login_with_backoff(bs, _Queue(), delays=(10.0,), sleep=slept.append)
    assert result.error_code == "10001001" and slept == [] and bs.calls == 1


def test_gives_up_after_all_delays():
    bs = _FakeBaoStock([("10002007", "网络接收错误。")] * 4)
    slept = []
    result = worker._login_with_backoff(bs, _Queue(), delays=(1.0, 2.0, 3.0), sleep=slept.append)
    assert result.error_code == "10002007" and slept == [1.0, 2.0, 3.0] and bs.calls == 4


def test_default_backoff_stays_under_small_batch_queue_timeout():
    # 小批量任务排队超时是 timeout_per_task × 2：K 线默认 240 秒、指标下载 120 秒，
    # 退避总时长要小于它，否则排队的任务会先被判超时
    assert sum(worker._LOGIN_RETRY_DELAYS) < 120
