"""Phase 0 + Phase 1: AuditSink (本地紧凑落盘) + Obsidian 开关 + rollup 基座.

验证点:
- A1  按日分桶: <audit_dir>/YYYY-MM-DD.jsonl, 一事件一行 (非一文件)
- A2  同日多事件追加进同一文件 (inode 友好)
- A3  无 timestamp → unknown.jsonl 兜底, 不崩
- A4  记录字段完整 (event_id/type/profile/ts/task_id/content)
- A5  accept 拒绝 _source=sink_writeback (继承防回路)
- A6  roll_and_compress: 过日 .jsonl → .jsonl.gz, 删原件, 内容可 zcat
- A7  roll_and_compress: 今天的文件不动
- A8  flush_pending == roll_and_compress (daemon hook)
- SW1 EVENT_BRIDGE_OBSIDIAN_PER_EVENT=0 → ObsidianSink.write 空转
- SW2 开关默认 (未设置) → 仍写
- DS1 default_sinks 始终含 AuditSink; 开关关时不含 ObsidianSink
- R1  summarize 纯聚合: 计数/失败/score 统计
- R2  rollup_day 读 .jsonl 与 .jsonl.gz 一致
"""
from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

from event_bridge.core import Event  # type: ignore
from event_bridge.sinks.audit import AuditSink  # type: ignore
from event_bridge.sinks.obsidian import ObsidianSink, per_event_enabled  # type: ignore
from event_bridge.sinks.supermemory import supermemory_enabled  # type: ignore
from event_bridge import rollup  # type: ignore


@pytest.fixture
def audit_env(tmp_path, monkeypatch):
    eb = tmp_path / "event-bridge"
    monkeypatch.setenv("EVENT_BRIDGE_HOME", str(eb))
    monkeypatch.delenv("EVENT_BRIDGE_AUDIT_DIR", raising=False)
    monkeypatch.delenv("EVENT_BRIDGE_ROLLUP_DIR", raising=False)
    return eb / "audit"


def _evt(event_id="ev1", event_type="execute", ts="2026-05-30T12:34:56Z",
         profile="regent", content=None, task_id="", source=""):
    raw = {
        "event_id": event_id,
        "event_type": event_type,
        "timestamp": ts,
        "content": content if content is not None else {"note": "hi"},
    }
    if task_id:
        raw["task_id"] = task_id
    if source:
        raw["_source"] = source
    return Event(raw=raw, profile=profile)


# ── A1: 按日分桶, 一行一事件 ───────────────────────────────────

def test_a_a1_daily_bucket_one_line(audit_env):
    AuditSink().write(_evt(event_id="abc", ts="2026-05-30T10:00:00Z"))
    f = audit_env / "2026-05-30.jsonl"
    assert f.exists()
    lines = f.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["event_id"] == "abc"


# ── A2: 同日多事件追加 (inode 友好: 仍是 1 文件) ───────────────

def test_a_a2_same_day_appends(audit_env):
    s = AuditSink()
    for i in range(5):
        s.write(_evt(event_id=f"e{i}", ts="2026-05-30T10:00:0%dZ" % i))
    files = list(audit_env.glob("*.jsonl"))
    assert len(files) == 1  # 5 事件 → 1 文件, 非 5 文件
    assert len(files[0].read_text(encoding="utf-8").splitlines()) == 5


# ── A3: 无 timestamp → unknown 兜底 ───────────────────────────

def test_a_a3_missing_ts_unknown(audit_env):
    AuditSink().write(_evt(event_id="nots", ts=""))
    assert (audit_env / "unknown.jsonl").exists()


# ── A4: 记录字段完整 ──────────────────────────────────────────

def test_a_a4_record_fields(audit_env):
    AuditSink().write(_evt(event_id="x1", event_type="dispatch",
                           ts="2026-05-30T08:00:00Z", profile="engineer",
                           task_id="t_abc", content={"cmd": "ls"}))
    rec = json.loads((audit_env / "2026-05-30.jsonl").read_text().splitlines()[0])
    assert rec["event_id"] == "x1"
    assert rec["event_type"] == "dispatch"
    assert rec["profile"] == "engineer"
    assert rec["timestamp"] == "2026-05-30T08:00:00Z"
    assert rec["task_id"] == "t_abc"
    assert rec["content"] == {"cmd": "ls"}


# ── A5: accept 拒绝 sink_writeback (防回路) ────────────────────

def test_a_a5_accept_rejects_writeback():
    s = AuditSink()
    assert s.accept(_evt(source="sink_writeback")) is False
    assert s.accept(_evt()) is True


# ── A6: roll_and_compress 过日 → gzip, 删原件, 内容可读 ────────

def test_a_a6_roll_compress_past_day(audit_env):
    s = AuditSink()
    s.write(_evt(event_id="old", ts="2026-05-29T10:00:00Z"))
    n = s.roll_and_compress(today="2026-05-31")
    assert n == 1
    assert not (audit_env / "2026-05-29.jsonl").exists()
    gz = audit_env / "2026-05-29.jsonl.gz"
    assert gz.exists()
    with gzip.open(gz, "rt", encoding="utf-8") as f:
        assert json.loads(f.read().splitlines()[0])["event_id"] == "old"


# ── A7: 今天的文件不压缩 ──────────────────────────────────────

def test_a_a7_today_not_compressed(audit_env):
    s = AuditSink()
    s.write(_evt(event_id="today", ts="2026-05-31T10:00:00Z"))
    assert s.roll_and_compress(today="2026-05-31") == 0
    assert (audit_env / "2026-05-31.jsonl").exists()
    assert not (audit_env / "2026-05-31.jsonl.gz").exists()


# ── A8: flush_pending == roll (daemon hook 契合) ───────────────

def test_a_a8_flush_pending_is_roll(audit_env, monkeypatch):
    s = AuditSink()
    s.write(_evt(event_id="o", ts="2020-01-01T00:00:00Z"))  # 远古日, 必压
    n = s.flush_pending()
    assert n == 1
    assert (audit_env / "2020-01-01.jsonl.gz").exists()


# ── SW1/SW2: Obsidian 开关 ────────────────────────────────────

def test_sw1_obsidian_switch_off(tmp_path, monkeypatch):
    monkeypatch.setenv("OBSIDIAN_VAULT", str(tmp_path / "vault"))
    monkeypatch.setenv("EVENT_BRIDGE_OBSIDIAN_PER_EVENT", "0")
    assert per_event_enabled() is False
    ObsidianSink().write(_evt(event_id="skip", ts="2026-05-30T10:00:00Z"))
    assert not (tmp_path / "vault" / "88_event-bridge").exists()


def test_sw2_obsidian_switch_default_on(tmp_path, monkeypatch):
    monkeypatch.setenv("OBSIDIAN_VAULT", str(tmp_path / "vault"))
    monkeypatch.delenv("EVENT_BRIDGE_OBSIDIAN_PER_EVENT", raising=False)
    assert per_event_enabled() is True
    ObsidianSink().write(_evt(event_id="keep", ts="2026-05-30T10:00:00Z"))
    assert (tmp_path / "vault" / "88_event-bridge" / "2026" / "05" / "30"
            / "keep.md").exists()


# ── SW3: Supermemory 开关 ─────────────────────────────────────

def test_sw3_supermemory_switch(monkeypatch):
    monkeypatch.delenv("EVENT_BRIDGE_SUPERMEMORY_ENABLED", raising=False)
    assert supermemory_enabled() is True  # 默认保持现状
    monkeypatch.setenv("EVENT_BRIDGE_SUPERMEMORY_ENABLED", "0")
    assert supermemory_enabled() is False
    monkeypatch.setenv("EVENT_BRIDGE_SUPERMEMORY_ENABLED", "off")
    assert supermemory_enabled() is False


# ── DS1: default_sinks 接线 ───────────────────────────────────

def test_ds1_default_sinks_wiring(monkeypatch):
    from event_bridge import daemon  # type: ignore
    monkeypatch.delenv("SUPERMEMORY_API_KEY", raising=False)

    monkeypatch.delenv("EVENT_BRIDGE_OBSIDIAN_PER_EVENT", raising=False)
    names_on = [s.name for s in daemon.default_sinks()]
    assert "audit" in names_on and "obsidian" in names_on

    monkeypatch.setenv("EVENT_BRIDGE_OBSIDIAN_PER_EVENT", "0")
    names_off = [s.name for s in daemon.default_sinks()]
    assert "audit" in names_off and "obsidian" not in names_off


# ── DS2: default_sinks Supermemory 开关门控 ───────────────────

def test_ds2_default_sinks_supermemory_gate(tmp_path, monkeypatch):
    from event_bridge import daemon  # type: ignore
    # 隔离 HERMES_HOME，避免读真实 supermemory.json
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "h"))
    monkeypatch.setenv("SUPERMEMORY_API_KEY", "fake-key-for-test")
    monkeypatch.delenv("EVENT_BRIDGE_OBSIDIAN_PER_EVENT", raising=False)

    # 开关默认开 + key 在 → 含 supermemory
    monkeypatch.delenv("EVENT_BRIDGE_SUPERMEMORY_ENABLED", raising=False)
    assert "supermemory" in [s.name for s in daemon.default_sinks()]

    # 开关关 → 不含 supermemory（即便 key 在）；audit 仍在
    monkeypatch.setenv("EVENT_BRIDGE_SUPERMEMORY_ENABLED", "0")
    names = [s.name for s in daemon.default_sinks()]
    assert "supermemory" not in names
    assert "audit" in names


# ── R1: summarize 纯聚合 ──────────────────────────────────────

def test_r1_summarize():
    recs = [
        {"profile": "regent", "event_type": "execute",
         "content": {"audit_score": {"overall": 1.0}}},
        {"profile": "regent", "event_type": "task_failed", "content": {}},
        {"profile": "engineer", "event_type": "execute",
         "content": {"audit_score": {"overall": 0.5}}},
    ]
    s = rollup.summarize(recs)
    assert s["total"] == 3
    assert s["by_profile"] == {"regent": 2, "engineer": 1}
    assert s["failures"] == 1  # task_failed
    assert s["audit_score"]["count"] == 2
    assert s["audit_score"]["min"] == 0.5
    assert s["audit_score"]["max"] == 1.0


# ── R2: rollup_day 读 jsonl 与 gz 一致 ─────────────────────────

def test_r2_rollup_day_jsonl_and_gz(tmp_path):
    recs = [{"profile": "regent", "event_type": "execute", "content": {}},
            {"profile": "regent", "event_type": "execute", "content": {}}]
    plain = tmp_path / "2026-05-30.jsonl"
    plain.write_text("\n".join(json.dumps(r) for r in recs) + "\n",
                     encoding="utf-8")
    gz = tmp_path / "2026-05-30b.jsonl.gz"
    with gzip.open(gz, "wt", encoding="utf-8") as f:
        f.write("\n".join(json.dumps(r) for r in recs) + "\n")

    s_plain = rollup.rollup_day(plain)
    s_gz = rollup.rollup_day(gz)
    assert s_plain["total"] == s_gz["total"] == 2
    assert s_plain["date"] == "2026-05-30"
