"""Rollup: 审计事件按日/类聚合的纯函数基座（L1.5）.

定位：趋势/审计档需要的是聚合信号，不是全量逐事件。有了 rollup，
原始事件可在保留期后安全丢弃，趋势信号永久留存——这是「长期可审计」
与「inode 可控」不冲突的支点。

本模块当前只提供：
  - summarize(records)        纯聚合函数（无副作用，易测）
  - rollup_day(day_file)      读一个 audit 日文件 → 聚合摘要
  - write_daily_rollup(...)   把摘要 append 到 rollup/YYYY-MM.jsonl

**刻意不接入 daemon 热路径**（低风险基座）。何时/如何调度由后续阶段决定。
"""
from __future__ import annotations

import gzip
import json
from collections import Counter
from pathlib import Path
from typing import Iterable

from .paths import rollup_dir


def summarize(records: Iterable[dict]) -> dict:
    """把一批审计记录聚合成一条 rollup 摘要.

    统计：总数、按 profile 计数、按 event_type 计数、失败数、
    audit_score.overall 的 count/min/max/avg（若有）.
    """
    total = 0
    by_profile: Counter = Counter()
    by_type: Counter = Counter()
    failures = 0
    scores: list[float] = []
    for r in records:
        total += 1
        by_profile[r.get("profile", "unknown")] += 1
        et = r.get("event_type", "unknown") or "unknown"
        by_type[et] += 1
        if "fail" in et or "error" in et:
            failures += 1
        content = r.get("content") or {}
        if isinstance(content, dict):
            score = content.get("audit_score")
            if isinstance(score, dict):
                ov = score.get("overall")
                if isinstance(ov, (int, float)) and not isinstance(ov, bool):
                    scores.append(float(ov))
    summary: dict = {
        "total": total,
        "by_profile": dict(by_profile),
        "by_event_type": dict(by_type),
        "failures": failures,
    }
    if scores:
        summary["audit_score"] = {
            "count": len(scores),
            "min": min(scores),
            "max": max(scores),
            "avg": round(sum(scores) / len(scores), 4),
        }
    return summary


def _iter_day_records(path: Path) -> Iterable[dict]:
    opener = gzip.open if path.name.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as f:  # type: ignore[operator]
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def rollup_day(day_file: Path) -> dict:
    """读一个 audit 日文件（.jsonl 或 .jsonl.gz）→ 含 date 的聚合摘要."""
    day = day_file.name.split(".")[0]
    summary = summarize(_iter_day_records(day_file))
    summary["date"] = day
    return summary


def write_daily_rollup(day_file: Path, out_dir: Path | None = None) -> dict:
    """聚合 day_file → append 到 rollup/YYYY-MM.jsonl，返回摘要.

    幂等性：append 语义，重复调用会追加重复摘要；下游按 date 取最新即可。
    """
    summary = rollup_day(day_file)
    out_dir = out_dir or rollup_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    month = summary["date"][:7] if len(summary["date"]) >= 7 else "unknown"
    target = out_dir / f"{month}.jsonl"
    with open(target, "a", encoding="utf-8") as f:
        f.write(json.dumps(summary, ensure_ascii=False) + "\n")
    return summary
