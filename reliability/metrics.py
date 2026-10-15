"""Reliability metrics collector, analysis engine, and report exporter for OFFERHUNT (Milestone M7).

Computes rigorous reliability metrics from controlled scenario executions:
- Verified Success Rate: numerator / denominator (percentage)
- Duplicate Rate: duplicate rows detected / total write runs
- Recovery Success Rate: successful recoveries / total faults injected
- False Completion Rate: unverified runs claiming completion / total completed runs
- Approval Bypass Count: actions executed despite rejection or lack of approval (must be 0)
- Average latency and tool calls

Rules:
- NEVER display invented or simulated numbers. Every metric derives from recorded runs.
- Always provide numerator, denominator, and total run count.
- Support exporting to CSV and JSON formats.
"""

from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Union


@dataclass
class ScenarioRunMetric:
    """Detailed telemetry record from an individual scenario execution."""
    run_id: str
    scenario_id: str
    scenario_code: str
    scenario_title: str
    injected_fault: str
    fault_detected: bool
    recovery_invoked: bool
    recovery_successful: bool
    duplicate_produced: bool
    false_completion: bool
    approval_bypassed: bool
    latency_ms: float
    tool_calls: int
    final_status: str
    is_baseline: bool = False
    timestamp_utc: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


@dataclass
class RatioMetric:
    """Exact metric reporting numerator, denominator, and computed percentage."""
    numerator: int
    denominator: int
    percentage: float
    label: str

    @classmethod
    def create(cls, numerator: int, denominator: int, label: str) -> RatioMetric:
        pct = (numerator / denominator * 100.0) if denominator > 0 else 0.0
        return cls(
            numerator=numerator,
            denominator=denominator,
            percentage=round(pct, 2),
            label=label,
        )

    def display(self) -> str:
        """Formatted representation: e.g. '5/5 (100.0%)'."""
        return f"{self.numerator}/{self.denominator} ({self.percentage:.1f}%)"


@dataclass
class MetricsAggregateSummary:
    """Aggregate reliability summary for a collection of scenario runs."""
    total_runs: int
    verified_success: RatioMetric
    recovery_success: RatioMetric
    duplicate_rate: RatioMetric
    false_completion_rate: RatioMetric
    approval_bypass_count: int
    average_latency_ms: float
    average_tool_calls: float
    runs: List[ScenarioRunMetric] = field(default_factory=list)


class ReliabilityMetricsCollector:
    """Collects individual scenario run metrics, aggregates summaries, and exports data."""

    def __init__(self):
        self._records: List[ScenarioRunMetric] = []

    def record_run(self, metric: ScenarioRunMetric) -> None:
        """Record an individual scenario run metric."""
        self._records.append(metric)

    def clear(self) -> None:
        """Clear all in-memory records."""
        self._records.clear()

    @property
    def records(self) -> List[ScenarioRunMetric]:
        return list(self._records)

    def compute_summary(self, baseline_only: Optional[bool] = None) -> MetricsAggregateSummary:
        """Compute exact aggregate metrics from recorded runs.

        If baseline_only is True, filters to baseline runs.
        If baseline_only is False, filters to OFFERHUNT runs.
        If None, computes across all runs.
        """
        filtered = self._records
        if baseline_only is not None:
            filtered = [r for r in filtered if r.is_baseline == baseline_only]

        total_runs = len(filtered)
        if total_runs == 0:
            return MetricsAggregateSummary(
                total_runs=0,
                verified_success=RatioMetric.create(0, 0, "Verified Success Rate"),
                recovery_success=RatioMetric.create(0, 0, "Recovery Success Rate"),
                duplicate_rate=RatioMetric.create(0, 0, "Duplicate Rate"),
                false_completion_rate=RatioMetric.create(0, 0, "False Completion Rate"),
                approval_bypass_count=0,
                average_latency_ms=0.0,
                average_tool_calls=0.0,
                runs=[],
            )

        # Numerator & denominator calculations
        verified_count = sum(1 for r in filtered if r.final_status == "VERIFIED")
        faults_injected = sum(1 for r in filtered if r.fault_detected or r.recovery_invoked)
        recoveries_succeeded = sum(1 for r in filtered if r.recovery_successful)
        duplicates_count = sum(1 for r in filtered if r.duplicate_produced)
        false_completions = sum(1 for r in filtered if r.false_completion)
        approval_bypasses = sum(1 for r in filtered if r.approval_bypassed)

        total_latency = sum(r.latency_ms for r in filtered)
        total_tool_calls = sum(r.tool_calls for r in filtered)

        return MetricsAggregateSummary(
            total_runs=total_runs,
            verified_success=RatioMetric.create(verified_count, total_runs, "Verified Success Rate"),
            recovery_success=RatioMetric.create(
                recoveries_succeeded,
                faults_injected if faults_injected > 0 else total_runs,
                "Recovery Success Rate",
            ),
            duplicate_rate=RatioMetric.create(duplicates_count, total_runs, "Duplicate Rate"),
            false_completion_rate=RatioMetric.create(false_completions, total_runs, "False Completion Rate"),
            approval_bypass_count=approval_bypasses,
            average_latency_ms=round(total_latency / total_runs, 2),
            average_tool_calls=round(total_tool_calls / total_runs, 2),
            runs=filtered,
        )

    def export_to_json(self, file_path: Union[str, Path]) -> str:
        """Export all recorded runs and aggregate summary to a JSON file."""
        target = Path(file_path).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)

        summary = self.compute_summary()
        data = {
            "exported_at_utc": datetime.now(timezone.utc).isoformat(),
            "total_runs": summary.total_runs,
            "summary": {
                "verified_success": asdict(summary.verified_success),
                "recovery_success": asdict(summary.recovery_success),
                "duplicate_rate": asdict(summary.duplicate_rate),
                "false_completion_rate": asdict(summary.false_completion_rate),
                "approval_bypass_count": summary.approval_bypass_count,
                "average_latency_ms": summary.average_latency_ms,
                "average_tool_calls": summary.average_tool_calls,
            },
            "runs": [asdict(r) for r in self._records],
        }

        with open(target, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

        return str(target)

    def export_to_csv(self, file_path: Union[str, Path]) -> str:
        """Export all recorded runs to a CSV file."""
        target = Path(file_path).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)

        if not self._records:
            with open(target, "w", newline="", encoding="utf-8") as f:
                f.write("No runs recorded\n")
            return str(target)

        fieldnames = list(asdict(self._records[0]).keys())
        with open(target, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for r in self._records:
                writer.writerow(asdict(r))

        return str(target)
