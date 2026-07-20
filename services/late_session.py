"""基于免费分钟行情的尾盘结构近似分析。"""

from datetime import datetime, time

import akshare as ak
import pandas as pd

from config import (
    LATE_LAST_MINUTES,
    LATE_MAX_DRAWDOWN,
    LATE_MIN_REQUIRED_MINUTES,
    LATE_RAPID_DROP_MAX_CONSECUTIVE,
    LATE_RAPID_DROP_PER_MINUTE,
    LATE_SCORE_DRAWDOWN_WEIGHT,
    LATE_SCORE_TREND_WEIGHT,
    LATE_SCORE_VOLUME_WEIGHT,
    LATE_SCORE_VWAP_WEIGHT,
    LATE_SESSION_END_TIME,
    LATE_SESSION_START_TIME,
    LATE_VOLUME_EXPANSION_RATIO,
    LATE_VWAP_ABOVE_RATIO_MIN,
    MINUTE_VOLUME_SHARE_MULTIPLIER,
)


class LateSessionDataError(RuntimeError):
    """免费分钟数据不足以可靠验证尾盘结构。"""


def _max_consecutive_true(values: pd.Series) -> int:
    maximum = current = 0
    for value in values.fillna(False).astype(bool):
        current = current + 1 if value else 0
        maximum = max(maximum, current)
    return maximum


def analyze_late_session(stock_code: str) -> dict[str, object]:
    """返回尾盘结构状态、评分、指标和排除原因。"""
    now = datetime.now()
    session_end = datetime.combine(
        now.date(),
        time.fromisoformat(LATE_SESSION_END_TIME),
    )
    query_end = min(now, session_end)
    query_start = datetime.combine(now.date(), time(9, 30))

    if query_end.time() < time.fromisoformat(LATE_SESSION_START_TIME):
        raise LateSessionDataError("当前尚未到达尾盘分析时段")

    try:
        minute_data = ak.stock_zh_a_hist_min_em(
            symbol=str(stock_code).zfill(6),
            start_date=query_start.strftime("%Y-%m-%d %H:%M:%S"),
            end_date=query_end.strftime("%Y-%m-%d %H:%M:%S"),
            period="1",
            adjust="",
        )
    except Exception as error:
        raise LateSessionDataError(
            f"分钟行情请求失败（{type(error).__name__}）"
        ) from error

    required = {"时间", "收盘", "最高", "成交量", "成交额"}
    if not isinstance(minute_data, pd.DataFrame) or minute_data.empty:
        raise LateSessionDataError("分钟行情为空")
    if not required.issubset(minute_data.columns):
        raise LateSessionDataError("分钟行情缺少必要字段")

    data = minute_data.loc[:, list(required)].copy()
    data["时间"] = pd.to_datetime(data["时间"], errors="coerce")
    for column in ("收盘", "最高", "成交量", "成交额"):
        data[column] = pd.to_numeric(data[column], errors="coerce")
    data.dropna(inplace=True)
    data = data.loc[data["时间"].dt.date.eq(now.date())].sort_values("时间")
    data = data.loc[(data["成交量"] > 0) & (data["成交额"] > 0)]

    if data.empty:
        raise LateSessionDataError("当日有效分钟行情为空")

    data["累计成交量"] = data["成交量"].cumsum()
    data["累计成交额"] = data["成交额"].cumsum()
    data["VWAP"] = data["累计成交额"] / (
        data["累计成交量"] * MINUTE_VOLUME_SHARE_MULTIPLIER
    )

    late_start = time.fromisoformat(LATE_SESSION_START_TIME)
    late = data.loc[data["时间"].dt.time.ge(late_start)].copy()
    if len(late) < LATE_MIN_REQUIRED_MINUTES:
        raise LateSessionDataError(
            f"14:30 后有效数据不足 {LATE_MIN_REQUIRED_MINUTES} 分钟"
        )

    pre_late = data.loc[data["时间"].dt.time.lt(late_start)]
    if pre_late.empty or pre_late["成交量"].mean() <= 0:
        raise LateSessionDataError("缺少尾盘前的成交量基准")

    above_ratio = float(late["收盘"].gt(late["VWAP"]).mean())
    late_high = float(late["最高"].max())
    latest_close = float(late.iloc[-1]["收盘"])
    if late_high <= 0:
        raise LateSessionDataError("尾盘最高价无效")
    drawdown = max(0.0, (late_high - latest_close) / late_high)

    last_minutes = late.tail(LATE_LAST_MINUTES)
    rapid_drop = last_minutes["收盘"].pct_change().le(
        LATE_RAPID_DROP_PER_MINUTE
    )
    max_consecutive_drop = _max_consecutive_true(rapid_drop)
    volume_ratio = float(late["成交量"].mean() / pre_late["成交量"].mean())

    vwap_pass = above_ratio >= LATE_VWAP_ABOVE_RATIO_MIN
    drawdown_pass = drawdown <= LATE_MAX_DRAWDOWN
    trend_pass = max_consecutive_drop <= LATE_RAPID_DROP_MAX_CONSECUTIVE
    volume_expanded = volume_ratio >= LATE_VOLUME_EXPANSION_RATIO

    score = (
        (LATE_SCORE_VWAP_WEIGHT if vwap_pass else 0)
        + (LATE_SCORE_DRAWDOWN_WEIGHT if drawdown_pass else 0)
        + (LATE_SCORE_TREND_WEIGHT if trend_pass else 0)
        + (LATE_SCORE_VOLUME_WEIGHT if volume_expanded else 0)
    )
    reasons = []
    if not vwap_pass:
        reasons.append("VWAP上方分钟占比不足")
    if not drawdown_pass:
        reasons.append("尾盘冲高回落超过阈值")
    if not trend_pass:
        reasons.append("最后10分钟连续快速下跌")

    return {
        "尾盘结构状态": "合格" if not reasons else "排除",
        "尾盘结构评分": score,
        "尾盘排除原因": "；".join(reasons),
        "VWAP上方占比": above_ratio,
        "尾盘回撤": drawdown,
        "尾盘量能倍数": volume_ratio,
        "尾盘量能异常放大": "是" if volume_expanded else "否",
    }
