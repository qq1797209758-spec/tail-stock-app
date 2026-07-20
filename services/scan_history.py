"""扫描历史持久化接口与 SQLite 临时实现。"""

from abc import ABC, abstractmethod
from contextlib import contextmanager
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import sqlite3
from typing import Any

import pandas as pd


@dataclass(frozen=True)
class ScanRecord:
    scan_id: str
    started_at: str
    completed_at: str
    scan_date: str
    duration_seconds: float
    status: str
    data_updated_at: str
    data_source_status: str
    strategy_parameters: dict[str, Any]
    counts: dict[str, int]
    interface_health: dict[str, Any]
    interface_errors: list[str]
    initial_results: list[dict[str, Any]]
    final_top5: list[dict[str, Any]]
    excluded_results: list[dict[str, Any]]
    missing_records: list[dict[str, Any]]


class ScanHistoryRepository(ABC):
    """历史存储抽象；后续 Supabase 实现保持同一契约。"""

    @abstractmethod
    def save_scan(self, record: ScanRecord) -> None: ...

    @abstractmethod
    def list_scans(self, scan_date: str | None = None) -> list[dict[str, Any]]: ...

    @abstractmethod
    def get_scan(self, scan_id: str) -> dict[str, Any] | None: ...

    @abstractmethod
    def candidate_counts_by_date(self) -> pd.DataFrame: ...

    @abstractmethod
    def stock_selection_counts(self) -> pd.DataFrame: ...


class SQLiteScanHistoryRepository(ScanHistoryRepository):
    """适合本地和开发测试的临时 SQLite 存储。"""

    JSON_FIELDS = (
        "strategy_parameters", "counts", "interface_health", "interface_errors",
        "initial_results", "final_top5", "excluded_results", "missing_records",
    )

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS scan_history (
                    scan_id TEXT PRIMARY KEY,
                    started_at TEXT NOT NULL,
                    completed_at TEXT NOT NULL,
                    scan_date TEXT NOT NULL,
                    duration_seconds REAL NOT NULL,
                    status TEXT NOT NULL,
                    data_updated_at TEXT NOT NULL,
                    data_source_status TEXT NOT NULL,
                    strategy_parameters TEXT NOT NULL,
                    counts TEXT NOT NULL,
                    interface_health TEXT NOT NULL,
                    interface_errors TEXT NOT NULL,
                    initial_results TEXT NOT NULL,
                    final_top5 TEXT NOT NULL,
                    excluded_results TEXT NOT NULL,
                    missing_records TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_scan_history_date ON scan_history(scan_date, started_at DESC)"
            )

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, allow_nan=False, default=str)

    @classmethod
    def _decode(cls, row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        for field in cls.JSON_FIELDS:
            result[field] = json.loads(result[field])
        return result

    def save_scan(self, record: ScanRecord) -> None:
        values = asdict(record)
        for field in self.JSON_FIELDS:
            values[field] = self._json(values[field])
        columns = list(values)
        placeholders = ",".join("?" for _ in columns)
        updates = ",".join(f"{column}=excluded.{column}" for column in columns if column != "scan_id")
        with self._connect() as connection:
            connection.execute(
                f"INSERT INTO scan_history ({','.join(columns)}) VALUES ({placeholders}) "
                f"ON CONFLICT(scan_id) DO UPDATE SET {updates}",
                [values[column] for column in columns],
            )

    def list_scans(self, scan_date: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM scan_history"
        parameters: list[Any] = []
        if scan_date:
            query += " WHERE scan_date = ?"
            parameters.append(scan_date)
        query += " ORDER BY started_at DESC LIMIT 500"
        with self._connect() as connection:
            return [self._decode(row) for row in connection.execute(query, parameters)]

    def get_scan(self, scan_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM scan_history WHERE scan_id = ?", (scan_id,)
            ).fetchone()
        return self._decode(row) if row else None

    def candidate_counts_by_date(self) -> pd.DataFrame:
        records = self.list_scans()
        if not records:
            return pd.DataFrame(columns=["扫描日期", "扫描次数", "初筛数量", "最终候选数量"])
        frame = pd.DataFrame(
            [{"扫描日期": row["scan_date"], "开始时间": row["started_at"], "初筛数量": row["counts"].get("initial", 0), "最终候选数量": row["counts"].get("final", 0)} for row in records]
        )
        scan_counts = frame.groupby("扫描日期").size().rename("扫描次数")
        latest = frame.sort_values("开始时间", ascending=False).drop_duplicates("扫描日期")
        latest = latest.set_index("扫描日期")[["初筛数量", "最终候选数量"]]
        return latest.join(scan_counts).reset_index()[
            ["扫描日期", "扫描次数", "初筛数量", "最终候选数量"]
        ].sort_values("扫描日期")

    def stock_selection_counts(self) -> pd.DataFrame:
        counts: dict[tuple[str, str], int] = {}
        for record in self.list_scans():
            for stock in record["final_top5"]:
                key = (str(stock.get("代码", "")).zfill(6), str(stock.get("名称", "")))
                counts[key] = counts.get(key, 0) + 1
        rows = [{"代码": code, "名称": name, "历史入选次数": count} for (code, name), count in counts.items()]
        return pd.DataFrame(rows, columns=["代码", "名称", "历史入选次数"]).sort_values(
            "历史入选次数", ascending=False, ignore_index=True
        ) if rows else pd.DataFrame(columns=["代码", "名称", "历史入选次数"])


class SupabaseScanHistoryRepository(ScanHistoryRepository):
    """预留的云端存储适配器；后续可以不改页面业务流即替换。"""

    def _pending(self, *args, **kwargs):
        raise NotImplementedError("尚未配置 Supabase 历史存储")

    save_scan = list_scans = get_scan = candidate_counts_by_date = stock_selection_counts = _pending


def dataframe_records(data: pd.DataFrame) -> list[dict[str, Any]]:
    """把 DataFrame 转换为可安全写入 JSON 的记录。"""
    if data.empty:
        return []
    normalized = data.copy().astype(object).where(pd.notna(data), None)
    return json.loads(normalized.to_json(orient="records", force_ascii=False, date_format="iso"))
