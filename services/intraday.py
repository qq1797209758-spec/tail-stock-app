"""真实分钟行情的统一获取、重试和东方财富熔断。"""

from __future__ import annotations

from datetime import date, datetime, timedelta
import logging
from threading import Lock
import time
from typing import Any

import akshare as ak
import pandas as pd
import requests
from requests import Response
from requests.exceptions import ConnectionError, Timeout

from services.network import call_with_proxy_fallback


logger = logging.getLogger(__name__)
EASTMONEY_URL = "https://push2his.eastmoney.com/api/qt/stock/trends2/get"
_SESSION = requests.Session()
_SESSION.trust_env = False
_SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
    "Referer": "https://quote.eastmoney.com/",
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
    "Connection": "keep-alive",
})
_EM_LOCK = Lock()
_EM_STATE: dict[str, Any] = {
    "failures": 0, "open_until": None, "probe_in_progress": False,
    "request_count": 0, "last_exception": "", "last_status": None,
}
_CIRCUIT_FAILURES = 2
_CIRCUIT_COOLDOWN = timedelta(minutes=5)


class IntradayDataError(RuntimeError):
    def __init__(self, reasons: list[str]):
        self.reasons = reasons
        super().__init__("分钟行情源全部不可用：" + "；".join(reasons))


def normalize_symbol(symbol: str) -> tuple[str, str, str]:
    code = str(symbol).strip().lower().replace("sh", "").replace("sz", "").zfill(6)
    if code.startswith("60"):
        return code, "sh" + code, "1." + code
    if code.startswith("00"):
        return code, "sz" + code, "0." + code
    raise IntradayDataError([f"股票代码或市场参数错误：{symbol!r}（仅支持00/60主板）"])


def _circuit_permission(now: datetime) -> tuple[bool, str]:
    with _EM_LOCK:
        until = _EM_STATE["open_until"]
        if until is None:
            return True, "关闭"
        if now < until:
            return False, f"开启至{until:%Y-%m-%d %H:%M:%S}"
        if _EM_STATE["probe_in_progress"]:
            return False, "半开探测中"
        _EM_STATE["probe_in_progress"] = True
        return True, "半开探测"


def _record_em_result(success: bool, error: str = "", status: int | None = None) -> None:
    with _EM_LOCK:
        _EM_STATE["request_count"] += 1
        _EM_STATE["last_status"] = status
        _EM_STATE["probe_in_progress"] = False
        if success:
            _EM_STATE.update(failures=0, open_until=None, last_exception="")
            return
        _EM_STATE["failures"] += 1
        _EM_STATE["last_exception"] = error
        if _EM_STATE["failures"] >= _CIRCUIT_FAILURES:
            _EM_STATE["open_until"] = datetime.now() + _CIRCUIT_COOLDOWN


def get_eastmoney_circuit_status() -> dict[str, Any]:
    with _EM_LOCK:
        state = dict(_EM_STATE)
    until = state["open_until"]
    state["status"] = "开启" if until and datetime.now() < until else ("半开" if until else "关闭")
    state["open_until"] = until.isoformat(timespec="seconds") if until else ""
    return state


def reset_eastmoney_circuit() -> None:
    with _EM_LOCK:
        _EM_STATE.update(failures=0, open_until=None, probe_in_progress=False, last_exception="", last_status=None)


def _request_eastmoney(params: dict[str, str]) -> Response:
    last_error: BaseException | None = None
    for attempt in range(4):  # 首次请求 + 最多3次重试，退避1/2/4秒
        try:
            response = _SESSION.get(EASTMONEY_URL, params=params, timeout=(5, 15))
            retryable = response.status_code == 429 or response.status_code >= 500
            logger.info(
                "minute_http source=eastmoney url=%s status=%s retry=%s bytes=%s",
                response.url, response.status_code, attempt, len(response.content),
            )
            if not retryable:
                response.raise_for_status()
                return response
            last_error = requests.HTTPError(f"HTTP {response.status_code}", response=response)
        except (ConnectionError, Timeout, requests.HTTPError) as error:
            last_error = error
            status = getattr(getattr(error, "response", None), "status_code", None)
            logger.warning(
                "minute_http source=eastmoney url=%s status=%s retry=%s bytes=0 error=%s: %s",
                EASTMONEY_URL, status, attempt, type(error).__name__, error,
            )
            if isinstance(error, requests.HTTPError) and status not in (429,) and (status or 0) < 500:
                break
        if attempt < 3:
            time.sleep(2 ** attempt)
    assert last_error is not None
    raise last_error


def _eastmoney_minutes(code: str, secid: str, trade_date: date) -> tuple[pd.DataFrame, dict[str, Any]]:
    allowed, circuit = _circuit_permission(datetime.now())
    params = {
        "secid": secid, "fields1": "f1,f2,f3,f4,f5,f6,f7,f8",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
        "iscr": "0", "ndays": "1",
    }
    diagnostic = {"source": "东方财富", "url": EASTMONEY_URL, "parameters": params, "circuit": circuit}
    if not allowed:
        raise IntradayDataError([f"东方财富熔断中（{circuit}），已直接切换备用源"])
    try:
        response = _request_eastmoney(params)
        payload = response.json()
        trends = ((payload.get("data") or {}).get("trends") or [])
        rows = [item.split(",") for item in trends]
        frame = pd.DataFrame(rows, columns=["datetime", "open", "close", "high", "low", "volume", "amount", "average"])
        frame = frame.loc[pd.to_datetime(frame["datetime"], errors="coerce").dt.date.eq(trade_date)].copy()
        if frame.empty:
            raise ValueError("返回数据为空")
        frame["volume"] = pd.to_numeric(frame["volume"], errors="coerce") * 100
        _record_em_result(True, status=response.status_code)
        diagnostic.update(status=response.status_code, response_length=len(response.content), rows=len(frame), columns=list(frame.columns))
        return frame, diagnostic
    except Exception as error:
        status = getattr(getattr(error, "response", None), "status_code", None)
        detail = f"{type(error).__name__}: {error}"
        _record_em_result(False, detail, status)
        diagnostic.update(status=status, response_length=0, rows=0, exception=detail)
        raise IntradayDataError([f"东方财富：{detail}"])


def _sina_minutes(market_symbol: str, trade_date: date) -> tuple[pd.DataFrame, dict[str, Any]]:
    params = {"symbol": market_symbol, "period": "1", "adjust": ""}
    try:
        raw = call_with_proxy_fallback(lambda: ak.stock_zh_a_minute(**params))
        columns = list(raw.columns) if isinstance(raw, pd.DataFrame) else []
        if not isinstance(raw, pd.DataFrame) or raw.empty:
            raise ValueError("返回数据为空")
        frame = raw.rename(columns={"day": "datetime"}).copy()
        frame["datetime"] = pd.to_datetime(frame["datetime"], errors="coerce")
        frame = frame.loc[frame["datetime"].dt.date.eq(trade_date)].copy()
        if frame.empty:
            raise ValueError("当日分钟数据为空")
        return frame, {"source": "新浪", "parameters": params, "status": "AKShare未暴露", "response_length": None, "rows": len(frame), "columns": columns}
    except Exception as error:
        raise IntradayDataError([f"新浪：{type(error).__name__}: {error}"])


def get_intraday_minutes(symbol: str, trade_date: date) -> pd.DataFrame:
    """按东方财富→新浪顺序返回统一分钟字段；失败证据保存在 attrs。"""
    code, market_symbol, secid = normalize_symbol(symbol)
    errors: list[str] = []
    attempts: list[dict[str, Any]] = []
    for fetcher, args in ((_eastmoney_minutes, (code, secid, trade_date)), (_sina_minutes, (market_symbol, trade_date))):
        try:
            frame, diagnostic = fetcher(*args)
            attempts.append(diagnostic)
            required = ["datetime", "open", "high", "low", "close", "volume", "amount"]
            missing = [field for field in required if field not in frame.columns]
            if missing:
                raise IntradayDataError([f"{diagnostic['source']}：字段缺失 {missing}"])
            result = frame[required].copy()
            for column in required[1:]:
                result[column] = pd.to_numeric(result[column], errors="coerce")
            result["source"] = diagnostic["source"]
            result.attrs.update(source=diagnostic["source"], attempts=attempts, errors=errors, symbol=code, market_symbol=market_symbol, secid=secid)
            logger.info("minute_result code=%s market=%s source=%s rows=%s first=%s last=%s fields=%s errors=%s", code, market_symbol, diagnostic["source"], len(result), result["datetime"].iloc[0], result["datetime"].iloc[-1], list(result.columns), errors)
            return result
        except IntradayDataError as error:
            errors.extend(error.reasons)
            attempts.append({"source": "东方财富" if fetcher is _eastmoney_minutes else "新浪", "exception": str(error)})
    raise IntradayDataError(errors)
