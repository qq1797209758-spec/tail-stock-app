"""下一交易日复盘任务、交易模拟与无未来数据的策略学习建议。"""

from __future__ import annotations

from datetime import date, datetime, time
import json
import math
from zoneinfo import ZoneInfo

import akshare as ak
import numpy as np
import pandas as pd

from config import (
    REVIEW_COMMISSION_RATE, REVIEW_FULL_MODEL_DAYS, REVIEW_MIN_OPTIMIZATION_DAYS,
    REVIEW_MIN_OPTIMIZATION_SAMPLES, REVIEW_SLIPPAGE_RATE, REVIEW_STAMP_DUTY_RATE,
    REVIEW_STOP_LOSS, REVIEW_TAKE_PROFIT,
)
from services.backtest_data import fetch_trade_calendar
from services.network import call_with_proxy_fallback
from services.review_store import ReviewRepository


SHANGHAI = ZoneInfo("Asia/Shanghai")


def safe_return(price: object, benchmark: object) -> float | None:
    try:
        price_value, base = float(price), float(benchmark)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(price_value) or not math.isfinite(base) or base <= 0:
        return None
    result = price_value / base - 1
    return float(result) if math.isfinite(result) else None


def next_trade_date(recommendation_date: date, calendar: pd.DatetimeIndex) -> date | None:
    candidates = calendar[calendar.date > recommendation_date]
    return candidates[0].date() if len(candidates) else None


def simulate_one_day(low_return: float | None, high_return: float | None,
                     close_return: float | None) -> tuple[float | None, str]:
    """日线不能判断先后时同时触发按止损；收益扣买卖成本、印花税和滑点。"""
    values=(low_return,high_return,close_return)
    if any(value is None or not math.isfinite(value) for value in values):
        return None, "证据不足"
    stop_hit = low_return <= REVIEW_STOP_LOSS
    profit_hit = high_return >= REVIEW_TAKE_PROFIT
    if stop_hit:
        gross = REVIEW_STOP_LOSS
        exit_reason = "止损（同时触发时保守优先）" if profit_hit else "止损"
    elif profit_hit:
        gross = REVIEW_TAKE_PROFIT
        exit_reason = "止盈"
    else:
        gross = close_return
        exit_reason = "收盘卖出"
    costs = REVIEW_COMMISSION_RATE * 2 + REVIEW_STAMP_DUTY_RATE + REVIEW_SLIPPAGE_RATE * 2
    result = gross - costs
    return (float(result) if math.isfinite(result) else None), exit_reason


def _daily_quote(symbol: str, trade_date: date) -> tuple[dict[str, float] | None, str]:
    day=trade_date.strftime("%Y%m%d")
    primary_error=""
    try:
        frame=call_with_proxy_fallback(lambda: ak.stock_zh_a_hist(
            symbol=str(symbol).zfill(6), period="daily", start_date=day, end_date=day, adjust=""
        ))
    except Exception as error:
        primary_error=f"东方财富日线失败（{type(error).__name__}）：{error}"
        frame=pd.DataFrame()
    mapping={"open_price":"开盘","high_price":"最高","low_price":"最低","close_price":"收盘","volume":"成交量","amount":"成交额"}
    source="AKShare·东方财富不复权日线"
    if not isinstance(frame,pd.DataFrame) or frame.empty:
        market=("sh" if str(symbol).zfill(6).startswith("60") else "sz")+str(symbol).zfill(6)
        try:
            frame=call_with_proxy_fallback(lambda: ak.stock_zh_a_daily(
                symbol=market,start_date=day,end_date=day,adjust=""
            ))
            mapping={"open_price":"open","high_price":"high","low_price":"low","close_price":"close","volume":"volume","amount":"amount"}
            source="AKShare·新浪不复权日线"
        except Exception as error:
            return None,primary_error+f"；新浪日线失败（{type(error).__name__}）：{error}"
    if not isinstance(frame,pd.DataFrame) or frame.empty:
        return None,"；".join(filter(None,[primary_error,"日线返回为空（可能停牌、退市或数据尚未发布）"]))
    row=frame.iloc[-1]
    values={key:pd.to_numeric(pd.Series([row.get(column)]),errors="coerce").iloc[0] for key,column in mapping.items()}
    if any(pd.isna(values[key]) for key in ("open_price","high_price","low_price","close_price")):
        return None,"日线价格字段缺失"
    return {key:float(value) if pd.notna(value) and np.isfinite(value) else None for key,value in values.items()},source+(f"（已降级：{primary_error}）" if primary_error else "")


def _conclusion(values: dict, exit_reason: str) -> str:
    if values.get("close_return") is None:
        return "证据不足，尚不能形成复盘结论。"
    opening="高开" if values["open_return"] > 0 else "低开或平开"
    return (f"该股次日{opening}{values['open_return']:.2%}，盘中最高{values['high_return']:.2%}，"
            f"最低{values['low_return']:.2%}，收盘{values['close_return']:.2%}；模拟规则按{exit_reason}执行。"
            "结论仅描述价格数据，不推断主力意图。")


def run_pending_reviews(repository: ReviewRepository, now: datetime | None = None,
                        quote_fetcher=_daily_quote, limit: int = 20) -> dict[str, int]:
    now=(now or datetime.now(SHANGHAI)).astimezone(SHANGHAI)
    calendar=fetch_trade_calendar()
    counts={"completed":0,"waiting":0,"failed":0}
    for item in repository.pending_recommendations(limit=limit):
        recommendation_day=date.fromisoformat(item["run_recommendation_date"])
        review_day=next_trade_date(recommendation_day,calendar)
        base=item.get("recommendation_close") or item.get("recommended_price")
        if review_day is None or now.date() < review_day or (now.date()==review_day and now.time()<time(15,5)):
            repository.upsert_review(item["id"],{"review_trade_date":review_day.isoformat() if review_day else None,
                "review_status":"等待复盘","error_reason":"下一交易日尚未收盘","reviewed_at":now.isoformat()})
            counts["waiting"]+=1; continue
        if item.get("recommendation_close") is None:
            recommendation_quote, recommendation_source=quote_fetcher(item["symbol"],recommendation_day)
            if recommendation_quote is None:
                repository.upsert_review(item["id"],{"review_trade_date":review_day.isoformat(),"review_status":"待补录",
                    "error_reason":"推荐日收盘价缺失；"+recommendation_source,"reviewed_at":now.isoformat()})
                counts["failed"]+=1; continue
            base=recommendation_quote["close_price"]
        quote,source=quote_fetcher(item["symbol"],review_day)
        if quote is None:
            repository.upsert_review(item["id"],{"review_trade_date":review_day.isoformat(),"review_status":"待补录",
                "error_reason":source,"reviewed_at":now.isoformat()})
            counts["failed"]+=1; continue
        values=dict(quote)
        for label,price_key in (("open_return","open_price"),("high_return","high_price"),
                                ("low_return","low_price"),("close_return","close_price")):
            values[label]=safe_return(values[price_key],base)
        simulated,exit_reason=simulate_one_day(values["low_return"],values["high_return"],values["close_return"])
        values.update(simulated_return=simulated,mfe=values["high_return"],mae=values["low_return"],
            opened_up=int(values["open_return"]>0),gap_up_fade=int(values["open_return"]>0 and values["close_return"]<values["open_return"]),
            gap_down_recover=int(values["open_return"]<0 and values["close_return"]>0),
            limit_up=int(values["high_return"]>=0.095),limit_down=int(values["low_return"]<=-0.095),
            take_profit_hit=int(values["high_return"]>=REVIEW_TAKE_PROFIT),stop_loss_hit=int(values["low_return"]<=REVIEW_STOP_LOSS),
            open_success=int(values["open_return"]>0),close_success=int(values["close_return"]>0),
            trade_success=int(simulated is not None and simulated>0),review_trade_date=review_day.isoformat(),
            review_status="完成",error_reason="",reviewed_at=now.isoformat())
        values["conclusion"]=_conclusion(values,exit_reason)
        repository.upsert_review(item["id"],values); counts["completed"]+=1
    return counts


def learning_status(frame: pd.DataFrame) -> dict[str, object]:
    complete=frame.loc[frame.get("review_status",pd.Series(dtype=str)).eq("完成")].copy()
    days=int(complete["recommendation_date"].nunique()) if not complete.empty else 0
    samples=len(complete)
    if days < REVIEW_MIN_OPTIMIZATION_DAYS or samples < REVIEW_MIN_OPTIMIZATION_SAMPLES:
        return {"eligible":False,"stage":"积累样本","message":f"当前{days}个交易日/{samples}只；至少需要20日且100只才生成优化建议。"}
    stage="完整模型比较" if days>=REVIEW_FULL_MODEL_DAYS else "候选参数建议"
    return {"eligible":True,"stage":stage,"message":"仅生成候选策略；按日期顺序walk-forward验证，未经用户确认不会启用。"}


def indicator_effectiveness(frame: pd.DataFrame) -> pd.DataFrame:
    complete=frame.loc[frame.get("review_status",pd.Series(dtype=str)).eq("完成")].copy()
    features={"量比":"量比","换手率":"换手率","资金净流入":"主力资金净流入","高于VWAP占比":"高于VWAP占比",
              "尾盘最大回撤":"尾盘最大回撤","最后10分钟涨跌幅":"最后10分钟涨跌幅","板块强度":"近5日板块强度"}
    rows=[]
    for label,key in features.items():
        values=[]; returns=[]
        for _,row in complete.iterrows():
            try: snapshot=json.loads(row["feature_snapshot"]); value=float(snapshot.get(key)); ret=float(row["close_return"])
            except Exception: continue
            if math.isfinite(value) and math.isfinite(ret): values.append(value); returns.append(ret)
        n=len(values)
        correlation=float(pd.Series(values).corr(pd.Series(returns))) if n>=3 else None
        strength=abs(correlation) if correlation is not None and math.isfinite(correlation) else 0
        verdict="暂不明确" if n<30 else ("有效" if strength>=0.25 else "可能有效" if strength>=0.12 else "可能无效")
        rows.append({"指标":label,"样本数":n,"与次日收盘收益相关系数":correlation,"统计结论":verdict})
    return pd.DataFrame(rows)


def build_candidate_strategy(frame: pd.DataFrame) -> dict[str, object]:
    """按日期顺序60/20/20切分；特征仅取推荐快照，标签仅用于区间评估。"""
    complete=frame.loc[frame.get("review_status",pd.Series(dtype=str)).eq("完成")].sort_values(
        ["recommendation_date","rank"],kind="mergesort"
    ).copy()
    status=learning_status(complete)
    if not status["eligible"]:
        return {"eligible":False,"message":status["message"]}
    dates=sorted(complete["recommendation_date"].unique())
    train_end=max(1,int(len(dates)*.6)); validation_end=max(train_end+1,int(len(dates)*.8))
    train_dates=set(dates[:train_end]); validation_dates=set(dates[train_end:validation_end]); test_dates=set(dates[validation_end:])
    train=complete.loc[complete["recommendation_date"].isin(train_dates)]
    weights: dict[str,float]={}
    component_names:set[str]=set()
    for raw in train["component_scores"].dropna():
        try: component_names.update(json.loads(raw))
        except Exception: continue
    strengths={}
    for name in component_names:
        x=[]; y=[]
        for _,row in train.iterrows():
            try: value=float(json.loads(row["component_scores"]).get(name)); target=float(row["close_return"])
            except Exception: continue
            if math.isfinite(value) and math.isfinite(target): x.append(value); y.append(target)
        corr=(pd.Series(x).corr(pd.Series(y)) if len(x)>=10 and len(set(x))>1 and len(set(y))>1 else np.nan)
        strengths[name]=max(0.01,float(corr)) if pd.notna(corr) else 0.01
    total=sum(strengths.values()) or 1
    weights={name:round(value/total*100,2) for name,value in strengths.items()}
    def metrics(dates_set:set[str]):
        subset=complete.loc[complete["recommendation_date"].isin(dates_set)]
        returns=pd.to_numeric(subset["close_return"],errors="coerce").replace([np.inf,-np.inf],np.nan).dropna()
        return {"samples":len(returns),"average_return":None if returns.empty else float(returns.mean()),
                "median_return":None if returns.empty else float(returns.median()),"win_rate":None if returns.empty else float((returns>0).mean())}
    return {"eligible":True,"candidate_weights":weights,
            "train":{"start":dates[0],"end":dates[train_end-1],**metrics(train_dates)},
            "validation":{"start":dates[train_end] if train_end<len(dates) else None,
                          "end":dates[validation_end-1] if validation_end>train_end else None,**metrics(validation_dates)},
            "out_of_sample":{"start":dates[validation_end] if validation_end<len(dates) else None,
                             "end":dates[-1],**metrics(test_dates)},
            "status":"候选策略（未启用）"}
