"""真实行情候选池、分层递补和稳定 Top 5 选择。"""

from dataclasses import dataclass
import re

import pandas as pd

from config import (
    EXCLUDED_NAME_KEYWORDS,
    ENRICHMENT_CANDIDATE_LIMIT,
    LEVEL1_PRICE_CHANGE_MIN,
    LEVEL1_TURNOVER_RATE_MAX,
    LEVEL1_TURNOVER_RATE_MIN,
    LEVEL2_MARKET_CAP_MAX,
    LEVEL2_MARKET_CAP_MIN,
    LEVEL2_VOLUME_RATIO_MIN,
    MAIN_BOARD_CODE_PREFIXES,
    MARKET_CAP_MAX,
    MARKET_CAP_MIN,
    PRICE_CHANGE_MAX,
    PRICE_CHANGE_MIN,
    TARGET_SELECTION_COUNT,
    TURNOVER_RATE_MAX,
    TURNOVER_RATE_MIN,
    VOLUME_RATIO_MIN,
)


SELECTION_TYPES = (
    "严格入选", "一级递补", "二级递补", "三级递补", "综合评分递补"
)
LAYER_REASONS = {
    "严格入选": "满足严格涨幅、量比、换手率、市值、20日涨停及尾盘结构条件",
    "一级递补": "满足一级放宽后的涨幅2%-8%和换手率3%-12%条件",
    "二级递补": "满足二级放宽后的量比>0.8和市值20亿-500亿元条件",
    "三级递补": "尾盘结构合格，20日涨停由硬性条件改为评分项",
    "综合评分递补": "来自真实有效主板股票池，按可用指标重归一化后综合得分靠前",
}


@dataclass(frozen=True)
class LayeredSelectionResult:
    selected: pd.DataFrame
    funnel: dict[str, int]
    valid_universe_count: int
    missing_count: int


def build_valid_main_board_universe(data: pd.DataFrame) -> pd.DataFrame:
    """仅保留可由真实快照确认的沪深主板正常交易股票。"""
    required = {"代码", "名称", "最新价", "成交量"}
    missing = required.difference(data.columns)
    if missing:
        raise ValueError("行情数据缺少有效股票判定字段：" + "、".join(sorted(missing)))

    working = data.copy()
    working["代码"] = working["代码"].astype("string").str.zfill(6)
    working["名称"] = working["名称"].astype("string")
    for column in ("最新价", "成交量", "成交额", "涨跌幅", "量比", "换手率", "总市值"):
        if column not in working:
            working[column] = pd.NA
        working[column] = pd.to_numeric(working[column], errors="coerce")

    excluded_pattern = "|".join(re.escape(value) for value in EXCLUDED_NAME_KEYWORDS)
    valid = (
        working["代码"].str.startswith(MAIN_BOARD_CODE_PREFIXES, na=False)
        & ~working["名称"].str.contains(excluded_pattern, case=False, regex=True, na=False)
        & working["最新价"].gt(0)
        & working["成交量"].gt(0)
    )
    if "交易状态" in working:
        valid &= ~working["交易状态"].astype("string").str.contains("停牌|退市", na=False)
    result = working.loc[valid].drop_duplicates("代码", keep="first").copy()
    return result.sort_values("代码", kind="mergesort").reset_index(drop=True)


def _between(frame: pd.DataFrame, field: str, low: float, high: float) -> pd.Series:
    return pd.to_numeric(frame[field], errors="coerce").between(low, high, inclusive="both")


def build_enrichment_pool(
    universe: pd.DataFrame, limit: int = ENRICHMENT_CANDIDATE_LIMIT
) -> pd.DataFrame:
    """用快照字段确定性预排，控制逐股接口数量且保留各递补层候选。"""
    if universe.empty:
        return universe.copy()
    frame = universe.copy()
    change = pd.to_numeric(frame["涨跌幅"], errors="coerce")
    ratio = pd.to_numeric(frame["量比"], errors="coerce")
    turnover = pd.to_numeric(frame["换手率"], errors="coerce")
    cap = pd.to_numeric(frame["总市值"], errors="coerce")
    frame["_预评分"] = (
        (1 - (change - 5.5).abs().div(5.5)).clip(0, 1).fillna(0) * 35
        + ratio.div(3).clip(0, 1).fillna(0) * 25
        + (1 - (turnover - 7.5).abs().div(7.5)).clip(0, 1).fillna(0) * 20
        + cap.between(LEVEL2_MARKET_CAP_MIN, LEVEL2_MARKET_CAP_MAX).fillna(False).astype(int) * 20
    )
    relaxed = (
        change.between(LEVEL1_PRICE_CHANGE_MIN, PRICE_CHANGE_MAX, inclusive="both")
        & turnover.between(LEVEL1_TURNOVER_RATE_MIN, LEVEL1_TURNOVER_RATE_MAX, inclusive="both")
        & ratio.gt(LEVEL2_VOLUME_RATIO_MIN)
        & cap.between(LEVEL2_MARKET_CAP_MIN, LEVEL2_MARKET_CAP_MAX, inclusive="both")
    )
    primary = frame.loc[relaxed]
    fallback = frame.loc[~frame["代码"].isin(primary["代码"])]
    ordered = pd.concat([
        primary.sort_values(["_预评分", "代码"], ascending=[False, True], kind="mergesort"),
        fallback.sort_values(["_预评分", "代码"], ascending=[False, True], kind="mergesort"),
    ])
    return ordered.head(limit).drop(columns="_预评分").reset_index(drop=True)


def selection_masks(scored: pd.DataFrame) -> dict[str, pd.Series]:
    """返回互相可重叠的层级资格；选择时按定义顺序去重。"""
    index = scored.index
    change_strict = _between(scored, "涨跌幅", PRICE_CHANGE_MIN, PRICE_CHANGE_MAX)
    change_relaxed = _between(scored, "涨跌幅", LEVEL1_PRICE_CHANGE_MIN, PRICE_CHANGE_MAX)
    turnover_strict = _between(scored, "换手率", TURNOVER_RATE_MIN, TURNOVER_RATE_MAX)
    turnover_relaxed = _between(scored, "换手率", LEVEL1_TURNOVER_RATE_MIN, LEVEL1_TURNOVER_RATE_MAX)
    cap_strict = _between(scored, "总市值", MARKET_CAP_MIN, MARKET_CAP_MAX)
    cap_relaxed = _between(scored, "总市值", LEVEL2_MARKET_CAP_MIN, LEVEL2_MARKET_CAP_MAX)
    ratio_strict = pd.to_numeric(scored["量比"], errors="coerce").gt(VOLUME_RATIO_MIN)
    ratio_relaxed = pd.to_numeric(scored["量比"], errors="coerce").gt(LEVEL2_VOLUME_RATIO_MIN)
    limit_up = scored.get("20日内是否涨停", pd.Series(pd.NA, index=index)).eq("是")
    late_ok = scored.get("尾盘结构状态", pd.Series(pd.NA, index=index)).eq("合格")

    return {
        "严格入选": change_strict & turnover_strict & cap_strict & ratio_strict & limit_up & late_ok,
        "一级递补": change_relaxed & turnover_relaxed & cap_strict & ratio_strict & limit_up & late_ok,
        "二级递补": change_relaxed & turnover_relaxed & cap_relaxed & ratio_relaxed & limit_up & late_ok,
        "三级递补": change_relaxed & turnover_relaxed & cap_relaxed & ratio_relaxed & late_ok,
        "综合评分递补": pd.Series(True, index=index),
    }


def stable_candidate_sort(frame: pd.DataFrame) -> pd.DataFrame:
    """实现评分、完整度、资金、VWAP、回撤及代码的确定性排序。"""
    result = frame.copy()
    def sort_values(field: str, default: float) -> pd.Series:
        values = result.get(field, pd.Series(pd.NA, index=result.index))
        return pd.to_numeric(values, errors="coerce").fillna(default)

    result["_得分排序"] = sort_values("综合得分", float("-inf"))
    result["_完整度排序"] = sort_values("数据完整度", float("-inf"))
    fund_amount = sort_values("主力资金净流入", float("-inf"))
    fund_ratio = sort_values("主力净流入占比", float("-inf"))
    result["_资金排序"] = fund_amount.where(fund_amount.ne(float("-inf")), fund_ratio)
    result["_回撤排序"] = sort_values("尾盘最大回撤", float("inf"))
    result["_VWAP排序"] = result.get(
        "VWAP状态", pd.Series("", index=result.index)
    ).eq("合格").astype(int)
    return result.sort_values(
        ["_得分排序", "_完整度排序", "_资金排序", "_VWAP排序", "_回撤排序", "代码"],
        ascending=[False, False, False, False, True, True],
        kind="mergesort",
    ).drop(columns=["_得分排序", "_完整度排序", "_资金排序", "_VWAP排序", "_回撤排序"])


def select_layered_top5(scored: pd.DataFrame, target: int = TARGET_SELECTION_COUNT) -> LayeredSelectionResult:
    """依次从五层真实候选中选择，最多 target 只，不生成或随机补充。"""
    if scored.empty:
        return LayeredSelectionResult(scored.copy(), {name: 0 for name in SELECTION_TYPES}, 0, target)
    working = scored.drop_duplicates("代码", keep="first").copy()
    # 尾盘更新后的Top5会再次稳定排序；旧排名属于展示字段，必须先移除。
    working.drop(columns=["排名"], errors="ignore", inplace=True)
    masks = selection_masks(working)
    funnel = {name: int(mask.sum()) for name, mask in masks.items()}
    selected_parts: list[pd.DataFrame] = []
    selected_codes: set[str] = set()
    for selection_type in SELECTION_TYPES:
        eligible = working.loc[masks[selection_type] & ~working["代码"].astype(str).isin(selected_codes)]
        eligible = stable_candidate_sort(eligible)
        needed = target - sum(len(part) for part in selected_parts)
        if needed <= 0:
            break
        picked = eligible.head(needed).copy()
        if not picked.empty:
            picked["入选类型"] = selection_type
            existing_reason = picked.get("入选原因", pd.Series("", index=picked.index)).fillna("").astype(str)
            picked["入选原因"] = LAYER_REASONS[selection_type] + "；" + existing_reason
            picked["入选原因"] = picked["入选原因"].str.rstrip("；")
            selected_parts.append(picked)
            selected_codes.update(picked["代码"].astype(str))
    selected = pd.concat(selected_parts, ignore_index=True) if selected_parts else working.head(0).copy()
    selected = stable_candidate_sort(selected).head(target).reset_index(drop=True)
    selected.insert(0, "排名", range(1, len(selected) + 1))
    return LayeredSelectionResult(selected, funnel, len(working), max(0, target - len(selected)))
