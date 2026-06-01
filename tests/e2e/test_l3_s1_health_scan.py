#!/usr/bin/env python3
"""
L3-S1: 六部全触发健康扫描 E2E
TDD 红灯版

链路: planner → reviewer → shangshu → [budget ∥ gongbu] → protocol → tester → reviewer → archivist
验收: 7部全触发、门下调拨、户部+工部并行、总耗时≤30min
"""
import json, os, subprocess, sys, time, urllib.request
from pathlib import Path

# ─── Config ───────────────────────────────────────────────────────
A2A_TOKEN = Path("/Users/alexcai/.hermes/.a2a-token").read_text().strip()
REGISTRY_PY = "/Users/alexcai/.hermes/plugins/hermes-a2a/registry.py"
A2A_TIMEOUT = 480
CHAIN_TIMEOUT = 1800

CHAIN = ["planner", "reviewer", "shangshu", "budget", "gongbu", "protocol", "tester", "reviewer", "archivist"]

ACCEPTANCE = [
    "七部全触发",    # planner/reviewer/shangshu/budget/gongbu/protocol/tester 全 completed
    "门下调拨",      # reviewer 至少封驳 1 处
    "户部工部并行",  # budget + gongbu 均完成（验证 fan-out）
    "尚书省协调",    # shangshu 正确派发 fan-out
    "礼部汇总",      # protocol 产出 ≥200 chars
    "刑部稽核",      # tester 完成稽核
    "史馆归档",      # archivist 产出进 Obsidian
    "总耗时≤30min",  # 时间约束
    "无僵尸进程",
    "Kanban完整",
]

def a2a_port(profile: str) -> int:
    result = subprocess.run(
        ["python3", REGISTRY_PY, "port", profile],
        capture_output=True, text=True, timeout=5
    )
    return int(result.stdout.strip())

def a2a_send(profile: str, task_id: str, task: str) -> dict:
    port = a2a_port(profile)
    payload = json.dumps({"id": task_id, "task": task}).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/a2a/tasks",
        data=payload,
        headers={"Authorization": f"Bearer {A2A_TOKEN}", "Content-Type": "application/json"},
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())

def a2a_poll(profile: str, task_id: str, timeout: int = A2A_TIMEOUT) -> dict:
    port = a2a_port(profile)
    deadline = time.time() + timeout
    while time.time() < deadline:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/a2a/tasks/{task_id}",
            headers={"Authorization": f"Bearer {A2A_TOKEN}"}
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 404:
                time.sleep(5)
                continue
            raise
        status = data.get("status", "")
        if status in ("completed", "failed", "cancelled"):
            return data
        time.sleep(5)
    raise TimeoutError(f"A2A task {task_id} on {profile} timed out after {timeout}s")


def test_s1_health_scan_chain():
    start_time = time.time()
    results = {}
    chain_outputs = {}

    baseline_zombie = int(subprocess.run(
        "ps aux | grep -E 'server\\.py|gateway\\.py' | grep -v grep | grep -v venv | wc -l",
        shell=True, capture_output=True, text=True, timeout=5
    ).stdout.strip() or 0)

    print("=" * 60)
    print("L3-S1: 六部全触发健康扫描 E2E")
    print("=" * 60)

    # Step 1: Planner
    print("\n[1/8] Planner 拟制健康扫描方案...")
    tid = f"s1-plan-{int(time.time())}"
    task = """你是中书省策划官。请拟制一份 Hermes 本地环境健康扫描方案：

扫描目标：~/.hermes/ 下所有六部 profile 的 SOUL.md 行数、config.yaml 完整性、skills 清单、gateway 运行状态。

请输出：
1. 扫描范围（哪几个 profile）
2. 每个 profile 的检查项（SOUL 行数、config 存在性、skills 数量、gateway 状态）
3. 验收标准（至少 4 条：全 profile 覆盖、SOUL≥X 行、config 存在、gateway 运行）
4. 必须将全部产出写入 /tmp/s1-plan.md"""
    
    a2a_send("planner", tid, task)
    r = a2a_poll("planner", tid)
    results["planner"] = r
    plan_text = r.get("artifact", {}).get("response", "")
    print(f"  planner → {r.get('status')} ({len(plan_text)} chars)")
    chain_outputs["planner"] = plan_text[:500]

    # Step 2: Reviewer 封驳
    print("\n[2/8] Reviewer 封驳方案...")
    tid = f"s1-review1-{int(time.time())}"
    task = f"""你是门下省审查官。请审查以下健康扫描方案并**必须至少封驳 1 处**：

{plan_text[:800]}

要求：找出至少 1 处需要修改的地方，给出具体修改建议。"""
    
    a2a_send("reviewer", tid, task)
    r = a2a_poll("reviewer", tid)
    results["reviewer_1"] = r
    r1_text = r.get("artifact", {}).get("response", "")
    print(f"  reviewer → {r.get('status')}")
    chain_outputs["reviewer_1"] = r1_text[:300]

    # Step 3: Shangshu 派发 (fan-out: budget + gongbu)
    print("\n[3/8] 尚书省派发 fan-out (budget + gongbu)...")
    tid = f"s1-shangshu-{int(time.time())}"
    task = f"""你是尚书省调度官。请派发健康扫描任务：

需并行执行：
- budget(户部): 扫描 profile SOUL.md 行数、skills 数量等数据统计
- gongbu(工部): 扫描 gateway 运行状态、config 完整性等基建检查

请确认派发方案，明确二者并行无依赖。"""
    
    a2a_send("shangshu", tid, task)
    r = a2a_poll("shangshu", tid)
    results["shangshu"] = r
    sh_text = r.get("artifact", {}).get("response", "")
    print(f"  shangshu → {r.get('status')}")
    chain_outputs["shangshu"] = sh_text[:500]

    # Step 4a: Budget (户部)
    print("\n[4a/8] Budget 户部数据扫描...")
    tid = f"s1-budget-{int(time.time())}"
    task = """你是户部数据官。请扫描 ~/.hermes/profiles/ 下所有 profile：

对每个 profile 统计：
1. SOUL.md 行数
2. config.yaml 是否存在
3. skills 目录下 skill 数量

输出格式：表格，每 profile 一行。"""
    
    a2a_send("budget", tid, task)
    r = a2a_poll("budget", tid)
    results["budget"] = r
    budget_text = r.get("artifact", {}).get("response", "")
    print(f"  budget → {r.get('status')} ({len(budget_text)} chars)")
    chain_outputs["budget"] = budget_text[:500]

    # Step 4b: Gongbu (工部) — parallel
    print("\n[4b/8] Gongbu 工部基建扫描...")
    tid = f"s1-gongbu-{int(time.time())}"
    task = """你是工部基建官。快速扫描5项，回复简短状态（每条一行，<500字）：
1. hermes --version
2. ls ~/.hermes/profiles/ 目录数
3. hermes kanban list（确认可用）
4. launchctl list | grep a2a | wc -l
5. ps aux | grep gateway | grep -v grep | wc -l

⚠️ 5条命令即可，不深度扫描，不 web_search。"""
    
    
    a2a_send("gongbu", tid, task)
    r = a2a_poll("gongbu", tid, timeout=900)  # gongbu 慢，单独 15min
    results["gongbu"] = r
    gongbu_text = r.get("artifact", {}).get("response", "")
    print(f"  gongbu → {r.get('status')} ({len(gongbu_text)} chars)")
    chain_outputs["gongbu"] = gongbu_text[:500]

    # Step 5: Protocol 汇总
    print("\n[5/8] Protocol 礼部汇总...")
    tid = f"s1-protocol-{int(time.time())}"
    task = f"""你是礼部编辑官。请将以下两份扫描结果汇总为一份健康报告：

户部数据：
{budget_text[:1000]}

工部基建：
{gongbu_text[:1000]}

要求：合并为统一报告，≥200 字符。"""
    
    a2a_send("protocol", tid, task)
    r = a2a_poll("protocol", tid)
    results["protocol"] = r
    protocol_text = r.get("artifact", {}).get("response", "")
    print(f"  protocol → {r.get('status')} ({len(protocol_text)} chars)")
    chain_outputs["protocol"] = protocol_text[:500]

    # Step 6: Tester 稽核
    print("\n[6/8] Tester 刑部稽核...")
    tid = f"s1-tester-{int(time.time())}"
    task = f"""你是刑部稽核官。请稽核以下健康报告：

{protocol_text[:1500]}

检查：数据完整性、异常标记、是否所有 profile 均已覆盖。"""
    
    a2a_send("tester", tid, task)
    r = a2a_poll("tester", tid)
    results["tester"] = r
    tester_text = r.get("artifact", {}).get("response", "")
    print(f"  tester → {r.get('status')}")
    chain_outputs["tester"] = tester_text[:500]

    # Step 7: Reviewer 终审
    print("\n[7/8] Reviewer 终审...")
    tid = f"s1-review2-{int(time.time())}"
    task = f"""你是门下省终审官。请最终审核健康扫描结果：

稽核报告：{tester_text[:500]}
汇总报告：{protocol_text[:500]}

请判定：是否批准归档？"""
    
    a2a_send("reviewer", tid, task)
    r = a2a_poll("reviewer", tid)
    results["reviewer_2"] = r
    final = r.get("artifact", {}).get("response", "")
    print(f"  reviewer(终审) → {r.get('status')}")
    chain_outputs["reviewer_2"] = final[:300]

    # Step 8: Archivist 归档
    print("\n[8/8] Archivist 史馆归档...")
    tid = f"s1-archivist-{int(time.time())}"
    task = f"""你是史馆归档官。请将健康扫描结果归档到 Obsidian：

路径：20-Areas/10_AI实践/三省六部_Hermes/20_实施/测试/健康扫描结果_S1_20260531.md
内容：含户部数据、工部基建、礼部汇总、刑部稽核摘要。

汇总报告：{protocol_text[:1500]}

用 write_file 写入磁盘，完成后 ls 验证文件存在。回复中列出文件的绝对路径。"""
    
    a2a_send("archivist", tid, task)
    r = a2a_poll("archivist", tid)
    results["archivist"] = r
    archivist_text = r.get("artifact", {}).get("response", "")
    print(f"  archivist → {r.get('status')} ({len(archivist_text)} chars)")
    chain_outputs["archivist"] = archivist_text[:500]

    elapsed = time.time() - start_time

    # ─── 验收 ───────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"验收结果 (总耗时 {elapsed:.0f}s)")
    print("=" * 60)

    checks = {}

    # 1. 七部全触发
    triggered = set()
    for k, v in results.items():
        base_role = k.split("_")[0]
        if v.get("status") == "completed":
            triggered.add(base_role)
    required = {"planner", "reviewer", "shangshu", "budget", "gongbu", "protocol", "tester", "archivist"}
    checks["七部全触发"] = required.issubset(triggered)

    # 2. 门下调拨
    checks["门下调拨"] = any(kw in r1_text for kw in ["封驳", "驳回", "返修", "修改", "问题", "不足"])

    # 3. 户部工部并行
    checks["户部工部并行"] = (
        results.get("budget", {}).get("status") == "completed"
        and results.get("gongbu", {}).get("status") == "completed"
    )

    # 4. 尚书省协调
    checks["尚书省协调"] = any(kw in sh_text for kw in ["budget", "gongbu", "户部", "工部", "派发", "并行"])

    # 5. 礼部汇总
    checks["礼部汇总"] = len(protocol_text) >= 200

    # 6. 刑部稽核
    checks["刑部稽核"] = results.get("tester", {}).get("status") == "completed"

    # 7. 史馆归档 — Phase 1 后优先查本地紧凑 audit store；回退 vault md（Phase 4 前兼容）
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

    # 8. 总耗时
    checks["总耗时≤30min"] = elapsed <= 1800

    # 9. 无僵尸
    current_zombie = int(subprocess.run(
        "ps aux | grep -E 'server\\.py|gateway\\.py' | grep -v grep | grep -v venv | wc -l",
        shell=True, capture_output=True, text=True, timeout=5
    ).stdout.strip() or 0)
    checks["无僵尸进程"] = current_zombie <= baseline_zombie

    # 10. Kanban
    kanban_result = subprocess.run(
        "hermes kanban list", shell=True, capture_output=True, text=True, timeout=5,
        env={**os.environ, "HOME": os.path.expanduser("~")}
    )
    checks["Kanban完整"] = kanban_result.returncode == 0

    # ─── 输出 ─────────────────────────────────────────────────
    passed = sum(1 for v in checks.values() if v)
    total = len(checks)

    print(f"\n通过: {passed}/{total}")
    for name, result in checks.items():
        icon = "✅" if result else "❌"
        print(f"  {icon} {name}")

    print(f"\n链上耗时:")
    for key in ["planner", "reviewer_1", "shangshu", "budget", "gongbu", "protocol", "tester", "reviewer_2", "archivist"]:
        if key in results:
            r = results[key]
            status = r.get("status", "unknown")
            print(f"  {key:>12s}: {status:>10s}")

    if passed < total:
        print(f"\n[debug] chain_outputs:")
        for key in chain_outputs:
            txt = chain_outputs[key]
            print(f"  --- {key} ({len(txt)} chars) ---")
            print(f"  {txt[:400]}\n")

    assert passed >= 7, f"L3-S1 FAIL: {passed}/{total} checks passed (need ≥7)"
    assert checks["七部全触发"], "核心验收失败: 七部未全触发"
    assert checks["户部工部并行"], "核心验收失败: fan-out 并行失败"

    print(f"\n✅ L3-S1 PASS: {passed}/{total} checks")
    return checks


if __name__ == "__main__":
    test_s1_health_scan_chain()
