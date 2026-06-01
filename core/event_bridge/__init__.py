"""EmpireThread v2 事件桥 — Audit + Obsidian + Supermemory 多 Sink dispatcher.

Sink 分层:
- Audit       本地紧凑权威 (L1, 按日 JSONL/gzip, inode 友好)
- Obsidian    人类可读 ADR (逐事件 md, 受 EVENT_BRIDGE_OBSIDIAN_PER_EVENT 开关控制)
- Supermemory 长期记忆后端 (best-effort)

设计文档: s6m-config/docs/EmpireThread_事件桥_v2_缩窄版.md
安置方案: Obsidian 02-Plan&CQI/88_event-bridge-审计日志安置方案.md
"""
from .core import Event, Sink, consume_for, dispatch_all
from .cursor import Cursor, CursorStore

__all__ = ["Event", "Sink", "Cursor", "CursorStore",
           "consume_for", "dispatch_all"]
