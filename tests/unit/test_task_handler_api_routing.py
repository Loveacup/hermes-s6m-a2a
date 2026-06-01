"""T2 RED: task_handler.py — API Server 路由全面铺开 + fallback.

新规约:
- 所有 profile（不再有"白名单"）尝试 API Server
- API Server 端口来自 port_resolver.api_server_port(profile)
- 任何 profile 的 API Server 不可达 → 透明 fallback 到 subprocess
"""
from __future__ import annotations

import json
import urllib.error
from unittest.mock import patch, MagicMock

import pytest

# P0-2 GREEN phase expectations: after engineer adds isinstance guard in
# handle_task, these tests should pass.  Currently fail because the guard
# does not exist yet (RED phase captured by
# TestVuln02_MessageFieldTypeConfusion in test_task_handler_vulnerabilities.py).
_XFAIL_MSG_TYPE = "P0-2: message field type confusion — needs isinstance guard in handle_task"


# ── R8: 所有 profile 走 API Server 路径（无白名单） ───────────

def test_th_t1_no_hardcoded_whitelist():
    """task_handler 不应再持有 _API_SERVER_PORTS 字典."""
    import task_handler
    assert not hasattr(task_handler, "_API_SERVER_PORTS"), \
        "硬编码白名单 _API_SERVER_PORTS 应已移除"


def test_th_t2_port_resolved_dynamically():
    """task_handler 应通过 port_resolver 模块取端口."""
    import task_handler
    from port_resolver import api_server_port
    # 暴露一个统一的查询函数（便于其他模块共用）
    assert hasattr(task_handler, "_api_server_port"), \
        "task_handler 应暴露 _api_server_port(profile) helper"
    assert task_handler._api_server_port("engineer") == api_server_port("engineer")


# ── R9: 连接错误 → subprocess fallback（任意 profile） ────────

def test_th_t3_connect_error_falls_back_to_subprocess(monkeypatch):
    import task_handler

    # 阻断真实 hermes CLI 启动
    fake_completed = MagicMock(returncode=0,
                               stdout="ok",
                               stderr="")

    def fake_run(*args, **kwargs):
        return fake_completed

    monkeypatch.setattr(task_handler.subprocess, "run", fake_run)

    # 强制 urlopen 抛 URLError
    def fake_urlopen(*args, **kwargs):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(task_handler.urllib.request, "urlopen", fake_urlopen)

    task = {"id": "t1", "summary": "test", "description": "x"}
    result = task_handler._via_api_server(task, "t1", "do thing", "engineer")
    # subprocess fallback 路径会把 stdout 放进 artifact
    assert result.get("artifact", {}).get("mode") == "subprocess"


# ── R10: API Server 成功路径不走 subprocess ──────────────────

def test_th_t4_api_server_success_path(monkeypatch):
    import task_handler

    sequence = iter([
        # 1) POST /v1/runs → run_id
        json.dumps({"run_id": "r1"}).encode(),
        # 2) GET /v1/runs/r1 → completed
        json.dumps({"status": "completed",
                    "output": "done"}).encode(),
    ])

    class FakeResp:
        def __init__(self, body): self.body = body
        def read(self): return self.body
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, *args, **kwargs):
        return FakeResp(next(sequence))

    monkeypatch.setattr(task_handler.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(task_handler.time, "sleep", lambda s: None)

    task = {"id": "t1"}
    result = task_handler._via_api_server(task, "t1", "say hi", "engineer")
    assert result["status"] == "completed"
    assert result["artifact"]["mode"] == "api_server"


# ── P0-13: 输入校验缺失 ──────────────────────────────────

def test_th_t5_handle_task_none_input():
    """handle_task(None) 应抛出 AttributeError 而非静默失败。

    防御性编程：函数签名标注 task: dict 但无运行时守卫。
    server.py 调用链已有 _execute_task 的 'if not task: return' 守卫，
    此测试确保直接调用时的行为可预期。
    """
    import task_handler
    with pytest.raises(AttributeError, match="NoneType.*has no attribute.*get"):
        task_handler.handle_task(None)


def test_th_t6_handle_task_non_dict_input():
    """handle_task 接收非 dict 入参应抛出 AttributeError 而非静默处理。"""
    import task_handler
    with pytest.raises(AttributeError):
        task_handler.handle_task("not_a_dict")
    with pytest.raises(AttributeError):
        task_handler.handle_task(42)
    with pytest.raises(AttributeError):
        task_handler.handle_task([1, 2, 3])


@pytest.mark.xfail(reason=_XFAIL_MSG_TYPE, strict=True)
def test_th_t7_handle_task_list_message_field():
    """handle_task 接收 message=[1,2,3] 应不崩溃，优雅处理为 failed。

    攻击面验证：
      POST /a2a/tasks  →  {"message": [1, 2, 3]}
      server.py 第238行 raw_msg = body.get("message")  ← 无类型校验
      server.py 第243行 task["message"] = raw_msg       ← 原样存储
      server.py 第301行 result = handle_task(task)      ← 传入 handle_task
      task_handler.py 第314行 msg.get("text")           ← AttributeError!
    """
    import task_handler
    task = {
        "id": "t7-list-msg",
        "message": [1, 2, 3],
    }
    # 不应抛出 AttributeError, 应返回 failed 状态
    result = task_handler.handle_task(task)
    assert result["status"] == "failed", \
        f"expected status=failed, got {result.get('status')}"
    assert "error" in result, \
        f"expected error message in result, got {result}"


@pytest.mark.xfail(reason=_XFAIL_MSG_TYPE, strict=True)
def test_th_t8_handle_task_numeric_message_field():
    """handle_task 接收 message=12345 应不崩溃，优雅处理为 failed。"""
    import task_handler
    task = {
        "id": "t8-num-msg",
        "message": 12345,
    }
    result = task_handler.handle_task(task)
    assert result["status"] == "failed"
    assert "error" in result


@pytest.mark.xfail(reason=_XFAIL_MSG_TYPE, strict=True)
def test_th_t9_handle_task_bool_message_field():
    """handle_task 接收 message=True 应不崩溃，优雅处理为 failed。"""
    import task_handler
    task = {
        "id": "t9-bool-msg",
        "message": True,
    }
    result = task_handler.handle_task(task)
    assert result["status"] == "failed"
    assert "error" in result


def test_th_t10_server_raw_msg_no_type_check():
    """验证 server.py 的 raw_msg 提取路径没有运行时类型校验。

    直接调用 task_handler._via_api_server 或 handle_task
    模拟 server._execute_task 传入了 message 为 list 的 task。
    此项为设计约束确认 — 确保类型校验必须添加在 task_handler 层。
    """
    import task_handler
    # 模拟 server.py 构造的 task: message 从 JSON body 直接提取
    malicious_task = {
        "id": "t10-raw-msg",
        "message": {"role": "user", "parts": [{"type": "text", "text": "hello"}]},
    }
    # dict 类型 message 是合法路径，不应失败
    result = task_handler.handle_task(malicious_task)
    # 因为没有设置 profile/env，预期会走到 try/except 捕获异常
    # 但不应因 AttributeError 崩溃
    assert isinstance(result, dict), "handle_task 必须返回 dict，不得抛出异常"
