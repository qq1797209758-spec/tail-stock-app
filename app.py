"""A股尾盘策略筛选 Web 应用。"""

from datetime import datetime, time as clock_time
from html import escape
from pathlib import Path
from threading import Lock
import time
from uuid import uuid4
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

import config as strategy_settings
from components.stock_table import build_strategy_report, render_stock_results
from components.backtest_report import build_backtest_excel
from config import (
    APP_DESCRIPTION,
    APP_TITLE,
    BACKTEST_DEFAULT_MAX_STOCKS,
    BACKTEST_DEFAULT_RANGE_TRADING_DAYS,
    BACKTEST_MARKET_CLOSE_TIME,
    BACKTEST_MAX_STOCKS_LIMIT,
    BACKTEST_MAX_TRADING_DAYS,
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
    SCORE_ACTIVITY_MAX,
    SCORE_FUNDS_MAX,
    SCORE_LATE_SESSION_MAX,
    SCORE_SECTOR_MAX,
    SCORE_TREND_MAX,
    SCORE_TURNOVER_MAX,
    SCORE_VOLUME_MAX,
    SCORING_CACHE_TTL,
    SCORING_MAX_RESULTS,
    SCORING_REQUEST_INTERVAL_SECONDS,
    SCAN_HISTORY_DATABASE,
    SECTOR_CHANGE_SCORE_MAX,
    SECTOR_CHANGE_SCORE_MIN,
    TURNOVER_RATE_MAX,
    TURNOVER_RATE_MIN,
    TARGET_SELECTION_COUNT,
    VOLUME_RATIO_MIN,
)
from services.market_data import MarketDataError, fetch_a_share_spot
from services.backtest_data import BacktestDataError, fetch_trade_calendar
from services.history_data import HistoryDataError, analyze_recent_limit_up
from services.ifind_client import (
    IFindConnectionError,
    fetch_test_realtime_quotes,
    test_ifind_connection,
)
from services.late_session import (
    LateSessionDataError,
    analyze_late_session,
    unverifiable_late_session_result,
)
from services.scoring_data import (
    ScoringDataError,
    fetch_industry_strength,
    fetch_industry_five_day_strength,
    fetch_stock_scoring_context,
    match_industry_change,
)
from services.scan_history import (
    ScanRecord,
    SQLiteScanHistoryRepository,
    dataframe_records,
)
from services.trading_session import is_trading_day
from strategy.filters import apply_filters
from strategy.backtest import BacktestResult, run_historical_backtest
from strategy.reporting import build_excluded_results, build_missing_records
from strategy.scoring import calculate_candidate_score
from strategy.selection import build_enrichment_pool, select_layered_top5
from utils.logger import get_logger


logger = get_logger(__name__)
_SCAN_LOCK = Lock()


def get_strategy_parameter_snapshot() -> dict[str, object]:
    """返回当前扫描所有可审计的配置快照。"""
    snapshot: dict[str, object] = {}
    for name, value in vars(strategy_settings).items():
        if not name.isupper() or name.startswith("APP_"):
            continue
        if isinstance(value, tuple):
            snapshot[name] = "、".join(str(item) for item in value)
        elif isinstance(value, (str, int, float, bool)):
            snapshot[name] = value
    return snapshot


BASE_DIR = Path(__file__).resolve().parent
CSS_FILE = BASE_DIR / "assets" / "styles.css"
MARKET_TIMEZONE = ZoneInfo("Asia/Shanghai")


@st.cache_resource(show_spinner=False)
def get_scan_history_repository() -> SQLiteScanHistoryRepository:
    return SQLiteScanHistoryRepository(BASE_DIR / SCAN_HISTORY_DATABASE)


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


@st.cache_data(ttl=SCORING_CACHE_TTL, show_spinner=False)
def load_industry_five_day_strength(industry_name: str) -> float:
    return fetch_industry_five_day_strength(industry_name)


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
        st.write(
            f"**综合评分：** 尾盘{SCORE_LATE_SESSION_MAX:g} + 量能{SCORE_VOLUME_MAX:g} + "
            f"资金{SCORE_FUNDS_MAX:g} + 板块{SCORE_SECTOR_MAX:g} + 趋势{SCORE_TREND_MAX:g} + "
            f"换手{SCORE_TURNOVER_MAX:g} + 活跃度{SCORE_ACTIVITY_MAX:g}"
        )
        st.write(f"**每日研究名单：** 固定目标 {TARGET_SELECTION_COUNT} 只")
        st.caption("严格候选不足时按四级真实行情候选递补；有效股票不足5只则明确报告缺口。")
        st.caption("评分字段缺失时，使用其余有效指标按可用权重重新归一化。")
        with st.expander("查看评分计算依据"):
            st.write(
                f"**资金 15分：** 主力净流入占比从 {FUND_FLOW_NET_RATIO_MIN:g}% "
                f"到 {FUND_FLOW_NET_RATIO_MAX:g}% 线性计分；成交量和量比另计15分。"
            )
            st.write(
                f"**板块强度 15分：** 近5日强度从 {SECTOR_CHANGE_SCORE_MIN:g}% "
                f"到 {SECTOR_CHANGE_SCORE_MAX:g}% 线性计分。"
            )
            st.write(
                "**趋势15分 + 换手10分 + 活跃度10分：** 结合涨幅位置、日内价格位置、"
                "换手率合理程度和最近20日涨停次数。"
            )
            st.write(
                f"**尾盘结构 20分：** 免费分钟行情尾盘结构评分按比例折算至"
                f" {SCORE_LATE_SESSION_MAX:g}分。"
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
        empty["历史错误原因"] = pd.Series(dtype="string")
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
                "历史错误原因": str(error),
            }
        except Exception as error:
            logger.exception("股票 %s 历史判断发生未知错误", stock_code)
            result = {
                "20日内是否涨停": "无法验证",
                "最近涨停日期": "",
                "20日涨停次数": 0,
                "数据状态": "无法验证",
                "涨停判断": "数据不足",
                "历史错误原因": f"未知错误（{type(error).__name__}）",
            }

        output_row = row.copy()
        for field in (
            "20日内是否涨停", "最近涨停日期", "20日涨停次数",
            "数据状态", "涨停判断", "历史错误原因",
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


def apply_candidate_scoring(candidates, market_data, updated_at):
    """对真实候选池评分并执行分层 Top 5 选择。"""
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
        if not industry_source_missing and matched_industry:
            try:
                industry_change = load_industry_five_day_strength(matched_industry)
            except ScoringDataError as error:
                logger.warning("行业 %s 近5日强度不可用：%s", matched_industry, error)
                industry_change = None
        else:
            industry_change = None
        if industry_change is None:
            missing.append("板块行业强度")

        scoring_row = row.copy()
        scoring_row["主力资金净流入"] = context.get("主力资金净流入")
        score = calculate_candidate_score(
            row=scoring_row,
            main_fund_ratio=context.get("主力净流入占比"),
            industry_name=matched_industry,
            industry_change=industry_change,
            market_advance_ratio=market_advance_ratio,
            context_missing=missing,
        )
        output_row = scoring_row.copy()
        for key, value in score.items():
            output_row[key] = value
        scored_rows.append(output_row)

        if position < total:
            time.sleep(SCORING_REQUEST_INTERVAL_SECONDS)

    progress.empty()
    scored = pd.DataFrame(scored_rows).sort_values(
        ["综合得分", "代码"], ascending=[False, True], kind="mergesort"
    ).reset_index(drop=True)
    scored["数据更新时间"] = updated_at.strftime("%Y-%m-%d %H:%M:%S")
    selection = select_layered_top5(scored)
    selected = selection.selected
    selected.attrs["funnel"] = selection.funnel
    selected.attrs["missing_count"] = selection.missing_count
    selected.attrs["valid_universe_count"] = selection.valid_universe_count
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
        "current_view": "dashboard",
        "scan_in_progress": False,
        "active_scan_id": None,
        "last_scan_duration": None,
        "interface_health_status": "待检查",
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def queue_dashboard_action(action: str) -> None:
    if st.session_state.get("scan_in_progress"):
        st.session_state.dashboard_notice = (
            "warning",
            "完整扫描正在运行，请勿重复提交。如需终止，可停止 Streamlit 进程或关闭当前会话。",
        )
        return
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
            st.session_state.interface_health_status = "部分降级"
            st.session_state.dashboard_notice = (
                "warning",
                "东方财富接口暂时不可用，已切换至 AKShare 新浪备用源。"
                "备用源缺少量比、换手率和总市值，相关字段会标记缺失并按其余真实指标重归一化；"
                "此类股票只会进入综合评分递补，不会被判定为严格合格。",
            )
        else:
            st.session_state.interface_health_status = "正常"
            st.session_state.dashboard_notice = ("success", "行情刷新成功，可以开始今日扫描。")
    except MarketDataError as error:
        logger.exception("刷新行情失败")
        st.session_state.data_source_status = "异常"
        st.session_state.interface_health_status = "异常"
        st.session_state.dashboard_notice = ("error", f"行情刷新失败：{error}")
    except Exception as error:
        logger.exception("刷新行情发生未知错误")
        st.session_state.data_source_status = "异常"
        st.session_state.interface_health_status = "异常"
        st.session_state.dashboard_notice = (
            "error",
            f"行情刷新失败（{type(error).__name__}），详细信息已记录。",
        )


def _scan_interface_health(
    data_source_status: str,
    history_results: pd.DataFrame,
    late_session_results: pd.DataFrame,
    scoring_results: pd.DataFrame,
) -> tuple[dict[str, object], list[str]]:
    history_ok = int(history_results.get("数据状态", pd.Series(dtype="string")).eq("正常").sum())
    history_failed = len(history_results) - history_ok
    late_ok = int(late_session_results.get("尾盘结构状态", pd.Series(dtype="string")).ne("无法验证").sum())
    late_failed = len(late_session_results) - late_ok
    scoring_missing = int(
        scoring_results.get("缺失项", pd.Series(dtype="string"))
        .astype("string").str.strip().fillna("").ne("").sum()
    )
    health = {
        "实时行情": data_source_status,
        "历史行情": {"成功": history_ok, "失败或不足": history_failed},
        "分钟行情": {"成功": late_ok, "无法验证": late_failed},
        "评分附加数据": {"评分数量": len(scoring_results), "存在缺失": scoring_missing},
    }
    errors: list[str] = []
    if history_failed:
        errors.append(f"历史行情：{history_failed}只数据不足或无法验证")
    if late_failed:
        errors.append(f"分钟行情：{late_failed}只无法验证")
    if scoring_missing:
        errors.append(f"评分数据：{scoring_missing}只存在缺失项")
    return health, errors


def _save_scan_record(
    *,
    scan_id: str,
    started_at: datetime,
    completed_at: datetime,
    status: str,
    data_updated_at: datetime,
    data_source_status: str,
    initial_results: pd.DataFrame,
    history_results: pd.DataFrame,
    limit_up_results: pd.DataFrame,
    late_session_results: pd.DataFrame,
    late_qualified: pd.DataFrame,
    scoring_results: pd.DataFrame,
    final_results: pd.DataFrame,
    interface_errors: list[str],
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, object], list[str]]:
    excluded_results = build_excluded_results(
        history_results, late_session_results, scoring_results, final_results
    )
    missing_records = build_missing_records(
        history_results, late_session_results, scoring_results
    )
    health, detected_errors = _scan_interface_health(
        data_source_status, history_results, late_session_results, scoring_results
    )
    all_errors = list(dict.fromkeys([*interface_errors, *detected_errors]))
    duration = max(0.0, (completed_at - started_at).total_seconds())
    record = ScanRecord(
        scan_id=scan_id,
        started_at=started_at.isoformat(),
        completed_at=completed_at.isoformat(),
        scan_date=started_at.date().isoformat(),
        duration_seconds=round(duration, 3),
        status=status,
        data_updated_at=data_updated_at.isoformat(),
        data_source_status=data_source_status,
        strategy_parameters=get_strategy_parameter_snapshot(),
        counts={
            "initial": len(initial_results),
            "history_success": int(history_results.get("数据状态", pd.Series(dtype="string")).eq("正常").sum()),
            "limit_up": len(limit_up_results),
            "late_qualified": len(late_qualified),
            "scored": len(scoring_results),
            "final": len(final_results),
        },
        interface_health=health,
        interface_errors=all_errors,
        initial_results=dataframe_records(initial_results),
        final_top5=dataframe_records(final_results),
        excluded_results=dataframe_records(excluded_results),
        missing_records=dataframe_records(missing_records),
    )
    get_scan_history_repository().save_scan(record)
    return excluded_results, missing_records, health, all_errors


def run_today_scan() -> None:
    try:
        calendar = fetch_trade_calendar()
        shanghai_today = datetime.now(MARKET_TIMEZONE).date()
        if not is_trading_day(shanghai_today, calendar):
            st.session_state.dashboard_notice = (
                "info", f"{shanghai_today:%Y-%m-%d} 不是A股交易日，不生成当日选股名单。"
            )
            return
    except BacktestDataError as error:
        logger.warning("交易日历不可用，继续按实时行情扫描：%s", error)

    if st.session_state.get("scan_in_progress") or not _SCAN_LOCK.acquire(blocking=False):
        st.session_state.dashboard_notice = (
            "warning", "已有一次完整扫描正在运行，本次重复请求已忽略。"
        )
        return

    scan_id = uuid4().hex
    started_at = datetime.now(MARKET_TIMEZONE)
    updated_at = started_at
    market_data = pd.DataFrame()
    initial_results = pd.DataFrame()
    history_results = pd.DataFrame()
    limit_up_results = pd.DataFrame()
    late_session_results = pd.DataFrame()
    late_qualified = pd.DataFrame()
    scoring_results = pd.DataFrame()
    final_results = pd.DataFrame()
    data_source_status = "异常"
    interface_errors: list[str] = []
    st.session_state.scan_in_progress = True
    st.session_state.active_scan_id = scan_id
    st.session_state.dashboard_notice = (
        "info", f"扫描 {scan_id[:8]} 已开始。扫描运行期间请勿重复点击；如需强制终止，请停止 Streamlit 进程。"
    )
    try:
        with st.spinner("正在获取行情并执行现有策略流程，请稍候……"):
            market_data, updated_at = load_market_data()
            filter_result = apply_filters(market_data)
            initial_results = filter_result.initial
            enrichment_pool = build_enrichment_pool(filter_result.initial)
            source = market_data.attrs.get("data_source", "AKShare")
            data_source_status = f"正常 · {source}"
        history_results, limit_up_results = apply_limit_up_filter(enrichment_pool)
        late_session_results, late_qualified = apply_late_session_filter(
            history_results
        )
        scoring_results, final_results = apply_candidate_scoring(
            late_session_results, market_data, updated_at
        )
    except MarketDataError as error:
        logger.exception("AKShare 行情连接失败")
        interface_errors.append(f"实时行情：{error}")
        st.session_state.data_source_status = "异常"
        st.session_state.dashboard_notice = ("error", f"扫描失败：{error}")
    except Exception as error:
        logger.exception("今日扫描失败")
        interface_errors.append(f"扫描流程：{type(error).__name__}")
        st.session_state.data_source_status = "异常"
        st.session_state.dashboard_notice = (
            "error",
            f"扫描失败（{type(error).__name__}），详细信息已记录，页面仍可使用。",
        )
    finally:
        completed_at = datetime.now(MARKET_TIMEZONE)
        status = "失败" if market_data.empty else ("部分完成" if interface_errors else "成功")
        try:
            excluded_results, missing_records, health, interface_errors = _save_scan_record(
                scan_id=scan_id, started_at=started_at, completed_at=completed_at,
                status=status, data_updated_at=updated_at,
                data_source_status=data_source_status, initial_results=initial_results,
                history_results=history_results, limit_up_results=limit_up_results,
                late_session_results=late_session_results, late_qualified=late_qualified,
                scoring_results=scoring_results, final_results=final_results,
                interface_errors=interface_errors,
            )
            st.session_state.interface_health_status = "正常" if not interface_errors and missing_records.empty else "部分降级"
        except Exception as storage_error:
            logger.exception("扫描历史保存失败")
            excluded_results = pd.DataFrame()
            missing_records = pd.DataFrame()
            health = {"历史存储": f"失败（{type(storage_error).__name__}）"}
            st.session_state.interface_health_status = "异常"
            interface_errors.append(f"历史存储：{type(storage_error).__name__}")

        duration = max(0.0, (completed_at - started_at).total_seconds())
        st.session_state.last_scan_duration = duration
        st.session_state.scan_in_progress = False
        st.session_state.active_scan_id = None
        _SCAN_LOCK.release()

    if market_data.empty:
        return

    highest_score = None
    if not scoring_results.empty and "综合得分" in scoring_results:
        score_values = pd.to_numeric(scoring_results["综合得分"], errors="coerce")
        if score_values.notna().any():
            highest_score = float(score_values.max())

    st.session_state.last_data_update = updated_at
    st.session_state.data_source_status = data_source_status
    st.session_state.market_stock_count = len(market_data)
    st.session_state.first_round_count = len(initial_results)
    st.session_state.final_candidate_count = len(final_results)
    st.session_state.highest_score = highest_score
    st.session_state.scan_payload = {
        "scan_id": scan_id, "scan_duration": duration, "interface_health": health,
        "interface_errors": interface_errors, "updated_at": updated_at,
        "initial_filter_count": len(initial_results), "initial_results": initial_results,
        "history_results": history_results, "limit_up_results": limit_up_results,
        "late_session_results": late_session_results, "late_qualified": late_qualified,
        "scoring_results": scoring_results, "final_results": final_results,
        "selection_funnel": final_results.attrs.get("funnel", {}),
        "selection_missing_count": final_results.attrs.get("missing_count", max(0, TARGET_SELECTION_COUNT - len(final_results))),
        "valid_universe_count": len(initial_results),
        "excluded_results": excluded_results, "missing_records": missing_records,
    }
    if interface_errors:
        st.session_state.dashboard_notice = (
            "warning", f"扫描 {scan_id[:8]} 已部分完成，耗时 {duration:.1f} 秒；请查看接口错误记录。"
        )
    else:
        st.session_state.dashboard_notice = (
            "success", f"扫描 {scan_id[:8]} 已完成，耗时 {duration:.1f} 秒。"
        )


def handle_pending_action() -> None:
    action = st.session_state.pending_action
    st.session_state.pending_action = None
    if action == "scan":
        run_today_scan()
    elif action == "refresh":
        refresh_market_snapshot()
    elif action == "history":
        st.session_state.current_view = "history"
    elif action == "dashboard":
        st.session_state.current_view = "dashboard"


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
        ("接口健康", str(st.session_state.interface_health_status),
         "status-live" if st.session_state.interface_health_status == "正常" else "status-warn"),
        ("最近扫描耗时", "--" if st.session_state.last_scan_duration is None else f"{st.session_state.last_scan_duration:.1f}秒", ""),
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
    initial_results = payload.get("initial_results", history_results)
    limit_up_results = payload["limit_up_results"]
    late_session_results = payload["late_session_results"]
    scoring_results = payload["scoring_results"]
    final_results = payload["final_results"]

    funnel = payload.get("selection_funnel", {})
    if funnel:
        funnel_items = [
            ("有效主板股票", int(payload.get("valid_universe_count", 0))),
            ("严格层", int(funnel.get("严格入选", 0))),
            ("一级递补层", int(funnel.get("一级递补", 0))),
            ("二级递补层", int(funnel.get("二级递补", 0))),
            ("三级递补层", int(funnel.get("三级递补", 0))),
            ("综合评分池", int(funnel.get("综合评分递补", 0))),
            ("最终输出", len(final_results)),
        ]
        funnel_html = "".join(
            f'<div class="metric-card"><span class="metric-label">{escape(label)}</span>'
            f'<strong class="metric-value">{count}</strong></div>'
            for label, count in funnel_items
        )
        st.markdown(
            '<div class="section-label">分层筛选漏斗</div>'
            f'<div class="metric-grid">{funnel_html}</div>',
            unsafe_allow_html=True,
        )

    scan_id = str(payload.get("scan_id", ""))
    scan_duration = payload.get("scan_duration")
    metadata = f"数据更新：{updated_at:%Y-%m-%d %H:%M:%S}"
    if scan_id:
        metadata += f" · scan_id: {scan_id}"
    if scan_duration is not None:
        metadata += f" · 耗时：{float(scan_duration):.1f}秒"
    st.caption(metadata)
    interface_health = payload.get("interface_health", {})
    interface_errors = payload.get("interface_errors", [])
    if interface_health:
        with st.expander("查看接口健康状态"):
            st.json(interface_health)
            if interface_errors:
                st.warning("；".join(str(item) for item in interface_errors))

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
        late_reasons = late_session_results.get(
            "淘汰原因",
            late_session_results.get(
                "尾盘排除原因",
                pd.Series(index=late_session_results.index, dtype="string"),
            ),
        )
        if late_reasons.astype("string").str.contains(
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
    missing_count = int(payload.get("selection_missing_count", max(0, TARGET_SELECTION_COUNT - len(final_results))))
    if final_results.empty:
        st.error(f"真实行情接口未能提供有效候选，目标5只，当前缺少 {missing_count} 只。")
    else:
        if missing_count:
            st.error(
                f"行情接口仅提供 {len(final_results)} 只有效真实候选，"
                f"距离固定目标5只仍缺 {missing_count} 只；系统未伪造或随机补足。"
            )
        else:
            st.success("已从真实行情中按分层规则稳定选出5只相对高概率候选。")
        supplemental_count = int(final_results.get("入选类型", pd.Series(dtype="string")).ne("严格入选").sum())
        if supplemental_count:
            st.info(f"其中 {supplemental_count} 只来自分层递补，入选类型已逐只标注。")
        render_stock_results(final_results)

    excluded_results = build_excluded_results(
        history_results, late_session_results, scoring_results, final_results
    )
    missing_records = build_missing_records(
        history_results, late_session_results, scoring_results
    )
    st.download_button(
        "下载完整策略报告 Excel",
        data=build_strategy_report(
            final_top5=final_results,
            initial_results=initial_results,
            excluded_results=excluded_results,
            missing_records=missing_records,
            strategy_parameters=get_strategy_parameter_snapshot(),
            updated_at=updated_at,
        ),
        file_name=f"A股尾盘策略报告_{updated_at:%Y%m%d_%H%M%S}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        width="stretch",
        on_click="ignore",
    )


def render_history_page() -> None:
    """查询本地 SQLite 扫描历史，并提供对比与报告下载。"""
    st.markdown('<div class="section-label">历史扫描记录</div>', unsafe_allow_html=True)
    st.warning(
        "当前使用本地 SQLite 临时保存。Streamlit Community Cloud 重启、"
        "重新部署或容器迁移后，历史数据可能被清空。"
    )
    st.button("返回仪表盘", on_click=queue_dashboard_action, args=("dashboard",))
    try:
        repository = get_scan_history_repository()
        all_scans = repository.list_scans()
    except Exception as error:
        logger.exception("读取扫描历史失败")
        st.error(f"历史记录读取失败（{type(error).__name__}），页面其他功能仍可使用。")
        return

    if not all_scans:
        st.info("暂无历史扫描记录。完成一次今日扫描后将在此显示。")
        return

    available_dates = sorted({record["scan_date"] for record in all_scans}, reverse=True)
    selected_date = st.date_input(
        "按日期查询",
        value=datetime.fromisoformat(available_dates[0]).date(),
        min_value=datetime.fromisoformat(available_dates[-1]).date(),
        max_value=datetime.now(MARKET_TIMEZONE).date(),
    ).isoformat()
    scans = repository.list_scans(selected_date)
    if not scans:
        st.info("该日期暂无扫描记录。")
    else:
        labels = {
            record["scan_id"]: (
                f"{datetime.fromisoformat(record['started_at']):%H:%M:%S} · "
                f"{record['scan_id'][:8]} · {record['status']}"
            )
            for record in scans
        }
        selected_id = st.selectbox(
            "选择某次扫描", options=list(labels), format_func=labels.get
        )
        record = repository.get_scan(selected_id)
        if record:
            counts = record["counts"]
            metric_columns = st.columns(4)
            metric_columns[0].metric("初筛数量", counts.get("initial", 0))
            metric_columns[1].metric("20日涨停", counts.get("limit_up", 0))
            metric_columns[2].metric("尾盘合格", counts.get("late_qualified", 0))
            metric_columns[3].metric("最终Top5", counts.get("final", 0))
            st.caption(
                f"scan_id: {record['scan_id']} · 状态：{record['status']} · "
                f"耗时：{record['duration_seconds']:.1f}秒 · "
                f"数据更新：{record['data_updated_at']}"
            )
            with st.expander("接口健康与错误记录"):
                st.json(record["interface_health"])
                if record["interface_errors"]:
                    st.warning("；".join(record["interface_errors"]))
                else:
                    st.success("本次扫描未记录接口错误。")

            top10 = pd.DataFrame(record["final_top5"])
            st.markdown("#### 当次最终Top5研究名单")
            if top10.empty:
                st.info("当次扫描没有通过前序数据验证的真实股票。")
            else:
                render_stock_results(top10)
            initial = pd.DataFrame(record["initial_results"])
            excluded = pd.DataFrame(record["excluded_results"])
            missing = pd.DataFrame(record["missing_records"])
            report_time = datetime.fromisoformat(record["completed_at"])
            st.download_button(
                "下载当次 Excel 报告",
                data=build_strategy_report(
                    final_top5=top10, initial_results=initial,
                    excluded_results=excluded, missing_records=missing,
                    strategy_parameters=record["strategy_parameters"],
                    updated_at=report_time,
                ),
                file_name=f"扫描报告_{record['scan_id'][:8]}_{record['scan_date']}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                width="stretch", on_click="ignore",
            )

    comparison = repository.candidate_counts_by_date()
    st.markdown("#### 不同日期候选数量对比")
    if comparison.empty:
        st.info("暂无可对比数据。")
    else:
        st.line_chart(
            comparison.set_index("扫描日期")[["初筛数量", "最终候选数量"]]
        )
        st.dataframe(comparison, width="stretch", hide_index=True)

    selection_counts = repository.stock_selection_counts()
    st.markdown("#### 股票历史入选次数")
    if selection_counts.empty:
        st.info("暂无股票入选记录。")
    else:
        st.dataframe(selection_counts, width="stretch", hide_index=True)


def render_mobile_action_bar() -> None:
    """窄屏幕底部快捷操作区。"""
    disabled = bool(st.session_state.get("scan_in_progress"))
    with st.container(key="mobile_action_bar"):
        actions = st.columns(3)
        actions[0].button("扫描", key="mobile_scan", type="primary", disabled=disabled, on_click=queue_dashboard_action, args=("scan",))
        actions[1].button("刷新", key="mobile_refresh", disabled=disabled, on_click=queue_dashboard_action, args=("refresh",))
        actions[2].button("历史", key="mobile_history", disabled=disabled, on_click=queue_dashboard_action, args=("history",))


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
    _render_home_content(now)


def _default_backtest_range(now: datetime) -> tuple[object, object]:
    """默认覆盖最近20个可用于选择的完整交易日。"""
    try:
        calendar = fetch_trade_calendar()
        today = pd.Timestamp(now.date())
        close_time = clock_time.fromisoformat(BACKTEST_MARKET_CLOSE_TIME)
        last_complete = today if now.time() >= close_time else today - pd.Timedelta(days=1)
        completed = calendar[calendar <= last_complete]
        eligible = completed[:-1]  # 最后一日没有已完成的下一交易日收益。
        if len(eligible):
            end = eligible[-1].date()
            start = eligible[max(0, len(eligible) - BACKTEST_DEFAULT_RANGE_TRADING_DAYS)].date()
            return start, end
    except Exception:
        pass
    today = now.date()
    return today - pd.Timedelta(days=35), today - pd.Timedelta(days=1)


def _format_backtest_metric(key: str, value: object) -> str:
    if value is None or pd.isna(value):
        return "--"
    if key in {"胜率", "最大回撤", "次日最高价高于买入价1%比例", "次日最高价高于买入价3%比例", "次日最高价高于买入价5%比例"}:
        return f"{float(value):.1%}"
    if "收益率" in key or key in {"累计收益率", "最大单笔盈利", "最大单笔亏损"}:
        return f"{float(value):.2f}%"
    return str(value)


def render_backtest_page() -> None:
    """历史回测标签页；不影响现有实时选股状态。"""
    now = datetime.now(MARKET_TIMEZONE)
    st.markdown('<div class="section-label">近10个交易日历史回测</div>', unsafe_allow_html=True)
    st.warning("历史回测结果不代表未来表现。本模块不会使用今天实时行情倒推历史日期。")
    st.caption(
        "股票池、名称、涨跌幅、换手率、总股本、涨停与尾盘数据均按筛选日截断；"
        "次日行情仅用于收益评价。历史主力资金和板块强度无法可靠复现时对应评分计0分并标记缺失。"
    )
    default_start, default_end = _default_backtest_range(now)
    controls = st.columns(4)
    start_date = controls[0].date_input("开始日期", value=default_start, key="backtest_start")
    end_date = controls[1].date_input("结束日期", value=default_end, key="backtest_end")
    max_stocks = controls[2].number_input(
        "每日最多股票数量", min_value=1, max_value=BACKTEST_MAX_STOCKS_LIMIT,
        value=BACKTEST_DEFAULT_MAX_STOCKS, step=1, key="backtest_max_stocks",
    )
    return_basis = controls[3].selectbox(
        "买卖口径",
        (
            "A. 次日开盘买入，次日收盘卖出",
            "B. 筛选日14:50后首条有效分钟收盘买入，次日收盘卖出",
        ),
        key="backtest_return_basis",
    )
    st.caption(
        f"日期控件默认覆盖最近 {BACKTEST_DEFAULT_RANGE_TRADING_DAYS} 个完整交易日；"
        f"为控制数据量，每次实际取其中最近 {BACKTEST_MAX_TRADING_DAYS} 个具备完整次日行情的交易日。"
    )
    if st.button("开始历史回测", type="primary", width="stretch"):
        if start_date > end_date:
            st.error("历史回测失败：开始日期不能晚于结束日期。")
        else:
            progress_bar = st.progress(0, text="准备历史回测数据……")

            def update_progress(value: float, text: str) -> None:
                progress_bar.progress(min(1.0, max(0.0, value)), text=text)

            try:
                result = run_historical_backtest(
                    start_date,
                    end_date,
                    max_stocks=int(max_stocks),
                    return_basis=return_basis,
                    now=now,
                    progress=update_progress,
                    max_days=BACKTEST_MAX_TRADING_DAYS,
                )
                st.session_state.backtest_result = result
                failed_days = int(result.summary.get("数据失败交易日数量", 0))
                total_days = int(result.summary.get("回测交易日数量", 0))
                if total_days and failed_days == total_days:
                    st.warning("回测流程已结束，但所有交易日均因数据不可用而失败；未生成虚假结果。")
                elif failed_days:
                    st.warning(f"历史回测部分完成，{failed_days}个交易日数据失败，请查看缺失记录。")
                else:
                    st.success("历史回测完成")
            except BacktestDataError as error:
                st.error(f"历史回测失败：{error}")
            except Exception as error:
                logger.exception("历史回测发生未知错误")
                st.error(f"历史回测失败（{type(error).__name__}），错误已记录，页面仍可使用。")
            finally:
                progress_bar.empty()

    result = st.session_state.get("backtest_result")
    if not isinstance(result, BacktestResult):
        st.info("请选择日期范围并点击“开始历史回测”。")
        return

    metric_items = list(result.summary.items())
    for start in range(0, len(metric_items), 4):
        columns = st.columns(4)
        for column, (key, value) in zip(columns, metric_items[start : start + 4]):
            column.metric(key, _format_backtest_metric(key, value))

    st.markdown("#### 每日收益曲线")
    if result.daily.empty:
        st.info("本次回测没有可展示的每日收益。")
    else:
        chart_data = result.daily.copy()
        chart_data["筛选日期"] = pd.to_datetime(chart_data["筛选日期"])
        st.line_chart(
            chart_data.set_index("筛选日期")[["每日等权平均收益率", "累计收益率"]]
        )
        st.dataframe(result.daily, hide_index=True, width="stretch")

    st.markdown("#### 回测明细")
    if result.details.empty:
        st.info("所选交易日没有满足全部可验证条件的股票，记录为空仓。")
    else:
        st.dataframe(result.details, hide_index=True, width="stretch")

    with st.expander(f"数据失败与缺失记录（{len(result.failures)}）"):
        if result.failures.empty:
            st.success("本次回测没有接口失败或无法验证记录。")
        else:
            st.dataframe(result.failures, hide_index=True, width="stretch")

    st.download_button(
        "下载历史回测 Excel",
        data=build_backtest_excel(result),
        file_name=f"A股尾盘历史回测_{now:%Y%m%d_%H%M%S}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        width="stretch",
        on_click="ignore",
    )
    st.markdown(
        '<div class="risk-banner">历史回测结果不代表未来表现，'
        '不构成任何投资建议。</div>',
        unsafe_allow_html=True,
    )


def _render_home_content(now: datetime) -> None:
    """渲染既有实时首页内容，保持回测模块与实时功能隔离。"""
    render_status_and_metrics(now)

    if st.session_state.current_view == "history":
        render_history_page()
        render_mobile_action_bar()
        st.markdown(
            '<div class="risk-banner">本地历史数据可能在云端重启后清空；'
            '本工具仅用于公开数据筛选和策略研究，不构成投资建议。</div>',
            unsafe_allow_html=True,
        )
        return

    st.markdown('<div class="section-label">今日操作</div>', unsafe_allow_html=True)
    actions = st.columns(3)
    scanning = bool(st.session_state.scan_in_progress)
    actions[0].button(
        "开始今日扫描", type="primary", width="stretch",
        on_click=queue_dashboard_action, args=("scan",),
        disabled=scanning,
    )
    actions[1].button(
        "刷新行情", width="stretch",
        on_click=queue_dashboard_action, args=("refresh",),
        disabled=scanning,
    )
    actions[2].button(
        "查看历史记录", width="stretch",
        on_click=queue_dashboard_action, args=("history",),
        disabled=scanning,
    )
    if st.button("测试同花顺连接", width="stretch", disabled=scanning):
        try:
            with st.spinner("正在测试同花顺连接……"):
                test_ifind_connection()
            st.success("同花顺连接成功")
        except IFindConnectionError as error:
            st.error(f"同花顺连接失败：{error}")
        except Exception:
            st.error("同花顺连接失败：发生未知错误，请稍后重试。")
    if st.button("测试同花顺实时行情", width="stretch", disabled=scanning):
        try:
            with st.spinner("正在获取两只股票的同花顺实时行情……"):
                ifind_quotes = fetch_test_realtime_quotes()
            st.caption("数据源：同花顺iFinD")
            st.dataframe(pd.DataFrame(ifind_quotes), hide_index=True, width="stretch")
        except IFindConnectionError as error:
            st.error(f"同花顺实时行情测试失败：{error}")
        except Exception:
            st.error("同花顺实时行情测试失败：发生未知错误，请稍后重试。")
    st.caption(
        "同一时刻只允许一次完整扫描，扫描期间操作按钮会锁定。"
        "当前版本不安全中断已发出的公开数据请求；需强制终止时请停止 Streamlit 进程。"
    )

    notice = st.session_state.dashboard_notice
    if notice:
        level, message = notice
        getattr(st, level)(message)

    render_strategy_flow()

    if st.session_state.scan_payload is None:
        st.info("点击“开始今日扫描”运行现有策略；首页不会自动请求行情。")
    else:
        render_scan_results(st.session_state.scan_payload)

    render_mobile_action_bar()

    st.markdown(
        '<div class="risk-banner">本工具仅用于公开数据筛选和策略研究，'
        '所有观察条件、失效条件、止损或目标区间均为策略研究参考，'
        '不构成任何投资建议。</div>',
        unsafe_allow_html=True,
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
        realtime_tab, backtest_tab = st.tabs(["实时选股", "历史回测"])
        with realtime_tab:
            render_home()
        with backtest_tab:
            render_backtest_page()
    except Exception as error:
        logger.exception("页面渲染失败")
        st.error(f"页面加载失败：{type(error).__name__}")
        st.info("请刷新页面重试；详细信息已写入应用日志。")


if __name__ == "__main__":
    main()
