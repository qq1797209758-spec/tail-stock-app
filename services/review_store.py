"""每日推荐、复盘和策略版本的持久化接口及 SQLite 实现。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from contextlib import contextmanager
from datetime import date
import json
from pathlib import Path
import sqlite3
from typing import Any

import pandas as pd


class ReviewRepository(ABC):
    @abstractmethod
    def save_official_run(self, **kwargs) -> int: ...
    @abstractmethod
    def pending_recommendations(self) -> list[dict[str, Any]]: ...
    @abstractmethod
    def upsert_review(self, recommendation_id: int, values: dict[str, Any]) -> None: ...
    @abstractmethod
    def review_frame(self, start: str | None = None, end: str | None = None) -> pd.DataFrame: ...


class SQLiteReviewRepository(ReviewRepository):
    """本地 SQLite 实现；接口刻意与未来云数据库实现解耦。"""

    def __init__(self, database_path: str | Path):
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.migrate()

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.database_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def migrate(self) -> None:
        with self._connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS recommendation_runs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              recommendation_date TEXT NOT NULL,
              generated_at TEXT NOT NULL,
              strategy_version TEXT NOT NULL,
              market_state TEXT,
              data_source TEXT,
              status TEXT NOT NULL DEFAULT '正式',
              UNIQUE(recommendation_date, strategy_version)
            );
            CREATE TABLE IF NOT EXISTS recommendations (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              run_id INTEGER NOT NULL REFERENCES recommendation_runs(id) ON DELETE CASCADE,
              symbol TEXT NOT NULL, name TEXT, rank INTEGER NOT NULL,
              sector TEXT, recommended_price REAL, recommendation_close REAL,
              total_score REAL, component_scores TEXT NOT NULL,
              selection_type TEXT, feature_snapshot TEXT NOT NULL,
              data_completeness REAL, selection_reason TEXT, risk_warning TEXT,
              UNIQUE(run_id, symbol)
            );
            CREATE TABLE IF NOT EXISTS review_results (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              recommendation_id INTEGER NOT NULL UNIQUE REFERENCES recommendations(id) ON DELETE CASCADE,
              review_trade_date TEXT, open_price REAL, high_price REAL, low_price REAL,
              close_price REAL, volume REAL, amount REAL,
              open_return REAL, high_return REAL, low_return REAL, close_return REAL,
              simulated_return REAL, mfe REAL, mae REAL,
              opened_up INTEGER, gap_up_fade INTEGER, gap_down_recover INTEGER,
              limit_up INTEGER, limit_down INTEGER, take_profit_hit INTEGER, stop_loss_hit INTEGER,
              open_success INTEGER, close_success INTEGER, trade_success INTEGER,
              conclusion TEXT, review_status TEXT NOT NULL DEFAULT '等待复盘',
              error_reason TEXT, reviewed_at TEXT,
              UNIQUE(recommendation_id, review_trade_date)
            );
            CREATE TABLE IF NOT EXISTS strategy_versions (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              version TEXT NOT NULL UNIQUE, weights TEXT NOT NULL, rules TEXT NOT NULL,
              created_at TEXT NOT NULL, activated_at TEXT, status TEXT NOT NULL,
              backtest_metrics TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_runs_date ON recommendation_runs(recommendation_date);
            CREATE INDEX IF NOT EXISTS idx_reviews_status ON review_results(review_status);
            """)

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, allow_nan=False, default=str)

    def ensure_strategy(self, version: str, weights: dict, rules: dict, created_at: str) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT INTO strategy_versions(version,weights,rules,created_at,activated_at,status) VALUES(?,?,?,?,?,'启用') "
                "ON CONFLICT(version) DO NOTHING",
                (version, self._json(weights), self._json(rules), created_at, created_at),
            )

    def save_official_run(self, *, recommendation_date: str, generated_at: str,
                          strategy_version: str, market_state: str, data_source: str,
                          recommendations: list[dict[str, Any]]) -> int:
        if len(recommendations) != 5:
            raise ValueError("正式推荐快照必须恰好包含5只股票")
        symbols = [str(item["symbol"]).zfill(6) for item in recommendations]
        if len(set(symbols)) != 5:
            raise ValueError("正式推荐快照包含重复股票")
        with self._connect() as db:
            db.execute(
                "INSERT INTO recommendation_runs(recommendation_date,generated_at,strategy_version,market_state,data_source,status) "
                "VALUES(?,?,?,?,?,'正式') ON CONFLICT(recommendation_date,strategy_version) DO UPDATE SET "
                "generated_at=excluded.generated_at,market_state=excluded.market_state,data_source=excluded.data_source",
                (recommendation_date, generated_at, strategy_version, market_state, data_source),
            )
            run_id = int(db.execute(
                "SELECT id FROM recommendation_runs WHERE recommendation_date=? AND strategy_version=?",
                (recommendation_date, strategy_version),
            ).fetchone()[0])
            for item in recommendations:
                db.execute("""
                INSERT INTO recommendations(run_id,symbol,name,rank,sector,recommended_price,recommendation_close,
                  total_score,component_scores,selection_type,feature_snapshot,data_completeness,selection_reason,risk_warning)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(run_id,symbol) DO UPDATE SET
                  name=excluded.name,rank=excluded.rank,sector=excluded.sector,recommended_price=excluded.recommended_price,
                  recommendation_close=excluded.recommendation_close,total_score=excluded.total_score,
                  component_scores=excluded.component_scores,selection_type=excluded.selection_type,
                  feature_snapshot=excluded.feature_snapshot,data_completeness=excluded.data_completeness,
                  selection_reason=excluded.selection_reason,risk_warning=excluded.risk_warning
                """, (run_id, str(item["symbol"]).zfill(6), item.get("name"), item["rank"], item.get("sector"),
                       item.get("recommended_price"), item.get("recommendation_close"), item.get("total_score"),
                       self._json(item.get("component_scores", {})), item.get("selection_type"),
                       self._json(item.get("feature_snapshot", {})), item.get("data_completeness"),
                       item.get("selection_reason"), item.get("risk_warning")))
            return run_id

    def pending_recommendations(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("""
              SELECT r.*, rr.review_status, rr.review_trade_date
              FROM recommendations r JOIN recommendation_runs run ON run.id=r.run_id
              LEFT JOIN review_results rr ON rr.recommendation_id=r.id
              WHERE rr.id IS NULL OR rr.review_status IN ('等待复盘','待补录')
              ORDER BY run.recommendation_date,r.rank LIMIT ?
            """,(limit,)).fetchall()
            result=[]
            for row in rows:
                item=dict(row)
                run=db.execute("SELECT * FROM recommendation_runs WHERE id=?",(item["run_id"],)).fetchone()
                item.update({f"run_{k}":v for k,v in dict(run).items()})
                item["feature_snapshot"]=json.loads(item["feature_snapshot"])
                item["component_scores"]=json.loads(item["component_scores"])
                result.append(item)
            return result

    def pending_count(self) -> int:
        with self._connect() as db:
            return int(db.execute("""
              SELECT COUNT(*) FROM recommendations r
              LEFT JOIN review_results rr ON rr.recommendation_id=r.id
              WHERE rr.id IS NULL OR rr.review_status IN ('等待复盘','待补录')
            """).fetchone()[0])

    def upsert_review(self, recommendation_id: int, values: dict[str, Any]) -> None:
        allowed = ["review_trade_date","open_price","high_price","low_price","close_price","volume","amount",
                   "open_return","high_return","low_return","close_return","simulated_return","mfe","mae",
                   "opened_up","gap_up_fade","gap_down_recover","limit_up","limit_down","take_profit_hit",
                   "stop_loss_hit","open_success","close_success","trade_success","conclusion","review_status",
                   "error_reason","reviewed_at"]
        payload={key:values.get(key) for key in allowed}
        columns=["recommendation_id",*allowed]
        updates=",".join(f"{key}=excluded.{key}" for key in allowed)
        with self._connect() as db:
            db.execute(
                f"INSERT INTO review_results({','.join(columns)}) VALUES({','.join('?' for _ in columns)}) "
                f"ON CONFLICT(recommendation_id) DO UPDATE SET {updates}",
                [recommendation_id,*[payload[key] for key in allowed]],
            )

    def review_frame(self, start: str | None = None, end: str | None = None) -> pd.DataFrame:
        query="""SELECT run.recommendation_date,run.generated_at,run.strategy_version,run.market_state,run.data_source,
          r.id AS recommendation_id,r.symbol,r.name,r.rank,r.sector,r.recommended_price,r.recommendation_close,
          r.total_score,r.component_scores,r.selection_type,r.feature_snapshot,r.data_completeness,
          r.selection_reason,r.risk_warning,rr.review_trade_date,rr.open_price,rr.high_price,rr.low_price,
          rr.close_price,rr.volume,rr.amount,rr.open_return,rr.high_return,rr.low_return,rr.close_return,
          rr.simulated_return,rr.mfe,rr.mae,rr.opened_up,rr.gap_up_fade,rr.gap_down_recover,
          rr.limit_up,rr.limit_down,rr.take_profit_hit,rr.stop_loss_hit,rr.open_success,rr.close_success,
          rr.trade_success,rr.conclusion,COALESCE(rr.review_status,'等待复盘') AS review_status,
          rr.error_reason,rr.reviewed_at
          FROM recommendations r JOIN recommendation_runs run ON run.id=r.run_id
          LEFT JOIN review_results rr ON rr.recommendation_id=r.id WHERE 1=1"""
        params=[]
        if start: query += " AND run.recommendation_date>=?"; params.append(start)
        if end: query += " AND run.recommendation_date<=?"; params.append(end)
        query += " ORDER BY run.recommendation_date DESC,r.rank"
        with self._connect() as db:
            return pd.read_sql_query(query, db, params=params)

    def export_tables(self) -> dict[str, pd.DataFrame]:
        with self._connect() as db:
            return {name:pd.read_sql_query(f"SELECT * FROM {name}",db) for name in
                    ("recommendation_runs","recommendations","review_results","strategy_versions")}

    def backup_bytes(self) -> bytes:
        with self._connect() as db:
            return db.serialize()

    def restore_bytes(self, payload: bytes) -> None:
        incoming=sqlite3.connect(":memory:")
        try:
            incoming.deserialize(payload)
            tables={row[0] for row in incoming.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            required={"recommendation_runs","recommendations","review_results","strategy_versions"}
            if not required.issubset(tables):
                raise ValueError("备份文件缺少复盘数据库表")
            with self._connect() as target:
                incoming.backup(target)
        finally:
            incoming.close()
