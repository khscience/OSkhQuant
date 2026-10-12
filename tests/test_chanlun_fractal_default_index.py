# -*- coding: utf-8 -*-
"""分型判断默认看最新三根K线（中间是倒数第二根），不能因为默认 index=-1 永远返回 False。"""
import pandas as pd

from khChanLunTools import is_bottom_fractal, is_top_fractal


def test_default_index_detects_latest_bottom_fractal():
    k = pd.DataFrame({"high": [12, 11, 10, 11], "low": [10, 9, 8, 9]})
    assert is_bottom_fractal(k) is True
    assert is_bottom_fractal(k, index=-2) is True
    assert is_bottom_fractal(k, index=-1) is False


def test_default_index_detects_latest_top_fractal():
    k = pd.DataFrame({"high": [10, 11, 12, 11], "low": [8, 9, 10, 9]})
    assert is_top_fractal(k) is True
