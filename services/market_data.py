"""AKShare 市场行情访问服务。"""

import logging
import re

import akshare as ak
import pandas as pd
import requests

from services.network import call_with_proxy_fallback


logger = logging.getLogger(__name__)


class MarketDataError(RuntimeError):
    """行情获取或校验失败。"""


def _market_prefixed_code(code: object) -> str | None:
    normalized = re.sub(r"^(sh|sz|bj)", "", str(code).lower()).zfill(6)
    if normalized.startswith("60"):
        return "sh" + normalized
    if normalized.startswith("00"):
        return "sz" + normalized
    return None


def fetch_tencent_quote_supplement(codes: pd.Series, batch_size: int = 60) -> pd.DataFrame:
    """从腾讯真实行情批量补齐换手率、量比和总市值。"""
    symbols = [symbol for code in codes if (symbol := _market_prefixed_code(code))]
    records: list[dict[str, object]] = []
    errors: list[str] = []
    for start in range(0, len(symbols), batch_size):
        batch = symbols[start : start + batch_size]
        try:
            response = call_with_proxy_fallback(
                lambda batch=batch: requests.get(
                    "https://qt.gtimg.cn/q=" + ",".join(batch),
                    headers={"Referer": "https://gu.qq.com/"},
                    timeout=15,
                )
            )
            response.raise_for_status()
            text = response.content.decode("gbk", errors="replace")
            logger.info(
                "腾讯行情补齐 status=%s batch=%s response_bytes=%s",
                response.status_code, len(batch), len(response.content),
            )
        except Exception as error:
            errors.append(f"{type(error).__name__}: {error}")
            continue
        for match in re.finditer(r'v_\w+="([^"]*)";', text):
            fields = match.group(1).split("~")
            if len(fields) <= 49:
                continue
            code = str(fields[2]).zfill(6)
            records.append(
                {
                    "代码": code,
                    "换手率": pd.to_numeric(fields[38], errors="coerce"),
                    "总市值": pd.to_numeric(fields[45], errors="coerce") * 100_000_000,
                    "量比": pd.to_numeric(fields[49], errors="coerce"),
                }
            )
    result = pd.DataFrame(records).drop_duplicates("代码") if records else pd.DataFrame(
        columns=["代码", "换手率", "总市值", "量比"]
    )
    result.attrs["errors"] = errors
    return result


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

    logger.info(
        "基础行情原始响应 source=%s rows=%s columns=%s",
        source, len(data), list(data.columns),
    )
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

    supplement_errors: list[str] = []
    if "备用源" in source:
        supplement = fetch_tencent_quote_supplement(result["代码"])
        supplement_errors = list(supplement.attrs.get("errors", []))
        if not supplement.empty:
            result = result.merge(
                supplement, on="代码", how="left", suffixes=("", "_腾讯")
            )
            for column in ("换手率", "量比", "总市值"):
                tencent_column = column + "_腾讯"
                if tencent_column in result:
                    if column not in result:
                        result[column] = result[tencent_column]
                    else:
                        result[column] = result[column].combine_first(result[tencent_column])
                    result.drop(columns=tencent_column, inplace=True)
            source = "AKShare · 新浪 + 腾讯行情补齐"

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
    result.attrs["is_fallback"] = "新浪" in source
    result.attrs["supplement_errors"] = supplement_errors
    result["当前行情数据源"] = source
    logger.info(
        "基础行情标准化完成 source=%s rows=%s columns=%s valid_turnover=%s "
        "valid_volume_ratio=%s valid_market_cap=%s supplement_errors=%s",
        source,
        len(result),
        list(result.columns),
        int(result["换手率"].notna().sum()),
        int(result["量比"].notna().sum()),
        int(result["总市值"].notna().sum()),
        supplement_errors,
    )
    return result
