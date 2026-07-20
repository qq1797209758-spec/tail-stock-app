"""AKShare 市场行情访问服务。"""

import akshare as ak
import pandas as pd


class MarketDataError(RuntimeError):
    """行情获取或校验失败。"""


def fetch_a_share_spot() -> pd.DataFrame:
    """获取全 A 股实时行情，并返回独立的 DataFrame。"""
    try:
        data = ak.stock_zh_a_spot_em()
    except Exception as error:
        error_type = type(error).__name__
        raise MarketDataError(
            f"AKShare 行情请求失败（错误类型：{error_type}）"
        ) from error

    if not isinstance(data, pd.DataFrame):
        raise MarketDataError("行情接口未返回 pandas DataFrame")
    if data.empty:
        raise MarketDataError("行情接口返回了空数据")

    required_columns = {"代码", "名称", "最新价", "涨跌幅"}
    missing_columns = required_columns.difference(data.columns)
    if missing_columns:
        missing = "、".join(sorted(missing_columns))
        raise MarketDataError(f"行情数据缺少必要字段：{missing}")

    result = data.copy()
    result["代码"] = result["代码"].astype("string").str.zfill(6)
    result["名称"] = result["名称"].astype("string")

    numeric_columns = [
        "最新价", "涨跌幅", "换手率", "量比", "总市值", "最高", "最低"
    ]
    for column in numeric_columns:
        if column not in result.columns:
            raise MarketDataError(f"行情数据缺少必要字段：{column}")
        result[column] = pd.to_numeric(result[column], errors="coerce")

    return result
