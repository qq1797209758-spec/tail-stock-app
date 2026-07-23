"""无前视偏差的历史尾盘策略回测引擎。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time as clock_time
from decimal import Decimal, ROUND_HALF_UP
import re
import time
from typing import Callable

import pandas as pd

from config import (
    BACKTEST_BUY_TIME,
    BACKTEST_MINUTE_LOOKBACK_TRADING_DAYS,
    BACKTEST_REQUEST_INTERVAL_SECONDS,
    EXCLUDED_NAME_KEYWORDS,
    LATE_DATA_COMPLETENESS_MIN,
    LATE_LAST_MINUTES,
    LATE_LAST_MINUTES_MAX_DROP,
    LATE_MAX_DRAWDOWN,
    LATE_RAPID_DROP_MAX_CONSECUTIVE,
    LATE_SESSION_START_TIME,
    LATE_VOLUME_BASELINE_MINUTES,
    LATE_VOLUME_EXPANSION_RATIO,
    LATE_VWAP_ABOVE_RATIO_MIN,
    LIMIT_UP_PRICE_TOLERANCE,
    MAIN_BOARD_LIMIT_UP_RATE,
    MARKET_CAP_MAX,
    MARKET_CAP_MIN,
    PRICE_CHANGE_MAX,
    PRICE_CHANGE_MIN,
    PRICE_TICK_SIZE,
    SCORE_LATE_SESSION_MAX,
    SCORE_MARKET_SENTIMENT_MAX,
    SCORE_TECH_CHANGE_PART,
    SCORE_TECH_CLOSE_POSITION_PART,
    SCORE_VOLUME_RATIO_MAX,
    SCORE_VOLUME_RATIO_MIN,
    SCORE_VOLUME_RATIO_PART,
    TURNOVER_RATE_MAX,
    TURNOVER_RATE_MIN,
    VOLUME_RATIO_MIN,
)
from services.backtest_data import (
    BacktestDataError,
    completed_backtest_dates,
    fetch_daily_history,
    fetch_historical_minutes,
    fetch_historical_universe,
    fetch_total_shares,
    fetch_trade_calendar,
)


ProgressCallback = Callable[[float, str], None]


@dataclass
class BacktestResult:
    summary: dict[str, object]
    daily: pd.DataFrame
    details: pd.DataFrame
    failures: pd.DataFrame
    parameters: dict[str, object]


DETAIL_COLUMNS = [
    "筛选日期", "股票代码", "股票名称", "当日排名", "当日综合评分",
    "当日涨跌幅", "当日量比", "量比口径", "当日换手率", "当日总市值",
    "近20日涨停次数", "尾盘买入价", "次日开盘价", "次日最高价",
    "次日最低价", "次日收盘价", "次日开盘到收盘收益率",
    "尾盘到次日收盘收益率", "是否盈利", "数据完整性", "数据缺失项",
    "数据来源",
]


def _scaled(value: float, lower: float, upper: float, points: float) -> float:
    if pd.isna(value) or upper <= lower:
        return 0.0
    return max(0.0, min(1.0, (float(value) - lower) / (upper - lower))) * points


def _limit_price(previous_close: float) -> float:
    tick = Decimal(str(PRICE_TICK_SIZE))
    return float(
        (Decimal(str(previous_close)) * (Decimal("1") + Decimal(str(MAIN_BOARD_LIMIT_UP_RATE))))
        .quantize(tick, rounding=ROUND_HALF_UP)
    )


def _recent_limit_up_count(history: pd.DataFrame, selection: pd.Timestamp) -> int | None:
    prior = history.loc[history["日期"].lt(selection), ["日期", "收盘", "最高"]].copy()
    if len(prior) < 21 or prior.tail(21)[["收盘", "最高"]].isna().any().any():
        return None
    window = prior.tail(21)
    previous_close = window["收盘"].shift(1)
    recent = window.tail(20).copy()
    recent["前收盘"] = previous_close.tail(20).values
    recent["理论涨停价"] = recent["前收盘"].map(_limit_price)
    return int(
        recent["最高"].add(float(LIMIT_UP_PRICE_TOLERANCE)).ge(recent["理论涨停价"]).sum()
    )


def _max_consecutive_declines(values: pd.Series) -> int:
    maximum = current = 0
    for declined in values.diff().lt(0).fillna(False):
        current = current + 1 if declined else 0
        maximum = max(maximum, current)
    return maximum


def _volume_multiplier(data: pd.DataFrame) -> float:
    valid = data.loc[
        data["成交量"].gt(0) & data["成交额"].gt(0) & data["收盘"].gt(0)
    ]
    if valid.empty:
        return 1.0
    ratio = (valid["成交额"] / (valid["成交量"] * valid["收盘"])).median()
    return 100.0 if abs(float(ratio) - 100.0) < abs(float(ratio) - 1.0) else 1.0


def _analyze_minutes(
    minutes: pd.DataFrame,
    selection: pd.Timestamp,
    previous_days: list[pd.Timestamp],
) -> dict[str, object]:
    data = minutes.copy()
    data = data.loc[data["成交量"].gt(0) & data["收盘"].gt(0)].copy()
    if data.empty:
        raise BacktestDataError("有效历史分钟行情为空")
    data["交易日"] = data["时间"].dt.normalize()

    target = data.loc[data["交易日"].eq(selection)].copy()
    previous = data.loc[data["交易日"].isin(previous_days)].copy()
    daily_volume = previous.groupby("交易日")["成交量"].sum()
    target_volume = target["成交量"].sum()
    if len(daily_volume) < BACKTEST_MINUTE_LOOKBACK_TRADING_DAYS or target_volume <= 0:
        raise BacktestDataError("近似量比缺少前5个交易日同一时刻成交量基准")
    approximate_volume_ratio = float(target_volume / daily_volume.tail(5).mean())

    session_start = datetime.combine(selection.date(), clock_time.fromisoformat(LATE_SESSION_START_TIME))
    session_end = datetime.combine(selection.date(), clock_time(15, 0))
    late = target.loc[target["时间"].between(session_start, session_end)].copy()
    expected_minutes = len(pd.date_range(session_start, session_end, freq="min"))
    completeness = min(1.0, late["时间"].dt.floor("min").nunique() / expected_minutes)
    if completeness < LATE_DATA_COMPLETENESS_MIN:
        raise BacktestDataError(
            f"尾盘分钟数据完整率 {completeness:.1%} 低于 {LATE_DATA_COMPLETENESS_MIN:.0%}"
        )

    multiplier = _volume_multiplier(target)
    target = target.sort_values("时间")
    target["VWAP"] = target["成交额"].cumsum() / (
        target["成交量"].cumsum() * multiplier
    )
    late = target.loc[target["时间"].between(session_start, session_end)].copy()
    baseline_start = session_start - pd.Timedelta(minutes=LATE_VOLUME_BASELINE_MINUTES)
    baseline = target.loc[target["时间"].ge(baseline_start) & target["时间"].lt(session_start)]
    if baseline.empty or baseline["成交量"].mean() <= 0:
        raise BacktestDataError("缺少14:30前30分钟成交量基准")

    above_ratio = float(late["收盘"].gt(late["VWAP"]).mean())
    late_high = float(late["最高"].max())
    final_close = float(late.iloc[-1]["收盘"])
    drawdown = max(0.0, (late_high - final_close) / late_high)
    last = late.tail(LATE_LAST_MINUTES)
    if len(last) < LATE_LAST_MINUTES:
        raise BacktestDataError("最后10分钟数据不足")
    last_change = float(last["收盘"].iloc[-1] / last["收盘"].iloc[0] - 1)
    consecutive = _max_consecutive_declines(last["收盘"])
    volume_expansion = float(late["成交量"].mean() / baseline["成交量"].mean())

    checks = {
        "vwap": above_ratio >= LATE_VWAP_ABOVE_RATIO_MIN,
        "drawdown": drawdown <= LATE_MAX_DRAWDOWN,
        "last_change": last_change >= LATE_LAST_MINUTES_MAX_DROP,
        "consecutive": consecutive <= LATE_RAPID_DROP_MAX_CONSECUTIVE,
    }
    score = (
        (40 if checks["vwap"] else 0)
        + (30 if checks["drawdown"] else 0)
        + (10 if checks["last_change"] else 0)
        + (10 if checks["consecutive"] else 0)
        + (10 if volume_expansion >= LATE_VOLUME_EXPANSION_RATIO else 0)
    )
    buy_at = datetime.combine(selection.date(), clock_time.fromisoformat(BACKTEST_BUY_TIME))
    buy_rows = target.loc[target["时间"].ge(buy_at)]
    if buy_rows.empty:
        raise BacktestDataError("14:50后没有有效分钟收盘价")
    return {
        "量比": approximate_volume_ratio,
        "尾盘合格": all(checks.values()),
        "尾盘结构评分": float(score),
        "尾盘买入价": float(buy_rows.iloc[0]["收盘"]),
        "尾盘完整率": completeness,
    }


def _historical_score(
    row: pd.Series,
    volume_ratio: float,
    late_score: float,
    market_advance_ratio: float,
) -> float:
    volume_points = _scaled(
        volume_ratio, SCORE_VOLUME_RATIO_MIN, SCORE_VOLUME_RATIO_MAX, SCORE_VOLUME_RATIO_PART
    )
    change_points = _scaled(
        float(row["涨跌幅"]), PRICE_CHANGE_MIN, PRICE_CHANGE_MAX, SCORE_TECH_CHANGE_PART
    )
    position_points = 0.0
    if row["最高"] > row["最低"]:
        position = (row["收盘"] - row["最低"]) / (row["最高"] - row["最低"])
        position_points = _scaled(position, 0.0, 1.0, SCORE_TECH_CLOSE_POSITION_PART)
    return round(
        volume_points
        + change_points
        + position_points
        + _scaled(late_score, 0.0, 100.0, SCORE_LATE_SESSION_MAX)
        + _scaled(market_advance_ratio, 0.0, 1.0, SCORE_MARKET_SENTIMENT_MAX),
        2,
    )


def _summary_metrics(
    daily: pd.DataFrame,
    details: pd.DataFrame,
    return_column: str,
) -> dict[str, object]:
    returns = pd.to_numeric(details.get(return_column, pd.Series(dtype=float)), errors="coerce").dropna()
    raw_daily_returns = pd.to_numeric(
        daily.get("每日等权平均收益率", pd.Series(dtype=float)), errors="coerce"
    )
    valid_daily_returns = raw_daily_returns.dropna()
    equity = (1 + raw_daily_returns.fillna(0) / 100).cumprod()
    drawdown = equity / equity.cummax() - 1 if not equity.empty else pd.Series(dtype=float)
    buy_column = "次日开盘价" if return_column.startswith("次日开盘") else "尾盘买入价"
    valid_hits = details.dropna(subset=[buy_column, "次日最高价"]) if not details.empty else details
    hit_ratios = {}
    for threshold in (0.01, 0.03, 0.05):
        hit_ratios[threshold] = (
            float((valid_hits["次日最高价"] > valid_hits[buy_column] * (1 + threshold)).mean())
            if not valid_hits.empty else None
        )
    return {
        "回测交易日数量": len(daily),
        "有效收益交易日数量": int(valid_daily_returns.size),
        "有选股交易日数量": int(daily["入选数量"].gt(0).sum()) if not daily.empty else 0,
        "空仓交易日数量": int(daily["状态"].eq("空仓").sum()) if not daily.empty else 0,
        "总入选股票数量": len(details),
        "盈利股票数量": int(returns.gt(0).sum()),
        "亏损股票数量": int(returns.lt(0).sum()),
        "胜率": float(returns.gt(0).mean()) if not returns.empty else None,
        "单笔平均收益率": float(returns.mean()) if not returns.empty else None,
        "每日等权平均收益率": float(valid_daily_returns.mean()) if not valid_daily_returns.empty else None,
        "累计收益率": float((equity.iloc[-1] - 1) * 100) if not equity.empty else None,
        "最大单笔盈利": float(returns.max()) if not returns.empty else None,
        "最大单笔亏损": float(returns.min()) if not returns.empty else None,
        "最大回撤": float(drawdown.min()) if not drawdown.empty else None,
        "次日最高价高于买入价1%比例": hit_ratios[0.01],
        "次日最高价高于买入价3%比例": hit_ratios[0.03],
        "次日最高价高于买入价5%比例": hit_ratios[0.05],
    }


def run_historical_backtest(
    start_date,
    end_date,
    *,
    max_stocks: int,
    return_basis: str,
    now: datetime,
    progress: ProgressCallback | None = None,
    max_days: int = 10,
) -> BacktestResult:
    """执行历史回测；单日或单股失败均记录后继续。"""
    pairs = completed_backtest_dates(
        start_date, end_date, max_days=max_days, now=now
    )
    if not pairs:
        raise BacktestDataError("所选范围内没有具备完整次日行情的交易日")
    calendar = fetch_trade_calendar()
    details: list[dict[str, object]] = []
    daily_records: list[dict[str, object]] = []
    failures: list[dict[str, object]] = []
    excluded_pattern = "|".join(re.escape(value) for value in EXCLUDED_NAME_KEYWORDS)

    for day_index, (selection, next_day) in enumerate(pairs):
        day_label = selection.strftime("%Y-%m-%d")
        if progress:
            progress(day_index / len(pairs), f"正在回测 {day_label}")
        selected_rows: list[dict[str, object]] = []
        try:
            universe = fetch_historical_universe(day_label)
            missing_name_count = int(universe["名称"].astype("string").str.strip().fillna("").eq("").sum())
            if missing_name_count:
                failures.append({"筛选日期": day_label, "股票代码": "", "阶段": "历史股票池", "失败原因": f"{missing_name_count}只股票缺少筛选日名称，无法验证ST状态，已排除"})
            universe = universe.loc[
                universe["代码"].str.startswith(("60", "00"), na=False)
                & universe["名称"].astype("string").str.strip().fillna("").ne("")
                & ~universe["名称"].astype("string").str.contains(
                    excluded_pattern, case=False, regex=True, na=False
                )
            ].copy()
            if universe.empty:
                raise BacktestDataError("历史主板股票池为空")
            calendar_position = calendar.get_indexer([selection])[0]
            history_start = calendar[max(0, calendar_position - 26)]
            daily_history = fetch_daily_history(
                tuple(universe["数据源代码"]),
                history_start.strftime("%Y-%m-%d"),
                next_day.strftime("%Y-%m-%d"),
            )
            today_rows = daily_history.loc[daily_history["日期"].eq(selection)].merge(
                universe, on="数据源代码", how="inner"
            )
            required_today = ["涨跌幅", "换手率", "开盘", "最高", "最低", "收盘"]
            incomplete_today = int(today_rows[required_today].isna().any(axis=1).sum())
            if incomplete_today:
                failures.append({"筛选日期": day_label, "股票代码": "", "阶段": "历史日线", "失败原因": f"{incomplete_today}只股票当日日线字段不完整，已排除"})
                today_rows = today_rows.dropna(subset=required_today)
            market_advance_ratio = float(today_rows["涨跌幅"].gt(0).mean())
            coarse = today_rows.loc[
                today_rows["涨跌幅"].between(PRICE_CHANGE_MIN, PRICE_CHANGE_MAX)
                & today_rows["换手率"].between(TURNOVER_RATE_MIN, TURNOVER_RATE_MAX)
            ].copy()
            if not coarse.empty:
                shares = fetch_total_shares(tuple(coarse["数据源代码"]), day_label)
                coarse = coarse.merge(shares, on="数据源代码", how="left")
                coarse["总市值"] = coarse["收盘"] * coarse["总股本"]
                missing_shares = coarse.loc[coarse["总股本"].isna()]
                for _, item in missing_shares.iterrows():
                    failures.append({"筛选日期": day_label, "股票代码": item["代码"], "阶段": "历史市值", "失败原因": "无法取得筛选日当时总股本，无法验证"})
                coarse = coarse.loc[coarse["总市值"].between(MARKET_CAP_MIN, MARKET_CAP_MAX)]

            candidates: list[dict[str, object]] = []
            for candidate_index, (_, row) in enumerate(coarse.iterrows()):
                code = row["数据源代码"]
                try:
                    stock_history = daily_history.loc[daily_history["数据源代码"].eq(code)]
                    limit_count = _recent_limit_up_count(stock_history, selection)
                    if limit_count is None:
                        raise BacktestDataError("当日前20日涨停判断数据不足")
                    if limit_count < 1:
                        continue
                    prior_dates = list(calendar[max(0, calendar_position - BACKTEST_MINUTE_LOOKBACK_TRADING_DAYS):calendar_position])
                    if len(prior_dates) < BACKTEST_MINUTE_LOOKBACK_TRADING_DAYS:
                        raise BacktestDataError("近似量比前5日交易日不足")
                    minute_start = datetime.combine(prior_dates[0].date(), clock_time(9, 30))
                    minute_end = datetime.combine(selection.date(), clock_time(15, 0))
                    minutes = fetch_historical_minutes(
                        code,
                        minute_start.strftime("%Y-%m-%d %H:%M:%S"),
                        minute_end.strftime("%Y-%m-%d %H:%M:%S"),
                    )
                    minute_result = _analyze_minutes(minutes, selection, prior_dates)
                    if minute_result["量比"] <= VOLUME_RATIO_MIN or not minute_result["尾盘合格"]:
                        continue
                    next_quote = stock_history.loc[stock_history["日期"].eq(next_day)]
                    if next_quote.empty or next_quote[["开盘", "最高", "最低", "收盘"]].isna().any().any():
                        raise BacktestDataError("次日行情不完整")
                    next_quote = next_quote.iloc[-1]
                    score = _historical_score(
                        row,
                        float(minute_result["量比"]),
                        float(minute_result["尾盘结构评分"]),
                        market_advance_ratio,
                    )
                    candidates.append(
                        {
                            "row": row,
                            "limit_count": limit_count,
                            "minute": minute_result,
                            "next": next_quote,
                            "score": score,
                        }
                    )
                except Exception as error:
                    failures.append({"筛选日期": day_label, "股票代码": row["代码"], "阶段": "单股验证", "失败原因": str(error)[:200]})
                if candidate_index + 1 < len(coarse):
                    time.sleep(BACKTEST_REQUEST_INTERVAL_SECONDS)

            candidates.sort(key=lambda item: item["score"], reverse=True)
            for rank, item in enumerate(candidates[:max_stocks], start=1):
                row, minute_result, next_quote = item["row"], item["minute"], item["next"]
                open_return = (next_quote["收盘"] / next_quote["开盘"] - 1) * 100
                late_return = (next_quote["收盘"] / minute_result["尾盘买入价"] - 1) * 100
                selected_return = open_return if return_basis.startswith("A") else late_return
                selected_rows.append(
                    {
                        "筛选日期": day_label,
                        "股票代码": row["代码"],
                        "股票名称": row["名称"],
                        "当日排名": rank,
                        "当日综合评分": item["score"],
                        "当日涨跌幅": row["涨跌幅"],
                        "当日量比": minute_result["量比"],
                        "量比口径": "近似量比（当日累计量/前5日同一时刻平均累计量）",
                        "当日换手率": row["换手率"],
                        "当日总市值": row["总市值"],
                        "近20日涨停次数": item["limit_count"],
                        "尾盘买入价": minute_result["尾盘买入价"],
                        "次日开盘价": next_quote["开盘"],
                        "次日最高价": next_quote["最高"],
                        "次日最低价": next_quote["最低"],
                        "次日收盘价": next_quote["收盘"],
                        "次日开盘到收盘收益率": open_return,
                        "尾盘到次日收盘收益率": late_return,
                        "是否盈利": "是" if selected_return > 0 else "否",
                        "数据完整性": 0.60,
                        "数据缺失项": "历史主力资金、历史板块强度（对应评分计0分）",
                        "数据来源": "已配置历史行情接口；AKShare交易日历",
                    }
                )
            details.extend(selected_rows)
            chosen_return = "次日开盘到收盘收益率" if return_basis.startswith("A") else "尾盘到次日收盘收益率"
            average_return = (
                float(pd.DataFrame(selected_rows)[chosen_return].mean()) if selected_rows else 0.0
            )
            daily_records.append({"筛选日期": day_label, "次日日期": next_day.strftime("%Y-%m-%d"), "入选数量": len(selected_rows), "每日等权平均收益率": average_return, "状态": "完成" if selected_rows else "空仓"})
        except Exception as error:
            failures.append({"筛选日期": day_label, "股票代码": "", "阶段": "单日回测", "失败原因": str(error)[:200]})
            daily_records.append({"筛选日期": day_label, "次日日期": next_day.strftime("%Y-%m-%d"), "入选数量": 0, "每日等权平均收益率": pd.NA, "状态": "数据失败"})

    daily = pd.DataFrame(daily_records)
    if not daily.empty:
        daily["累计收益率"] = (
            (1 + pd.to_numeric(daily["每日等权平均收益率"], errors="coerce").fillna(0) / 100)
            .cumprod()
            .sub(1)
            .mul(100)
        )
    detail_frame = pd.DataFrame(details, columns=DETAIL_COLUMNS)
    failure_frame = pd.DataFrame(failures, columns=["筛选日期", "股票代码", "阶段", "失败原因"])
    return_column = "次日开盘到收盘收益率" if return_basis.startswith("A") else "尾盘到次日收盘收益率"
    parameters = {
        "开始日期": str(start_date), "结束日期": str(end_date),
        "实际回测日数量上限": max_days, "每日最多股票数量": max_stocks,
        "收益口径": return_basis, "量比口径": "近似量比",
        "未来数据控制": "股票池、名称、日线、总股本和分钟数据均以筛选日为截止；仅次日OHLC用于收益评价",
    }
    summary = _summary_metrics(daily, detail_frame, return_column)
    summary["数据失败交易日数量"] = int(daily["状态"].eq("数据失败").sum()) if not daily.empty else 0
    if progress:
        progress(1.0, "历史回测完成")
    return BacktestResult(summary, daily, detail_frame, failure_frame, parameters)
