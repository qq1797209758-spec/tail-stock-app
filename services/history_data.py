"""个股历史行情与最近涨停判断。"""

from datetime import datetime, timedelta

import akshare as ak
import pandas as pd

from config import (
    HISTORY_CALENDAR_LOOKBACK_DAYS,
    HISTORY_TRADING_DAYS,
    LIMIT_UP_CHANGE_THRESHOLD,
)
from services.network import call_with_proxy_fallback


class HistoryDataError(RuntimeError):
    """单只股票历史行情无法可靠判断。"""


def analyze_recent_limit_up(stock_code: str) -> dict[str, str]:
    """判断主板普通股票最近 20 个交易日是否至少出现一次涨停。"""
    end_date = datetime.now()
    start_date = end_date - timedelta(days=HISTORY_CALENDAR_LOOKBACK_DAYS)

    try:
        history = call_with_proxy_fallback(
            lambda: ak.stock_zh_a_hist(
                symbol=str(stock_code).zfill(6),
                period="daily",
                start_date=start_date.strftime("%Y%m%d"),
                end_date=end_date.strftime("%Y%m%d"),
                adjust="",
            )
        )
    except Exception as error:
        raise HistoryDataError(
            f"历史行情请求失败（{type(error).__name__}）"
        ) from error

    required_columns = {"日期", "涨跌幅"}
    if not isinstance(history, pd.DataFrame) or history.empty:
        raise HistoryDataError("历史行情为空")
    if not required_columns.issubset(history.columns):
        raise HistoryDataError("历史行情缺少日期或涨跌幅字段")

    cleaned = history.loc[:, ["日期", "涨跌幅"]].copy()
    cleaned["日期"] = pd.to_datetime(cleaned["日期"], errors="coerce")
    cleaned["涨跌幅"] = pd.to_numeric(cleaned["涨跌幅"], errors="coerce")
    cleaned.dropna(subset=["日期", "涨跌幅"], inplace=True)
    cleaned.sort_values("日期", inplace=True)

    if len(cleaned) < HISTORY_TRADING_DAYS:
        raise HistoryDataError(
            f"有效历史数据不足 {HISTORY_TRADING_DAYS} 个交易日"
        )

    recent = cleaned.tail(HISTORY_TRADING_DAYS)
    limit_up_days = recent.loc[
        recent["涨跌幅"].ge(LIMIT_UP_CHANGE_THRESHOLD),
        "日期",
    ]

    if limit_up_days.empty:
        return {"涨停判断": "不符合", "最近涨停日期": ""}

    latest_date = limit_up_days.max().strftime("%Y-%m-%d")
    return {"涨停判断": "符合", "最近涨停日期": latest_date}
