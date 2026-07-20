"""A股尾盘策略筛选 Web 应用。"""

from datetime import datetime, time as clock_time
from html import escape
from pathlib import Path
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
    HISTORY_MIN_FETCH_TRADING_DAYS,
    HISTORY_REQUEST_INTERVAL_SECONDS,
    HISTORY_TRADING_DAYS,
    INTRADAY_CACHE_TTL,
    INTRADAY_REQUEST_INTERVAL_SECONDS,
    MAIN_BOARD_CODE_PREFIXES,
    MARKET_CAP_MAX,
    MARKET_CAP_MIN,
    MARKET_CAP_UNIT,
    LIMIT_UP_PRICE_TOLERANCE,
    MAIN_BOARD_LIMIT_UP_RATE,
    LATE_DATA_COMPLETENESS_MIN,
    LATE_LAST_MINUTES,
    LATE_LAST_MINUTES_MAX_DROP,
    LATE_MAX_DRAWDOWN,
    LATE_RAPID_DROP_MAX_CONSECUTIVE,
    LATE_SESSION_START_TIME,
    LATE_VOLUME_EXPANSION_RATIO,
    LATE_VWAP_ABOVE_RATIO_MIN,
    PRICE_CHANGE_MAX,
    PRICE_CHANGE_MIN,
    PRICE_TICK_SIZE,
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
from services.late_session import (
    LateSessionDataError,
    analyze_late_session,
    unverifiable_late_session_result,
)
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
BASE_DIR = Path(__file__).resolve().parent
CSS_FILE = BASE_DIR / "assets" / "styles.css"
MARKET_TIMEZONE = ZoneInfo("Asia/Shanghai")


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
    """从单一文件加载全站样式。"""
    try:
        css = CSS_FILE.read_text(encoding="utf-8")
    except OSError:
        logger.exception("无法加载仪表盘样式")
        st.warning("页面样式暂时无法加载，核心功能仍可使用。")
        return
    st.markdown(f"<style>{css}</style>", unsafe_allow_html=True)


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
        st.write(
            f"**历史范围：** 至少取 {HISTORY_MIN_FETCH_TRADING_DAYS} 日，"
            f"判断最近 {HISTORY_TRADING_DAYS} 个有效交易日"
        )
        st.write(
            f"**涨停规则：** 前收盘 × {1 + MAIN_BOARD_LIMIT_UP_RATE:.2f}，"
            f"按{PRICE_TICK_SIZE:.2f}元半入四舍五入"
        )
        st.write(f"**价格容差：** {LIMIT_UP_PRICE_TOLERANCE:g} 元")
        st.write(f"**请求间隔：** {HISTORY_REQUEST_INTERVAL_SECONDS:g} 秒/股")
        st.write(f"**历史缓存：** {HISTORY_CACHE_TTL // 60} 分钟")
        st.divider()
        st.write("**尾盘结构：** 免费分钟数据近似判断")
        st.write(f"**分析起点：** {LATE_SESSION_START_TIME[:5]}")
        st.write(f"**VWAP要求：** 上方分钟占比 ≥ {LATE_VWAP_ABOVE_RATIO_MIN:.0%}")
        st.write(f"**最大回撤：** ≤ {LATE_MAX_DRAWDOWN:.1%}")
        st.write(f"**最后10分钟：** 跌幅不超过 {abs(LATE_LAST_MINUTES_MAX_DROP):.1%}")
        st.write(
            f"**连续走弱：** 最多 {LATE_RAPID_DROP_MAX_CONSECUTIVE} 分钟"
        )
        st.write(f"**数据完整性：** ≥ {LATE_DATA_COMPLETENESS_MIN:.0%}")
        st.write(f"**量能放大：** 尾盘/14:30前30分钟均量 ≥ {LATE_VOLUME_EXPANSION_RATIO:g} 倍")
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
        empty["20日内是否涨停"] = pd.Series(dtype="string")
        empty["最近涨停日期"] = pd.Series(dtype="string")
        empty["20日涨停次数"] = pd.Series(dtype="int64")
        empty["数据状态"] = pd.Series(dtype="string")
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
            result = {
                "20日内是否涨停": "无法验证",
                "最近涨停日期": "",
                "20日涨停次数": 0,
                "数据状态": error.status,
                "涨停判断": "数据不足",
            }
        except Exception as error:
            logger.exception("股票 %s 历史判断发生未知错误", stock_code)
            result = {
                "20日内是否涨停": "无法验证",
                "最近涨停日期": "",
                "20日涨停次数": 0,
                "数据状态": "无法验证",
                "涨停判断": "数据不足",
            }

        output_row = row.copy()
        for field in (
            "20日内是否涨停", "最近涨停日期", "20日涨停次数",
            "数据状态", "涨停判断",
        ):
            output_row[field] = result[field]
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
        for field in (
            "尾盘结构状态", "VWAP状态", "高于VWAP占比", "尾盘最大回撤",
            "最后10分钟涨跌幅", "连续走弱状态", "尾盘成交量状态",
            "尾盘结构评分", "淘汰原因", "数据完整性", "尾盘排除原因",
        ):
            empty[field] = pd.Series(dtype="object")
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
            result = unverifiable_late_session_result(str(error))
        except Exception as error:
            logger.exception("股票 %s 尾盘结构发生未知错误", stock_code)
            result = unverifiable_late_session_result(
                f"未知错误（{type(error).__name__}）"
            )

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


def initialize_dashboard_state() -> None:
    defaults = {
        "pending_action": None,
        "last_data_update": None,
        "data_source_status": "待连接",
        "market_stock_count": None,
        "first_round_count": None,
        "final_candidate_count": None,
        "highest_score": None,
        "scan_payload": None,
        "dashboard_notice": None,
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def queue_dashboard_action(action: str) -> None:
    st.session_state.pending_action = action


def get_market_status(now: datetime) -> tuple[str, str]:
    """根据北京时间给出基础交易时段状态，不推断节假日。"""
    if now.weekday() >= 5:
        return "已收盘", "status-warn"
    current_time = now.time()
    if current_time < clock_time.fromisoformat("09:30:00"):
        return "未开盘", "status-warn"
    if current_time <= clock_time.fromisoformat("11:30:00"):
        return "交易中", "status-live"
    if current_time < clock_time.fromisoformat("13:00:00"):
        return "午间休市", "status-warn"
    if current_time <= clock_time.fromisoformat("15:00:00"):
        return "交易中", "status-live"
    return "已收盘", "status-warn"


def _format_metric(value: object, suffix: str = "") -> str:
    if value is None or pd.isna(value):
        return "--"
    if isinstance(value, float):
        return f"{value:.2f}{suffix}"
    return f"{int(value):,}{suffix}"


def refresh_market_snapshot() -> None:
    try:
        with st.spinner("正在刷新全市场行情……"):
            load_market_data.clear()
            market_data, updated_at = load_market_data()
        st.session_state.last_data_update = updated_at
        source = market_data.attrs.get("data_source", "AKShare")
        st.session_state.data_source_status = f"正常 · {source}"
        st.session_state.market_stock_count = len(market_data)
        st.session_state.first_round_count = None
        st.session_state.final_candidate_count = None
        st.session_state.highest_score = None
        st.session_state.scan_payload = None
        if market_data.attrs.get("is_fallback"):
            st.session_state.dashboard_notice = (
                "warning",
                "东方财富接口暂时不可用，已切换至 AKShare 新浪备用源。"
                "备用源缺少量比、换手率和总市值，可查看行情，但不会将数据不足的股票判定为策略合格。",
            )
        else:
            st.session_state.dashboard_notice = ("success", "行情刷新成功，可以开始今日扫描。")
    except MarketDataError as error:
        logger.exception("刷新行情失败")
        st.session_state.data_source_status = "异常"
        st.session_state.dashboard_notice = ("error", f"行情刷新失败：{error}")
    except Exception as error:
        logger.exception("刷新行情发生未知错误")
        st.session_state.data_source_status = "异常"
        st.session_state.dashboard_notice = (
            "error",
            f"行情刷新失败（{type(error).__name__}），详细信息已记录。",
        )


def run_today_scan() -> None:
    try:
        with st.spinner("正在获取行情并执行现有策略流程，请稍候……"):
            market_data, updated_at = load_market_data()
            filter_result = apply_filters(market_data)
        history_results, limit_up_results = apply_limit_up_filter(filter_result.final)
        late_session_results, late_qualified = apply_late_session_filter(
            limit_up_results
        )
        scoring_results, final_results = apply_candidate_scoring(
            late_qualified, market_data
        )
    except MarketDataError as error:
        logger.exception("AKShare 行情连接失败")
        st.session_state.data_source_status = "异常"
        st.session_state.dashboard_notice = ("error", f"扫描失败：{error}")
        return
    except Exception as error:
        logger.exception("今日扫描失败")
        st.session_state.data_source_status = "异常"
        st.session_state.dashboard_notice = (
            "error",
            f"扫描失败（{type(error).__name__}），详细信息已记录，页面仍可使用。",
        )
        return

    highest_score = None
    if not scoring_results.empty and "综合得分" in scoring_results:
        score_values = pd.to_numeric(scoring_results["综合得分"], errors="coerce")
        if score_values.notna().any():
            highest_score = float(score_values.max())

    st.session_state.last_data_update = updated_at
    source = market_data.attrs.get("data_source", "AKShare")
    st.session_state.data_source_status = f"正常 · {source}"
    st.session_state.market_stock_count = len(market_data)
    st.session_state.first_round_count = len(filter_result.final)
    st.session_state.final_candidate_count = len(final_results)
    st.session_state.highest_score = highest_score
    st.session_state.scan_payload = {
        "updated_at": updated_at,
        "initial_filter_count": len(filter_result.final),
        "history_results": history_results,
        "limit_up_results": limit_up_results,
        "late_session_results": late_session_results,
        "late_qualified": late_qualified,
        "scoring_results": scoring_results,
        "final_results": final_results,
    }
    st.session_state.dashboard_notice = ("success", "今日扫描已完成。")


def handle_pending_action() -> None:
    action = st.session_state.pending_action
    st.session_state.pending_action = None
    if action == "scan":
        run_today_scan()
    elif action == "refresh":
        refresh_market_snapshot()
    elif action == "history":
        st.session_state.dashboard_notice = (
            "info",
            "历史记录将在 V2 后续阶段接入；当前版本不会生成虚假历史数据。",
        )


def render_status_and_metrics(now: datetime) -> None:
    market_status, market_status_class = get_market_status(now)
    updated_at = st.session_state.last_data_update
    updated_text = updated_at.strftime("%m-%d %H:%M:%S") if updated_at else "--"
    source_status = str(st.session_state.data_source_status)
    source_class = (
        "status-live" if source_status.startswith("正常")
        else "status-error" if source_status == "异常"
        else "status-warn"
    )
    status_items = [
        ("当前日期", now.strftime("%Y-%m-%d"), ""),
        ("当前时间", now.strftime("%H:%M:%S"), ""),
        ("A股市场状态", market_status, market_status_class),
        ("最近数据更新时间", updated_text, ""),
        ("数据源状态", source_status, source_class),
    ]
    status_html = "".join(
        f'<div class="status-card"><span class="status-label">{escape(label)}</span>'
        f'<strong class="status-value {css_class}">{escape(value)}</strong></div>'
        for label, value, css_class in status_items
    )
    st.markdown(f'<div class="status-grid">{status_html}</div>', unsafe_allow_html=True)

    metrics = [
        ("全市场股票数量", _format_metric(st.session_state.market_stock_count)),
        ("第一轮符合数量", _format_metric(st.session_state.first_round_count)),
        ("最终候选数量", _format_metric(st.session_state.final_candidate_count)),
        ("今日最高综合评分", _format_metric(st.session_state.highest_score, "分")),
    ]
    metric_html = "".join(
        f'<div class="metric-card"><span class="metric-label">{escape(label)}</span>'
        f'<strong class="metric-value">{escape(value)}</strong></div>'
        for label, value in metrics
    )
    st.markdown(f'<div class="metric-grid">{metric_html}</div>', unsafe_allow_html=True)


def render_strategy_flow() -> None:
    steps = [
        "主板过滤", "排除ST", "涨幅筛选", "量比与换手率",
        "市值筛选", "20日涨停", "尾盘结构", "综合评分",
    ]
    parts = []
    for index, step in enumerate(steps):
        parts.append(f'<div class="flow-step">{escape(step)}</div>')
        if index < len(steps) - 1:
            parts.append('<div class="flow-arrow">→</div>')
    st.markdown(
        '<div class="section-label">策略流程</div>'
        f'<div class="strategy-flow">{"".join(parts)}</div>',
        unsafe_allow_html=True,
    )


def render_scan_results(payload: dict[str, object]) -> None:
    updated_at = payload["updated_at"]
    history_results = payload["history_results"]
    limit_up_results = payload["limit_up_results"]
    late_session_results = payload["late_session_results"]
    scoring_results = payload["scoring_results"]
    final_results = payload["final_results"]

    successful_history_count = int(history_results["数据状态"].eq("正常").sum())
    insufficient_count = int(history_results["数据状态"].ne("正常").sum())
    limit_up_count = len(limit_up_results)
    history_stats = [
        ("初筛数量", int(payload["initial_filter_count"])),
        ("成功取得历史数据数量", successful_history_count),
        ("20日内有涨停数量", limit_up_count),
        ("数据不足数量", insufficient_count),
    ]
    stat_html = "".join(
        f'<div class="metric-card"><span class="metric-label">{escape(label)}</span>'
        f'<strong class="metric-value">{value}</strong></div>'
        for label, value in history_stats
    )
    st.markdown(
        '<div class="section-label">20日涨停筛选统计</div>'
        f'<div class="metric-grid">{stat_html}</div>',
        unsafe_allow_html=True,
    )
    if insufficient_count:
        st.warning(f"{insufficient_count} 只股票历史数据不足或无法验证，未计入后续结果。")

    if not history_results.empty:
        with st.expander("查看全部20日涨停判断"):
            render_stock_results(history_results)

    unverifiable_count = int(
        late_session_results["尾盘结构状态"].eq("无法验证").sum()
    )
    if unverifiable_count:
        st.warning(f"{unverifiable_count} 只股票尾盘结构无法验证，未计入最终结果。")
        if late_session_results["淘汰原因"].astype("string").str.contains(
            "尚未进入尾盘分析时段", na=False
        ).any():
            st.info("当前尚未进入14:30–15:00尾盘分析时段。")

    st.info(
        "VWAP为公开分钟成交额和成交量计算的近似均价线，"
        "不是交易所 Level-2 黄线数据。"
    )
    if not late_session_results.empty:
        with st.expander("查看全部尾盘结构分析"):
            render_stock_results(late_session_results)
    if not scoring_results.empty:
        with st.expander("查看全部评分明细"):
            render_stock_results(scoring_results)

    st.markdown('<div class="section-label">今日候选结果</div>', unsafe_allow_html=True)
    if final_results.empty:
        st.info("今日暂无符合条件股票")
        return

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


def render_home() -> None:
    initialize_dashboard_state()
    handle_pending_action()
    now = datetime.now(MARKET_TIMEZONE)

    st.markdown(
        '<section class="dashboard-hero">'
        '<p class="dashboard-eyebrow">V2.0 · Market Intelligence</p>'
        f'<h1 class="dashboard-title">{escape(APP_TITLE)}</h1>'
        f'<p class="dashboard-subtitle">{escape(APP_DESCRIPTION)}</p>'
        '</section>',
        unsafe_allow_html=True,
    )
    render_status_and_metrics(now)

    st.markdown('<div class="section-label">今日操作</div>', unsafe_allow_html=True)
    actions = st.columns(3)
    actions[0].button(
        "开始今日扫描", type="primary", width="stretch",
        on_click=queue_dashboard_action, args=("scan",),
    )
    actions[1].button(
        "刷新行情", width="stretch",
        on_click=queue_dashboard_action, args=("refresh",),
    )
    actions[2].button(
        "查看历史记录", width="stretch",
        on_click=queue_dashboard_action, args=("history",),
    )

    notice = st.session_state.dashboard_notice
    if notice:
        level, message = notice
        getattr(st, level)(message)

    st.markdown(
        '<div class="risk-banner">本工具仅用于公开数据筛选和策略研究，'
        '不构成任何投资建议。</div>',
        unsafe_allow_html=True,
    )
    render_strategy_flow()

    if st.session_state.scan_payload is None:
        st.info("点击“开始今日扫描”运行现有策略；首页不会自动请求行情。")
    else:
        render_scan_results(st.session_state.scan_payload)


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
