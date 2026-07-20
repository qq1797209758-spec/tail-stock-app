"""个股历史行情与最近涨停判断。"""

from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP

import akshare as ak
import pandas as pd

from config import (
    HISTORY_CALENDAR_LOOKBACK_DAYS,
    HISTORY_MIN_FETCH_TRADING_DAYS,
    HISTORY_TRADING_DAYS,
    LIMIT_UP_PRICE_TOLERANCE,
    MAIN_BOARD_LIMIT_UP_RATE,
    PRICE_TICK_SIZE,
)
from services.network import call_with_proxy_fallback


class HistoryDataError(RuntimeError):
    """单只股票历史行情无法可靠判断。"""

    def __init__(self, message: str, status: str = "无法验证") -> None:
        super().__init__(message)
        self.status = status


def calculate_main_board_limit_price(previous_close: float) -> float:
    """按主板 10% 涨停规则和最小价格单位计算理论涨停价。"""
    close = Decimal(str(previous_close))
    tick = Decimal(str(PRICE_TICK_SIZE))
    if not close.is_finite() or close <= 0:
        raise ValueError("前收盘价无效")
    return float(
        (close * (Decimal("1") + Decimal(str(MAIN_BOARD_LIMIT_UP_RATE))))
        .quantize(tick, rounding=ROUND_HALF_UP)
    )


def analyze_recent_limit_up(stock_code: str) -> dict[str, object]:
    """判断主板普通股票最近 20 个交易日是否至少出现一次涨停。"""
    end_date = datetime.now()
    start_date = end_date - timedelta(days=HISTORY_CALENDAR_LOOKBACK_DAYS)
    normalized_code = str(stock_code).zfill(6)

    try:
        history = call_with_proxy_fallback(
            lambda: ak.stock_zh_a_hist(
                symbol=normalized_code,
                period="daily",
                start_date=start_date.strftime("%Y%m%d"),
                end_date=end_date.strftime("%Y%m%d"),
                adjust="",
            )
        )
    except Exception as primary_error:
        market_prefix = "sh" if normalized_code.startswith("6") else "sz"
        try:
            history = call_with_proxy_fallback(
                lambda: ak.stock_zh_a_daily(
                    symbol=f"{market_prefix}{normalized_code}",
                    start_date=start_date.strftime("%Y%m%d"),
                    end_date=end_date.strftime("%Y%m%d"),
                    adjust="",
                )
            ).rename(columns={"date": "日期", "close": "收盘", "high": "最高"})
        except Exception as fallback_error:
            raise HistoryDataError(
                "主用和备用历史行情接口均请求失败"
                f"（{type(primary_error).__name__} / {type(fallback_error).__name__}）"
            ) from fallback_error

    required_columns = {"日期", "收盘", "最高"}
    if not isinstance(history, pd.DataFrame) or history.empty:
        raise HistoryDataError("历史行情为空", status="数据不足")
    if not required_columns.issubset(history.columns):
        raise HistoryDataError("历史行情缺少日期、收盘或最高价字段")

    cleaned = history.loc[:, ["日期", "收盘", "最高"]].copy()
    cleaned["日期"] = pd.to_datetime(cleaned["日期"], errors="coerce")
    cleaned["收盘"] = pd.to_numeric(cleaned["收盘"], errors="coerce")
    cleaned["最高"] = pd.to_numeric(cleaned["最高"], errors="coerce")
    cleaned.dropna(subset=["日期"], inplace=True)
    cleaned.drop_duplicates(subset=["日期"], keep="last", inplace=True)
    cleaned.sort_values("日期", inplace=True)

    if len(cleaned) < HISTORY_MIN_FETCH_TRADING_DAYS:
        raise HistoryDataError(
            f"历史数据不足 {HISTORY_MIN_FETCH_TRADING_DAYS} 个交易日",
            status="数据不足",
        )

    calculation_window = cleaned.tail(HISTORY_MIN_FETCH_TRADING_DAYS).copy()
    if calculation_window[["收盘", "最高"]].isna().any().any():
        raise HistoryDataError(
            f"最近{HISTORY_MIN_FETCH_TRADING_DAYS}个交易日存在缺失价格",
            status="数据不足",
        )

    calculation_window["前收盘"] = calculation_window["收盘"].shift(1)
    recent = calculation_window.tail(HISTORY_TRADING_DAYS).copy()
    if recent["前收盘"].isna().any():
        raise HistoryDataError("缺少计算理论涨停价所需的前收盘价", status="数据不足")

    try:
        recent["理论涨停价"] = recent["前收盘"].map(
            calculate_main_board_limit_price
        )
    except (ValueError, ArithmeticError) as error:
        raise HistoryDataError("理论涨停价计算失败") from error

    tolerance = float(LIMIT_UP_PRICE_TOLERANCE)
    recent["触及涨停"] = recent["最高"].add(tolerance).ge(
        recent["理论涨停价"]
    )
    limit_up_days = recent.loc[recent["触及涨停"], "日期"]
    count = int(limit_up_days.size)

    if limit_up_days.empty:
        return {
            "20日内是否涨停": "否",
            "最近涨停日期": "",
            "20日涨停次数": 0,
            "数据状态": "正常",
            "涨停判断": "不符合",
        }

    latest_date = limit_up_days.max().strftime("%Y-%m-%d")
    return {
        "20日内是否涨停": "是",
        "最近涨停日期": latest_date,
        "20日涨停次数": count,
        "数据状态": "正常",
        "涨停判断": "符合",
    }
