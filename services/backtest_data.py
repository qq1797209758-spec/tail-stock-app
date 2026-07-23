"""历史回测专用数据层：只读取目标日期及此前可得数据。"""

from __future__ import annotations

from datetime import date, datetime
import re
import time
from typing import Any, Iterable

import akshare as ak
import pandas as pd
import streamlit as st

from config import (
    BACKTEST_BASIC_BATCH_SIZE,
    BACKTEST_DAILY_BATCH_SIZE,
    BACKTEST_DATA_CACHE_TTL,
    BACKTEST_REQUEST_INTERVAL_SECONDS,
)
from services.ifind_client import IFindConnectionError, post_authenticated_json
from services.network import call_with_proxy_fallback


IFIND_DATA_POOL_URL = "https://quantapi.51ifind.com/api/v1/data_pool"
IFIND_HISTORY_URL = "https://quantapi.51ifind.com/api/v1/cmd_history_quotation"
IFIND_BASIC_DATA_URL = "https://quantapi.51ifind.com/api/v1/basic_data_service"
IFIND_HIGH_FREQUENCY_URL = "https://quantapi.51ifind.com/api/v1/high_frequency"
ALL_A_SHARE_BLOCK = "001005010"
_CODE_PATTERN = re.compile(r"^(\d{6})\.(SH|SZ)$", re.IGNORECASE)


class BacktestDataError(RuntimeError):
    """历史回测数据无法可靠取得。"""


def _chunks(values: tuple[str, ...], size: int) -> Iterable[tuple[str, ...]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _at(value: object, index: int) -> object:
    if isinstance(value, list):
        return value[index] if index < len(value) else None
    return value


def _table_records(payload: dict[str, Any]) -> list[dict[str, object]]:
    tables = payload.get("tables")
    if not isinstance(tables, list):
        return []
    records: list[dict[str, object]] = []
    for item in tables:
        if not isinstance(item, dict):
            continue
        table = item.get("table")
        if not isinstance(table, dict):
            continue
        code_value = item.get("thscode", item.get("THSCODE"))
        time_value = item.get("time")
        lengths = [len(value) for value in table.values() if isinstance(value, list)]
        if isinstance(code_value, list):
            lengths.append(len(code_value))
        if isinstance(time_value, list):
            lengths.append(len(time_value))
        row_count = max(lengths or [1])
        for index in range(row_count):
            row = {str(key): _at(value, index) for key, value in table.items()}
            row.setdefault("thscode", _at(code_value, index))
            row.setdefault("time", _at(time_value, index))
            records.append(row)
    return records


def _find_code(row: dict[str, object]) -> str | None:
    preferred = ("thscode", "THSCODE", "p03291_f002", "code", "证券代码")
    for key in (*preferred, *row.keys()):
        value = row.get(key)
        if value is None:
            continue
        match = _CODE_PATTERN.match(str(value).strip())
        if match:
            return f"{match.group(1)}.{match.group(2).upper()}"
    return None


def _find_name(row: dict[str, object], code: str) -> str:
    preferred = (
        "security_name", "SECURITY_NAME", "secName", "name",
        "证券名称", "p03291_f003", "p03291_f004",
    )
    for key in (*preferred, *row.keys()):
        value = row.get(key)
        text = str(value or "").strip()
        if not text or text == code or _CODE_PATTERN.match(text):
            continue
        lowered = str(key).lower()
        if key in preferred or "name" in lowered or "简称" in str(key):
            return text
    return ""


@st.cache_data(ttl=BACKTEST_DATA_CACHE_TTL, show_spinner=False)
def fetch_trade_calendar() -> pd.DatetimeIndex:
    try:
        calendar = call_with_proxy_fallback(ak.tool_trade_date_hist_sina)
    except Exception as error:
        raise BacktestDataError(
            f"交易日历请求失败（{type(error).__name__}）"
        ) from error
    if not isinstance(calendar, pd.DataFrame) or calendar.empty:
        raise BacktestDataError("交易日历为空")
    column = "trade_date" if "trade_date" in calendar else calendar.columns[0]
    dates = pd.to_datetime(calendar[column], errors="coerce").dropna().drop_duplicates()
    return pd.DatetimeIndex(dates.sort_values())


@st.cache_data(ttl=BACKTEST_DATA_CACHE_TTL, show_spinner=False)
def fetch_historical_universe(selection_date: str) -> pd.DataFrame:
    """获取筛选日当日的全A股票池，避免使用今天的代码列表。"""
    date_value = pd.Timestamp(selection_date).strftime("%Y%m%d")
    payloads = [
        {
            "reportname": "block",
            "functionpara": {"date": date_value, "blockname": ALL_A_SHARE_BLOCK},
            "outputpara": "date:Y,thscode:Y,security_name:Y",
        },
        {
            "reportname": "p03291",
            "functionpara": {
                "date": date_value,
                "blockname": ALL_A_SHARE_BLOCK,
                "iv_type": "allcontract",
            },
            "outputpara": "p03291_f001:Y,p03291_f002:Y,p03291_f003:Y,p03291_f004:Y",
        },
    ]
    last_error: Exception | None = None
    for payload in payloads:
        try:
            response = post_authenticated_json(
                IFIND_DATA_POOL_URL, payload, operation="历史股票池"
            )
            rows = _table_records(response)
            records = []
            for row in rows:
                code = _find_code(row)
                if code:
                    records.append({"数据源代码": code, "名称": _find_name(row, code)})
            result = pd.DataFrame(records).drop_duplicates("数据源代码")
            if not result.empty:
                result["代码"] = result["数据源代码"].str[:6]
                return result.reset_index(drop=True)
        except IFindConnectionError as error:
            last_error = error
    raise BacktestDataError(
        f"无法取得 {selection_date} 的历史股票池：{last_error or '响应无有效代码'}"
    )


def _column(row: dict[str, object], name: str) -> object:
    for key, value in row.items():
        if str(key).lower() == name.lower():
            return value
    return None


@st.cache_data(ttl=BACKTEST_DATA_CACHE_TTL, show_spinner=False)
def fetch_daily_history(
    codes: tuple[str, ...], start_date: str, end_date: str
) -> pd.DataFrame:
    """批量获取不复权日线，范围明确截止到次日评价日。"""
    frames: list[pd.DataFrame] = []
    for batch_index, batch in enumerate(_chunks(codes, BACKTEST_DAILY_BATCH_SIZE)):
        payload = {
            "codes": ",".join(batch),
            "indicators": "open,high,low,close,volume,amount,changeRatio,turnoverRatio",
            "startdate": pd.Timestamp(start_date).strftime("%Y-%m-%d"),
            "enddate": pd.Timestamp(end_date).strftime("%Y-%m-%d"),
            "functionpara": {"Fill": "Blank"},
        }
        response = post_authenticated_json(
            IFIND_HISTORY_URL, payload, operation="历史日线"
        )
        records = []
        for row in _table_records(response):
            code = _find_code(row)
            trade_time = _column(row, "time")
            if not code or trade_time is None:
                continue
            records.append(
                {
                    "数据源代码": code,
                    "日期": trade_time,
                    "开盘": _column(row, "open"),
                    "最高": _column(row, "high"),
                    "最低": _column(row, "low"),
                    "收盘": _column(row, "close"),
                    "成交量": _column(row, "volume"),
                    "成交额": _column(row, "amount"),
                    "涨跌幅": _column(row, "changeRatio"),
                    "换手率": _column(row, "turnoverRatio"),
                }
            )
        if records:
            frames.append(pd.DataFrame(records))
        if batch_index + 1 < (len(codes) + BACKTEST_DAILY_BATCH_SIZE - 1) // BACKTEST_DAILY_BATCH_SIZE:
            time.sleep(BACKTEST_REQUEST_INTERVAL_SECONDS)
    if not frames:
        raise BacktestDataError("历史日线响应中没有有效记录")
    result = pd.concat(frames, ignore_index=True)
    result["日期"] = pd.to_datetime(result["日期"], errors="coerce").dt.normalize()
    for column in ("开盘", "最高", "最低", "收盘", "成交量", "成交额", "涨跌幅", "换手率"):
        result[column] = pd.to_numeric(result[column], errors="coerce")
    return result.dropna(subset=["数据源代码", "日期"]).sort_values(
        ["数据源代码", "日期"]
    ).reset_index(drop=True)


@st.cache_data(ttl=BACKTEST_DATA_CACHE_TTL, show_spinner=False)
def fetch_total_shares(codes: tuple[str, ...], selection_date: str) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    date_value = pd.Timestamp(selection_date).strftime("%Y%m%d")
    for batch_index, batch in enumerate(_chunks(codes, BACKTEST_BASIC_BATCH_SIZE)):
        response = post_authenticated_json(
            IFIND_BASIC_DATA_URL,
            {
                "codes": ",".join(batch),
                "indipara": [
                    {
                        "indicator": "ths_total_shares_stock",
                        "indiparams": [date_value],
                    }
                ],
            },
            operation="历史总股本",
        )
        records = []
        for row in _table_records(response):
            code = _find_code(row)
            shares = _column(row, "ths_total_shares_stock")
            if code:
                records.append({"数据源代码": code, "总股本": shares})
        if records:
            frames.append(pd.DataFrame(records))
        if batch_index + 1 < (len(codes) + BACKTEST_BASIC_BATCH_SIZE - 1) // BACKTEST_BASIC_BATCH_SIZE:
            time.sleep(BACKTEST_REQUEST_INTERVAL_SECONDS)
    if not frames:
        return pd.DataFrame(columns=["数据源代码", "总股本"])
    result = pd.concat(frames, ignore_index=True).drop_duplicates("数据源代码")
    result["总股本"] = pd.to_numeric(result["总股本"], errors="coerce")
    return result


@st.cache_data(ttl=BACKTEST_DATA_CACHE_TTL, show_spinner=False)
def fetch_historical_minutes(
    code: str, start_time: str, end_time: str
) -> pd.DataFrame:
    response = post_authenticated_json(
        IFIND_HIGH_FREQUENCY_URL,
        {
            "codes": code,
            "indicators": "open,high,low,close,volume,amount",
            "starttime": start_time,
            "endtime": end_time,
            "functionpara": {
                "CPS": "0", "Fill": "Blank", "Interval": "1",
                "Timeformat": "LocalTime",
            },
        },
        operation="历史分钟行情",
    )
    records = []
    for row in _table_records(response):
        row_code = _find_code(row) or code
        records.append(
            {
                "数据源代码": row_code,
                "时间": _column(row, "time"),
                "开盘": _column(row, "open"),
                "最高": _column(row, "high"),
                "最低": _column(row, "low"),
                "收盘": _column(row, "close"),
                "成交量": _column(row, "volume"),
                "成交额": _column(row, "amount"),
            }
        )
    data = pd.DataFrame(records)
    if data.empty:
        raise BacktestDataError("历史分钟行情为空")
    data["时间"] = pd.to_datetime(data["时间"], errors="coerce")
    for column in ("开盘", "最高", "最低", "收盘", "成交量", "成交额"):
        data[column] = pd.to_numeric(data[column], errors="coerce")
    return data.dropna(subset=["时间"]).sort_values("时间").reset_index(drop=True)


def completed_backtest_dates(
    start_date: date,
    end_date: date,
    *,
    max_days: int,
    now: datetime,
) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    """返回筛选日及其已完成的下一交易日，确保收益数据完整。"""
    calendar = fetch_trade_calendar()
    today = pd.Timestamp(now.date())
    today_completed = now.time() >= datetime.strptime("15:05:00", "%H:%M:%S").time()
    last_complete = today if today_completed else today - pd.Timedelta(days=1)
    pairs: list[tuple[pd.Timestamp, pd.Timestamp]] = []
    for index in range(len(calendar) - 1):
        selection = calendar[index].normalize()
        next_day = calendar[index + 1].normalize()
        if selection.date() < start_date or selection.date() > end_date:
            continue
        if next_day > last_complete:
            continue
        pairs.append((selection, next_day))
    return pairs[-max_days:]
