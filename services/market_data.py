"""AKShare 市场行情访问服务。"""

import logging

import akshare as ak
import pandas as pd

from services.network import call_with_proxy_fallback


logger = logging.getLogger(__name__)


class MarketDataError(RuntimeError):
    """行情获取或校验失败。"""


def fetch_a_share_spot() -> pd.DataFrame:
    """获取全 A 股实时行情，并返回独立的 DataFrame。"""
    try:
        data = call_with_proxy_fallback(ak.stock_zh_a_spot_em)
        source = "AKShare · 东方财富"
    except Exception as primary_error:
        logger.warning(
            "东方财富实时行情不可用，尝试 AKShare 新浪备用源：%s",
            type(primary_error).__name__,
        )
        try:
            data = call_with_proxy_fallback(ak.stock_zh_a_spot)
            source = "AKShare · 新浪备用源"
        except Exception as fallback_error:
            raise MarketDataError(
                "AKShare 主用和备用行情源均请求失败"
                f"（{type(primary_error).__name__} / {type(fallback_error).__name__}）"
            ) from fallback_error

    if not isinstance(data, pd.DataFrame):
        raise MarketDataError("行情接口未返回 pandas DataFrame")
    if data.empty:
        raise MarketDataError("行情接口返回了空数据")

    required_columns = {"代码", "名称", "最新价", "涨跌幅", "成交量"}
    missing_columns = required_columns.difference(data.columns)
    if missing_columns:
        missing = "、".join(sorted(missing_columns))
        raise MarketDataError(f"行情数据缺少必要字段：{missing}")

    result = data.copy()
    result["代码"] = (
        result["代码"].astype("string").str.lower()
        .str.replace(r"^(sh|sz|bj)", "", regex=True).str.zfill(6)
    )
    result["名称"] = result["名称"].astype("string")

    numeric_columns = [
        "最新价", "涨跌幅", "换手率", "量比", "总市值", "最高", "最低",
        "成交量", "成交额",
    ]
    for column in numeric_columns:
        if column not in result.columns:
            # 备用源不提供全部策略字段；保留缺失值，由筛选层如实排除。
            result[column] = pd.NA
        result[column] = pd.to_numeric(result[column], errors="coerce")

    result.attrs["data_source"] = source
    result.attrs["is_fallback"] = "备用源" in source
    result["当前行情数据源"] = source
    return result
