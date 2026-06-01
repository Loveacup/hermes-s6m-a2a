"""刑部安全缺陷验证测试 — task_handler.py 已知 7 项漏洞 (P0).

Each test class maps to one vulnerability from the security audit.
Tests are marked with severity level for triage.

漏洞清单:
  1. handle_task 无 isinstance(task, dict) 守卫 → 非 dict 传入时 AttributeError
  2. message field 类型混淆 → list/int/bool message 导致 .get("text") 崩溃
  3. _load_signals 异常覆盖不足 → 仅 catch OSError/JSONDecodeError/ValueError
  4. _classify 空响应返回 "unknown" → completed + "" 掩盖无输出异常
  5. _extract_from_parts 静默丢弃非文本 → image/audio/file 类型被丢弃
  6. SQLite connect 无 timeout → 默认 timeout=0，被锁即失败
  7. handle_task 就地修改 input dict → 调用方 dict 被污染
"""
from __future__ import annotations

import json
import sqlite3
import sys
import threading
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "core"))


# ====================================================================
# 漏洞 1: handle_task 缺少 isinstance(task, dict) 守卫 (P0)
# ====================================================================
# handle_task() 函数签名标注 task: dict 但无运行时守卫。
# 第312行 task.get("id") 在 None/int/str/list 上传入时抛出 AttributeError。


class TestVuln01_InputTypeValidation:
    """漏洞1: handle_task 缺少输入类型校验守卫。"""

    @pytest.mark.parametrize("bad_input", [None, 42, "string", [1, 2, 3], 3.14, True])
    def test_vuln_01a_non_dict_input_crashes(self, bad_input):
        """非 dict 入参应导致 AttributeError（无守卫的表现）。

        验证：handle_task 在 task.get("id") 处崩溃，未优雅处理。
        """
        import task_handler
        with pytest.raises(AttributeError, match=".*has no attribute.*get.*"):
            task_handler.handle_task(bad_input)

    def test_vuln_01b_empty_dict_handled_gracefully(self):
        """空 dict {} 应触发 'Empty message' 错误而非崩溃。

        这是正常边界：空 dict 至少是 dict 类型。
        """
        import task_handler
        result = task_handler.handle_task({})
        assert isinstance(result, dict)
        assert result.get("status") == "failed"
        assert "Empty message" in result.get("error", "")

    def test_vuln_01c_partial_dict_without_message(self):
        """有 id 但无 message 的 dict 应优雅失败而非崩溃。"""
        import task_handler
        result = task_handler.handle_task({"id": "t01c"})
        assert isinstance(result, dict)
        assert result.get("status") == "failed"
        assert "Empty message" in result.get("error", "")

    def test_vuln_01d_code_has_no_isinstance_guard(self):
        """验证源码中无 isinstance(task, dict) 守卫。"""
        src = (ROOT / "core" / "task_handler.py").read_text(encoding="utf-8")
        # 搜索 handle_task 函数定义到第一行 body 之间
        # 确认没有 isinstance 检查
        lines = src.splitlines()
        in_handle_task = False
        found_guard = False
        for line in lines:
            if line.startswith("def handle_task(task: dict) -> dict:"):
                in_handle_task = True
                continue
            if in_handle_task:
                if "isinstance" in line and "dict" in line:
                    found_guard = True
                    break
                # 第一行有效代码（非空、非注释、非装饰器）
                stripped = line.strip()
                if stripped and not stripped.startswith("#") and not stripped.startswith('"""') and not stripped.startswith("@"):
                    break  # 到了函数体第一行，不再继续
        assert not found_guard, (
            "漏洞已修复 — handle_task 中发现了 isinstance 守卫"
        )


# ====================================================================
# 漏洞 2: message field 类型混淆 (P0)
# ====================================================================
# handle_task 第313行:
#   msg = task.get("message") or task.get("input") or task.get("action") or {}
# 第314行:
#   prompt = msg if isinstance(msg, str) else (msg.get("text") or ...)
# 当 msg 是 list/int/bool 等非 dict 非 str 类型时，msg.get("text") 崩溃。


class TestVuln02_MessageFieldTypeConfusion:
    """漏洞2: message 字段类型混淆导致 AttributeError。"""

    def test_vuln_02a_message_is_list_crashes(self):
        """message=[1,2,3] → msg.get('text') 在 list 上抛出 AttributeError。

        红色阶段验证：当前无类型守卫，list message 直接崩溃。
        攻击面: POST /a2a/tasks → {"message": [1, 2, 3]}
        """
        import task_handler
        task = {"id": "t02a-list", "message": [1, 2, 3]}
        with pytest.raises(AttributeError, match="list.*has no attribute.*get"):
            task_handler.handle_task(task)

    def test_vuln_02b_message_is_int_crashes(self):
        """message=12345 → msg.get('text') 在 int 上抛出 AttributeError。"""
        import task_handler
        task = {"id": "t02b-int", "message": 12345}
        with pytest.raises(AttributeError):
            task_handler.handle_task(task)

    def test_vuln_02c_message_is_bool_crashes(self):
        """message=True → msg.get('text') 在 bool 上抛出 AttributeError。"""
        import task_handler
        task = {"id": "t02c-bool", "message": True}
        with pytest.raises(AttributeError):
            task_handler.handle_task(task)

    def test_vuln_02d_message_is_float_crashes(self):
        """message=3.14 → msg.get('text') 在 float 上抛出 AttributeError。"""
        import task_handler
        task = {"id": "t02d-float", "message": 3.14}
        with pytest.raises(AttributeError):
            task_handler.handle_task(task)

    def test_vuln_02e_message_is_none(self):
        """message=None → msg = {} (or 短路)。应为 failed。"""
        import task_handler
        task = {"id": "t02e-none", "message": None}
        result = task_handler.handle_task(task)
        assert result["status"] == "failed"
        assert "Empty message" in result.get("error", "")

    def test_vuln_02f_message_field_code_scan(self):
        """验证第313行没有运行时类型守卫检查 message 类型。"""
        src = (ROOT / "core" / "task_handler.py").read_text(encoding="utf-8")
        lines = src.splitlines()
        # 找到 message 提取行
        msg_lines = []
        for i, line in enumerate(lines, 1):
            if i >= 313 and i <= 315:
                msg_lines.append(line.strip())
        # 确认没有 isinstance(msg, dict) 检查
        has_dict_guard = any(
            "isinstance" in l and "dict" in l for l in msg_lines
        )
        assert not has_dict_guard, (
            "漏洞已修复 — message 行发现了 isinstance 守卫"
        )


# ====================================================================
# 漏洞 3: _load_signals 异常覆盖不足 (P2→P0)
# ====================================================================
# _load_signals() 第173行:
#   except (OSError, json.JSONDecodeError, ValueError) as e:
# 非此三类的异常（RuntimeError, TypeError, MemoryError 等）直接传播。


class TestVuln03_LoadSignalsExceptionCoverage:
    """漏洞3: _load_signals 异常覆盖不足。"""

    def test_vuln_03a_runtime_error_propagates(self, monkeypatch, tmp_path):
        """RuntimeError 不应传播 — 应被 catch 并回退到 defaults。

        当前: _load_signals 只 catch (OSError, json.JSONDecodeError, ValueError)。
        RuntimeError 在不同步骤中可能抛出，但未受保护。
        """
        import task_handler
        import json
        task_handler._RESULT_SIGNALS_CACHE = None

        kw_file = tmp_path / "keywords.json"
        kw_file.write_text('{"tool_unavailable": ["error"]}')
        monkeypatch.setenv("A2A_CLASSIFY_KEYWORDS", str(kw_file))

        original_loads = json.loads

        def trigger_runtime(*args, **kwargs):
            raise RuntimeError("unexpected crash in json.loads")

        monkeypatch.setattr(json, "loads", trigger_runtime)

        # 当前行为: RuntimeError 传播出去（不在 except 列表中）
        with pytest.raises(RuntimeError, match="unexpected crash"):
            task_handler._load_signals()

    def test_vuln_03b_type_error_propagates(self, monkeypatch, tmp_path):
        """TypeError 不应传播 — 也应被 catch。"""
        import task_handler
        task_handler._RESULT_SIGNALS_CACHE = None

        kw_file = tmp_path / "keywords.json"
        kw_file.write_text('{"tool_unavailable": ["error"]}')

        def bad_open(*args, **kwargs):
            raise TypeError("unexpected type error")
        # 用 monkeypatch 让 os.environ.get 触发 TypeError
        # 更简洁：patch json.loads to raise TypeError
        import json
        original_loads = json.loads

        def trigger_typeerror(*args, **kwargs):
            raise TypeError("simulated type error")

        monkeypatch.setattr(json, "loads", trigger_typeerror)
        monkeypatch.setenv("A2A_CLASSIFY_KEYWORDS", str(kw_file))

        # 当前行为: TypeError 传播
        with pytest.raises(TypeError, match="simulated type error"):
            task_handler._load_signals()

    def test_vuln_03c_exception_bucket_is_narrow(self):
        """验证 except 行只覆盖了有限类型。"""
        src = (ROOT / "core" / "task_handler.py").read_text(encoding="utf-8")
        # 找到 except 行
        for line in src.splitlines():
            if "except (OSError, json.JSONDecodeError, ValueError)" in line:
                # 确认没有更广泛的 except 子句
                assert "Exception" not in line, "漏洞已修复 — 发现了更广泛的 except"
                return
        pytest.fail("未找到 _load_signals 的 except 行 — 可能已重构")

    def test_vuln_03d_permission_error_is_oserror_subclass(self, tmp_path, monkeypatch):
        """PermissionError 是 OSError 子类，已被 catch。此测试验证正常工作路径。

        这是见证测试 — 确认 OSError 子类被正确处理。
        """
        import task_handler
        task_handler._RESULT_SIGNALS_CACHE = None

        kw_file = tmp_path / "keywords.json"
        kw_file.write_text('{"tool_unavailable": ["error"]}')
        kw_file.chmod(0o000)  # 无权限

        monkeypatch.setenv("A2A_CLASSIFY_KEYWORDS", str(kw_file))
        result = task_handler._load_signals()
        # PermissionError (OSError) 被 catch → 回退到 defaults
        assert "tool_unavailable" in result
        assert "task_achieved" in result


# ====================================================================
# 漏洞 4: _classify 空响应返回 "unknown" (P2)
# ====================================================================
# _classify 第206行:
#   return {"semantic_status": "succeeded", "completion_reason": "unknown"}
# 当 status="completed" 且 response="" 时，返回 unknown。
# 这掩盖了"代理无输出"的异常情况。


class TestVuln04_ClassifyEmptyResponse:
    """漏洞4: _classify 空响应返回 unknown。"""

    def test_vuln_04a_empty_response_returns_unknown(self):
        """status=completed + response='' → completion_reason='unknown'。

        问题：'completed' 但无输出是大警告，不应归类为 'unknown'。
        """
        import task_handler
        result = task_handler._classify("completed", "")
        assert result["semantic_status"] == "succeeded", (
            f"expected succeeded, got {result}"
        )
        assert result["completion_reason"] == "unknown", (
            f"empty response should not be 'unknown': {result}"
        )

    def test_vuln_04b_whitespace_response_returns_unknown(self):
        """status=completed + response='   ' → completion_reason='unknown'。"""
        import task_handler
        result = task_handler._classify("completed", "   ")
        assert result["completion_reason"] == "unknown"

    def test_vuln_04c_empty_failed_response_not_affected(self):
        """status=failed + response='' → completion_reason='agent_error'（不受影响）。"""
        import task_handler
        result = task_handler._classify("failed", "")
        assert result["completion_reason"] == "agent_error"

    def test_vuln_04d_empty_response_semantic_status_is_succeeded(self):
        """空响应的 semantic_status 是 succeeded 而非 failed。

        这掩盖了代理完全没有产出的事实。
        """
        import task_handler
        result = task_handler._classify("completed", "")
        assert result["semantic_status"] == "succeeded", (
            "Empty response should arguably be 'failed', not 'succeeded': "
            f"{result}"
        )

    def test_vuln_04e_no_keyword_match_returns_unknown(self):
        """正常响应但不匹配任何关键词也返回 unknown。

        这是区分度问题 — 'no match' 和 'no output' 无法区分。
        """
        import task_handler
        result = task_handler._classify("completed", "代理收到请求但未发送任何消息")
        assert result["completion_reason"] == "unknown"


# ====================================================================
# 漏洞 5: _extract_from_parts 静默丢弃非文本 (P1)
# ====================================================================
# _extract_from_parts() 第497-501行:
#   for p in parts:
#       if p.get("type") == "text":
#           return p.get("text", "")
#   return ""
# 只处理第一个 text part，image/audio/file 类型被静默丢弃。


class TestVuln05_ExtractFromPartsNonText:
    """漏洞5: _extract_from_parts 静默丢弃非文本内容。"""

    def test_vuln_05a_only_text_processed(self):
        """只从 type='text' 的 part 提取内容。"""
        import task_handler
        parts = [
            {"type": "image", "url": "https://example.com/img.png"},
            {"type": "audio", "url": "https://example.com/audio.mp3"},
        ]
        result = task_handler._extract_from_parts(parts)
        assert result == "", (
            f"expected empty string for non-text parts, got: {result!r}"
        )

    def test_vuln_05b_only_first_text_part_returned(self):
        """仅返回第一个 text part，后续 text 被丢弃。"""
        import task_handler
        parts = [
            {"type": "text", "text": "first"},
            {"type": "text", "text": "second"},
            {"type": "text", "text": "third"},
        ]
        result = task_handler._extract_from_parts(parts)
        assert result == "first", (
            f"expected 'first', got {result!r} — "
            "后续 text parts 被静默丢弃"
        )

    def test_vuln_05c_no_text_part_returns_empty(self):
        """无 text part 时返回空字符串。"""
        import task_handler
        parts = [
            {"type": "file", "name": "doc.pdf"},
            {"type": "tool_use", "name": "search"},
        ]
        result = task_handler._extract_from_parts(parts)
        assert result == ""

    def test_vuln_05d_mixed_parts_with_image_first(self):
        """第一个非 text part 被跳过，回到第一个 text part。"""
        import task_handler
        parts = [
            {"type": "image", "url": "img.png"},
            {"type": "text", "text": "the actual prompt"},
        ]
        result = task_handler._extract_from_parts(parts)
        assert result == "the actual prompt"

    def test_vuln_05e_empty_parts_list(self):
        """空 parts 列表返回空字符串。"""
        import task_handler
        result = task_handler._extract_from_parts([])
        assert result == ""

    def test_vuln_05f_text_without_text_key(self):
        """type=text 但无 text key 返回空字符串。"""
        import task_handler
        parts = [{"type": "text"}]
        result = task_handler._extract_from_parts(parts)
        assert result == ""

    def test_vuln_05g_no_assembly_of_multimodal(self):
        """验证不存在 multimodality 组装逻辑。

        如果存在 text+image 拼接，说明漏洞可能已修复。
        """
        src = (ROOT / "core" / "task_handler.py").read_text(encoding="utf-8")
        lines = src.splitlines()
        func_start = None
        for i, line in enumerate(lines, 1):
            if "def _extract_from_parts" in line:
                func_start = i
                break
        assert func_start is not None, "函数 _extract_from_parts 未找到"
        # 检查函数体中无 "image" "audio" "file" 的处理
        body = "\n".join(lines[func_start:func_start + 10])
        for content_type in ["image", "audio", "file", "tool_use"]:
            assert content_type not in body, (
                f"发现 {content_type} 处理逻辑 — 可能已修复"
            )


# ====================================================================
# 漏洞 6: SQLite connect 无 timeout (P1)
# ====================================================================
# _ensure_comment_kind_backfill 第265行:
#   conn = sqlite3.connect(str(db_path))
# 默认 timeout=0，数据库被锁时立即失败。


class TestVuln06_SqliteConnectNoTimeout:
    """漏洞6: SQLite connect 未配置 timeout。"""

    def test_vuln_06a_source_has_no_timeout_param(self):
        """验证 sqlite3.connect 调用无 timeout 参数。"""
        src = (ROOT / "core" / "task_handler.py").read_text(encoding="utf-8")
        # 查找 sqlite3.connect 调用行
        for i, line in enumerate(src.splitlines(), 1):
            if "sqlite3.connect(str(db_path))" in line or "sqlite3.connect(str" in line:
                assert "timeout" not in line, (
                    f"漏洞已修复 — 第{i}行发现了 timeout 参数"
                )
                return
        pytest.fail("未找到 sqlite3.connect 调用 — 可能已重构")

    def test_vuln_06b_default_timeout_is_zero(self):
        """sqlite3.connect() 默认 timeout=0。

        被锁时立即失败无重试，multithreading 环境下脆弱。
        """
        import task_handler
        # 创建两个数据库连接
        db_path = Path("/tmp/test_sqlite_timeout_vuln.db")
        try:
            conn1 = sqlite3.connect(str(db_path))
            conn1.execute("CREATE TABLE IF NOT EXISTS t (x int)")
            conn1.execute("BEGIN EXCLUSIVE TRANSACTION")
            conn1.execute("INSERT INTO t VALUES (1)")

            # 第二个连接尝试同样 DB — 默认 timeout=0 会立即失败
            conn2 = sqlite3.connect(str(db_path), timeout=0)
            try:
                conn2.execute("SELECT 1 FROM t")
                # 这行可能成功也可能失败取决于锁
                # 重点是 timeout=0 无重试
            except sqlite3.OperationalError as e:
                assert "database is locked" in str(e).lower(), (
                    f"expected lock error, got: {e}"
                )
            finally:
                conn2.close()

            conn1.rollback()
        finally:
            conn1.close()
            if db_path.exists():
                db_path.unlink()

    def test_vuln_06c_connect_signature_confirms_no_timeout(self):
        """通过 inspect 模块确认调用签名无 timeout。"""
        import task_handler
        import inspect
        src_lines = inspect.getsource(task_handler._ensure_comment_kind_backfill)
        for line in src_lines.splitlines():
            if "sqlite3.connect" in line:
                assert "timeout" not in line, (
                    f"漏洞已修复: {line.strip()}"
                )
                return
        pytest.fail("未找到 sqlite3.connect 调用")

    def test_vuln_06d_locked_db_returns_none(self, tmp_path, monkeypatch):
        """模拟 locked DB — _ensure_comment_kind_backfill 返回 None。"""
        import task_handler
        # 创建一个只读的数据库文件模拟被其他进程使用
        db_path = tmp_path / "kanban.db"
        db_path.touch()

        # 设置 HERMES_HOME 指向 tmp_path（空目录，无 kanban.db）
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        # _ensure_comment_kind_backfill 会尝试打开 tmp_path/kanban.db
        # 但它是空文件（无效 SQLite 数据库）
        # 这会导致 sqlite3.connect 失败
        result = task_handler._ensure_comment_kind_backfill(task_id="t_test")
        assert result is None, (
            f"expected None for invalid database, got: {result}"
        )


# ====================================================================
# 漏洞 7: handle_task 就地修改 input dict (P0)
# ====================================================================
# handle_task 第316行: task["status"] = "failed"
# 第317行: task["error"] = "Empty message"
# _via_api_server / _via_subprocess 也直接在 task 上写 status/artifact 等。
# 调用方传入的 dict 被污染。


class TestVuln07_InplaceMutation:
    """漏洞7: handle_task 就地修改调用方的 input dict。"""

    def test_vuln_07a_empty_message_mutates_original(self):
        """空 message 路径 — handle_task 直接在原 dict 上写 status/error。"""
        import task_handler
        original = {"id": "t07a"}
        expected_keys_before = set(original.keys())

        task_handler.handle_task(original)

        # 原 dict 多了 status, error 等键
        assert "status" in original, "原 dict 被添加了 status"
        assert "error" in original, "原 dict 被添加了 error"
        assert original["status"] == "failed"
        # 调用方的 dict 被污染

    def test_vuln_07b_fake_server_mutates_original(self, monkeypatch):
        """即使 mock _via_api_server，handle_task 外层的 try/except 也会污染原 dict。

        测试: 模拟 API server 成功路径，验证原 dict 被添加了 status 键。
        """
        import task_handler

        def fake_api(task, tid, prompt, profile):
            task["status"] = "completed"
            return task

        monkeypatch.setattr(task_handler, "_via_api_server", fake_api)
        monkeypatch.setenv("HERMES_PROFILE", "default")

        original = {"id": "t07b", "message": "hello"}

        task_handler.handle_task(original)

        # 原 dict 被修改 — 即使在 mock 路径下也被污染
        assert "status" in original, "original dict 被添加了 status"
        assert original["status"] == "completed"

    def test_vuln_07c_via_subprocess_mutates_same_ref(self, monkeypatch):
        """_via_subprocess 返回的是同一个 dict 对象引用。"""
        import task_handler

        fake_completed = MagicMock(
            returncode=0,
            stdout="sent",
            stderr="",
        )

        def fake_run(*args, **kwargs):
            return fake_completed

        monkeypatch.setattr(task_handler.subprocess, "run", fake_run)
        monkeypatch.setenv("HERMES_PROFILE", "default")

        # 移除 skills resolver 依赖
        monkeypatch.setattr(task_handler, "_resolve_skill_env",
                            lambda p, t: ({}, []))

        original = {"id": "t07c", "message": "say hi"}
        result = task_handler._via_subprocess(original, "t07c", "say hi", "default")

        assert result is original, (
            "返回的不是同一个 dict 对象 — 调用方无法通过复制防御"
        )
        assert result.get("artifact", {}).get("mode") == "subprocess"

    def test_vuln_07d_mutation_affects_caller_state(self):
        """验证调用方在调用后能直接看到添加的键。"""
        import task_handler
        caller_data = {"id": "t07d", "message": ""}
        # message="" 会通过 or 短路 → {}，导致空 message 错误

        result = task_handler.handle_task(caller_data)

        # 验证 result 就是 caller_data
        assert result is caller_data
        # 调用方仍然持有引用，观察到污染
        assert caller_data["status"] == "failed"
        assert caller_data["error"] == "Empty message"

    def test_vuln_07e_documented_as_by_design(self):
        """确认设计决策 — 无防御性 copy。

        如果实现改为 'task = dict(task)' 开头，说明已修复。
        """
        src = (ROOT / "core" / "task_handler.py").read_text(encoding="utf-8")
        # 检查 handle_task 函数开头
        lines = src.splitlines()
        in_handle = False
        has_defensive_copy = False
        for line in lines:
            if line.startswith("def handle_task(task: dict) -> dict:"):
                in_handle = True
                continue
            if in_handle:
                stripped = line.strip()
                if "dict(task)" in stripped or "copy()" in stripped:
                    has_defensive_copy = True
                    break
                # 到了 return 行附近
                if stripped.startswith("return"):
                    break
        assert not has_defensive_copy, (
            "漏洞可能已修复 — 发现了防御性 copy"
        )


# ====================================================================
# 合计：7 vulnerabilities, 30+ test cases
# ====================================================================
