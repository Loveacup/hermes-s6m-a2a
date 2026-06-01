"""邢部 — task_handler.py 安全缺陷验证测试套件 (RED phase)

覆盖 7 个已确认 P0/P1/P2 安全缺陷：
V1. handle_task 缺少 isinstance 守卫 — 传入 None 抛出 AttributeError
V2. message 字段类型混淆 — 传入 list/int/bool 抛出 AttributeError
V3. _load_signals 异常覆盖不足 — 不报错但不记录关键错误详情
V4. _classify 对空响应返回 "unknown" — 原因不明确
V5. _extract_from_parts 静默丢弃非文本内容
V6. SQLite connect 未配置 timeout — 阻塞风险
V7. handle_task 就地修改 input dict — 污染调用方
"""
import json
import os
import re
import inspect
import tempfile
import copy
import sys

import pytest

sys.path.insert(0, "core")


# ═══════════════════════════════════════════════════════════
# V1: handle_task 缺少输入类型校验 (P0)
# ═══════════════════════════════════════════════════════════

class TestV1InputTypeValidation:
    """handle_task 声明 dict 参数但无运行时守卫 — None/非 dict 直接崩溃。"""

    def test_v1_handle_task_none_crashes(self):
        """handle_task(None) 抛出 AttributeError，证明无 isinstance 守卫。"""
        import task_handler
        with pytest.raises(AttributeError, match="NoneType"):
            task_handler.handle_task(None)

    def test_v1_handle_task_string_crashes(self):
        """handle_task("not_a_dict") 应抛出 AttributeError。"""
        import task_handler
        with pytest.raises(AttributeError):
            task_handler.handle_task("not_a_dict")

    def test_v1_handle_task_int_crashes(self):
        """handle_task(42) 应抛出 AttributeError。"""
        import task_handler
        with pytest.raises(AttributeError):
            task_handler.handle_task(42)

    def test_v1_handle_task_list_crashes(self):
        """handle_task([1, 2, 3]) 应抛出 AttributeError。"""
        import task_handler
        with pytest.raises(AttributeError):
            task_handler.handle_task([1, 2, 3])


# ═══════════════════════════════════════════════════════════
# V2: message 字段类型混淆 → AttributeError (P0)
# ═══════════════════════════════════════════════════════════

class TestV2MessageFieldTypeConfusion:
    """当 message 字段为 list/int/bool 时，handle_task 的第 314 行崩溃。

    Line 314: prompt = msg if isinstance(msg, str) else (
        msg.get("text") or ...
    msg 为 list/int/bool ⇒ isintance(msg, str) 为 False
    ⇒ 调用 msg.get("text") ⇒ AttributeError 因为 list/int/bool 无 .get()
    """

    def test_v2_message_list_crashes(self):
        """message=[1,2,3] 抛出 AttributeError。"""
        import task_handler
        task = {"id": "v2-list", "message": [1, 2, 3]}
        with pytest.raises(AttributeError):
            task_handler.handle_task(task)

    def test_v2_message_int_crashes(self):
        """message=12345 抛出 AttributeError。"""
        import task_handler
        task = {"id": "v2-int", "message": 12345}
        with pytest.raises(AttributeError):
            task_handler.handle_task(task)

    def test_v2_message_bool_crashes(self):
        """message=True 抛出 AttributeError。"""
        import task_handler
        task = {"id": "v2-bool", "message": True}
        with pytest.raises(AttributeError):
            task_handler.handle_task(task)

    def test_v2_message_float_crashes(self):
        """message=3.14 抛出 AttributeError。"""
        import task_handler
        task = {"id": "v2-float", "message": 3.14}
        with pytest.raises(AttributeError):
            task_handler.handle_task(task)

    def test_v2_message_none_crashes(self):
        """message=None 作为 falsy 值走 fallback 链到 {}，不崩溃。
        但语义上 message=None 应报错而非静默使用空 dict。"""
        import task_handler
        task = {"id": "v2-none", "message": None}
        result = task_handler.handle_task(task)
        assert result["status"] == "failed"

    def test_v2_message_dict_no_text_parts_returns_failed(self):
        """message={"role": "user"} 是合法 dict 但无 text → 返回 failed。"""
        import task_handler
        task = {"id": "v2-dict", "message": {"role": "user"}}
        result = task_handler.handle_task(task)
        assert result["status"] == "failed"


# ═══════════════════════════════════════════════════════════
# V3: _load_signals 异常覆盖不足 (P0)
# ═══════════════════════════════════════════════════════════

class TestV3LoadSignalsExceptionCoverage:
    """_load_signals 的异常处理仅返回默认信号，调用方无法区分加载成功/失败。"""

    @pytest.fixture(autouse=True)
    def reset_cache(self):
        import task_handler
        task_handler._RESULT_SIGNALS_CACHE = None
        if "A2A_CLASSIFY_KEYWORDS" in os.environ:
            del os.environ["A2A_CLASSIFY_KEYWORDS"]
        yield

    def test_v3_nonexistent_env_path_falls_back_gracefully(self):
        """A2A_CLASSIFY_KEYWORDS 指向不存在的文件 → 静默使用默认值。"""
        import task_handler
        os.environ["A2A_CLASSIFY_KEYWORDS"] = "/nonexistent/surely/missing.json"
        result = task_handler._load_signals()
        assert "tool_unavailable" in result
        assert "task_achieved" in result

    def test_v3_broken_json_falls_back_silently(self):
        """损坏的 JSON 文件 → 静默使用默认值，不抛出顶层异常。"""
        import task_handler
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write("{invalid json content!!!")
            tmp = f.name
        try:
            os.environ["A2A_CLASSIFY_KEYWORDS"] = tmp
            result = task_handler._load_signals()
            assert "tool_unavailable" in result
            assert "task_achieved" in result
        finally:
            os.unlink(tmp)
            del os.environ["A2A_CLASSIFY_KEYWORDS"]

    def test_v3_non_dict_json_falls_back_silently(self):
        """JSON 文件内容非 dict → 静默使用默认值，无返回通知。"""
        import task_handler
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(["not", "a", "dict"], f)
            tmp = f.name
        try:
            os.environ["A2A_CLASSIFY_KEYWORDS"] = tmp
            result = task_handler._load_signals()
            assert "tool_unavailable" in result
            assert "task_achieved" in result
        finally:
            os.unlink(tmp)
            del os.environ["A2A_CLASSIFY_KEYWORDS"]

    def test_v3_malformed_bucket_items_skipped_silently(self):
        """bucket 中非字符串条目被静默跳过。"""
        import task_handler
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({
                "tool_unavailable": ["unable to send", 42, None, "failed to send"],
                "task_achieved": ["sent"],
            }, f)
            tmp = f.name
        try:
            os.environ["A2A_CLASSIFY_KEYWORDS"] = tmp
            result = task_handler._load_signals()
            tu = result.get("tool_unavailable", [])
            assert 42 not in tu
            assert None not in tu
        finally:
            os.unlink(tmp)
            del os.environ["A2A_CLASSIFY_KEYWORDS"]


# ═══════════════════════════════════════════════════════════
# V4: _classify 对空响应返回 unknown (P2)
# ═══════════════════════════════════════════════════════════

class TestV4ClassifyEmptyResponse:
    """_classify 对空/空白响应返回 succeeded/unknown。

    问题：
    1. 空响应应视为 degraded（无输出）而非 succeeded
    2. "unknown" 不提供任何信息
    """

    @pytest.fixture(autouse=True)
    def reset_cache(self):
        import task_handler
        task_handler._RESULT_SIGNALS_CACHE = None
        yield

    def test_v4_empty_string_returns_succeeded_unknown(self):
        """_classify("completed", "") 返回 semantic_status=succeeded + completion_reason=unknown。
        空响应不应视为 succeeded。"""
        import task_handler
        result = task_handler._classify("completed", "")
        assert result["completion_reason"] == "unknown"
        assert result["semantic_status"] == "succeeded"

    def test_v4_whitespace_only_returns_succeeded_unknown(self):
        """_classify("completed", "   ") 返回 succeeded/unknown。"""
        import task_handler
        result = task_handler._classify("completed", "   ")
        assert result["completion_reason"] == "unknown"

    def test_v4_no_signal_match_returns_unknown(self):
        """任何不匹配关键词的响应都返回 unknown。"""
        import task_handler
        result = task_handler._classify("completed", "this is a completely random response")
        assert result["completion_reason"] == "unknown"

    def test_v4_unknown_classification_no_diagnostic_info(self):
        """_classify 不返回任何诊断信息。"""
        import task_handler
        result = task_handler._classify("completed", "nonsense text here")
        assert set(result.keys()) == {"semantic_status", "completion_reason"}

    def test_v4_failed_with_timeout_error_proper(self):
        """failed + timeout error → 正确识别为 timeout。这条路径正常。"""
        import task_handler
        result = task_handler._classify("failed", "", "timeout after 300s")
        assert result["completion_reason"] == "timeout"


# ═══════════════════════════════════════════════════════════
# V5: _extract_from_parts 静默丢弃非文本内容 (P1)
# ═══════════════════════════════════════════════════════════

class TestV5ExtractFromPartsNonText:
    """_extract_from_parts 只提取 type=="text" 的内容，完全忽略 image/file/audio。"""

    def test_v5_non_text_only_returns_empty(self):
        """只有 image/file 类型的 parts → 返回空字符串。"""
        import task_handler
        result = task_handler._extract_from_parts([
            {"type": "image", "url": "https://example.com/img.png"},
            {"type": "file", "name": "doc.pdf", "mimeType": "application/pdf"},
        ])
        assert result == ""

    def test_v5_first_text_returned_rest_ignored(self):
        """多 parts 中仅返回第一个 text，后续 text 被静默丢弃。"""
        import task_handler
        result = task_handler._extract_from_parts([
            {"type": "text", "text": "first message"},
            {"type": "image", "url": "img.png"},
            {"type": "text", "text": "second message"},
        ])
        assert result == "first message"

    def test_v5_empty_parts_returns_empty(self):
        """空 parts → 返回空字符串。"""
        import task_handler
        assert task_handler._extract_from_parts([]) == ""

    def test_v5_parts_none_raises_attribute_error(self):
        """parts=None → _extract_from_parts 抛出 AttributeError
        （但 line 314 的 .get("parts", []) 保护了调用方）。"""
        import task_handler
        with pytest.raises(TypeError):
            # list expected, None can't be iterated
            task_handler._extract_from_parts(None)

    def test_v5_no_text_parts_in_message_returns_failed(self):
        """message 有 parts 但全是非 text → 返回 failed。"""
        import task_handler
        task = {
            "id": "v5-notext",
            "message": {
                "parts": [
                    {"type": "image", "url": "img.png"},
                    {"type": "file", "name": "doc.pdf"},
                ]
            },
        }
        result = task_handler.handle_task(task)
        assert result["status"] == "failed"


# ═══════════════════════════════════════════════════════════
# V6: SQLite connect 未配置 timeout (P1)
# ═══════════════════════════════════════════════════════════

class TestV6SQLiteNoTimeout:
    """_ensure_comment_kind_backfill 的 sqlite3.connect() 无显式 timeout。"""

    def test_v6_sqlite_connect_missing_timeout_in_source(self):
        """源码验证：sqlite3.connect() 无 timeout 参数。"""
        import task_handler
        src = inspect.getsource(task_handler._ensure_comment_kind_backfill)
        conn_calls = re.findall(r"sqlite3\.connect\([^)]*\)", src)
        assert len(conn_calls) >= 1
        for call in conn_calls:
            assert "timeout" not in call, f"预期无 timeout: {call}"

    def test_v6_no_timeout_in_runtime_call(self):
        """运行时调用确认无 timeout 参数。"""
        import task_handler
        src = inspect.getsource(task_handler._ensure_comment_kind_backfill)
        assert "sqlite3.connect(str(db_path))" in src

    def test_v6_concurrent_block_risk(self):
        """高并发场景下默认 timeout=5 可能不足。
        此测试验证 SQLite 并发写的风险存在——不要求精确复现。

        CPython/macOS 的 sqlite3.connect 是 builtin，某些 3.11 构建中
        inspect.signature(sqlite3.connect) 会抛 ValueError("invalid signature")。
        因此这里不依赖 builtin 签名解析，只验证被测函数源码没有显式 timeout。
        """
        import task_handler
        src = inspect.getsource(task_handler._ensure_comment_kind_backfill)
        conn_calls = re.findall(r"sqlite3\.connect\(([^)]*)\)", src)
        assert conn_calls, "预期存在 sqlite3.connect 调用"
        assert all("timeout" not in call for call in conn_calls), \
            f"当前 RED phase 预期未配置 timeout: {conn_calls}"


# ═══════════════════════════════════════════════════════════
# V7: handle_task 就地修改 input dict — 污染调用方 (P0)
# ═══════════════════════════════════════════════════════════

class TestV7HandleTaskMutatesInputDict:
    """handle_task 在多个路径中修改传入的 task dict：
    - 空 prompt 路径: task["status"] / task["error"]
    - 异常路径: task["status"] / task["error"]
    - _via_api_server: task["status"], semantic_status, completion_reason, artifact
    - _via_subprocess: task["status"], semantic_status, completion_reason, artifact
    """

    def test_v7_inplace_mutation_on_empty_message(self):
        """空 message → task["status"] 和 task["error"] 就地修改。"""
        import task_handler
        original = {"id": "v7-empty", "message": {}}
        before_keys = set(original.keys())
        task_handler.handle_task(original)
        new_keys = set(original.keys()) - before_keys
        assert len(new_keys) > 0
        assert "status" in new_keys

    def test_v7_inplace_mutation_on_no_prompt(self):
        """有 message 但无 prompt → status/error 就地修改。"""
        import task_handler
        original = {"id": "v7-noprompt", "message": {"role": "user"}}
        before_keys = set(original.keys())
        task_handler.handle_task(original)
        new_keys = set(original.keys()) - before_keys
        assert "status" in new_keys
        assert original["status"] == "failed"

    def test_v7_original_id_preserved(self):
        """原始 id 至少保留——最小副作用检查。"""
        import task_handler
        original = {"id": "v7-preserve", "message": {}}
        task_handler.handle_task(original)
        assert original.get("id") == "v7-preserve"

    def test_v7_contamination_keys_documented(self):
        """记录可能被污染的 keys 集合。"""
        import task_handler
        original = {"id": "v7-conta", "message": {"text": "hello"}, "skills": []}
        before_keys = set(original.keys())
        try:
            task_handler.handle_task(original)
        except Exception:
            pass
        after_keys = set(original.keys())
        contaminated = after_keys - before_keys
        suspect = {"status", "error", "artifact", "semantic_status", "completion_reason"}
        hit = contaminated & suspect
        # 记录信息（不强制断言路径）
        print(f"  污染 keys: {contaminated} (命中: {hit})")

    def test_v7_caller_data_changed_after_call(self):
        """调用方传入的 dict 在 handle_task 后改变。"""
        import task_handler
        caller_data = {"id": "t123", "message": {}}
        task_before = dict(caller_data)
        task_handler.handle_task(caller_data)
        assert caller_data != task_before
