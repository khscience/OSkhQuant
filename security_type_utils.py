# coding: utf-8
"""Lightweight security-code classification shared by GUI, CLI and importers."""

from __future__ import annotations


def split_security_code(stock_code: str) -> tuple[str, str]:
    """Return ``(six_digit_code, market)`` for suffix or prefix notation."""
    raw = str(stock_code or "").strip().upper()
    if not raw:
        return "", ""
    if "." in raw:
        left, right = raw.split(".", 1)
        if left in {"SH", "SZ", "BJ"}:
            return right, left
        if right in {"SH", "SZ", "BJ"}:
            return left, right
    return raw, ""


def is_etf_code(stock_code: str) -> bool:
    """Return whether a code is an ETF, explicitly excluding LOF securities."""
    code, market = split_security_code(stock_code)
    if not code:
        return False
    if market == "SH":
        return code.startswith(("51", "52", "53", "55", "56", "58"))
    if market == "SZ":
        return code.startswith(("158", "159"))
    return code.startswith(("51", "52", "53", "55", "56", "58", "158", "159"))


def is_listed_fund_code(stock_code: str) -> bool:
    """Return whether a code is a listed fund (ETF, LOF, REIT or closed fund).

    Shanghai listed funds use the ``5xxxxx`` family. Shenzhen uses ``15xxxx``
    for ETFs/legacy funds, ``16xxxx``/``17xxxx`` for LOFs, and selected
    ``18xxxx`` ranges for infrastructure or closed-end funds.
    """
    code, market = split_security_code(stock_code)
    if not code:
        return False
    sh_fund = code.startswith("5")
    sz_fund = code.startswith(("15", "16", "17", "180", "184"))
    if market == "SH":
        return sh_fund
    if market == "SZ":
        return sz_fund
    if market == "BJ":
        return False
    return sh_fund or sz_fund
