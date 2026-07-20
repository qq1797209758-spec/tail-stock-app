"""第一版 A 股尾盘策略筛选。"""

from dataclasses import dataclass
import re

import pandas as pd

from config import (
    EXCLUDED_NAME_KEYWORDS,
    MAIN_BOARD_CODE_PREFIXES,
    MARKET_CAP_MAX,
    MARKET_CAP_MIN,
    PRICE_CHANGE_MAX,
    PRICE_CHANGE_MIN,
    TURNOVER_RATE_MAX,
    TURNOVER_RATE_MIN,
    VOLUME_RATIO_MIN,
)


REQUIRED_COLUMNS = {"代码", "名称", "涨跌幅", "量比", "换手率", "总市值"}


@dataclass(frozen=True)
class FilterResult:
    """保存两阶段筛选结果。"""

    initial: pd.DataFrame
    final: pd.DataFrame


def apply_filters(data: pd.DataFrame) -> FilterResult:
    """依次执行主板/名称初筛和行情数值终筛。"""
    missing_columns = REQUIRED_COLUMNS.difference(data.columns)
    if missing_columns:
        missing = "、".join(sorted(missing_columns))
        raise ValueError(f"筛选数据缺少必要字段：{missing}")

    working = data.copy()
    codes = working["代码"].astype("string").str.zfill(6)
    names = working["名称"].astype("string")
    excluded_pattern = "|".join(
        re.escape(keyword) for keyword in EXCLUDED_NAME_KEYWORDS
    )

    main_board_mask = codes.str.startswith(MAIN_BOARD_CODE_PREFIXES, na=False)
    allowed_name_mask = ~names.str.contains(
        excluded_pattern,
        case=False,
        regex=True,
        na=False,
    )
    initial = working.loc[main_board_mask & allowed_name_mask].copy()

    final_mask = (
        initial["涨跌幅"].between(
            PRICE_CHANGE_MIN,
            PRICE_CHANGE_MAX,
            inclusive="both",
        )
        & initial["量比"].gt(VOLUME_RATIO_MIN)
        & initial["换手率"].between(
            TURNOVER_RATE_MIN,
            TURNOVER_RATE_MAX,
            inclusive="both",
        )
        & initial["总市值"].between(
            MARKET_CAP_MIN,
            MARKET_CAP_MAX,
            inclusive="both",
        )
    )
    final = initial.loc[final_mask].copy()
    final.sort_values(
        ["涨跌幅", "量比"],
        ascending=[False, False],
        inplace=True,
    )

    return FilterResult(
        initial=initial.reset_index(drop=True),
        final=final.reset_index(drop=True),
    )
