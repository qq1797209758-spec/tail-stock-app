"""基于免费分钟行情的尾盘结构近似分析。"""

from datetime import datetime, time, timedelta
import logging
from zoneinfo import ZoneInfo

import akshare as ak
import numpy as np
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
)
from services.network import call_with_proxy_fallback


SHANGHAI_TIMEZONE = ZoneInfo("Asia/Shanghai")
logger = logging.getLogger(__name__)


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
    *,
    minute_source: str = "未取得",
    minute_count: int = 0,
    interface_error: str = "",
    current_time: str = "",
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
        "分钟数据源": minute_source,
        "分钟K线条数": minute_count,
        "接口错误原因": interface_error or reason,
        "当前北京时间": current_time,
    }


def _market_symbol(stock_code: str) -> str:
    code = str(stock_code).lower().replace("sh", "").replace("sz", "").zfill(6)
    if code.startswith("60"):
        return "sh" + code
    if code.startswith("00"):
        return "sz" + code
    raise LateSessionDataError("股票代码或市场参数错误：仅支持00/60沪深主板代码")


def _fetch_minute_data(
    stock_code: str, query_start: datetime, query_end: datetime
) -> tuple[pd.DataFrame, str, list[str]]:
    """优先东方财富，失败或空数据后使用新浪真实1分钟行情。"""
    numeric_code = _market_symbol(stock_code)[2:]
    market_symbol = _market_symbol(stock_code)
    request_parameters = {
        "code": numeric_code,
        "market_symbol": market_symbol,
        "start": query_start.strftime("%Y-%m-%d %H:%M:%S"),
        "end": query_end.strftime("%Y-%m-%d %H:%M:%S"),
        "period": "1",
    }
    errors: list[str] = []
    try:
        eastmoney = call_with_proxy_fallback(
            lambda: ak.stock_zh_a_hist_min_em(
                symbol=numeric_code,
                start_date=query_start.strftime("%Y-%m-%d %H:%M:%S"),
                end_date=query_end.strftime("%Y-%m-%d %H:%M:%S"),
                period="1",
                adjust="",
            )
        )
        if isinstance(eastmoney, pd.DataFrame) and not eastmoney.empty:
            data = eastmoney.rename(columns={
                "时间": "时间", "收盘": "收盘", "最高": "最高",
                "成交量": "成交量", "成交额": "成交额",
            }).copy()
            # 东方财富分钟成交量单位为手，统一转换为股后再按统一公式计算VWAP。
            data["成交量"] = pd.to_numeric(data["成交量"], errors="coerce") * 100
            times = pd.to_datetime(data.get("时间"), errors="coerce").dropna()
            logger.info(
                "分钟行情诊断 request=%s source=东方财富 success=true rows=%s first=%s "
                "last=%s columns=%s status=AKShare未暴露HTTP状态",
                request_parameters, len(data),
                None if times.empty else times.iloc[0],
                None if times.empty else times.iloc[-1], list(eastmoney.columns),
            )
            return data, "AKShare · 东方财富1分钟", errors
        errors.append("东方财富分钟接口返回数据为空")
        logger.warning(
            "分钟行情诊断 request=%s source=东方财富 success=false rows=0 columns=%s error=返回数据为空",
            request_parameters, list(eastmoney.columns) if isinstance(eastmoney, pd.DataFrame) else [],
        )
    except Exception as error:
        errors.append(f"东方财富分钟接口请求失败（{type(error).__name__}）：{error}")
        logger.warning(
            "分钟行情诊断 request=%s source=东方财富 success=false rows=0 error=%s: %s",
            request_parameters, type(error).__name__, error,
        )

    symbol = market_symbol
    try:
        sina = call_with_proxy_fallback(
            lambda: ak.stock_zh_a_minute(symbol=symbol, period="1", adjust="")
        )
        if isinstance(sina, pd.DataFrame) and not sina.empty:
            data = sina.rename(columns={
                "day": "时间", "close": "收盘", "high": "最高",
                "volume": "成交量", "amount": "成交额",
            }).copy()
            data["时间"] = pd.to_datetime(data["时间"], errors="coerce")
            data = data.loc[data["时间"].between(query_start, query_end, inclusive="both")]
            if not data.empty:
                times = data["时间"].dropna()
                logger.info(
                    "分钟行情诊断 request=%s source=新浪 success=true rows=%s first=%s "
                    "last=%s columns=%s status=AKShare未暴露HTTP状态 primary_errors=%s",
                    request_parameters, len(data),
                    None if times.empty else times.iloc[0],
                    None if times.empty else times.iloc[-1], list(sina.columns), errors,
                )
                return data, "AKShare · 新浪1分钟", errors
        errors.append("新浪分钟接口返回数据为空")
        logger.warning(
            "分钟行情诊断 request=%s source=新浪 success=false rows=0 columns=%s error=返回数据为空",
            request_parameters, list(sina.columns) if isinstance(sina, pd.DataFrame) else [],
        )
    except Exception as error:
        errors.append(f"新浪分钟接口请求失败（{type(error).__name__}）：{error}")
        logger.warning(
            "分钟行情诊断 request=%s source=新浪 success=false rows=0 error=%s: %s",
            request_parameters, type(error).__name__, error,
        )
    raise LateSessionDataError("；".join(errors))


def analyze_late_session(
    stock_code: str,
    now: datetime | None = None,
) -> dict[str, object]:
    """返回尾盘结构状态、评分、指标和排除原因。"""
    if now is None:
        now = datetime.now(SHANGHAI_TIMEZONE)
    elif now.tzinfo is not None:
        now = now.astimezone(SHANGHAI_TIMEZONE)

    # AkShare 返回的分钟时间和下方 datetime.combine() 均为无时区时间；
    # 先换算为上海本地时间，再移除 tzinfo，避免混用时报 TypeError。
    now = now.replace(tzinfo=None)
    current_time = now.strftime("%Y-%m-%d %H:%M:%S")
    late_start_time = time.fromisoformat(LATE_SESSION_START_TIME)
    session_end = datetime.combine(
        now.date(),
        time.fromisoformat(LATE_SESSION_END_TIME),
    )
    query_end = min(now, session_end)
    query_start = datetime.combine(now.date(), time(9, 30))

    if query_end.time() < late_start_time:
        return unverifiable_late_session_result(
            "尚未进入尾盘时段", current_time=current_time
        )

    try:
        minute_data, minute_source, source_errors = _fetch_minute_data(
            stock_code, query_start, query_end
        )
    except LateSessionDataError as error:
        return unverifiable_late_session_result(
            str(error), interface_error=str(error), current_time=current_time
        )

    required = {"时间", "收盘", "最高", "成交量", "成交额"}
    if not isinstance(minute_data, pd.DataFrame) or minute_data.empty:
        return unverifiable_late_session_result(
            "返回数据为空", minute_source=minute_source,
            interface_error="；".join(source_errors), current_time=current_time,
        )
    if not required.issubset(minute_data.columns):
        missing = required.difference(minute_data.columns)
        return unverifiable_late_session_result(
            "成交量/成交额字段缺失" if {"成交量", "成交额"}.intersection(missing)
            else "分钟数据字段缺失：" + "、".join(sorted(missing)),
            minute_source=minute_source, minute_count=len(minute_data),
            interface_error="；".join(source_errors), current_time=current_time,
        )

    data = minute_data.loc[:, list(required)].copy()
    data["时间"] = pd.to_datetime(data["时间"], errors="coerce")
    for column in ("收盘", "最高", "成交量", "成交额"):
        data[column] = pd.to_numeric(data[column], errors="coerce")
    data.dropna(subset=["时间", "收盘", "最高"], inplace=True)
    data = data.loc[data["时间"].dt.date.eq(now.date())].sort_values("时间")
    minute_count = int(len(data))
    invalid_volume = data["成交量"].isna() | data["成交量"].le(0)
    invalid_amount = data["成交额"].isna() | data["成交额"].le(0)
    data = data.loc[~invalid_volume & ~invalid_amount].copy()

    if data.empty:
        return unverifiable_late_session_result(
            "成交量为0或成交量/成交额无有效值",
            minute_source=minute_source, minute_count=minute_count,
            interface_error="；".join(source_errors), current_time=current_time,
        )

    data["累计成交量"] = data["成交量"].cumsum()
    data["累计成交额"] = data["成交额"].cumsum()
    data["VWAP"] = np.where(
        data["累计成交量"].gt(0),
        data["累计成交额"] / data["累计成交量"],
        np.nan,
    )
    data.loc[~np.isfinite(data["VWAP"]), "VWAP"] = np.nan

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
            f"分钟数据不足：完整率低于 {LATE_DATA_COMPLETENESS_MIN:.0%}", completeness,
            minute_source=minute_source, minute_count=actual_minutes,
            interface_error="；".join(source_errors), current_time=current_time,
        )
    if len(late) < LATE_MIN_REQUIRED_MINUTES:
        return unverifiable_late_session_result(
            f"分钟数据不足：14:30后少于 {LATE_MIN_REQUIRED_MINUTES} 条", completeness,
            minute_source=minute_source, minute_count=actual_minutes,
            interface_error="；".join(source_errors), current_time=current_time,
        )

    baseline_start = late_start_at - timedelta(minutes=LATE_VOLUME_BASELINE_MINUTES)
    pre_late = data.loc[
        data["时间"].ge(baseline_start) & data["时间"].lt(late_start_at)
    ]
    if pre_late.empty or pre_late["成交量"].mean() <= 0:
        return unverifiable_late_session_result(
            "分钟数据不足：缺少14:30前30分钟成交量基准", completeness,
            minute_source=minute_source, minute_count=actual_minutes,
            interface_error="；".join(source_errors), current_time=current_time,
        )

    valid_vwap = late["VWAP"].notna() & np.isfinite(late["VWAP"])
    if not valid_vwap.any():
        return unverifiable_late_session_result(
            "分钟数据不足：无法计算VWAP", completeness,
            minute_source=minute_source, minute_count=actual_minutes,
            interface_error="；".join(source_errors), current_time=current_time,
        )
    above_ratio = float(late.loc[valid_vwap, "收盘"].gt(late.loc[valid_vwap, "VWAP"]).mean())
    late_high = float(late["最高"].max())
    latest_close = float(late.iloc[-1]["收盘"])
    if not np.isfinite(late_high) or late_high <= 0 or not np.isfinite(latest_close):
        return unverifiable_late_session_result(
            "分钟数据不足：尾盘价格无效", completeness,
            minute_source=minute_source, minute_count=actual_minutes,
            interface_error="；".join(source_errors), current_time=current_time,
        )
    drawdown = (late_high - latest_close) / late_high
    if not np.isfinite(drawdown):
        return unverifiable_late_session_result(
            "分钟数据不足：尾盘回撤无法计算", completeness,
            minute_source=minute_source, minute_count=actual_minutes,
            interface_error="；".join(source_errors), current_time=current_time,
        )
    drawdown = max(0.0, float(drawdown))

    last_minutes = late.tail(LATE_LAST_MINUTES)
    if len(last_minutes) < LATE_LAST_MINUTES:
        return unverifiable_late_session_result(
            "分钟数据不足：最后10分钟数据不足", completeness,
            minute_source=minute_source, minute_count=actual_minutes,
            interface_error="；".join(source_errors), current_time=current_time,
        )
    first_close = float(last_minutes["收盘"].iloc[0])
    final_close = float(last_minutes["收盘"].iloc[-1])
    if first_close <= 0 or not np.isfinite(first_close) or not np.isfinite(final_close):
        return unverifiable_late_session_result(
            "分钟数据不足：最后10分钟价格无效", completeness,
            minute_source=minute_source, minute_count=actual_minutes,
            interface_error="；".join(source_errors), current_time=current_time,
        )
    last_change = float(final_close / first_close - 1)
    if not np.isfinite(last_change):
        return unverifiable_late_session_result(
            "分钟数据不足：最后10分钟涨跌幅无法计算", completeness,
            minute_source=minute_source, minute_count=actual_minutes,
            interface_error="；".join(source_errors), current_time=current_time,
        )
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
        "分钟数据源": minute_source,
        "分钟K线条数": actual_minutes,
        "接口错误原因": "；".join(source_errors),
        "当前北京时间": current_time,
    }
