"""A2A Task Handler — forward Tasks to Hermes agent loop.

Execution strategy (T2 后):
1. 所有 profile 默认走 API Server (端口由 port_resolver.api_server_port 公式决定)
2. API Server 连接错误 → 透明 fallback 到 subprocess (hermes chat -q --profile <name>)

Result-classification keyword bank is externalised (P1-13):
    Priority: env A2A_CLASSIFY_KEYWORDS (path) > <hermes_home>/a2a-classify-keywords.json
              > built-in defaults below.
    JSON shape: {"tool_unavailable": [...], "task_achieved": [...]}
"""

import json, logging, os, shutil, sqlite3, subprocess, time, urllib.request, urllib.error
from datetime import datetime, timezone
from pathlib import Path

from identity import load_identity_prefix
from port_resolver import api_server_port as _resolve_api_port

try:
    from skill_resolver import resolve_skills, to_env as _skills_to_env
except ImportError:  # resolver may be absent in pre-P0-2 deployments
    resolve_skills = None
    _skills_to_env = None

try:
    from comment_kind_backfill import backfill as _comment_kind_backfill
except ImportError:  # bridge may be absent in pre-P1-A deployments
    _comment_kind_backfill = None

logger = logging.getLogger("hermes-a2a.task_handler")

# Bounded per-tick sweep for DCI bypass-table backfill (P1-A).
# Keeps the post-task hook predictable on a busy kanban.
_BACKFILL_SWEEP_LIMIT = 100

# Profile → API Server port 现由 port_resolver 公式动态决定（详见 _api_server_port）.
_API_TIMEOUT = 300

_API_SERVER_KEY_CACHE: str | None = None  # populated on first use; "" if absent


def _api_server_port(profile: str) -> int:
    """Resolve API Server port for any profile via the canonical formula."""
    return _resolve_api_port(profile)


def _api_server_key() -> str:
    """Resolve the API Server Bearer token.

    Priority:
        1. env API_SERVER_KEY (set by parent process)
        2. <HERMES_HOME>/.env  (profile-local .env)
        3. ~/.hermes/.env      (global Hermes env)

    Returns "" when no key is configured (caller should then skip the header,
    matching the API server's `not self._api_key` no-auth branch).
    """
    global _API_SERVER_KEY_CACHE
    if _API_SERVER_KEY_CACHE is not None:
        return _API_SERVER_KEY_CACHE

    key = os.environ.get("API_SERVER_KEY", "").strip()
    if not key:
        candidates: list[Path] = []
        hermes_home = os.environ.get("HERMES_HOME")
        if hermes_home:
            candidates.append(Path(hermes_home) / ".env")
        # Profile envs live under ~/.hermes/profiles/<profile>/.env; the
        # canonical global key file is ~/.hermes/.env (one level up).
        candidates.append(Path(os.path.expanduser("~/.hermes/.env")))
        for env_path in candidates:
            if not env_path.is_file():
                continue
            try:
                for line in env_path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    if k.strip() == "API_SERVER_KEY":
                        key = v.strip().strip('"').strip("'")
                        if key:
                            break
            except OSError:
                continue
            if key:
                break
    _API_SERVER_KEY_CACHE = key
    return key

# ── Result classification ──────────────────────────────────────────────
# Heuristic signals in the agent's final response text.
# Scoped to send_message / delivery failures — NOT general "can't do X" analysis.
# Ordered: tool_unavailable checked FIRST (degraded > succeeded).
_DEFAULT_RESULT_SIGNALS: dict[str, list[str]] = {
    "tool_unavailable": [
        # English — send_message specific
        "unable to send", "cannot send", "can't send",
        "unable to deliver", "could not send", "failed to send",
        "conflict",  # Telegram polling conflict
        # Chinese — 发送特定
        "无法发送", "发送失败", "发送不了", "发不了",
        "发不出去", "无法投递",
    ],
    "task_achieved": [
        # English
        "sent", "delivered", "successful",
        "successfully",
        # Chinese
        "已发送", "已完成", "发送成功", "成功发送",
        "已送达", "已投递", "已发到", "已发出", "已发至",
    ],
}

_KEYWORDS_FILE_NAME = "a2a-classify-keywords.json"
_KEYWORDS_ENV_VAR = "A2A_CLASSIFY_KEYWORDS"
_RESULT_SIGNALS_CACHE: dict[str, list[str]] | None = None  # populated on first use


def _load_signals() -> dict[str, list[str]]:
    """Return the active keyword bank (cached after first call).

    Priority:
        1. env A2A_CLASSIFY_KEYWORDS → JSON file path
        2. <HERMES_HOME>/a2a-classify-keywords.json
        3. built-in _DEFAULT_RESULT_SIGNALS

    Malformed JSON falls back to defaults with a warning.  Unknown buckets
    are accepted (forward-compat for new categories); only ``tool_unavailable``
    and ``task_achieved`` are read by ``_classify``.
    """
    global _RESULT_SIGNALS_CACHE
    if _RESULT_SIGNALS_CACHE is not None:
        return _RESULT_SIGNALS_CACHE

    candidate: Path | None = None
    env_path = os.environ.get(_KEYWORDS_ENV_VAR, "").strip()
    if env_path:
        p = Path(env_path).expanduser()
        if p.is_file():
            candidate = p
    if candidate is None:
        home = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
        fallback = Path(home) / _KEYWORDS_FILE_NAME
        if fallback.is_file():
            candidate = fallback

    if candidate is None:
        _RESULT_SIGNALS_CACHE = {k: list(v) for k, v in _DEFAULT_RESULT_SIGNALS.items()}
        return _RESULT_SIGNALS_CACHE

    try:
        data = json.loads(candidate.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("classify keywords file must be a JSON object")
        loaded: dict[str, list[str]] = {}
        for bucket, items in data.items():
            if not isinstance(items, list):
                continue
            cleaned = [s for s in items if isinstance(s, str) and s.strip()]
            if cleaned:
                loaded[bucket] = cleaned
        # Ensure both default buckets present (per-bucket fallback).
        for k, default_vals in _DEFAULT_RESULT_SIGNALS.items():
            if k not in loaded:
                loaded[k] = list(default_vals)
        _RESULT_SIGNALS_CACHE = loaded
        logger.info(
            "[hermes-a2a] classify keywords loaded from %s (%d buckets)",
            candidate, len(loaded),
        )
    except Exception as e:
        logger.warning(
            "[hermes-a2a] classify keywords file %s unreadable (%s); using defaults",
            candidate, e,
        )
        _RESULT_SIGNALS_CACHE = {k: list(v) for k, v in _DEFAULT_RESULT_SIGNALS.items()}

    return _RESULT_SIGNALS_CACHE


def _classify(status: str, response: str, error: str = "") -> dict:
    """Return semantic_status + completion_reason for the task result.

    semantic_status  ∈ {succeeded, degraded, failed}
    completion_reason ∈ {task_achieved, tool_unavailable, agent_error, timeout, unknown}
    """
    if status == "failed":
        if error and "timeout" in error.lower():
            return {"semantic_status": "failed", "completion_reason": "timeout"}
        return {"semantic_status": "failed", "completion_reason": "agent_error"}

    if status == "completed" and not response.strip():
        return {"semantic_status": "degraded", "completion_reason": "empty_response"}

    r = response.lower()
    signals = _load_signals()

    # degraded trumps succeeded — check first
    for sig in signals.get("tool_unavailable", []):
        if sig in r:
            return {"semantic_status": "degraded", "completion_reason": "tool_unavailable"}

    for sig in signals.get("task_achieved", []):
        if sig in r:
            return {"semantic_status": "succeeded", "completion_reason": "task_achieved"}

    return {"semantic_status": "succeeded", "completion_reason": "unknown"}


def _resolve_skill_env(profile: str, task: dict) -> tuple[dict[str, str], list]:
    """Build env dict that propagates per-task --skill names through to the worker.

    Contract (matches tdd-test-plan.md §2.5 / skill_resolver.to_env):
        HERMES_TASK_SKILLS         comma-separated effective skill names
        HERMES_SKILL_SOURCE_LAYERS comma-separated <name>:<layer>[:<owner>]

    A2A clients pass per-task skills as `task["skills"]` (list[str]) in the
    POST body. We merge with the profile's dept defaults via skill_resolver
    and return (env_vars, resolved_skills).

    Returns ({}, []) when resolution fails or no skills requested.
    """
    if resolve_skills is None or _skills_to_env is None:
        return {}, []
    requested = task.get("skills") or []
    if not isinstance(requested, list):
        return {}, []
    try:
        resolved = resolve_skills(profile=profile, per_task=requested)
    except Exception as e:
        logger.warning("[hermes-a2a] skill resolver failed for %s: %s", profile, e)
        return {}, []
    if not resolved:
        return {}, []
    return _skills_to_env(resolved), resolved


def _ensure_comment_kind_backfill(task_id: str | None = None) -> dict | None:
    """Classify+record unclassified comments into the DCI bypass table.

    Wired into ``handle_task`` so each A2A tick advances the bypass table —
    agents write free-text via ``kanban_comment`` (touches task_comments
    only) and the orchestrator needs ``a2a_comment_kinds`` populated to
    route on DCI kinds.

    - Task scope first (so the just-completed task is classified immediately).
    - Then a bounded global sweep (limit=100) for catch-up.
    - Idempotent: ``backfill`` LEFT JOINs out rows that already have a kind.
    - Best-effort: any sqlite/import error → silent no-op.
    """
    if _comment_kind_backfill is None:
        return None
    # Kanban is a cross-profile primitive: comments written via `kanban_comment`
    # always land in the canonical ~/.hermes/kanban.db. A2A workers run with
    # HERMES_HOME pointing at their per-profile scratch dir
    # (~/.hermes/profiles/<profile>) where no kanban.db lives. Prefer the
    # profile path when it exists (backward compat / per-profile deploys);
    # otherwise fall back to the global file the CLI uses.
    home = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
    db_path = Path(home) / "kanban.db"
    if not db_path.is_file():
        db_path = Path(os.path.expanduser("~/.hermes/kanban.db"))
    if not db_path.is_file():
        return None
    try:
        conn = sqlite3.connect(str(db_path), timeout=30)
    except sqlite3.Error as e:
        logger.warning("[hermes-a2a] backfill: cannot open %s: %s", db_path, e)
        return None
    classified = 0
    defaulted = 0
    skipped = 0
    by_kind: dict[str, int] = {}
    try:
        if task_id:
            try:
                r = _comment_kind_backfill(conn, task_id=task_id)
                classified += r.classified
                defaulted += r.defaulted
                skipped += r.skipped
                for k, n in r.by_kind.items():
                    by_kind[k] = by_kind.get(k, 0) + n
            except sqlite3.OperationalError as e:
                # Bypass table missing — migration not applied here.
                logger.debug("[hermes-a2a] backfill task=%s skipped: %s", task_id, e)
                return None
            except sqlite3.Error as e:
                logger.warning("[hermes-a2a] backfill task=%s: %s", task_id, e)
        try:
            r = _comment_kind_backfill(conn, limit=_BACKFILL_SWEEP_LIMIT)
            classified += r.classified
            defaulted += r.defaulted
            skipped += r.skipped
            for k, n in r.by_kind.items():
                by_kind[k] = by_kind.get(k, 0) + n
        except sqlite3.OperationalError as e:
            logger.debug("[hermes-a2a] backfill sweep skipped: %s", e)
            return None
        except sqlite3.Error as e:
            logger.warning("[hermes-a2a] backfill sweep: %s", e)
            return None
        return {
            "classified": classified,
            "defaulted": defaulted,
            "skipped": skipped,
            "by_kind": by_kind,
        }
    finally:
        conn.close()


def handle_task(task: dict) -> dict:
    if not isinstance(task, dict):
        return {"id": "unknown", "status": "failed", "error": "Invalid task: expected object"}

    task = dict(task)
    tid = task.get("id", "unknown")
    msg = task.get("message") or task.get("input") or task.get("action") or {}
    if isinstance(msg, str):
        prompt = msg
    elif isinstance(msg, dict):
        prompt = msg.get("text") or msg.get("prompt") or _extract_from_parts(msg.get("parts", []))
    else:
        prompt = ""
    if not prompt:
        task["status"] = "failed"
        task["error"] = "Empty message"
        return task

    profile = os.environ.get("HERMES_PROFILE", "")
    hermes_home = os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))

    # Identity prefix is owned by the deploying business — loaded from
    # env var / profile file / generic fallback.  See core/identity.py.
    if not prompt.startswith("【系统提示】"):
        identity_prefix = load_identity_prefix(hermes_home, profile)
        prompt = identity_prefix + prompt

    try:
        # T2: 所有 profile 默认走 API Server；连接错误时 _via_api_server 内部
        # 透明 fallback 到 _via_subprocess.
        result = _via_api_server(task, tid, prompt, profile)
    except Exception as e:
        task["status"] = "failed"
        task["error"] = str(e)
        result = task

    # P1-A: post-tick DCI bypass-table backfill. Bounded + best-effort.
    _ensure_comment_kind_backfill(task_id=tid if tid != "unknown" else None)
    return result


def _via_api_server(task: dict, tid: str, prompt: str, profile: str) -> dict:
    """Execute task via Hermes /v1/runs API (thin adapter mode)."""
    task = dict(task)
    port = _api_server_port(profile)
    start = time.time()

    # Create run
    body = json.dumps({"input": prompt, "model": "hermes-agent"}).encode()
    headers = {"Content-Type": "application/json"}
    api_key = _api_server_key()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/runs",
        data=body,
        headers=headers,
        method="POST",
    )
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        run = json.loads(resp.read())
        run_id = run.get("run_id") or run.get("id")
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
        logger.warning(f"[hermes-a2a] API Server {profile}:{port} unreachable, falling back to subprocess: {e}")
        return _via_subprocess(task, tid, prompt, profile)

    # Poll for completion
    deadline = start + _API_TIMEOUT
    poll_headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    while time.time() < deadline:
        time.sleep(1)
        try:
            poll_req = urllib.request.Request(
                f"http://127.0.0.1:{port}/v1/runs/{run_id}",
                headers=poll_headers,
                method="GET",
            )
            resp = urllib.request.urlopen(poll_req, timeout=5)
            run = json.loads(resp.read())
            status = run.get("status", "")
            if status in ("completed", "failed", "cancelled"):
                task["status"] = "completed" if status == "completed" else "failed"
                output = run.get("output") or run.get("response", "")
                if isinstance(output, list):
                    output = "\n".join(
                        m.get("content", "") for m in output
                        if isinstance(m, dict) and m.get("type") == "message"
                    )
                output_str = str(output)
                cls = _classify(task["status"], output_str, task.get("error", ""))
                task["semantic_status"] = cls["semantic_status"]
                task["completion_reason"] = cls["completion_reason"]
                task["artifact"] = {
                    "response": output_str,
                    "fallback_text": output_str,
                    "duration_s": round(time.time() - start, 2),
                    "run_id": run_id,
                    "mode": "api_server",
                }
                return task
        except (urllib.error.URLError, OSError, json.JSONDecodeError):
            continue

    task["status"] = "failed"
    task["error"] = f"Timeout after {_API_TIMEOUT}s"
    return task


def _hermes_bin() -> str:
    """Resolve hermes CLI path. Check PATH first, then common install locations."""
    hermes = shutil.which("hermes")
    if hermes:
        return hermes
    candidates = [
        os.path.expanduser("~/.hermes/hermes-agent/venv/bin/hermes"),
        os.path.expanduser("~/.hermes/venv/bin/hermes"),
        "/opt/homebrew/bin/hermes",
        "/usr/local/bin/hermes",
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return "hermes"  # fallback — will fail with clear error


def _ensure_m2cl_symlinks(resolved: list, profile: str) -> None:
    """Symlink dept-other (M2CL cross-dept) skills into the worker's skills dir.

    Hermes CLI --skills only searches the profile's own skills directory.
    When the skill_resolver locates a skill via DEPT_OTHER (cross-dept loading),
    we create a symlink so the hermes subprocess can find it.

    Symlinks are idempotent and harmless if stale — they just point to the
    real skill directory in jz-skills.
    """
    if not resolved:
        return
    hermes_home = os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))
    skills_dir = Path(hermes_home) / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)

    from skill_resolver import SkillSource
    for sk in resolved:
        if getattr(sk, 'source_layer', None) != SkillSource.DEPT_OTHER:
            continue
        target = skills_dir / sk.name
        src = sk.path
        if target.is_symlink() or target.exists():
            continue  # already exists — skip
        try:
            target.symlink_to(src)
            logger.info("[hermes-a2a] M2CL symlink: %s → %s", target, src)
        except OSError as e:
            logger.warning("[hermes-a2a] M2CL symlink failed for %s: %s", sk.name, e)


def _via_subprocess(task: dict, tid: str, prompt: str, profile: str) -> dict:
    """Execute task via hermes chat subprocess (fallback mode)."""
    task = dict(task)
    start = time.time()
    cmd = [_hermes_bin(), "chat", "-q", prompt, "--quiet"]
    if profile:
        cmd += ["--profile", profile]
    # Pass parent env + ensure API keys are available from the main .env
    env = os.environ.copy()
    main_env = os.path.expanduser("~/.hermes/.env")
    if os.path.isfile(main_env):
        for line in open(main_env):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                if k not in env:
                    env[k] = v.strip().strip('"').strip("'")
    # P0-2: inject per-task skill env so the worker loads dept + per-task skills
    skill_env, resolved = _resolve_skill_env(profile, task)
    if skill_env:
        env.update(skill_env)
        # Also forward as --skills to hermes chat (CLI contract)
        cmd += ["--skills", skill_env["HERMES_TASK_SKILLS"]]
        # P0-2 M2CL symlink: for dept-other skills, symlink into worker's skills dir
        # so hermes CLI can find them (it only searches profile skills directory).
        _ensure_m2cl_symlinks(resolved, profile)
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=480, env=env)
    output = r.stdout.strip() or r.stderr.strip()
    task["status"] = "completed" if r.returncode == 0 else "failed"
    cls = _classify(task["status"], output, task.get("error", ""))
    task["semantic_status"] = cls["semantic_status"]
    task["completion_reason"] = cls["completion_reason"]
    task["artifact"] = {
        "response": output,
        "fallback_text": output,
        "duration_s": round(time.time() - start, 2),
        "mode": "subprocess",
    }
    return task

def _extract_from_parts(parts: list) -> str:
    if not isinstance(parts, list):
        return ""
    chunks: list[str] = []
    for p in parts:
        if not isinstance(p, dict):
            continue
        kind = p.get("type")
        if kind == "text":
            text = p.get("text", "")
            if text:
                chunks.append(str(text))
        elif kind in {"image", "audio", "file", "tool_use"}:
            label = p.get("name") or p.get("url") or p.get("mimeType") or "attachment"
            chunks.append(f"[{kind}: {label}]")
    return "\n".join(chunks)
