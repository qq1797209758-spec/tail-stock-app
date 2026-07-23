"""扫描阶段计时、计数和总预算控制。"""

from dataclasses import asdict, dataclass
from datetime import datetime
from time import monotonic
from zoneinfo import ZoneInfo


@dataclass
class StageMetric:
    stage: str
    started_at: str
    ended_at: str = ""
    duration_seconds: float = 0.0
    input_count: int = 0
    output_count: int = 0
    request_count: int = 0
    success_count: int = 0
    failure_count: int = 0
    cache_hit_count: int = 0
    retry_count: int = 0


class ScanPerformance:
    def __init__(self, hard_budget_seconds: float = 90.0):
        self.started = monotonic()
        self.deadline = self.started + hard_budget_seconds
        self.metrics: list[StageMetric] = []

    def remaining(self) -> float:
        return max(0.0, self.deadline - monotonic())

    def expired(self, reserve: float = 0.0) -> bool:
        return self.remaining() <= reserve

    def begin(self, stage: str, input_count: int = 0):
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        metric = StageMetric(
            stage=stage,
            started_at=now.strftime("%H:%M:%S.%f")[:-3],
            input_count=input_count,
        )
        self.metrics.append(metric)
        return metric, monotonic()

    def end(self, token, output_count: int = 0, **counts) -> None:
        metric, start = token
        metric.ended_at = datetime.now(ZoneInfo("Asia/Shanghai")).strftime(
            "%H:%M:%S.%f"
        )[:-3]
        metric.duration_seconds = round(monotonic() - start, 3)
        metric.output_count = output_count
        for key, value in counts.items():
            if hasattr(metric, key):
                setattr(metric, key, int(value))

    def records(self) -> list[dict]:
        return [asdict(metric) for metric in self.metrics]

    def slowest(self) -> str:
        return (
            max(self.metrics, key=lambda item: item.duration_seconds).stage
            if self.metrics
            else "--"
        )
