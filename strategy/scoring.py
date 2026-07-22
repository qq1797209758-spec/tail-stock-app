"""可审计的 100 分候选股评分，缺失分项按可用权重重归一化。"""

import pandas as pd

from config import (
    FUND_FLOW_NET_RATIO_MAX,
    FUND_FLOW_NET_RATIO_MIN,
    PRICE_CHANGE_MAX,
    SCORE_ACTIVITY_MAX,
    SCORE_FUNDS_MAX,
    SCORE_LATE_SESSION_MAX,
    SCORE_SECTOR_MAX,
    SCORE_TREND_MAX,
    SCORE_TURNOVER_MAX,
    SCORE_VOLUME_MAX,
    SECTOR_CHANGE_SCORE_MAX,
    SECTOR_CHANGE_SCORE_MIN,
)


def _number(value: object) -> float | None:
    try:
        return None if value is None or pd.isna(value) else float(value)
    except (TypeError, ValueError):
        return None


def _scaled(value: object, lower: float, upper: float, points: float) -> float | None:
    numeric = _number(value)
    if numeric is None or upper <= lower:
        return None
    return max(0.0, min(1.0, (numeric - lower) / (upper - lower))) * points


def _centered(value: object, low: float, ideal: float, high: float, points: float) -> float | None:
    numeric = _number(value)
    if numeric is None:
        return None
    if numeric <= low or numeric >= high:
        return 0.0
    if numeric <= ideal:
        return (numeric - low) / (ideal - low) * points
    return (high - numeric) / (high - ideal) * points


def calculate_candidate_score(
    row: pd.Series,
    main_fund_ratio: float | None,
    industry_name: str | None,
    industry_change: float | None,
    market_advance_ratio: float | None,
    context_missing: list[str],
) -> dict[str, object]:
    """按七项权重评分；缺失项不扣零分，而用剩余有效权重归一到100。"""
    missing = list(dict.fromkeys(context_missing))
    components: list[tuple[str, float, float | None]] = []

    late_raw = _number(row.get("尾盘结构评分"))
    late_score = None if late_raw is None else max(0.0, min(100.0, late_raw)) / 100 * SCORE_LATE_SESSION_MAX
    components.append(("尾盘走势与VWAP位置", SCORE_LATE_SESSION_MAX, late_score))

    volume_score = _scaled(row.get("量比"), 0.8, 3.0, SCORE_VOLUME_MAX)
    components.append(("成交量和量比", SCORE_VOLUME_MAX, volume_score))

    fund_score = _scaled(main_fund_ratio, FUND_FLOW_NET_RATIO_MIN, FUND_FLOW_NET_RATIO_MAX, SCORE_FUNDS_MAX)
    components.append(("主力资金净流入", SCORE_FUNDS_MAX, fund_score))

    sector_score = _scaled(industry_change, SECTOR_CHANGE_SCORE_MIN, SECTOR_CHANGE_SCORE_MAX, SCORE_SECTOR_MAX)
    components.append(("所属板块近5日强度", SCORE_SECTOR_MAX, sector_score))

    change_score = _centered(row.get("涨跌幅"), 0.0, 5.5, 10.0, SCORE_TREND_MAX * 0.6)
    position = None
    latest, high, low = (_number(row.get(name)) for name in ("最新价", "最高", "最低"))
    if latest is not None and high is not None and low is not None and high > low:
        position = (latest - low) / (high - low)
    position_score = _scaled(position, 0.0, 1.0, SCORE_TREND_MAX * 0.4)
    trend_score = None if change_score is None and position_score is None else (change_score or 0.0) + (position_score or 0.0)
    components.append(("当日趋势和涨幅位置", SCORE_TREND_MAX, trend_score))

    turnover_score = _centered(row.get("换手率"), 0.0, 7.5, 15.0, SCORE_TURNOVER_MAX)
    components.append(("换手率合理程度", SCORE_TURNOVER_MAX, turnover_score))

    limit_count = _number(row.get("20日涨停次数"))
    activity_score = None if limit_count is None else min(1.0, max(0.0, limit_count) / 2.0) * SCORE_ACTIVITY_MAX
    components.append(("最近20日涨停和活跃度", SCORE_ACTIVITY_MAX, activity_score))

    raw_points = 0.0
    available_weight = 0.0
    component_scores: dict[str, object] = {}
    for label, weight, score in components:
        if score is None:
            if not any(str(item).startswith(label) for item in missing):
                missing.append(label)
            component_scores[label + "得分"] = pd.NA
        else:
            raw_points += score
            available_weight += weight
            component_scores[label + "得分"] = round(score, 2)
    total = round(raw_points / available_weight * 100, 2) if available_weight else 0.0
    market_cap_available = _number(row.get("总市值")) is not None
    completeness = (available_weight + (10.0 if market_cap_available else 0.0)) / 110.0

    reasons = [
        f"{label}{score:.1f}/{weight:g}分"
        for label, weight, score in components if score is not None and score >= weight * 0.65
    ]
    risks: list[str] = []
    if missing:
        risks.append("以下指标缺失，得分已按可用权重重归一化：" + "、".join(dict.fromkeys(missing)))
    if main_fund_ratio is not None and main_fund_ratio < 0:
        risks.append("主力资金净流入占比为负")
    drawdown = _number(row.get("尾盘最大回撤"))
    if drawdown is not None and drawdown >= 0.005:
        risks.append("尾盘存在一定回撤")
    if market_advance_ratio is None:
        missing.append("市场情绪")
        risks.append("市场上涨家数占比缺失")
    elif market_advance_ratio < 0.4:
        risks.append(f"市场情绪偏弱（上涨家数占比{market_advance_ratio:.1%}）")
    if not risks:
        risks.append("模型仅表示相对更可能上涨，仍存在低开和行情反转风险")

    result = {
        **component_scores,
        "尾盘结构得分": component_scores["尾盘走势与VWAP位置得分"],
        "资金表现得分": component_scores["主力资金净流入得分"],
        "板块强度得分": component_scores["所属板块近5日强度得分"],
        "技术形态得分": component_scores["当日趋势和涨幅位置得分"],
        "综合得分": total,
        "观察标记": "相对高概率候选" if total >= 70 else "综合评分递补候选",
        "所属行业": industry_name or row.get("所属行业", ""),
        "近5日板块强度": industry_change,
        "市场情绪": market_advance_ratio,
        "行业涨跌幅": industry_change,
        "主力净流入占比": main_fund_ratio,
        "主力资金净流入": row.get("主力资金净流入", pd.NA),
        "缺失字段": "、".join(dict.fromkeys(missing)),
        "缺失项": "、".join(dict.fromkeys(missing)),
        "数据完整度": round(completeness, 4),
        "评分依据": "；".join(
            f"{label}:{'缺失' if score is None else f'{score:.2f}/{weight:g}'}"
            for label, weight, score in components
        ) + f"；按可用权重{available_weight:g}/100重归一化",
        "入选原因": "；".join(reasons) if reasons else "可用指标综合评分相对靠前",
        "主要风险": "；".join(risks),
        "风险提示": "；".join(risks),
        "风险": "；".join(risks),
        "次日观察条件": "关注量能、资金流向、板块强度及VWAP支撑是否延续。",
    }
    return result


def select_top_candidates(scored: pd.DataFrame) -> pd.DataFrame:
    """兼容旧调用；正式分层选择由 strategy.selection 完成。"""
    from strategy.selection import select_layered_top5

    return select_layered_top5(scored).selected
