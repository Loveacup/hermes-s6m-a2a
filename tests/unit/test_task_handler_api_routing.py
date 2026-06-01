"""task_handler.py — API Server routing + P0 input validation regressions."""
from __future__ import annotations

import json
import urllib.error
from unittest.mock import MagicMock


def test_th_t1_no_hardcoded_whitelist():
    import task_handler
    assert not hasattr(task_handler, "_API_SERVER_PORTS")


def test_th_t2_port_resolved_dynamically():
    import task_handler
    from port_resolver import api_server_port
    assert hasattr(task_handler, "_api_server_port")
    assert task_handler._api_server_port("engineer") == api_server_port("engineer")


def test_th_t3_connect_error_falls_back_to_subprocess(monkeypatch):
    import task_handler
    fake_completed = MagicMock(returncode=0, stdout="ok", stderr="")
    monkeypatch.setattr(task_handler.subprocess, "run", lambda *a, **k: fake_completed)
    monkeypatch.setattr(task_handler.urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(urllib.error.URLError("connection refused")))
    task = {"id": "t1", "summary": "test", "description": "x"}
    result = task_handler._via_api_server(task, "t1", "do thing", "engineer")
    assert result.get("artifact", {}).get("mode") == "subprocess"
    assert "artifact" not in task, "helper must not mutate caller dict"


def test_th_t4_api_server_success_path(monkeypatch):
    import task_handler
    sequence = iter([
        json.dumps({"run_id": "r1"}).encode(),
        json.dumps({"status": "completed", "output": "done"}).encode(),
    ])
    class FakeResp:
        def __init__(self, body): self.body = body
        def read(self): return self.body
        def __enter__(self): return self
        def __exit__(self, *a): return False
    monkeypatch.setattr(task_handler.urllib.request, "urlopen", lambda req, *a, **k: FakeResp(next(sequence)))
    monkeypatch.setattr(task_handler.time, "sleep", lambda s: None)
    task = {"id": "t1"}
    result = task_handler._via_api_server(task, "t1", "say hi", "engineer")
    assert result["status"] == "completed"
    assert result["artifact"]["mode"] == "api_server"
    assert "status" not in task, "helper must not mutate caller dict"


def test_th_t5_handle_task_none_input_returns_failed():
    import task_handler
    result = task_handler.handle_task(None)
    assert result["status"] == "failed"
    assert "Invalid task" in result["error"]


def test_th_t6_handle_task_non_dict_input_returns_failed():
    import task_handler
    for bad in ["not_a_dict", 42, [1, 2, 3], True]:
        result = task_handler.handle_task(bad)
        assert result["status"] == "failed"
        assert "Invalid task" in result["error"]


def test_th_t7_handle_task_list_message_field():
    import task_handler
    task = {"id": "t7-list-msg", "message": [1, 2, 3]}
    result = task_handler.handle_task(task)
    assert result["status"] == "failed"
    assert "error" in result
    assert "status" not in task


def test_th_t8_handle_task_numeric_message_field():
    import task_handler
    task = {"id": "t8-num-msg", "message": 12345}
    result = task_handler.handle_task(task)
    assert result["status"] == "failed"
    assert "error" in result
    assert "status" not in task


def test_th_t9_handle_task_bool_message_field():
    import task_handler
    task = {"id": "t9-bool-msg", "message": True}
    result = task_handler.handle_task(task)
    assert result["status"] == "failed"
    assert "error" in result
    assert "status" not in task


def test_th_t10_server_raw_msg_valid_dict_still_supported():
    import task_handler
    malicious_task = {"id": "t10-raw-msg", "message": {"role": "user", "parts": [{"type": "text", "text": "hello"}]}}
    result = task_handler.handle_task(malicious_task)
    assert isinstance(result, dict)
