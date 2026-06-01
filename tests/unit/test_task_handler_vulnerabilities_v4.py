"""task_handler.py P0/P1 security regression suite (GREEN phase)."""
from __future__ import annotations

import json
import os
import re
import inspect
import tempfile
import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, "core")


class TestInputTypeValidation:
    @pytest.mark.parametrize("bad_input", [None, 42, "string", [1, 2, 3], 3.14, True])
    def test_non_dict_input_returns_failed(self, bad_input):
        import task_handler
        result = task_handler.handle_task(bad_input)
        assert result["status"] == "failed"
        assert "Invalid task" in result["error"]


class TestMessageFieldTypeConfusion:
    @pytest.mark.parametrize("message", [[1, 2, 3], 12345, True, 3.14])
    def test_non_string_non_dict_message_returns_failed(self, message):
        import task_handler
        task = {"id": "bad-msg", "message": message}
        result = task_handler.handle_task(task)
        assert result["status"] == "failed"
        assert "Empty message" in result["error"]
        assert "status" not in task

    def test_dict_message_with_parts_text_still_works(self, monkeypatch):
        import task_handler
        monkeypatch.setattr(task_handler, "_via_api_server", lambda task, tid, prompt, profile: {**task, "status": "completed", "artifact": {"prompt": prompt}})
        task = {"id": "parts", "message": {"parts": [{"type": "text", "text": "hello"}]}}
        result = task_handler.handle_task(task)
        assert result["status"] == "completed"
        assert "hello" in result["artifact"]["prompt"]
        assert "status" not in task


class TestLoadSignalsExceptionCoverage:
    @pytest.fixture(autouse=True)
    def reset_cache(self):
        import task_handler
        task_handler._RESULT_SIGNALS_CACHE = None
        old = os.environ.pop("A2A_CLASSIFY_KEYWORDS", None)
        yield
        task_handler._RESULT_SIGNALS_CACHE = None
        if old is not None:
            os.environ["A2A_CLASSIFY_KEYWORDS"] = old
        else:
            os.environ.pop("A2A_CLASSIFY_KEYWORDS", None)

    @pytest.mark.parametrize("exc", [RuntimeError("boom"), TypeError("bad")])
    def test_unexpected_loader_errors_fall_back_to_defaults(self, monkeypatch, tmp_path, exc):
        import task_handler
        kw_file = tmp_path / "keywords.json"
        kw_file.write_text('{"tool_unavailable": ["error"]}')
        monkeypatch.setenv("A2A_CLASSIFY_KEYWORDS", str(kw_file))
        monkeypatch.setattr(json, "loads", lambda *a, **k: (_ for _ in ()).throw(exc))
        result = task_handler._load_signals()
        assert "tool_unavailable" in result
        assert "task_achieved" in result


class TestClassifyEmptyResponse:
    def test_empty_string_is_degraded_empty_response(self):
        import task_handler
        result = task_handler._classify("completed", "")
        assert result == {"semantic_status": "degraded", "completion_reason": "empty_response"}

    def test_whitespace_is_degraded_empty_response(self):
        import task_handler
        result = task_handler._classify("completed", "   ")
        assert result == {"semantic_status": "degraded", "completion_reason": "empty_response"}

    def test_failed_timeout_still_timeout(self):
        import task_handler
        result = task_handler._classify("failed", "", "timeout after 300s")
        assert result["completion_reason"] == "timeout"


class TestExtractFromPartsNonText:
    def test_text_parts_are_joined_not_silently_dropped(self):
        import task_handler
        result = task_handler._extract_from_parts([
            {"type": "text", "text": "first"},
            {"type": "text", "text": "second"},
        ])
        assert result == "first\nsecond"

    def test_non_text_parts_are_represented(self):
        import task_handler
        result = task_handler._extract_from_parts([
            {"type": "image", "url": "https://example.com/img.png"},
            {"type": "file", "name": "doc.pdf"},
        ])
        assert "[image: https://example.com/img.png]" in result
        assert "[file: doc.pdf]" in result

    def test_parts_none_is_safe(self):
        import task_handler
        assert task_handler._extract_from_parts(None) == ""


class TestSQLiteTimeout:
    def test_sqlite_connect_has_explicit_timeout(self):
        import task_handler
        src = inspect.getsource(task_handler._ensure_comment_kind_backfill)
        assert "sqlite3.connect(str(db_path), timeout=30)" in src


class TestHandleTaskDoesNotMutateInputDict:
    def test_empty_message_does_not_mutate_original(self):
        import task_handler
        original = {"id": "v7-empty", "message": {}}
        before = dict(original)
        result = task_handler.handle_task(original)
        assert result["status"] == "failed"
        assert original == before
        assert result is not original

    def test_success_path_does_not_mutate_original(self, monkeypatch):
        import task_handler
        def fake_api(task, tid, prompt, profile):
            task["status"] = "completed"
            return task
        monkeypatch.setattr(task_handler, "_via_api_server", fake_api)
        original = {"id": "v7-ok", "message": "hello"}
        before = dict(original)
        result = task_handler.handle_task(original)
        assert result["status"] == "completed"
        assert original == before

    def test_via_subprocess_returns_new_dict(self, monkeypatch):
        import task_handler
        fake_completed = MagicMock(returncode=0, stdout="sent", stderr="")
        monkeypatch.setattr(task_handler.subprocess, "run", lambda *a, **k: fake_completed)
        monkeypatch.setattr(task_handler, "_resolve_skill_env", lambda p, t: ({}, []))
        original = {"id": "t07c", "message": "say hi"}
        result = task_handler._via_subprocess(original, "t07c", "say hi", "default")
        assert result is not original
        assert result.get("artifact", {}).get("mode") == "subprocess"
        assert "artifact" not in original
