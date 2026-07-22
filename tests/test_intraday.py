from datetime import date, datetime, timedelta

import pandas as pd
import pytest
import requests

from services import intraday


class FakeResponse:
    status_code = 200
    content = b"payload"
    url = intraday.EASTMONEY_URL + "?secid=0.000001"

    def raise_for_status(self):
        return None

    def json(self):
        return {"data": {"trends": ["2026-07-22 14:30,10,10.1,10.2,9.9,12,12120,10.1"]}}


def test_symbol_market_mapping_keeps_leading_zero():
    assert intraday.normalize_symbol("1") == ("000001", "sz000001", "0.000001")
    assert intraday.normalize_symbol("600000") == ("600000", "sh600000", "1.600000")


def test_eastmoney_success_returns_unified_columns(monkeypatch):
    intraday.reset_eastmoney_circuit()
    monkeypatch.setattr(intraday, "_request_eastmoney", lambda params: FakeResponse())
    frame, diagnostic = intraday._eastmoney_minutes("000001", "0.000001", date(2026, 7, 22))
    assert list(frame.columns) == ["datetime", "open", "close", "high", "low", "volume", "amount", "average"]
    assert frame.iloc[0]["volume"] == 1200
    assert diagnostic["status"] == 200


def test_request_retries_connection_error_with_exponential_backoff(monkeypatch):
    calls = []
    sleeps = []

    def fail(*args, **kwargs):
        calls.append(1)
        raise requests.ConnectionError("disconnect")

    monkeypatch.setattr(intraday._SESSION, "get", fail)
    monkeypatch.setattr(intraday.time, "sleep", sleeps.append)
    with pytest.raises(requests.ConnectionError):
        intraday._request_eastmoney({"secid": "0.000001"})
    assert len(calls) == 4
    assert sleeps == [1, 2, 4]


def test_fallback_to_sina_and_open_circuit_after_two_failures(monkeypatch):
    intraday.reset_eastmoney_circuit()
    monkeypatch.setattr(intraday, "_request_eastmoney", lambda params: (_ for _ in ()).throw(requests.ConnectionError("disconnect")))
    sample = pd.DataFrame({
        "day": ["2026-07-22 14:30"], "open": [10], "high": [10.2],
        "low": [9.9], "close": [10.1], "volume": [1000], "amount": [10100],
    })
    monkeypatch.setattr(intraday.ak, "stock_zh_a_minute", lambda **kwargs: sample)
    assert intraday.get_intraday_minutes("000001", date(2026, 7, 22)).attrs["source"] == "新浪"
    assert intraday.get_intraday_minutes("600000", date(2026, 7, 22)).attrs["source"] == "新浪"
    state = intraday.get_eastmoney_circuit_status()
    assert state["status"] == "开启"
    assert state["failures"] == 2


def test_all_sources_fail_reports_each_reason(monkeypatch):
    intraday.reset_eastmoney_circuit()
    monkeypatch.setattr(intraday, "_request_eastmoney", lambda params: (_ for _ in ()).throw(requests.Timeout("slow")))
    monkeypatch.setattr(intraday.ak, "stock_zh_a_minute", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("sina down")))
    with pytest.raises(intraday.IntradayDataError) as caught:
        intraday.get_intraday_minutes("000001", date(2026, 7, 22))
    message = str(caught.value)
    assert "分钟行情源全部不可用" in message
    assert "东方财富" in message and "新浪" in message
