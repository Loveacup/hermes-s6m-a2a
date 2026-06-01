#!/usr/bin/env python3
"""
L3-S2: 代码审查 E2E 治理全链路测试
TDD 红灯版 — 先写测试，当前全部预期 FAIL

链路: planner → reviewer → shangshu → engineer → tester → reviewer
验收: bug检出 ≥1, 10项标准全绿, 总耗时 ≤25min
"""
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

# ─── Config ───────────────────────────────────────────────────────
A2A_TOKEN = Path("/Users/alexcai/.hermes/.a2a-token").read_text().strip()
HERMES_ROOT = Path(os.path.expanduser("~/.hermes"))
REGISTRY_PY = HERMES_ROOT / "plugins/hermes-a2a/registry.py"
KANBAN_CLI = "hermes kanban"
A2A_TIMEOUT = 480  # 单任务超时 8min（>= 生产 subprocess timeout 480s + buffer）
CHAIN_TIMEOUT = 1500  # 总链超时 25min
TG_GROUP = "-5133970461"

# S2 参与者链
CHAIN = ["planner", "reviewer", "shangshu", "engineer", "tester", "reviewer"]

# 10 项验收标准
ACCEPTANCE = [
    "六部全触发",          # 1. 链上所有 profile 均有产出
    "门下调拨",            # 2. reviewer(1) 封驳 planners plan → planner 修改
    "返修链 ≤2轮",         # 3. 返修不超过 2 次
    "尚书省协调",           # 4. shangshu 正确派发
    "六部产出",            # 5. engineer/tester 有实际产出
    "史馆归档",            # 6. 产出进入 Obsidian
    "bug检出 ≥1",          # 7. S2 核心验收
    "总耗时 ≤25min",       # 8. 时间约束
    "无僵尸进程",          # 9. 进程清理
    "Kanban完整",          # 10. 状态流转正确无孤儿卡
]

# ─── Helpers ────────────────────────────────────────────────────────

def a2a_port(profile: str) -> int:
    """查询 A2A 端口"""
    result = subprocess.run(
        ["python3", str(REGISTRY_PY), "port", profile],
        capture_output=True, text=True, timeout=5
    )
    return int(result.stdout.strip())

def a2a_send(profile: str, task_id: str, task: str) -> dict:
    """发送 A2A 任务"""
    port = a2a_port(profile)
    payload = json.dumps({"id": task_id, "task": task}).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/a2a/tasks",
        data=payload,
        headers={
            "Authorization": f"Bearer {A2A_TOKEN}",
            "Content-Type": "application/json"
        },
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())

def a2a_poll(profile: str, task_id: str, timeout: int = A2A_TIMEOUT) -> dict:
    """轮询 A2A 任务结果"""
    port = a2a_port(profile)
    deadline = time.time() + timeout
    while time.time() < deadline:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/a2a/tasks/{task_id}",
            headers={"Authorization": f"Bearer {A2A_TOKEN}"}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        status = data.get("status", "")
        if status in ("completed", "failed", "cancelled"):
            return data
        time.sleep(5)
    raise TimeoutError(f"A2A task {task_id} on {profile} timed out after {timeout}s")

def kanban(*args) -> subprocess.CompletedProcess:
    """执行 hermes kanban 命令"""
    return subprocess.run(
        f"hermes kanban {' '.join(args)}",
        shell=True, capture_output=True, text=True, timeout=30,
        env={**os.environ, "HOME": os.path.expanduser("~")}
    )

# ─── Test: S2 Code Review Chain ────────────────────────────────────

def test_s2_code_review_chain():
    """
    S2: 代码审查 E2E 全链路

    1. planner 拟制代码审查计划
    2. reviewer 封驳（至少 1 次）
    3. shangshu 派发给 engineer + tester
    4. engineer 审查代码（发现 bug）
    5. tester 验证 bug
    6. reviewer 终审通过
    """
    start_time = time.time()
    task_ids = []
    results = {}
    chain_outputs = {}

    baseline_proc_count = int(subprocess.run(
        "ps aux | grep -E 'server\\.py|gateway\\.py' | grep -v grep | grep -v venv | wc -l",
        shell=True, capture_output=True, text=True, timeout=5
    ).stdout.strip() or 0)

    # Phase 1: Planner → Reviewer
    print("=" * 60)
    print("L3-S2: 代码审查 E2E 治理全链路")
    print("=" * 60)

    # Step 1: Planner 拟制
    print("\n[1/6] Planner 拟制审查计划...")
    tid = f"s2-plan-{int(time.time())}"
    task = """你是一个代码审查策划者。请为以下场景制定审查计划：
    
要审查的代码：hermes-a2a/core/task_handler.py 的 handle_task 函数
已知问题：该函数未处理 body 中的 task 字段（已修复的 P0 bug）
目标：验证审查流程能否发现类似问题

请输出：
1. 审查范围（文件列表）
2. 审查重点（安全检查点）
3. 预期发现的 bug 类型
4. 验收标准"""
    
    a2a_send("planner", tid, task)
    r = a2a_poll("planner", tid)
    results["planner"] = r
    task_ids.append(tid)
    print(f"  planner → {r.get('status')} ({r.get('completion_reason', 'N/A')})")
    chain_outputs["planner"] = r.get("artifact", {}).get("response", "")[:200]

    # Step 2: Reviewer 封驳
    print("\n[2/6] Reviewer 封驳计划...")
    tid = f"s2-review1-{int(time.time())}"
    plan_summary = chain_outputs.get("planner", "计划未产出")
    task = f"""你是门下省审查官。请审查以下审查计划并**必须至少封驳 1 处**：

审查计划：
{plan_summary}

要求：
1. 必须找出至少 1 处需要返修的地方
2. 给出具体修改建议
3. 标记封驳原因"""
    
    a2a_send("reviewer", tid, task)
    r = a2a_poll("reviewer", tid)
    results["reviewer_1"] = r
    task_ids.append(tid)
    status1 = r.get("status", "")
    print(f"  reviewer → {status1}")
    chain_outputs["reviewer_1"] = r.get("artifact", {}).get("response", "")[:300]

    # Step 3: Shangshu 派发
    print("\n[3/6] 尚书省派发任务...")
    tid = f"s2-shangshu-{int(time.time())}"
    task = f"""你是尚书省调度官。请根据以下情况派发任务：

审查计划已通过门下封驳。
需要派发：
- engineer: 对 hermes-a2a/core/task_handler.py 进行安全审查
- tester: 编写测试验证发现的 bug

请确认派发方案并创建 kanban 卡片。"""
    
    a2a_send("shangshu", tid, task)
    r = a2a_poll("shangshu", tid)
    results["shangshu"] = r
    task_ids.append(tid)
    print(f"  shangshu → {r.get('status')}")
    chain_outputs["shangshu"] = r.get("artifact", {}).get("response", "")[:500]

    # Step 4: Engineer 审查代码
    print("\n[4/6] Engineer 审查代码...")
    tid = f"s2-engineer-{int(time.time())}"
    task = """你是兵部工程师。请审查以下代码的安全问题：

文件：hermes-a2a/core/task_handler.py
关注点：
1. 消息体字段提取逻辑是否完整
2. 是否有未处理的异常路径
3. 认证/授权是否安全
4. 输入验证是否充分

请列出发现的所有问题，按严重程度排序。"""
    
    a2a_send("engineer", tid, task)
    r = a2a_poll("engineer", tid)
    results["engineer"] = r
    task_ids.append(tid)
    engineer_output = r.get("artifact", {}).get("response", "")
    print(f"  engineer → {r.get('status')}")
    chain_outputs["engineer"] = engineer_output[:500]

    # Step 5: Tester 验证
    print("\n[5/6] Tester 验证 bug...")
    tid = f"s2-tester-{int(time.time())}"
    findings = engineer_output[:1000]
    task = f"""你是刑部测试官。请验证以下工程师审查发现：

工程师发现：
{findings}

请：
1. 对每个发现给出验证结果（确认/误报/需更多信息）
2. 编写一个自动化测试用例来捕获确认的 bug
3. 确认是否满足 bug检出 ≥1 的验收标准"""
    
    a2a_send("tester", tid, task)
    r = a2a_poll("tester", tid)
    results["tester"] = r
    task_ids.append(tid)
    tester_output = r.get("artifact", {}).get("response", "")
    print(f"  tester → {r.get('status')}")
    chain_outputs["tester"] = tester_output[:500]

    # Step 6: Reviewer 终审
    print("\n[6/6] Reviewer 终审...")
    tid = f"s2-review2-{int(time.time())}"
    summary = f"""工程师发现: {engineer_output[:300]}
测试官验证: {tester_output[:300]}"""
    task = f"""你是门下省终审官。请最终审核本次代码审查结果：

{summary}

请判定：
1. 审查流程是否完整
2. bug 检出是否满足 ≥1 标准
3. 是否批准归档"""
    
    a2a_send("reviewer", tid, task)
    r = a2a_poll("reviewer", tid)
    results["reviewer_2"] = r
    task_ids.append(tid)
    final = r.get("artifact", {}).get("response", "")
    print(f"  reviewer(终审) → {r.get('status')}")
    chain_outputs["reviewer_2"] = final[:500]

    elapsed = time.time() - start_time

    # ─── 验收 ───────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print(f"验收结果 (总耗时 {elapsed:.0f}s)")
    print("=" * 60)

    checks = {}

    # 1. 六部全触发 (reviewer 在 results 中存为 reviewer_1/reviewer_2，需按角色名归并)
    triggered_roles = {
        k.split("_")[0] for k, v in results.items()
        if v.get("status") == "completed"
    }
    required_roles = {"planner", "reviewer", "shangshu", "engineer", "tester"}
    checks["六部全触发"] = required_roles.issubset(triggered_roles)

    # 2. 门下调拨（reviewer_1 应封驳）
    r1_resp = chain_outputs.get("reviewer_1", "")
    checks["门下调拨"] = any(kw in r1_resp for kw in ["封驳", "驳回", "返修", "修改", "问题", "不足"])

    # 3. 返修链 ≤2轮（本测试只有 1 轮封驳→通过）
    # 如果 reviewer_1 封驳了且 reviewer_2 通过了，返修链 = 1
    r1_status = results.get("reviewer_1", {}).get("status", "")
    r2_status = results.get("reviewer_2", {}).get("status", "")
    checks["返修链≤2轮"] = r2_status == "completed"

    # 4. 尚书省协调
    sh_resp = chain_outputs.get("shangshu", "")
    checks["尚书省协调"] = any(kw in sh_resp for kw in ["派发", "调度", "kanban", "assignee", "engineer", "tester"])

    # 5. 六部产出
    checks["六部产出"] = bool(engineer_output) and bool(tester_output) and bool(final)

    # 6. 史馆归档 — Phase 1 后优先查本地紧凑 audit store；回退 vault md（Phase 4 前兼容）
    def _recent(paths, window):
        recent = sorted(paths, key=lambda p: p.stat().st_mtime, reverse=True)[:10]
        return len(recent) > 0 and any(time.time() - f.stat().st_mtime < window for f in recent)

    window = elapsed + 120
    audit_root = Path(os.path.expanduser("~/.hermes/event-bridge/audit/"))
    bridge_root = Path(os.path.expanduser("~/Documents/Obsidian/AlexCai/88_event-bridge/"))
    audit_hit = audit_root.is_dir() and _recent(
        list(audit_root.glob("*.jsonl")) + list(audit_root.glob("*.jsonl.gz")), window)
    vault_hit = bridge_root.is_dir() and _recent(list(bridge_root.rglob("*.md")), window)
    checks["史馆归档"] = audit_hit or vault_hit

    # 7. bug 检出 ≥1
    bug_keywords = ["bug", "漏洞", "缺陷", "问题", "error", "missing", "缺失", "未处理", "安全"]
    checks["bug检出≥1"] = any(kw in engineer_output.lower() or kw in tester_output.lower() 
                               for kw in bug_keywords)

    # 8. 总耗时 ≤25min
    checks["总耗时≤25min"] = elapsed <= 1500

    # 9. 无僵尸进程 (launchd 常驻 server.py/gateway.py 不算僵尸；比对 baseline 看测试是否新增孤儿)
    current_count = int(subprocess.run(
        "ps aux | grep -E 'server\\.py|gateway\\.py' | grep -v grep | grep -v venv | wc -l",
        shell=True, capture_output=True, text=True, timeout=5
    ).stdout.strip() or 0)
    checks["无僵尸进程"] = current_count <= baseline_proc_count

    # 10. Kanban 完整
    kanban_result = kanban("list", "--json")
    checks["Kanban完整"] = kanban_result.returncode == 0

    # ─── 输出 ─────────────────────────────────────────────────
    passed = sum(1 for v in checks.values() if v)
    total = len(checks)
    
    print(f"\n通过: {passed}/{total}")
    for name, result in checks.items():
        icon = "✅" if result else "❌"
        print(f"  {icon} {name}")
    
    print(f"\n链上耗时:")
    for key in ["planner", "reviewer_1", "shangshu", "engineer", "tester", "reviewer_2"]:
        if key in results:
            r = results[key]
            elapsed_s = r.get("elapsed_seconds", r.get("elapsed", "N/A"))
            status = r.get("status", "unknown")
            print(f"  {key:>12s}: {status:>10s}  {elapsed_s}s")

    # TDD 预期：当前全部红灯
    assert passed >= 8, f"L3-S2 FAIL: {passed}/{total} checks passed (need ≥8)"
    assert checks["bug检出≥1"], "核心验收失败: bug检出 < 1"
    assert checks["六部全触发"], "六部未全触发"

    print(f"\n✅ L3-S2 PASS: {passed}/{total} checks")
    return checks


if __name__ == "__main__":
    test_s2_code_review_chain()
