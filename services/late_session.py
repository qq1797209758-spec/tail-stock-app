"""基于免费分钟行情的尾盘结构近似分析。"""

from datetime import datetime, time, timedelta

import akshare as ak
import pandas as pd

from config import (
    LATE_DATA_COMPLETENESS_MIN,
    LATE_LAST_MINUTES,
    LATE_LAST_MINUTES_MAX_DROP,
    LATE_MAX_DRAWDOWN,
    LATE_MIN_REQUIRED_MINUTES,
    LATE_RAPID_DROP_MAX_CONSECUTIVE,
    LATE_SCORE_DRAWDOWN_WEIGHT,
    LATE_SCORE_TREND_WEIGHT,
    LATE_SCORE_VOLUME_WEIGHT,
    LATE_SCORE_VWAP_WEIGHT,
    LATE_SESSION_END_TIME,
    LATE_SESSION_START_TIME,
    LATE_VOLUME_EXPANSION_RATIO,
    LATE_VOLUME_BASELINE_MINUTES,
    LATE_VWAP_ABOVE_RATIO_MIN,
    MINUTE_VOLUME_SHARE_MULTIPLIER,
)
from services.network import call_with_proxy_fallback


class LateSessionDataError(RuntimeError):
    """免费分钟数据不足以可靠验证尾盘结构。"""


def _max_consecutive_true(values: pd.Series) -> int:
    maximum = current = 0
    for value in values.fillna(False).astype(bool):
        current = current + 1 if value else 0
        maximum = max(maximum, current)
    return maximum


def unverifiable_late_session_result(
    reason: str,
    completeness: float | object = pd.NA,
) -> dict[str, object]:
    """生成字段完整的无法验证结果，不将缺失数据判为合格。"""
    return {
        "尾盘结构状态": "无法验证",
        "VWAP状态": "无法验证",
        "高于VWAP占比": pd.NA,
        "尾盘最大回撤": pd.NA,
        "最后10分钟涨跌幅": pd.NA,
        "连续走弱状态": "无法验证",
        "尾盘成交量状态": "无法验证",
        "尾盘结构评分": pd.NA,
        "淘汰原因": reason,
        "数据完整性": completeness,
        # 保留旧字段，兼容现有评分和展示流程。
        "尾盘排除原因": reason,
    }


def analyze_late_session(
    stock_code: str,
    now: datetime | None = None,
) -> dict[str, object]:
    """返回尾盘结构状态、评分、指标和排除原因。"""
    now = now or datetime.now()
    late_start_time = time.fromisoformat(LATE_SESSION_START_TIME)
    session_end = datetime.combine(
        now.date(),
        time.fromisoformat(LATE_SESSION_END_TIME),
    )
    query_end = min(now, session_end)
    query_start = datetime.combine(now.date(), time(9, 30))

    if query_end.time() < late_start_time:
        return unverifiable_late_session_result("尚未进入尾盘分析时段")

    try:
        minute_data = call_with_proxy_fallback(
            lambda: ak.stock_zh_a_hist_min_em(
                symbol=str(stock_code).zfill(6),
                start_date=query_start.strftime("%Y-%m-%d %H:%M:%S"),
                end_date=query_end.strftime("%Y-%m-%d %H:%M:%S"),
                period="1",
                adjust="",
            )
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

    late_start_at = datetime.combine(now.date(), late_start_time)
    late = data.loc[
        data["时间"].between(late_start_at, query_end, inclusive="both")
    ].copy()
    expected_minutes = len(
        pd.date_range(late_start_at, query_end.replace(second=0, microsecond=0), freq="min")
    )
    actual_minutes = int(late["时间"].dt.floor("min").nunique())
    completeness = min(1.0, actual_minutes / expected_minutes) if expected_minutes else 0.0
    if completeness < LATE_DATA_COMPLETENESS_MIN:
        return unverifiable_late_session_result(
            f"尾盘分钟数据完整率低于 {LATE_DATA_COMPLETENESS_MIN:.0%}",
            completeness,
        )
    if len(late) < LATE_MIN_REQUIRED_MINUTES:
        return unverifiable_late_session_result(
            f"14:30 后有效数据不足 {LATE_MIN_REQUIRED_MINUTES} 分钟",
            completeness,
        )

    baseline_start = late_start_at - timedelta(minutes=LATE_VOLUME_BASELINE_MINUTES)
    pre_late = data.loc[
        data["时间"].ge(baseline_start) & data["时间"].lt(late_start_at)
    ]
    if pre_late.empty or pre_late["成交量"].mean() <= 0:
        return unverifiable_late_session_result("缺少14:30前30分钟成交量基准", completeness)

    above_ratio = float(late["收盘"].gt(late["VWAP"]).mean())
    late_high = float(late["最高"].max())
    latest_close = float(late.iloc[-1]["收盘"])
    if late_high <= 0:
        raise LateSessionDataError("尾盘最高价无效")
    drawdown = max(0.0, (late_high - latest_close) / late_high)

    last_minutes = late.tail(LATE_LAST_MINUTES)
    if len(last_minutes) < LATE_LAST_MINUTES:
        return unverifiable_late_session_result("最后10分钟数据不足", completeness)
    last_change = float(last_minutes["收盘"].iloc[-1] / last_minutes["收盘"].iloc[0] - 1)
    weakening = last_minutes["收盘"].diff().lt(0)
    max_consecutive_drop = _max_consecutive_true(weakening)
    volume_ratio = float(late["成交量"].mean() / pre_late["成交量"].mean())

    vwap_pass = above_ratio >= LATE_VWAP_ABOVE_RATIO_MIN
    drawdown_pass = drawdown <= LATE_MAX_DRAWDOWN
    last_change_pass = last_change >= LATE_LAST_MINUTES_MAX_DROP
    weakening_pass = max_consecutive_drop <= LATE_RAPID_DROP_MAX_CONSECUTIVE
    volume_expanded = volume_ratio >= LATE_VOLUME_EXPANSION_RATIO

    score = (
        (LATE_SCORE_VWAP_WEIGHT if vwap_pass else 0)
        + (LATE_SCORE_DRAWDOWN_WEIGHT if drawdown_pass else 0)
        + (LATE_SCORE_TREND_WEIGHT / 2 if last_change_pass else 0)
        + (LATE_SCORE_TREND_WEIGHT / 2 if weakening_pass else 0)
        + (LATE_SCORE_VOLUME_WEIGHT if volume_expanded else 0)
    )
    reasons = []
    if not vwap_pass:
        reasons.append("VWAP上方分钟占比不足")
    if not drawdown_pass:
        reasons.append("尾盘冲高回落超过阈值")
    if not last_change_pass:
        reasons.append("最后10分钟跌幅超过阈值")
    if not weakening_pass:
        reasons.append("最后10分钟连续走弱")

    vwap_status = "合格" if vwap_pass else "高于VWAP占比不足"
    weakening_status = (
        f"正常（最长{max_consecutive_drop}分钟）"
        if weakening_pass else f"连续走弱（{max_consecutive_drop}分钟）"
    )
    volume_status = (
        f"明显放大（{volume_ratio:.2f}倍）"
        if volume_expanded else f"未明显放大（{volume_ratio:.2f}倍）"
    )
    elimination_reason = "；".join(reasons)

    return {
        "尾盘结构状态": "合格" if not reasons else "排除",
        "VWAP状态": vwap_status,
        "高于VWAP占比": above_ratio,
        "尾盘最大回撤": drawdown,
        "最后10分钟涨跌幅": last_change,
        "连续走弱状态": weakening_status,
        "尾盘成交量状态": volume_status,
        "尾盘结构评分": float(score),
        "淘汰原因": elimination_reason,
        "数据完整性": completeness,
        "尾盘排除原因": elimination_reason,
        "VWAP上方占比": above_ratio,
        "尾盘回撤": drawdown,
        "尾盘量能倍数": volume_ratio,
        "尾盘量能异常放大": "是" if volume_expanded else "否",
    }
