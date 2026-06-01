"""Audit Sink: 本地紧凑审计落盘（L1，三省六部黑匣子 / 飞行记录仪）.

与 ObsidianSink 的关键差别：不再 1-event-1-file，而是 1-day-1-file 追加，
把上万个 inode 压成「天数级」。同源全量、本地可靠、inode 友好。

布局:
    <audit_dir>/YYYY-MM-DD.jsonl       当天热写（O_APPEND + fsync）
    <audit_dir>/YYYY-MM-DD.jsonl.gz    过日封口后 gzip（flush_pending 滚动）

可靠性:
    - write 走 O_APPEND + fsync，崩溃安全（同 pending.py / dlq.py 风格）。
    - at-least-once：冷启重读可能产生重复行；下游按 event_id 去重。
    - flush_pending() 把「已封口的过去某天」gzip 成 .jsonl.gz 并删原件，幂等。
      daemon.tick 已有通用 flush_pending hook，自动每 tick 调用一次。

无 vault 依赖、无网络依赖——这是 Supermemory 不可信时的本地权威兜底。
"""
from __future__ import annotations

import gzip
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

from ..core import Event, Sink
from ..paths import audit_dir


def _bucket_for(ts: str) -> str:
    """从 ISO timestamp 取 YYYY-MM-DD 日期桶；无法解析 → 'unknown'."""
    if len(ts) >= 10 and ts[4] == "-" and ts[7] == "-":
        return ts[0:10]
    return "unknown"


def _record(evt: Event) -> dict:
    """紧凑审计记录：一行一事件，保留回放所需的全部字段."""
    rec: dict = {
        "event_id": evt.event_id,
        "event_type": evt.event_type or "unknown",
        "profile": evt.profile,
        "timestamp": evt.timestamp,
    }
    if evt.task_id:
        rec["task_id"] = evt.task_id
    if evt.content:
        rec["content"] = evt.content
    if evt.source:
        rec["_source"] = evt.source
    return rec


class AuditSink(Sink):
    name = "audit"

    def write(self, evt: Event) -> None:
        d = audit_dir()
        d.mkdir(parents=True, exist_ok=True)
        target = d / f"{_bucket_for(evt.timestamp)}.jsonl"
        line = json.dumps(_record(evt), ensure_ascii=False) + "\n"
        fd = os.open(str(target),
                     os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            os.write(fd, line.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)

    # ── 日终滚动（gzip-ready）──────────────────────────────────

    def flush_pending(self) -> int:
        """daemon.tick 通用 hook：滚动压缩已封口的过去某天。返回压缩文件数."""
        return self.roll_and_compress()

    def roll_and_compress(self, today: str | None = None) -> int:
        """把 < today 的 YYYY-MM-DD.jsonl gzip 成 .jsonl.gz 并删原件。

        - 只动「过去已封口的天」，今天/未来/unknown 不碰。
        - 幂等：若目标 .gz 已存在（疑似上次半成品），跳过留人工，避免重复/丢数。
        - today 默认取 UTC 当天，便于测试注入。
        """
        d = audit_dir()
        if not d.is_dir():
            return 0
        if today is None:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        compressed = 0
        for jf in sorted(d.glob("*.jsonl")):
            day = jf.stem  # "YYYY-MM-DD" 或 "unknown"
            if day == "unknown" or day >= today:
                continue
            gz = jf.with_suffix(".jsonl.gz")
            if gz.exists():
                continue  # 已压缩 / 半成品，留待人工
            tmp = gz.with_suffix(".gz.tmp")
            with open(jf, "rb") as src, gzip.open(tmp, "wb") as dst:
                shutil.copyfileobj(src, dst)
                dst.flush()
            os.replace(tmp, gz)
            os.remove(jf)
            compressed += 1
        return compressed
