"""每日复盘、历史复盘和策略学习页面。"""

from __future__ import annotations

from datetime import date, datetime, timedelta
import io
import json
import math

import numpy as np
import pandas as pd
import streamlit as st

from services.review_service import build_candidate_strategy, indicator_effectiveness, learning_status, run_pending_reviews
from services.review_store import SQLiteReviewRepository


DISCLAIMER="历史复盘与模型评分仅用于量化研究，不代表未来收益，不构成投资建议。"


@st.fragment(run_every="10m")
def _auto_review_fragment(repository: SQLiteReviewRepository) -> None:
    """浏览器保持打开时定时检查；离线无人值守需外部调度器。"""
    try:
        result=run_pending_reviews(repository)
        if result["completed"] or result["failed"]:
            st.caption(f"自动复盘检查：完成{result['completed']}，待补录{result['failed']}。")
    except Exception as error:
        st.caption(f"自动复盘检查暂不可用：{type(error).__name__}")


def _pct(value: object) -> str:
    try: numeric=float(value)
    except (TypeError,ValueError): return "数据不足"
    return f"{numeric:.2%}" if math.isfinite(numeric) else "数据不足"


def _summary(frame: pd.DataFrame) -> dict[str, object]:
    complete=frame.loc[frame["review_status"].eq("完成")].copy()
    returns=pd.to_numeric(complete["close_return"],errors="coerce").replace([np.inf,-np.inf],np.nan).dropna()
    simulated=pd.to_numeric(complete["simulated_return"],errors="coerce").replace([np.inf,-np.inf],np.nan).dropna()
    return {"completed":len(complete),"open_up":int(pd.to_numeric(complete["open_success"],errors="coerce").fillna(0).sum()),
            "close_up":int(pd.to_numeric(complete["close_success"],errors="coerce").fillna(0).sum()),
            "average":returns.mean() if not returns.empty else None,"median":returns.median() if not returns.empty else None,
            "simulation":simulated.mean() if not simulated.empty else None,
            "best":None if returns.empty else complete.loc[returns.idxmax(),"name"],
            "worst":None if returns.empty else complete.loc[returns.idxmin(),"name"]}


def render_daily_review(repository: SQLiteReviewRepository) -> None:
    st.markdown("## 每日复盘")
    st.caption("收益基准：推荐日不复权收盘价。日线统一使用不复权口径；停牌或缺失数据保持待补录。")
    _auto_review_fragment(repository)
    if st.button("手动补录复盘",type="primary"):
        with st.spinner("正在检查待复盘记录……"):
            result=run_pending_reviews(repository)
        st.success(f"完成 {result['completed']} 条，等待 {result['waiting']} 条，待补录 {result['failed']} 条。")
    frame=repository.review_frame()
    if frame.empty:
        st.info("尚无正式Top5推荐快照。完整生成5只后会自动保存。")
        st.warning("当前使用本地 SQLite；Streamlit Cloud 重启、迁移或重建容器可能导致数据丢失，请定期下载备份。")
        st.info(DISCLAIMER); return
    latest=frame["recommendation_date"].max()
    daily=frame.loc[frame["recommendation_date"].eq(latest)].copy()
    summary=_summary(daily)
    cols=st.columns(7)
    metrics=[("开盘上涨",f"{summary['open_up']}/{len(daily)}"),("收盘上涨",f"{summary['close_up']}/{len(daily)}"),
             ("Top5平均收益",_pct(summary["average"])),("中位数收益",_pct(summary["median"])),
             ("最佳股票",summary["best"] or "等待"),("最差股票",summary["worst"] or "等待"),
             ("模拟策略收益",_pct(summary["simulation"]))]
    for column,(label,value) in zip(cols,metrics): column.metric(label,value)
    conclusion=("等待下一交易日收盘数据" if summary["completed"]<len(daily) else
                f"收盘上涨{summary['close_up']}只，Top5平均收益{_pct(summary['average'])}。")
    st.info(f"当日复盘结论：{conclusion}")
    shown=pd.DataFrame({
        "昨日排名":daily["rank"],"股票代码":daily["symbol"],"股票名称":daily["name"],
        "推荐价":daily["recommended_price"],"次日开盘价":daily["open_price"],"次日最高价":daily["high_price"],
        "次日最低价":daily["low_price"],"次日收盘价":daily["close_price"],"开盘涨幅":daily["open_return"],
        "最高涨幅":daily["high_return"],"最大回撤":daily["mae"],"收盘涨幅":daily["close_return"],
        "模拟交易收益":daily["simulated_return"],"入选类型":daily["selection_type"],"综合得分":daily["total_score"],
        "是否成功":daily["close_success"].map({1:"是",0:"否"}).fillna("等待数据"),
        "复盘状态":daily["review_status"],"复盘结论":daily["conclusion"].fillna(daily["error_reason"]),
    })
    percent=["开盘涨幅","最高涨幅","最大回撤","收盘涨幅","模拟交易收益"]
    st.dataframe(shown.style.format({key:_pct for key in percent}),hide_index=True,width="stretch")
    st.warning("SQLite 云端持久性提示：应用重启或重新部署可能清空本地文件，请使用下方备份。")
    st.download_button("下载复盘数据库备份",repository.backup_bytes(),"tail-stock-review.sqlite","application/x-sqlite3")
    uploaded=st.file_uploader("导入复盘数据库备份",type=["sqlite","db"])
    if uploaded and st.button("确认导入备份"):
        repository.restore_bytes(uploaded.getvalue()); st.session_state.review_auto_checked=False; st.success("备份已导入，请刷新页面。")
    st.info(DISCLAIMER)


def _max_streak(values: pd.Series, positive: bool) -> int:
    best=current=0
    for value in values.dropna():
        matched=value>0 if positive else value<=0
        current=current+1 if matched else 0; best=max(best,current)
    return best


def render_historical_review(repository: SQLiteReviewRepository) -> None:
    st.markdown("## 历史复盘与策略学习")
    frame=repository.review_frame()
    if frame.empty: st.info("暂无复盘样本。"); st.info(DISCLAIMER); return
    range_choice=st.selectbox("统计区间",["最近5个交易日","最近20个交易日","最近60个交易日","自定义日期"])
    days={"最近5个交易日":5,"最近20个交易日":20,"最近60个交易日":60}.get(range_choice)
    dates=sorted(frame["recommendation_date"].dropna().unique())
    if days: selected_dates=set(dates[-days:]); filtered=frame.loc[frame["recommendation_date"].isin(selected_dates)].copy()
    else:
        interval=st.date_input("日期范围",value=(date.fromisoformat(min(dates)),date.fromisoformat(max(dates))))
        filtered=frame.copy() if len(interval)!=2 else frame.loc[frame["recommendation_date"].between(interval[0].isoformat(),interval[1].isoformat())].copy()
    versions=st.multiselect("策略版本",sorted(frame["strategy_version"].dropna().unique()),default=sorted(frame["strategy_version"].dropna().unique()))
    types=st.multiselect("入选类型",sorted(frame["selection_type"].dropna().unique()),default=sorted(frame["selection_type"].dropna().unique()))
    sectors=st.multiselect("所属板块",sorted(frame["sector"].dropna().unique()))
    if versions: filtered=filtered.loc[filtered["strategy_version"].isin(versions)]
    if types: filtered=filtered.loc[filtered["selection_type"].isin(types)]
    if sectors: filtered=filtered.loc[filtered["sector"].isin(sectors)]
    complete=filtered.loc[filtered["review_status"].eq("完成")].copy()
    returns=pd.to_numeric(complete["close_return"],errors="coerce").replace([np.inf,-np.inf],np.nan)
    simulated=pd.to_numeric(complete["simulated_return"],errors="coerce").replace([np.inf,-np.inf],np.nan)
    wins=simulated[simulated>0]; losses=simulated[simulated<0]
    profit_factor=(wins.mean()/abs(losses.mean())) if not wins.empty and not losses.empty and losses.mean()!=0 else None
    daily=complete.assign(_ret=returns,_sim=simulated).groupby("recommendation_date").agg(平均收益=("_ret","mean"),模拟收益=("_sim","mean"),开盘上涨率=("open_success","mean"),收盘上涨率=("close_success","mean"))
    equity=(1+daily["模拟收益"].fillna(0)).cumprod(); drawdown=equity/equity.cummax()-1
    metrics=[("推荐总数",len(filtered)),("完成复盘",len(complete)),("开盘上涨率",_pct(complete["open_success"].mean() if not complete.empty else None)),
             ("收盘上涨率",_pct(complete["close_success"].mean() if not complete.empty else None)),("平均收益",_pct(returns.mean())),
             ("收益中位数",_pct(returns.median())),("累计模拟收益",_pct(equity.iloc[-1]-1 if not equity.empty else None)),
             ("盈亏比","数据不足" if profit_factor is None else f"{profit_factor:.2f}"),("最大回撤",_pct(drawdown.min() if not drawdown.empty else None)),
             ("最长连赢",_max_streak(simulated,True)),("最长连亏",_max_streak(simulated,False)),
             ("止盈触发率",_pct(complete["take_profit_hit"].mean() if not complete.empty else None)),
             ("止损触发率",_pct(complete["stop_loss_hit"].mean() if not complete.empty else None))]
    for start in range(0,len(metrics),4):
        for column,(label,value) in zip(st.columns(4),metrics[start:start+4]): column.metric(label,value)
    if not daily.empty:
        st.line_chart(daily[["平均收益"]]); st.line_chart(pd.DataFrame({"模拟策略累计收益":equity-1,"最近20日回撤":drawdown}))
        st.line_chart(daily[["开盘上涨率","收盘上涨率"]])
    for title,column in (("不同排名胜率","rank"),("严格与递补胜率","selection_type"),("不同板块胜率","sector"),("不同市场环境胜率","market_state")):
        if not complete.empty:
            st.markdown(f"#### {title}"); st.bar_chart(complete.groupby(column)["close_success"].agg(["mean","count"]).rename(columns={"mean":"胜率","count":"样本数"}))
    if not complete.empty:
        bins=pd.cut(pd.to_numeric(complete["total_score"],errors="coerce"),bins=[0,60,70,80,90,100],include_lowest=True)
        st.markdown("#### 各评分区间收益对比"); st.bar_chart(complete.assign(评分区间=bins).groupby("评分区间",observed=True)["close_return"].agg(["mean","count"]))
    st.markdown("#### 指标有效性"); st.dataframe(indicator_effectiveness(filtered),hide_index=True,width="stretch")
    status=learning_status(filtered); (st.success if status["eligible"] else st.info)(f"{status['stage']}：{status['message']}")
    if status["eligible"] and st.button("生成候选权重与时间序列验证报告"):
        suggestion=build_candidate_strategy(filtered)
        st.json(suggestion)
        st.warning("该结果仅为候选策略，不会修改当前正式权重；必须经用户确认后另行创建并启用策略版本。")
    st.caption("学习流程按推荐日期顺序划分训练、验证和样本外测试区间；不随机打乱，不读取次日特征，也不会自动替换正式策略。")
    st.info(DISCLAIMER)
