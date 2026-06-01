"""launchd sidecar 入口.

KeepAlive=true，每 N 秒一个 tick，每次发现并 dispatch 全 profile JSONL.
W2 后接入 fsevents/kqueue 加速；当前用纯轮询.

Tick 内做两件事：
1. dispatch_all: empire-thread.jsonl → sink.write (Obsidian 直写、Supermemory 直发)
2. flush_pending: 凡是 Sink 暴露 flush_pending() 都调用一次（保留 hook，便于未来扩展）
"""
from __future__ import annotations

import argparse
import logging
import os
import time
from typing import Iterable

from .core import Sink, dispatch_all
from .paths import jsonl_paths_for_all_profiles
from .sinks.audit import AuditSink
from .sinks.obsidian import ObsidianSink, per_event_enabled
from .sinks.supermemory import SupermemorySink, supermemory_enabled

log = logging.getLogger("event_bridge.daemon")


def default_sinks() -> list[Sink]:
    # AuditSink: 本地紧凑权威层（L1），inode 友好，始终启用
    sinks: list[Sink] = [AuditSink()]
    # ObsidianSink: 逐事件 md，受 Phase 0 开关控制（默认开，兼容历史）
    if per_event_enabled():
        sinks.append(ObsidianSink())
    else:
        log.info("EVENT_BRIDGE_OBSIDIAN_PER_EVENT 关闭，跳过 ObsidianSink 逐事件写入")
    # SupermemorySink: 受开关 + API key 双重控制
    if not supermemory_enabled():
        log.info("EVENT_BRIDGE_SUPERMEMORY_ENABLED 关闭，跳过 SupermemorySink")
    elif os.environ.get("SUPERMEMORY_API_KEY"):
        sinks.append(SupermemorySink())
    else:
        log.info("SUPERMEMORY_API_KEY 未设置，跳过 SupermemorySink")
    return sinks


def tick(sinks: Iterable[Sink]) -> dict[str, int]:
    sinks_list = list(sinks)
    counts = dispatch_all(sinks_list, jsonl_paths_for_all_profiles())
    for s in sinks_list:
        flush = getattr(s, "flush_pending", None)
        if callable(flush):
            sent = flush()
            if sent:
                counts[f"{s.name}/flush"] = sent
    return counts


def run(poll_interval: float = 1.0) -> None:
    sinks = default_sinks()
    while True:
        counts = tick(sinks)
        if any(counts.values()):
            log.info("dispatch: %s", counts)
        time.sleep(poll_interval)


def main() -> None:
    p = argparse.ArgumentParser(prog="event_bridge.daemon")
    p.add_argument("--interval", type=float, default=1.0,
                   help="poll interval seconds")
    p.add_argument("--once", action="store_true",
                   help="run one tick then exit")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    if args.once:
        print(tick(default_sinks()))
    else:
        run(args.interval)


if __name__ == "__main__":
    main()
