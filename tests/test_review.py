from datetime import date, datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from services.review_service import (
    build_candidate_strategy, learning_status, next_trade_date, run_pending_reviews, safe_return, simulate_one_day,
)
from services.review_store import SQLiteReviewRepository


def _repo(tmp_path):
    repo=SQLiteReviewRepository(tmp_path/"review.db")
    records=[]
    for rank,code in enumerate(("000001","000002","000003","600000","600001"),1):
        records.append({"symbol":code,"name":code,"rank":rank,"recommended_price":10.0,
            "recommendation_close":10.0,"total_score":80-rank,"component_scores":{"量能得分":10},
            "selection_type":"严格入选" if rank<3 else "一级递补","feature_snapshot":{"量比":1.2},
            "data_completeness":.9,"selection_reason":"测试","risk_warning":"测试风险"})
    repo.save_official_run(recommendation_date="2026-07-17",generated_at="2026-07-17T15:00:00+08:00",
        strategy_version="test-v1",market_state="中性",data_source="真实测试快照",recommendations=records)
    return repo,records


def test_save_top5_and_duplicate_scan_is_upsert(tmp_path):
    repo,records=_repo(tmp_path)
    repo.save_official_run(recommendation_date="2026-07-17",generated_at="2026-07-17T15:01:00+08:00",
        strategy_version="test-v1",market_state="中性",data_source="真实测试快照",recommendations=records)
    frame=repo.review_frame()
    assert len(frame)==5
    assert frame["recommendation_id"].nunique()==5


def test_friday_and_holiday_use_next_real_trade_day():
    calendar=pd.DatetimeIndex(["2026-07-17","2026-07-20","2026-10-09"])
    assert next_trade_date(date(2026,7,17),calendar)==date(2026,7,20)
    assert next_trade_date(date(2026,10,1),calendar)==date(2026,10,9)


def test_review_does_not_run_before_next_close(tmp_path,monkeypatch):
    repo,_=_repo(tmp_path)
    monkeypatch.setattr("services.review_service.fetch_trade_calendar",lambda:pd.DatetimeIndex(["2026-07-17","2026-07-20"]))
    result=run_pending_reviews(repo,datetime(2026,7,20,14,59,tzinfo=ZoneInfo("Asia/Shanghai")),
        quote_fetcher=lambda *args:(_ for _ in ()).throw(AssertionError("不应提前请求")))
    assert result["waiting"]==5
    assert set(repo.review_frame()["review_status"])=={"等待复盘"}


def test_failure_remains_pending_and_missing_is_not_loss(tmp_path,monkeypatch):
    repo,_=_repo(tmp_path)
    monkeypatch.setattr("services.review_service.fetch_trade_calendar",lambda:pd.DatetimeIndex(["2026-07-17","2026-07-20"]))
    result=run_pending_reviews(repo,datetime(2026,7,20,16,0,tzinfo=ZoneInfo("Asia/Shanghai")),quote_fetcher=lambda *args:(None,"接口失败"))
    frame=repo.review_frame()
    assert result["failed"]==5 and set(frame["review_status"])=={"待补录"}
    assert frame["close_return"].isna().all() and frame["trade_success"].isna().all()


def test_completed_review_is_idempotent(tmp_path,monkeypatch):
    repo,_=_repo(tmp_path)
    monkeypatch.setattr("services.review_service.fetch_trade_calendar",lambda:pd.DatetimeIndex(["2026-07-17","2026-07-20"]))
    quote={"open_price":10.1,"high_price":10.4,"low_price":9.9,"close_price":10.2,"volume":100,"amount":1000}
    now=datetime(2026,7,20,16,0,tzinfo=ZoneInfo("Asia/Shanghai"))
    assert run_pending_reviews(repo,now,lambda *args:(quote,"test"))["completed"]==5
    assert run_pending_reviews(repo,now,lambda *args:(quote,"test"))["completed"]==0
    assert len(repo.review_frame())==5


def test_safe_return_never_produces_non_finite():
    assert safe_return(10,0) is None
    assert safe_return(np.inf,10) is None
    assert safe_return(11,10)==pytest.approx(0.1)


def test_both_profit_and_stop_hit_uses_conservative_stop():
    result,reason=simulate_one_day(-0.03,0.04,0.01)
    assert result < -0.02
    assert "保守优先" in reason


def test_learning_requires_20_days_and_100_completed_samples():
    small=pd.DataFrame({"review_status":["完成"]*99,"recommendation_date":[f"2026-06-{(i%19)+1:02d}" for i in range(99)]})
    assert learning_status(small)["eligible"] is False


def test_strategy_is_not_automatically_replaced(tmp_path):
    repo,_=_repo(tmp_path)
    repo.ensure_strategy("official",{"a":1},{},"2026-07-01")
    learning_status(pd.DataFrame({"review_status":["完成"]*100,"recommendation_date":[f"2026-06-{(i%20)+1:02d}" for i in range(100)]}))
    versions=repo.export_tables()["strategy_versions"]
    assert len(versions)==1 and versions.iloc[0]["version"]=="official"


def test_backup_restore_survives_repository_restart(tmp_path):
    repo,_=_repo(tmp_path); payload=repo.backup_bytes()
    restored=SQLiteReviewRepository(tmp_path/"restored.db"); restored.restore_bytes(payload)
    assert len(SQLiteReviewRepository(tmp_path/"restored.db").review_frame())==5


def test_candidate_strategy_uses_chronological_non_overlapping_splits():
    rows=[]
    for day in range(1,21):
        for rank in range(1,6):
            rows.append({"review_status":"完成","recommendation_date":f"2026-06-{day:02d}","rank":rank,
                         "component_scores":'{"量能得分": 10}',"close_return":.01 if rank<4 else -.01})
    result=build_candidate_strategy(pd.DataFrame(rows))
    assert result["eligible"] is True
    assert result["train"]["end"] < result["validation"]["start"]
    assert result["validation"]["end"] < result["out_of_sample"]["start"]
    assert result["status"]=="候选策略（未启用）"
