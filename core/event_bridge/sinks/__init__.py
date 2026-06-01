"""Sink 实现:
- Audit       (本地紧凑权威 L1, 按日 JSONL/gzip, inode 友好)
- Obsidian    (人类可读 ADR, 逐事件 md, 受 Phase 0 开关控制)
- Supermemory (长期记忆后端, best-effort).
"""
