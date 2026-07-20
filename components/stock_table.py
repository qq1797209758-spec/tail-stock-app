"""响应式股票结果组件与 Excel 导出。"""

from html import escape
from io import BytesIO

import pandas as pd
import streamlit as st

from config import MARKET_CAP_UNIT


DISPLAY_COLUMNS = [
    "代码",
    "名称",
    "最新价",
    "涨跌幅",
    "换手率",
    "量比",
    "总市值",
    "最近涨停日期",
    "尾盘结构状态",
    "VWAP状态",
    "高于VWAP占比",
    "尾盘最大回撤",
    "最后10分钟涨跌幅",
    "连续走弱状态",
    "尾盘成交量状态",
    "尾盘结构评分",
    "淘汰原因",
    "数据完整性",
    "尾盘排除原因",
    "综合得分",
    "观察标记",
    "资金表现得分",
    "板块强度得分",
    "技术形态得分",
    "尾盘结构得分",
    "市场情绪得分",
    "所属行业",
    "行业涨跌幅",
    "主力净流入占比",
    "缺失项",
    "入选原因",
    "风险",
    "次日观察条件",
]


def _display_data(data: pd.DataFrame) -> pd.DataFrame:
    available_columns = [column for column in DISPLAY_COLUMNS if column in data.columns]
    result = data.loc[:, available_columns].copy()
    result.rename(
        columns={
            "涨跌幅": "涨跌幅 (%)",
            "换手率": "换手率 (%)",
            "总市值": "总市值 (亿元)",
        },
        inplace=True,
    )
    result["总市值 (亿元)"] = result["总市值 (亿元)"] / MARKET_CAP_UNIT
    return result


def _format_number(value: object, digits: int = 2) -> str:
    if pd.isna(value):
        return "--"
    return f"{float(value):.{digits}f}"


def _format_display_value(column: str, value: object) -> str:
    if pd.isna(value):
        return "--"
    numeric_columns = {
        "最新价",
        "涨跌幅 (%)",
        "换手率 (%)",
        "量比",
        "总市值 (亿元)",
        "综合得分",
        "资金表现得分",
        "板块强度得分",
        "技术形态得分",
        "尾盘结构得分",
        "市场情绪得分",
        "行业涨跌幅",
        "主力净流入占比",
    }
    if column == "尾盘结构评分":
        return _format_number(value, 0)
    if column in {
        "高于VWAP占比", "尾盘最大回撤", "最后10分钟涨跌幅", "数据完整性",
    }:
        return f"{float(value):.1%}"
    if column in numeric_columns:
        return _format_number(value)
    return escape(str(value))


def render_stock_results(data: pd.DataFrame) -> None:
    """桌面显示表格，窄屏显示卡片，数据均来自真实筛选结果。"""
    display = _display_data(data)
    headers = "".join(f"<th>{escape(str(column))}</th>" for column in display.columns)
    rows = []
    cards = []

    for _, row in display.iterrows():
        values = [
            _format_display_value(column, row[column]) for column in display.columns
        ]
        rows.append("<tr>" + "".join(f"<td>{value}</td>" for value in values) + "</tr>")
        code = _format_display_value("代码", row.get("代码"))
        name = _format_display_value("名称", row.get("名称"))
        card_fields = "".join(
            f"<div><small>{escape(str(column))}</small><b>{_format_display_value(column, row[column])}</b></div>"
            for column in display.columns
            if column not in {"代码", "名称"}
        )
        cards.append(
            f"""
            <details class="stock-card">
                <summary class="stock-card-title">
                    <strong>{name}</strong><span>{code}</span>
                </summary>
                <div class="stock-card-grid">{card_fields}</div>
            </details>
            """
        )

    st.markdown(
        f"""
        <div class="stock-desktop-table">
            <table><thead><tr>{headers}</tr></thead><tbody>{''.join(rows)}</tbody></table>
        </div>
        <div class="stock-mobile-cards">{''.join(cards)}</div>
        """,
        unsafe_allow_html=True,
    )


def build_excel(data: pd.DataFrame) -> bytes:
    """把真实筛选结果导出为 Excel 字节流。"""
    output = BytesIO()
    export_data = _display_data(data)
    export_data.to_excel(output, index=False, engine="openpyxl", sheet_name="筛选结果")
    return output.getvalue()
