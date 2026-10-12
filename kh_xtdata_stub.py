# coding: utf-8
"""xtdata 占位对象：开源版不含 miniQMT / xtquant。

V2.1 时代的策略可能直接调用 ``xtdata.get_market_data_ex`` 等接口。开源版
只读本地 DuckDB，这里返回一个占位对象：导入不报错，真正调用任何属性时
给出明确的中文提示，而不是晦涩的 AttributeError。
"""


class XtdataUnavailableError(RuntimeError):
    """在开源版中调用 xtdata 时抛出。"""


class _XtdataStub:
    def __getattr__(self, name):
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        raise XtdataUnavailableError(
            f"看海量化开源版不支持 xtdata（调用了 xtdata.{name}）。"
            "请改用 khHistory / khKline / khPrice 读取本地 DuckDB 数据，"
            "数据可在「数据管理」里用 baostock 或 tushare 下载。"
        )

    def __bool__(self):
        # 旧代码里常见 `if xtdata:` 判断是否可用，这里返回 False
        return False

    def __repr__(self):
        return "<xtdata 在开源版中不可用>"


xtdata = _XtdataStub()

__all__ = ["xtdata", "XtdataUnavailableError"]
