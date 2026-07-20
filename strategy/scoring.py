"""可审计的 100 分候选股评分。"""

import pandas as pd

from config import (
    FUND_FLOW_NET_RATIO_MAX,
    FUND_FLOW_NET_RATIO_MIN,
    PRICE_CHANGE_MAX,
    PRICE_CHANGE_MIN,
    SCORE_CAUTION_MAX,
    SCORE_FOCUS_MIN,
    SCORE_FUND_FLOW_PART,
    SCORE_LATE_SESSION_MAX,
    SCORE_MINIMUM,
    SCORING_MAX_RESULTS,
    SCORE_MARKET_SENTIMENT_MAX,
    SCORE_SECTOR_MAX,
    SCORE_TECH_CHANGE_PART,
    SCORE_TECH_CLOSE_POSITION_PART,
    SCORE_VOLUME_RATIO_MAX,
    SCORE_VOLUME_RATIO_MIN,
    SCORE_VOLUME_RATIO_PART,
    SCORE_WATCH_MAX,
    SCORE_WATCH_MIN,
    SECTOR_CHANGE_SCORE_MAX,
    SECTOR_CHANGE_SCORE_MIN,
)


def _scaled(value: object, lower: float, upper: float, points: float) -> float | None:
    if value is None or pd.isna(value) or upper <= lower:
        return None
    numeric = float(value)
    ratio = max(0.0, min(1.0, (numeric - lower) / (upper - lower)))
    return ratio * points


def calculate_candidate_score(
    row: pd.Series,
    main_fund_ratio: float | None,
    industry_name: str | None,
    industry_change: float | None,
    market_advance_ratio: float | None,
    context_missing: list[str],
) -> dict[str, object]:
    """按真实数据计算五项评分，缺失部分计 0 分。"""
    missing = list(dict.fromkeys(context_missing))
    available_points = 0.0

    fund_flow_points = _scaled(
        main_fund_ratio,
        FUND_FLOW_NET_RATIO_MIN,
        FUND_FLOW_NET_RATIO_MAX,
        SCORE_FUND_FLOW_PART,
    )
    if fund_flow_points is None:
        fund_flow_points = 0.0
        missing.append("主力资金净流入")
    else:
        available_points += SCORE_FUND_FLOW_PART
    volume_points = _scaled(
        row.get("量比"),
        SCORE_VOLUME_RATIO_MIN,
        SCORE_VOLUME_RATIO_MAX,
        SCORE_VOLUME_RATIO_PART,
    )
    if volume_points is None:
        volume_points = 0.0
        missing.append("量比")
    else:
        available_points += SCORE_VOLUME_RATIO_PART
    funds_score = fund_flow_points + volume_points

    sector_points = _scaled(
        industry_change,
        SECTOR_CHANGE_SCORE_MIN,
        SECTOR_CHANGE_SCORE_MAX,
        SCORE_SECTOR_MAX,
    )
    if sector_points is None:
        sector_points = 0.0
        missing.append("板块行业强度")
    else:
        available_points += SCORE_SECTOR_MAX

    change_points = _scaled(
        row.get("涨跌幅"),
        PRICE_CHANGE_MIN,
        PRICE_CHANGE_MAX,
        SCORE_TECH_CHANGE_PART,
    )
    if change_points is None:
        change_points = 0.0
        missing.append("涨跌幅")
    else:
        available_points += SCORE_TECH_CHANGE_PART
    close_position = None
    if all(pd.notna(row.get(column)) for column in ("最新价", "最高", "最低")):
        high, low = float(row["最高"]), float(row["最低"])
        if high > low:
            close_position = (float(row["最新价"]) - low) / (high - low)
    position_points = _scaled(
        close_position, 0.0, 1.0, SCORE_TECH_CLOSE_POSITION_PART
    )
    if position_points is None:
        position_points = 0.0
        missing.append("日内价格位置")
    else:
        available_points += SCORE_TECH_CLOSE_POSITION_PART
    technical_score = change_points + position_points

    late_points = _scaled(
        row.get("尾盘结构评分"), 0.0, 100.0, SCORE_LATE_SESSION_MAX
    )
    if late_points is None:
        late_points = 0.0
        missing.append("尾盘结构")
    else:
        available_points += SCORE_LATE_SESSION_MAX

    sentiment_points = _scaled(
        market_advance_ratio, 0.0, 1.0, SCORE_MARKET_SENTIMENT_MAX
    )
    if sentiment_points is None:
        sentiment_points = 0.0
        missing.append("市场上涨家数占比")
    else:
        available_points += SCORE_MARKET_SENTIMENT_MAX

    total = round(
        funds_score + sector_points + technical_score + late_points + sentiment_points,
        2,
    )
    if total >= SCORE_FOCUS_MIN:
        rating = "重点观察"
    elif SCORE_WATCH_MIN <= total <= SCORE_WATCH_MAX:
        rating = "观察"
    elif SCORE_MINIMUM <= total <= SCORE_CAUTION_MAX:
        rating = "谨慎观察"
    else:
        rating = "未入选"

    reasons = []
    if funds_score >= 21:
        reasons.append("资金表现较强")
    if sector_points >= 14:
        reasons.append("所属板块强度较高")
    if technical_score >= 14:
        reasons.append("日内技术形态较强")
    if late_points >= 14:
        reasons.append("尾盘结构通过近似验证")
    if sentiment_points >= 7:
        reasons.append("市场上涨情绪较强")

    risks = []
    if missing:
        risks.append("缺失数据已扣分：" + "、".join(dict.fromkeys(missing)))
    if main_fund_ratio is not None and main_fund_ratio < 0:
        risks.append("主力资金净流入占比为负")
    if industry_change is not None and industry_change < 0:
        risks.append("所属行业当日走弱")
    if float(row.get("尾盘回撤", 0) or 0) >= 0.005:
        risks.append("尾盘已出现一定回撤")
    if not risks:
        risks.append("仍存在次日低开及行情反转风险")

    observation = (
        "策略参考：次日观察量比能否继续大于1、价格能否守住今日最低价，"
        "并关注主力净流入与所属行业强度是否延续。"
    )
    raw_fund = "缺失" if main_fund_ratio is None else f"{main_fund_ratio:.2f}%"
    raw_industry = "缺失" if industry_change is None else f"{industry_change:.2f}%"
    raw_sentiment = "缺失" if market_advance_ratio is None else f"{market_advance_ratio:.1%}"
    score_basis = (
        f"资金{funds_score:.2f}/30（主力净流入{raw_fund}→{fund_flow_points:.2f}/20，"
        f"量比{row.get('量比')}→{volume_points:.2f}/10）；"
        f"板块{raw_industry}→{sector_points:.2f}/20；"
        f"技术{technical_score:.2f}/20（涨幅{row.get('涨跌幅')}%→{change_points:.2f}/10，"
        f"日内位置{close_position if close_position is not None else '缺失'}"
        f"→{position_points:.2f}/10）；尾盘{row.get('尾盘结构评分')}→{late_points:.2f}/20；"
        f"市场上涨占比{raw_sentiment}→{sentiment_points:.2f}/10"
    )
    return {
        "资金表现得分": round(funds_score, 2),
        "板块强度得分": round(sector_points, 2),
        "技术形态得分": round(technical_score, 2),
        "尾盘结构得分": round(late_points, 2),
        "市场情绪得分": round(sentiment_points, 2),
        "综合得分": total,
        "观察标记": rating,
        "所属行业": industry_name or "",
        "行业涨跌幅": industry_change,
        "主力净流入占比": main_fund_ratio,
        "缺失项": "、".join(dict.fromkeys(missing)),
        "数据完整度": round(available_points / 100.0, 4),
        "评分依据": score_basis,
        "入选原因": "；".join(reasons) if reasons else "各项得分未形成突出优势",
        "主要风险": "；".join(risks),
        "风险": "；".join(risks),
        "次日观察条件": observation,
    }


def select_top_candidates(scored: pd.DataFrame) -> pd.DataFrame:
    """按综合得分返回最多10只真实候选，未达80分者明确标记为研究递补。"""
    if scored.empty:
        return scored.copy()
    selected = (
        scored.sort_values("综合得分", ascending=False)
        .head(SCORING_MAX_RESULTS)
        .reset_index(drop=True)
    )
    below_threshold = selected["综合得分"].lt(SCORE_MINIMUM)
    selected.loc[below_threshold, "观察标记"] = "未达80分，仅供研究"
    selected["评分达标"] = selected["综合得分"].ge(SCORE_MINIMUM).map(
        {True: "是", False: "否"}
    )
    selected["名单说明"] = "达到评分门槛"
    selected.loc[below_threshold, "名单说明"] = (
        "真实候选不足10只时按综合得分递补；未达80分，仅供策略研究"
    )
    selected.insert(0, "排名", range(1, len(selected) + 1))
    return selected
