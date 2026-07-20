"""A股尾盘策略筛选 Web 应用。"""

from datetime import datetime
import time
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

from components.stock_table import build_excel, render_stock_results
from config import (
    APP_DESCRIPTION,
    APP_TITLE,
    DATA_CACHE_TTL,
    EXCLUDED_NAME_KEYWORDS,
    FUND_FLOW_NET_RATIO_MAX,
    FUND_FLOW_NET_RATIO_MIN,
    HISTORY_CACHE_TTL,
    HISTORY_REQUEST_INTERVAL_SECONDS,
    HISTORY_TRADING_DAYS,
    INTRADAY_CACHE_TTL,
    INTRADAY_REQUEST_INTERVAL_SECONDS,
    MAIN_BOARD_CODE_PREFIXES,
    MARKET_CAP_MAX,
    MARKET_CAP_MIN,
    MARKET_CAP_UNIT,
    LIMIT_UP_CHANGE_THRESHOLD,
    LATE_LAST_MINUTES,
    LATE_MAX_DRAWDOWN,
    LATE_RAPID_DROP_MAX_CONSECUTIVE,
    LATE_RAPID_DROP_PER_MINUTE,
    LATE_SESSION_START_TIME,
    LATE_VOLUME_EXPANSION_RATIO,
    LATE_VWAP_ABOVE_RATIO_MIN,
    PRICE_CHANGE_MAX,
    PRICE_CHANGE_MIN,
    SCORE_MINIMUM,
    SCORE_FUND_FLOW_PART,
    SCORE_LATE_SESSION_MAX,
    SCORE_MARKET_SENTIMENT_MAX,
    SCORE_SECTOR_MAX,
    SCORE_TECH_CHANGE_PART,
    SCORE_TECH_CLOSE_POSITION_PART,
    SCORE_VOLUME_RATIO_MAX,
    SCORE_VOLUME_RATIO_MIN,
    SCORE_VOLUME_RATIO_PART,
    SCORING_CACHE_TTL,
    SCORING_MAX_RESULTS,
    SCORING_REQUEST_INTERVAL_SECONDS,
    SECTOR_CHANGE_SCORE_MAX,
    SECTOR_CHANGE_SCORE_MIN,
    TURNOVER_RATE_MAX,
    TURNOVER_RATE_MIN,
    VOLUME_RATIO_MIN,
)
from services.market_data import MarketDataError, fetch_a_share_spot
from services.history_data import HistoryDataError, analyze_recent_limit_up
from services.late_session import LateSessionDataError, analyze_late_session
from services.scoring_data import (
    ScoringDataError,
    fetch_industry_strength,
    fetch_stock_scoring_context,
    match_industry_change,
)
from strategy.filters import apply_filters
from strategy.scoring import calculate_candidate_score, select_top_candidates
from utils.logger import get_logger


logger = get_logger(__name__)


@st.cache_data(ttl=DATA_CACHE_TTL, show_spinner=False)
def load_market_data():
    data = fetch_a_share_spot()
    updated_at = datetime.now(ZoneInfo("Asia/Shanghai"))
    return data, updated_at


@st.cache_data(ttl=HISTORY_CACHE_TTL, show_spinner=False)
def load_limit_up_result(stock_code: str) -> dict[str, str]:
    """缓存单股历史判断，避免重复请求同一股票。"""
    return analyze_recent_limit_up(stock_code)


@st.cache_data(ttl=INTRADAY_CACHE_TTL, show_spinner=False)
def load_late_session_result(stock_code: str) -> dict[str, object]:
    """缓存单股免费分钟行情分析结果。"""
    return analyze_late_session(stock_code)


@st.cache_data(ttl=SCORING_CACHE_TTL, show_spinner=False)
def load_industry_strength() -> pd.DataFrame:
    return fetch_industry_strength()


@st.cache_data(ttl=SCORING_CACHE_TTL, show_spinner=False)
def load_stock_scoring_context(stock_code: str) -> dict[str, object]:
    return fetch_stock_scoring_context(stock_code)


def apply_responsive_styles() -> None:
    st.markdown(
        """
        <style>
        html, body { max-width: 100%; }
        [data-testid="stMainBlockContainer"] {
            width: 100%; max-width: none;
            padding: 2rem clamp(1rem, 3vw, 3rem) 3rem;
        }
        [data-testid="stMetric"] {
            border: 1px solid rgba(128,128,128,.22); border-radius: 14px;
            padding: 1rem; background: rgba(128,128,128,.05);
        }
        [data-testid="stAlert"] { border-radius: 12px; }
        .stock-desktop-table {
            width: 100%; max-width: 100%; overflow-x: auto; margin-top: .5rem;
            -webkit-overflow-scrolling: touch; overscroll-behavior-inline: contain;
        }
        .stock-desktop-table table { width: 100%; border-collapse: collapse; font-size: .92rem; }
        .stock-desktop-table th { background: #eaf2f8; color: #17324d; text-align: left; }
        .stock-desktop-table th, .stock-desktop-table td {
            padding: .72rem .8rem; border-bottom: 1px solid rgba(128,128,128,.2); white-space: nowrap;
        }
        .stock-mobile-cards { display: none; }
        @media (max-width: 767px) {
            [data-testid="stMainBlockContainer"] { width: 100%; padding: 1rem .85rem 2rem; }
            h1 { font-size: 1.65rem !important; line-height: 1.3 !important; }
            h2 { font-size: 1.3rem !important; line-height: 1.35 !important; }
            h3 { font-size: 1.12rem !important; line-height: 1.4 !important; }
            p, li, label, button { font-size: 1rem !important; line-height: 1.55 !important; }
            [data-testid="stHorizontalBlock"] {
                flex-direction: column !important; gap: .75rem !important;
            }
            [data-testid="stHorizontalBlock"] > div {
                width: 100% !important; min-width: 0 !important; flex: 1 1 100% !important;
            }
            [data-testid="stMetric"] { padding: .85rem; width: 100%; }
            [data-testid="stButton"], [data-testid="stDownloadButton"] { width: 100%; }
            [data-testid="stButton"] button,
            [data-testid="stDownloadButton"] button {
                width: 100% !important; min-height: 2.8rem; padding: .7rem 1rem;
                touch-action: manipulation;
            }
            .stock-desktop-table { display: none; }
            .stock-mobile-cards { display: grid; gap: .75rem; }
            .stock-card {
                width: 100%; min-width: 0; padding: 1rem;
                border: 1px solid rgba(128,128,128,.22);
                border-radius: 14px; background: rgba(128,128,128,.04);
            }
            .stock-card-title { display: flex; justify-content: space-between; gap: 1rem; margin-bottom: .8rem; }
            .stock-card-title strong, .stock-card-title span { overflow-wrap: anywhere; }
            .stock-card-title span { color: #617181; font-variant-numeric: tabular-nums; }
            .stock-card-grid { display: grid; grid-template-columns: 1fr; gap: .7rem; }
            .stock-card-grid div {
                display: grid; grid-template-columns: minmax(7rem, 42%) minmax(0, 1fr);
                align-items: start; gap: .6rem; padding-bottom: .55rem;
                border-bottom: 1px solid rgba(128,128,128,.14);
            }
            .stock-card-grid small { color: #617181; margin-bottom: .15rem; }
            .stock-card-grid b { font-weight: 600; overflow-wrap: anywhere; word-break: break-word; }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def render_strategy_sidebar() -> None:
    with st.sidebar:
        st.header("策略参数")
        st.caption("参数统一读取自 config.py")
        st.write(f"**市场范围：** 沪深主板（代码前缀：{'、'.join(MAIN_BOARD_CODE_PREFIXES)}）")
        st.write(f"**排除名称：** {'、'.join(EXCLUDED_NAME_KEYWORDS)}")
        st.write(f"**涨跌幅：** {PRICE_CHANGE_MIN:g}% ～ {PRICE_CHANGE_MAX:g}%")
        st.write(f"**量比：** > {VOLUME_RATIO_MIN:g}")
        st.write(f"**换手率：** {TURNOVER_RATE_MIN:g}% ～ {TURNOVER_RATE_MAX:g}%")
        st.write(
            f"**总市值：** {MARKET_CAP_MIN / MARKET_CAP_UNIT:g}亿 ～ "
            f"{MARKET_CAP_MAX / MARKET_CAP_UNIT:g}亿元"
        )
        st.divider()
        st.write(f"**历史范围：** 最近 {HISTORY_TRADING_DAYS} 个交易日")
        st.write(f"**涨停容差：** 涨跌幅 ≥ {LIMIT_UP_CHANGE_THRESHOLD:g}%")
        st.write(f"**请求间隔：** {HISTORY_REQUEST_INTERVAL_SECONDS:g} 秒/股")
        st.write(f"**历史缓存：** {HISTORY_CACHE_TTL // 60} 分钟")
        st.divider()
        st.write("**尾盘结构：** 免费分钟数据近似判断")
        st.write(f"**分析起点：** {LATE_SESSION_START_TIME[:5]}")
        st.write(f"**VWAP要求：** 上方分钟占比 ≥ {LATE_VWAP_ABOVE_RATIO_MIN:.0%}")
        st.write(f"**最大回撤：** ≤ {LATE_MAX_DRAWDOWN:.1%}")
        st.write(
            f"**快速下跌：** 单分钟 ≤ {LATE_RAPID_DROP_PER_MINUTE:.1%}，"
            f"最多连续 {LATE_RAPID_DROP_MAX_CONSECUTIVE} 次"
        )
        st.write(f"**量能放大：** 尾盘/此前均量 ≥ {LATE_VOLUME_EXPANSION_RATIO:g} 倍")
        st.divider()
        st.write("**综合评分：** 资金30 + 板块20 + 技术20 + 尾盘20 + 情绪10")
        st.write(f"**入选门槛：** ≥ {SCORE_MINIMUM:g} 分")
        st.write(f"**最多展示：** {SCORING_MAX_RESULTS} 只")
        st.caption("评分数据缺失时，对应分项计0分并在结果中说明。")
        with st.expander("查看评分计算依据"):
            st.write(
                f"**资金表现 30分：** 主力净流入占比从 {FUND_FLOW_NET_RATIO_MIN:g}% "
                f"到 {FUND_FLOW_NET_RATIO_MAX:g}% 线性计 0～{SCORE_FUND_FLOW_PART:g}分；"
                f"量比从 {SCORE_VOLUME_RATIO_MIN:g} 到 {SCORE_VOLUME_RATIO_MAX:g} "
                f"线性计 0～{SCORE_VOLUME_RATIO_PART:g}分。"
            )
            st.write(
                f"**板块强度 20分：** 所属行业涨跌幅从 {SECTOR_CHANGE_SCORE_MIN:g}% "
                f"到 {SECTOR_CHANGE_SCORE_MAX:g}% 线性计 0～{SCORE_SECTOR_MAX:g}分。"
            )
            st.write(
                f"**技术形态 20分：** 当前涨跌幅计 0～{SCORE_TECH_CHANGE_PART:g}分；"
                f"最新价在当日最高/最低区间的位置计 0～{SCORE_TECH_CLOSE_POSITION_PART:g}分。"
            )
            st.write(
                f"**尾盘结构 20分：** 免费分钟行情尾盘结构评分按比例折算至"
                f" {SCORE_LATE_SESSION_MAX:g}分。"
            )
            st.write(
                f"**市场情绪 10分：** 全市场上涨股票占比按比例折算至"
                f" {SCORE_MARKET_SENTIMENT_MAX:g}分。"
            )


def apply_limit_up_filter(candidates):
    """只对第一轮候选股执行历史涨停二次筛选。"""
    if candidates.empty:
        empty = candidates.copy()
        empty["涨停判断"] = pd.Series(dtype="string")
        empty["最近涨停日期"] = pd.Series(dtype="string")
        return empty, empty.copy()

    processed_rows = []
    total = len(candidates)
    progress = st.progress(0, text="准备查询个股历史行情……")

    for position, (_, row) in enumerate(candidates.iterrows(), start=1):
        stock_code = str(row["代码"]).zfill(6)
        stock_name = str(row["名称"])
        progress.progress(
            position / total,
            text=f"正在处理 {position}/{total}：{stock_name}（{stock_code}）",
        )
        try:
            result = load_limit_up_result(stock_code)
        except HistoryDataError as error:
            logger.warning("股票 %s 历史判断跳过：%s", stock_code, error)
            result = {"涨停判断": "数据不足", "最近涨停日期": ""}
        except Exception as error:
            logger.exception("股票 %s 历史判断发生未知错误", stock_code)
            result = {"涨停判断": "数据不足", "最近涨停日期": ""}

        output_row = row.copy()
        output_row["涨停判断"] = result["涨停判断"]
        output_row["最近涨停日期"] = result["最近涨停日期"]
        processed_rows.append(output_row)

        if position < total:
            time.sleep(HISTORY_REQUEST_INTERVAL_SECONDS)

    progress.empty()
    processed = pd.DataFrame(processed_rows).reset_index(drop=True)
    final = processed.loc[processed["涨停判断"].eq("符合")].copy()
    return processed, final.reset_index(drop=True)


def apply_late_session_filter(candidates):
    """仅对涨停条件通过的候选股执行免费分钟数据近似分析。"""
    if candidates.empty:
        empty = candidates.copy()
        empty["尾盘结构状态"] = pd.Series(dtype="string")
        empty["尾盘结构评分"] = pd.Series(dtype="float64")
        empty["尾盘排除原因"] = pd.Series(dtype="string")
        return empty, empty.copy()

    processed_rows = []
    total = len(candidates)
    progress = st.progress(0, text="准备分析尾盘分钟结构……")

    for position, (_, row) in enumerate(candidates.iterrows(), start=1):
        stock_code = str(row["代码"]).zfill(6)
        stock_name = str(row["名称"])
        progress.progress(
            position / total,
            text=f"尾盘结构 {position}/{total}：{stock_name}（{stock_code}）",
        )
        try:
            result = load_late_session_result(stock_code)
        except LateSessionDataError as error:
            logger.warning("股票 %s 尾盘结构无法验证：%s", stock_code, error)
            result = {
                "尾盘结构状态": "无法验证",
                "尾盘结构评分": pd.NA,
                "尾盘排除原因": str(error),
            }
        except Exception as error:
            logger.exception("股票 %s 尾盘结构发生未知错误", stock_code)
            result = {
                "尾盘结构状态": "无法验证",
                "尾盘结构评分": pd.NA,
                "尾盘排除原因": f"未知错误（{type(error).__name__}）",
            }

        output_row = row.copy()
        for key, value in result.items():
            output_row[key] = value
        processed_rows.append(output_row)

        if position < total:
            time.sleep(INTRADAY_REQUEST_INTERVAL_SECONDS)

    progress.empty()
    processed = pd.DataFrame(processed_rows).reset_index(drop=True)
    qualified = processed.loc[processed["尾盘结构状态"].eq("合格")].copy()
    return processed, qualified.reset_index(drop=True)


def apply_candidate_scoring(candidates, market_data):
    """对尾盘结构合格股票进行可审计评分并返回 Top 5。"""
    if candidates.empty:
        empty = candidates.copy()
        empty["综合得分"] = pd.Series(dtype="float64")
        return empty, empty.copy()

    market_changes = pd.to_numeric(market_data["涨跌幅"], errors="coerce").dropna()
    market_advance_ratio = (
        float(market_changes.gt(0).mean()) if not market_changes.empty else None
    )
    try:
        industry_strength = load_industry_strength()
        industry_source_missing = False
    except ScoringDataError as error:
        logger.warning("行业强度数据不可用：%s", error)
        industry_strength = pd.DataFrame(columns=["行业", "行业-涨跌幅"])
        industry_source_missing = True

    progress = st.progress(0, text="准备计算候选股综合评分……")
    scored_rows = []
    total = len(candidates)
    for position, (_, row) in enumerate(candidates.iterrows(), start=1):
        code = str(row["代码"]).zfill(6)
        progress.progress(
            position / total,
            text=f"综合评分 {position}/{total}：{row['名称']}（{code}）",
        )
        try:
            context = load_stock_scoring_context(code)
        except Exception:
            logger.exception("股票 %s 评分附加数据失败", code)
            context = {
                "所属行业": None,
                "主力净流入占比": None,
                "数据缺失": ["所属行业", "主力资金净流入"],
            }

        matched_industry, industry_change = match_industry_change(
            context.get("所属行业"), industry_strength
        )
        missing = list(context.get("数据缺失", []))
        if industry_source_missing or industry_change is None:
            missing.append("板块行业强度")

        score = calculate_candidate_score(
            row=row,
            main_fund_ratio=context.get("主力净流入占比"),
            industry_name=matched_industry,
            industry_change=industry_change,
            market_advance_ratio=market_advance_ratio,
            context_missing=missing,
        )
        output_row = row.copy()
        for key, value in score.items():
            output_row[key] = value
        scored_rows.append(output_row)

        if position < total:
            time.sleep(SCORING_REQUEST_INTERVAL_SECONDS)

    progress.empty()
    scored = pd.DataFrame(scored_rows).sort_values(
        "综合得分", ascending=False
    ).reset_index(drop=True)
    selected = select_top_candidates(scored)
    return scored, selected.reset_index(drop=True)


def render_home() -> None:
    st.title(APP_TITLE)
    st.caption(APP_DESCRIPTION)
    st.warning(
        "本工具仅用于公开行情数据的筛选与分析，不是自动交易软件，"
        "不构成投资建议，也不承诺任何收益。"
    )
    st.write("点击开始扫描后，系统才会请求 AKShare 全 A 股实时行情。")

    if not st.button("开始扫描", type="primary", width="stretch"):
        st.info("尚未扫描。策略条件可在左侧栏查看。")
        return

    try:
        with st.spinner("正在获取行情并执行筛选，请稍候……"):
            market_data, updated_at = load_market_data()
            filter_result = apply_filters(market_data)
    except MarketDataError as error:
        logger.exception("AKShare 行情连接失败")
        st.error(f"扫描失败：{error}")
        st.info("请检查网络后重试；免费公开接口也可能暂时限流或维护。")
        return
    except Exception as error:
        logger.exception("策略扫描失败")
        st.error(f"扫描失败：{type(error).__name__}")
        st.info("详细错误已写入本地日志，页面仍可继续使用。")
        return

    history_results, limit_up_results = apply_limit_up_filter(filter_result.final)
    late_session_results, late_qualified = apply_late_session_filter(limit_up_results)
    scoring_results, final_results = apply_candidate_scoring(
        late_qualified, market_data
    )

    metrics = st.columns(5)
    metrics[0].metric("原始股票数量", f"{len(market_data):,}")
    metrics[1].metric("初筛数量", f"{len(filter_result.final):,}")
    metrics[2].metric("涨停筛选", f"{len(limit_up_results):,}")
    metrics[3].metric("尾盘合格", f"{len(late_qualified):,}")
    metrics[4].metric("最终数量", f"{len(final_results):,}")
    st.caption(f"数据更新时间：{updated_at:%Y-%m-%d %H:%M:%S}（北京时间）")

    insufficient_count = int(history_results["涨停判断"].eq("数据不足").sum())
    if insufficient_count:
        st.warning(
            f"有 {insufficient_count} 只股票因历史数据不足或请求失败未能可靠判断，"
            "已跳过且未计入最终结果。"
        )

    unverifiable_count = int(
        late_session_results["尾盘结构状态"].eq("无法验证").sum()
    )
    if unverifiable_count:
        st.warning(
            f"有 {unverifiable_count} 只股票的免费分钟数据不完整，尾盘结构标记为"
            "“无法验证”，未计入最终结果。"
        )

    st.info(
        "尾盘结构为免费分钟行情的近似判断；VWAP为根据分钟成交额和成交量计算的"
        "“VWAP近似黄线”，不等同于交易软件或交易所逐笔数据。"
    )

    if not late_session_results.empty:
        with st.expander("查看全部尾盘结构分析（含排除与无法验证）"):
            render_stock_results(late_session_results)

    if not scoring_results.empty:
        with st.expander("查看全部评分明细（含80分以下）"):
            render_stock_results(scoring_results)

    if final_results.empty:
        st.info("今日暂无符合条件股票")
        return

    st.subheader("筛选结果")
    st.caption(
        "按资金30分、板块20分、技术20分、尾盘20分、市场情绪10分计算，"
        "结果按综合得分降序排列，最多展示5只。"
    )
    render_stock_results(final_results)
    st.download_button(
        "下载筛选结果 Excel",
        data=build_excel(final_results),
        file_name=f"尾盘筛选结果_{updated_at:%Y%m%d_%H%M%S}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        width="stretch",
        on_click="ignore",
    )
    st.download_button(
        "下载全部尾盘结构分析 Excel",
        data=build_excel(late_session_results),
        file_name=f"尾盘结构分析_{updated_at:%Y%m%d_%H%M%S}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        width="stretch",
        on_click="ignore",
    )
    st.caption("结果仅来自本次实时行情筛选，未生成或补充任何虚假股票。")
    st.warning(
        "所有观察条件、价格和买卖区间（如后续展示）均仅作策略参考，"
        "不构成投资建议或收益承诺。"
    )


def main() -> None:
    st.set_page_config(
        page_title=APP_TITLE,
        page_icon="📊",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    apply_responsive_styles()
    render_strategy_sidebar()
    try:
        render_home()
    except Exception as error:
        logger.exception("页面渲染失败")
        st.error(f"页面加载失败：{type(error).__name__}")
        st.info("请刷新页面重试；详细信息已写入应用日志。")


if __name__ == "__main__":
    main()
