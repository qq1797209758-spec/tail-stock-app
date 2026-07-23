"""100 分评分所需的免费公开数据。"""

from datetime import datetime, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

import akshare as ak
import pandas as pd

from services.network import call_with_proxy_fallback


class ScoringDataError(RuntimeError):
    """评分附加数据请求失败。"""


@lru_cache(maxsize=4)
def fetch_industry_strength() -> pd.DataFrame:
    """获取行业当日涨跌幅。"""
    try:
        data = call_with_proxy_fallback(
            lambda: ak.stock_fund_flow_industry(symbol="即时")
        )
    except Exception as error:
        raise ScoringDataError(
            f"行业强度请求失败（{type(error).__name__}）"
        ) from error
    required = {"行业", "行业-涨跌幅"}
    if not isinstance(data, pd.DataFrame) or not required.issubset(data.columns):
        raise ScoringDataError("行业强度数据格式不完整")
    result = data.loc[:, ["行业", "行业-涨跌幅"]].copy()
    result["行业"] = result["行业"].astype("string")
    result["行业-涨跌幅"] = pd.to_numeric(result["行业-涨跌幅"], errors="coerce")
    return result.dropna().reset_index(drop=True)


@lru_cache(maxsize=4096)
def fetch_stock_scoring_context(stock_code: str) -> dict[str, object]:
    """获取单股所属行业和最新主力净流入占比。"""
    code = str(stock_code).zfill(6)
    context: dict[str, object] = {
        "所属行业": None,
        "主力净流入占比": None,
        "主力资金净流入": None,
        "数据缺失": [],
    }

    try:
        profile = call_with_proxy_fallback(
            lambda: ak.stock_profile_cninfo(symbol=code)
        )
        if isinstance(profile, pd.DataFrame) and not profile.empty and "所属行业" in profile:
            industry = profile.iloc[0]["所属行业"]
            if pd.notna(industry) and str(industry).strip():
                context["所属行业"] = str(industry).strip()
            else:
                context["数据缺失"].append("所属行业")
        else:
            context["数据缺失"].append("所属行业")
    except Exception:
        context["数据缺失"].append("所属行业")

    market = "sh" if code.startswith("6") else "sz"
    fund_flow = None
    try:
        fund_flow = call_with_proxy_fallback(
            lambda: ak.stock_individual_fund_flow(stock=code, market=market)
        )
        if (
            isinstance(fund_flow, pd.DataFrame)
            and not fund_flow.empty
            and "主力净流入-净占比" in fund_flow
        ):
            values = pd.to_numeric(
                fund_flow["主力净流入-净占比"], errors="coerce"
            ).dropna()
            if not values.empty:
                context["主力净流入占比"] = float(values.iloc[-1])
            else:
                context["数据缺失"].append("主力资金净流入")
        else:
            context["数据缺失"].append("主力资金净流入")
    except Exception:
        context["数据缺失"].append("主力资金净流入")

    if isinstance(fund_flow, pd.DataFrame) and not fund_flow.empty and "主力净流入-净额" in fund_flow:
        amounts = pd.to_numeric(fund_flow["主力净流入-净额"], errors="coerce").dropna()
        if not amounts.empty:
            context["主力资金净流入"] = float(amounts.iloc[-1])
    if context["主力资金净流入"] is None:
        context["数据缺失"].append("主力资金净流入净额")

    return context


@lru_cache(maxsize=1024)
def fetch_industry_five_day_strength(industry_name: str) -> float:
    """用真实行业日线计算最近5个有效交易日收盘变化百分比。"""
    end = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    start = end - timedelta(days=20)
    try:
        data = call_with_proxy_fallback(
            lambda: ak.stock_board_industry_hist_em(
                symbol=str(industry_name),
                start_date=start.strftime("%Y%m%d"),
                end_date=end.strftime("%Y%m%d"),
                period="日k",
                adjust="",
            )
        )
    except Exception as error:
        raise ScoringDataError(f"行业近5日数据请求失败（{type(error).__name__}）") from error
    if not isinstance(data, pd.DataFrame) or data.empty or "收盘" not in data:
        raise ScoringDataError("行业近5日数据格式不完整")
    closes = pd.to_numeric(data["收盘"], errors="coerce").dropna().tail(5)
    if len(closes) < 2 or float(closes.iloc[0]) <= 0:
        raise ScoringDataError("行业近5日有效交易数据不足")
    return float((closes.iloc[-1] / closes.iloc[0] - 1) * 100)


def match_industry_change(
    profile_industry: object,
    industry_strength: pd.DataFrame,
) -> tuple[str | None, float | None]:
    """仅用明确的名称包含关系匹配行业，不进行猜测映射。"""
    if profile_industry is None or industry_strength.empty:
        return None, None
    profile_name = str(profile_industry).strip()
    matches = []
    for _, row in industry_strength.iterrows():
        board_name = str(row["行业"]).strip()
        if len(board_name) >= 2 and (
            board_name in profile_name or profile_name in board_name
        ):
            matches.append((len(board_name), board_name, float(row["行业-涨跌幅"])))
    if not matches:
        return None, None
    _, name, change = max(matches, key=lambda item: item[0])
    return name, change
