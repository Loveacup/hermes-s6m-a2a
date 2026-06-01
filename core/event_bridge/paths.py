"""路径解析 — 所有 dirs 都允许 ENV 覆盖以便测试."""
from __future__ import annotations

import os
from pathlib import Path


def hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes"))


def obsidian_vault() -> Path:
    return Path(os.environ.get("OBSIDIAN_VAULT")
                or os.path.expanduser("~/Documents/Obsidian/AlexCai"))


def event_bridge_home() -> Path:
    explicit = os.environ.get("EVENT_BRIDGE_HOME")
    return Path(explicit) if explicit else hermes_home() / "event-bridge"


def cursors_dir() -> Path:
    return event_bridge_home() / "cursors"


def audit_dir() -> Path:
    """L1 本地紧凑审计归档目录（按日 JSONL / gzip）。ENV 可覆盖。"""
    explicit = os.environ.get("EVENT_BRIDGE_AUDIT_DIR")
    return Path(explicit) if explicit else event_bridge_home() / "audit"


def rollup_dir() -> Path:
    """L1.5 滚动汇总目录（按月聚合）。ENV 可覆盖。"""
    explicit = os.environ.get("EVENT_BRIDGE_ROLLUP_DIR")
    return Path(explicit) if explicit else event_bridge_home() / "rollup"


def obsidian_event_dir() -> Path:
    return obsidian_vault() / "88_event-bridge"


def jsonl_paths_for_all_profiles() -> list[Path]:
    """扫描 ~/.hermes/profiles/<name>/empire-thread.jsonl，按名字升序."""
    profiles_dir = hermes_home() / "profiles"
    if not profiles_dir.is_dir():
        return []
    out: list[Path] = []
    for p in sorted(profiles_dir.iterdir()):
        if not p.is_dir():
            continue
        j = p / "empire-thread.jsonl"
        if j.exists():
            out.append(j)
    return out
