"""A股交易日判定辅助。"""

from datetime import date

import pandas as pd


def is_trading_day(day: date, calendar: pd.DatetimeIndex | pd.Series) -> bool:
    """只依据真实交易日历判断，不用工作日猜测节假日。"""
    values = pd.to_datetime(calendar, errors="coerce")
    normalized = pd.DatetimeIndex(values).normalize()
    return pd.Timestamp(day).normalize() in normalized
