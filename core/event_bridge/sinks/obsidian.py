"""Obsidian Sink: Markdown ADR / 事件日志写入 vault.

布局: <vault>/88_event-bridge/YYYY/MM/DD/<event_id>.md
幂等: target.exists() → skip.

Phase 0 开关: 环境变量 EVENT_BRIDGE_OBSIDIAN_PER_EVENT 控制是否逐事件写 md.
  - 未设置 / "1" / "true" / "on" / "yes" → 写（默认，保持历史行为）
  - "0" / "false" / "off" / "no"        → 不写（write() 空转）
迁出 vault 时只需把开关置 0，无需改架构。本地紧凑权威由 AuditSink 承担。
"""
from __future__ import annotations

import json
import os

from ..core import Event, Sink
from ..paths import obsidian_event_dir


def per_event_enabled() -> bool:
    """逐事件 markdown 写入是否启用（默认 True，保持向后兼容）."""
    val = os.environ.get("EVENT_BRIDGE_OBSIDIAN_PER_EVENT")
    if val is None:
        return True
    return val.strip().lower() not in ("0", "false", "off", "no")


class ObsidianSink(Sink):
    name = "obsidian"

    def write(self, evt: Event) -> None:
        if not per_event_enabled():
            return  # Phase 0: 开关关闭则不再往 vault 写逐事件 md
        ts = evt.timestamp
        if len(ts) >= 10 and ts[4] == "-" and ts[7] == "-":
            year, month, day = ts[0:4], ts[5:7], ts[8:10]
        else:
            year, month, day = "unknown", "unknown", "unknown"
        target_dir = obsidian_event_dir() / year / month / day
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{evt.event_id or 'noid'}.md"
        if target.exists():
            return  # 幂等
        target.write_text(_render(evt), encoding="utf-8")


def _render(evt: Event) -> str:
    et = evt.event_type or "unknown"
    fm = [
        "---",
        f"event_id: {evt.event_id}",
        f"event_type: {et}",
        f"profile: {evt.profile}",
        f"timestamp: {evt.timestamp}",
    ]
    if evt.task_id:
        fm.append(f"task_id: {evt.task_id}")
    fm.append("---")
    body = [
        "",
        f"# {et.upper()} — {evt.timestamp}",
        "",
        "## Content",
        "",
        "```json",
        json.dumps(evt.content, ensure_ascii=False, indent=2),
        "```",
        "",
    ]
    return "\n".join(fm + body) + "\n"
