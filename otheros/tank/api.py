"""tank — FastAPI mission control for disposable Claude Code sessions.

Each task gets a fresh tmux session running `claude --session-id <uuid>` (or
`--resume <uuid>` for continuations) in a sandboxed cwd. Per-task settings.json
registers hooks that POST lifecycle events back to this API. When the Stop hook
fires, we capture the last assistant response from the JSONL transcript and
kill the tmux session.
"""
from __future__ import annotations

import asyncio
import contextlib
import fcntl
import getpass
import io
import json
import os
import re
import pty
import shlex
import shutil
import signal
import socket
import sqlite3
import struct
import tarfile
import termios
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, File, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict
from sse_starlette.sse import EventSourceResponse

# ── Bootstrap config (env-only) ───────────────────────────────────────────────
# These are needed before the DB exists (they locate the DB) and/or only take
# effect at process start, so they live in env vars, not the GUI-editable
# config table. Override with TANK_INSTALL_DIR / TANK_PORT at deploy time.
# The service runs as a dedicated unprivileged user, so Path.home() resolves to
# that user's home — we never hardcode a username or home path.
INSTALL_DIR = Path(os.environ.get("TANK_INSTALL_DIR", "/opt/tank"))
PORT = int(os.environ.get("TANK_PORT", "7878"))
SERVICE_USER = os.environ.get("TANK_SERVICE_USER") or getpass.getuser()
# Set when this tank process is itself a *dev preview* of tank, launched by the
# real tank's preview feature (the tank project's dev_command exports it). A
# preview runs from a worktree with its own isolated DB but shares the user's
# tmux server, uploads dir, and ~/.claude.json with the real tank. It must NOT
# run the startup/background jobs that manage that shared state — most acutely
# reconcile_previews, which would see the `preview-…` tmux session it is running
# inside, fail to find it in its empty DB, and reap it (suicide), plus kill
# every other live preview. So in preview mode we skip reconciliation and the
# sweeper loops; the preview is for looking at the UI, not housekeeping.
PREVIEW_MODE = os.environ.get("TANK_PREVIEW_MODE", "").strip() == "1"

ROOT = INSTALL_DIR
DB_PATH = ROOT / "db.sqlite"
STATIC_DIR = ROOT / "static"
# Image attachments uploaded from the dashboard. One subdir per upload so each
# can be removed atomically. Lives outside any project cwd so a dropped image
# never gets accidentally git-add-ed. Swept by upload_sweeper_loop.
UPLOADS_ROOT = Path("/tmp/tank-uploads")
# Per-turn prompt files. The prompt is handed to claude as its positional CLI
# arg, but a long prompt can't ride inside the tmux `new-session` command string
# (tmux caps it at ~16KB → "command too long"). So we drop it in a file here and
# the spawned shell reads it into claude's argv (bounded by ARG_MAX, ~2MB) then
# deletes it. Lives outside any project cwd so it never gets git-add-ed.
PROMPTS_ROOT = Path("/tmp/tank-prompts")
UPLOAD_MAX_BYTES = 10 * 1024 * 1024
UPLOAD_ALLOWED_MIME = {
    "image/png", "image/jpeg", "image/gif", "image/webp",
}
UPLOAD_EXT_BY_MIME = {
    "image/png":  ".png",
    "image/jpeg": ".jpg",
    "image/gif":  ".gif",
    "image/webp": ".webp",
}
UPLOAD_TTL_SEC = 24 * 60 * 60
UPLOAD_SWEEP_INTERVAL_SEC = 60 * 60
# Names that are safe to use as a directory name when we derive the path
# from `name` — no slashes, no leading dots, conservative charset.
PROJECT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

# Optional projects seeded on first startup. Empty by default so a fresh
# install starts clean; populate via the dashboard. Each entry is
# {"name", "path", "description"}.
DEFAULT_PROJECTS: list[dict] = []

# ── Runtime config (GUI-editable, DB-backed) ──────────────────────────────────
# Defaults below are seeded into the `config` table on first boot. Each can be
# overridden at seed time with an env var named TANK_<KEY_UPPERCASE>, and edited
# live from the dashboard settings cog thereafter. cfg()/cfg_*() read these.
RUNTIME_CONFIG_DEFAULTS: dict[str, str] = {
    # Disposable cwds for chat-kind projects.
    "chats_root": "/tmp/tank-chat",
    # Per-task git worktrees for projects with isolate_tasks=1. Outside any
    # project tree so projects never see stray dirs.
    "worktrees_root": str(Path.home() / "tank-worktrees"),
    # Default parent directory for new projects created from the dashboard.
    "projects_root": str(Path.home() / "projects"),
    # Max concurrent task spawns (semaphore size; change needs a restart).
    "max_concurrent": "3",
    # Inclusive port range tank draws from when launching a task's live
    # "dev preview" (run the project's dev_command from the task's worktree on
    # a dedicated port). Format "lo-hi"; ports must be reachable on the box.
    "preview_port_range": "7900-7950",
    # AI title/todo-tidy features. Off by default — they need an
    # OpenAI-compatible chat-completions endpoint (title_model_url). When off
    # or unconfigured, tasks keep their placeholder titles.
    "ai_features_enabled": "0",
    "title_model_url": "",
    "title_model": "",
    # Git provider for the PR/merge task buttons: "none" (hidden) or "forgejo".
    "git_provider": "none",
    # Forgejo base URL + API token for the "flatten repo" feature (list repos
    # and download any repo flattened to a single text file). Independent of
    # git_provider, which only gates the per-task PR/merge buttons.
    "forgejo_url": "",
    "forgejo_token": "",
    # Dashboard heading + browser title.
    "dashboard_title": "tank",
    # Base URL the served HTML links house-style (design system) assets from.
    # Blank (the default) serves the committed copy under /static/house-style/,
    # so the repo carries no private hostname and works offline. Set it (e.g.
    # https://design.example.com/latest) to track a live design-system channel —
    # each tag still falls back to the vendored copy if that host is
    # unreachable. Also consumed by deploy.sh (via TANK_HOUSE_STYLE_URL) to
    # refresh the vendored copy at deploy time.
    "house_style_url": "",
    # Build-queue runner: how often (seconds) the background runner re-checks
    # the in-flight item's task status and pops the next eligible item. The
    # runner is also nudged immediately on start/resume, so this is just the
    # ceiling on how long a finished item waits before the next one spawns.
    "queue_poll_secs": "10",
    # Shared bearer token for the HTTP API. Empty = auth OFF (LAN-trust
    # default, the historical behaviour). When set, every request must present
    # the token (Authorization: Bearer <token>, X-API-Token, the tank_token
    # cookie, or ?token= for EventSource); a handful of paths stay open — see
    # api_token_guard. External programs use the Bearer header; the browser SPA
    # stores the token and rides the cookie.
    "api_token": "",
}

# In-memory mirror of the config table; refreshed on boot and after each PATCH.
_config_cache: dict[str, str] = {}


def load_config_cache() -> None:
    try:
        with db_conn() as c:
            rows = c.execute("SELECT key, value FROM config").fetchall()
    except sqlite3.OperationalError:
        return  # table not created yet
    _config_cache.clear()
    _config_cache.update({r["key"]: r["value"] for r in rows})


def cfg(key: str) -> str:
    """Resolve a runtime config value: DB cache → TANK_<KEY> env → default."""
    if key in _config_cache:
        return _config_cache[key]
    env = os.environ.get("TANK_" + key.upper())
    if env is not None:
        return env
    return RUNTIME_CONFIG_DEFAULTS.get(key, "")


def cfg_int(key: str) -> int:
    try:
        return int(cfg(key))
    except (TypeError, ValueError):
        return int(RUNTIME_CONFIG_DEFAULTS.get(key, "0") or "0")


def cfg_path(key: str) -> Path:
    return Path(cfg(key))


def cfg_bool(key: str) -> bool:
    return cfg(key).strip().lower() in ("1", "true", "yes", "on")


def ai_enabled() -> bool:
    """AI title/todo features only run when explicitly enabled AND an endpoint
    is configured — otherwise we'd hammer a nonexistent host on every task."""
    return cfg_bool("ai_features_enabled") and bool(cfg("title_model_url").strip())


# Sent as the prompt of the first turn after a server-restart-induced resume.
RESUME_NUDGE = (
    "This session was interrupted by a tank server restart. "
    "Please pick up where you left off."
)

# Tool calls that block waiting for operator input — used to flip a task's
# status to 'awaiting_input' on PreToolUse and back to 'running' on PostToolUse.
BLOCKING_TOOLS = {"AskUserQuestion", "ExitPlanMode"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
  id            TEXT PRIMARY KEY,
  name          TEXT NOT NULL UNIQUE,
  path          TEXT NOT NULL,
  kind          TEXT NOT NULL CHECK (kind IN ('project', 'chat')),
  description   TEXT,
  git_remote    TEXT,
  isolate_tasks INTEGER NOT NULL DEFAULT 0,
  base_branch   TEXT NOT NULL DEFAULT 'main',
  dev_command   TEXT,
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
  id            TEXT PRIMARY KEY,
  project_id    TEXT NOT NULL,
  title         TEXT NOT NULL,
  cwd           TEXT NOT NULL,
  status        TEXT NOT NULL,
  branch        TEXT,
  worktree_path TEXT,
  pr_url        TEXT,
  pr_pending    INTEGER NOT NULL DEFAULT 0,
  merge_pending INTEGER NOT NULL DEFAULT 0,
  error_reason  TEXT,
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL,
  FOREIGN KEY (project_id) REFERENCES projects(id)
);
CREATE TABLE IF NOT EXISTS turns (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id     TEXT NOT NULL,
  turn_num    INTEGER NOT NULL,
  prompt      TEXT NOT NULL,
  result      TEXT,
  started_at  TEXT NOT NULL,
  finished_at TEXT,
  FOREIGN KEY (task_id) REFERENCES tasks(id)
);
CREATE TABLE IF NOT EXISTS events (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id   TEXT NOT NULL,
  kind      TEXT NOT NULL,
  payload   TEXT NOT NULL,
  at        TEXT NOT NULL,
  FOREIGN KEY (task_id) REFERENCES tasks(id)
);
CREATE TABLE IF NOT EXISTS config (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS queue_items (
  id            TEXT PRIMARY KEY,
  project_id    TEXT NOT NULL,
  parent_id     TEXT,
  seq           INTEGER NOT NULL,
  title         TEXT NOT NULL,
  detail        TEXT,
  status        TEXT NOT NULL DEFAULT 'pending',
  depends_on    TEXT,
  result        TEXT,
  agent_task_id TEXT,
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL,
  FOREIGN KEY (project_id) REFERENCES projects(id)
);
CREATE TABLE IF NOT EXISTS queue_runs (
  project_id    TEXT PRIMARY KEY,
  status        TEXT NOT NULL DEFAULT 'idle',
  branch        TEXT,
  worktree_path TEXT,
  current_item  TEXT,
  started_at    TEXT,
  updated_at    TEXT NOT NULL,
  FOREIGN KEY (project_id) REFERENCES projects(id)
);
CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id, id);
CREATE INDEX IF NOT EXISTS idx_turns_task ON turns(task_id, turn_num);
CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project_id, created_at);
CREATE INDEX IF NOT EXISTS idx_queue_items_project ON queue_items(project_id, seq);
"""


def db_conn() -> sqlite3.Connection:
    c = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


def db_init() -> None:
    UPLOADS_ROOT.mkdir(parents=True, exist_ok=True)
    PROMPTS_ROOT.mkdir(parents=True, exist_ok=True)
    with db_conn() as c:
        # Migration: pre-projects schema had tasks without project_id. CREATE
        # TABLE IF NOT EXISTS won't add the column, so detect + drop the old
        # tables (smoke-test data only at this point — no production data).
        existing = c.execute("PRAGMA table_info(tasks)").fetchall()
        if existing and "project_id" not in [r["name"] for r in existing]:
            print("[db_init] Old schema detected; dropping tasks/turns/events for migration.", flush=True)
            c.execute("DROP TABLE IF EXISTS events")
            c.execute("DROP TABLE IF EXISTS turns")
            c.execute("DROP TABLE IF EXISTS tasks")
            c.commit()
        c.executescript(SCHEMA)
        # Additive migrations for the isolate-tasks feature. ALTER TABLE ADD
        # COLUMN is a no-op when the column already exists; we just swallow
        # the OperationalError so this stays idempotent.
        for table, col, ddl in (
            ("projects", "isolate_tasks", "INTEGER NOT NULL DEFAULT 0"),
            ("projects", "base_branch",   "TEXT NOT NULL DEFAULT 'main'"),
            ("projects", "dev_command",   "TEXT"),
            ("projects", "icon",          "TEXT"),
            ("tasks",    "branch",        "TEXT"),
            ("tasks",    "worktree_path", "TEXT"),
            ("tasks",    "pr_url",        "TEXT"),
            ("tasks",    "pr_pending",    "INTEGER NOT NULL DEFAULT 0"),
            ("tasks",    "merge_pending", "INTEGER NOT NULL DEFAULT 0"),
            ("tasks",    "error_reason",  "TEXT"),
            ("tasks",    "prev_status",   "TEXT"),
        ):
            cols = [r["name"] for r in c.execute(f"PRAGMA table_info({table})").fetchall()]
            if col not in cols:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
        c.commit()
    seed_config()
    # Dirs depend on config, so create them after the config table is seeded.
    cfg_path("chats_root").mkdir(parents=True, exist_ok=True)
    cfg_path("worktrees_root").mkdir(parents=True, exist_ok=True)
    seed_default_projects()


def seed_config() -> None:
    """Insert any missing config rows from RUNTIME_CONFIG_DEFAULTS, taking each
    value from TANK_<KEY> env if set, else the hardcoded default. Existing rows
    (set via the dashboard) are never overwritten. Then refresh the cache."""
    with db_conn() as c:
        existing = {r["key"] for r in c.execute("SELECT key FROM config").fetchall()}
        for key, default in RUNTIME_CONFIG_DEFAULTS.items():
            if key in existing:
                continue
            val = os.environ.get("TANK_" + key.upper(), default)
            c.execute("INSERT INTO config (key, value) VALUES (?, ?)", (key, val))
        c.commit()
    load_config_cache()


def seed_default_projects() -> None:
    """Idempotent: insert each entry from DEFAULT_PROJECTS if name not present."""
    ts = now_iso()
    with db_conn() as c:
        for p in DEFAULT_PROJECTS:
            exists = c.execute(
                "SELECT 1 FROM projects WHERE name=?", (p["name"],)
            ).fetchone()
            if exists:
                continue
            c.execute(
                """INSERT INTO projects (id, name, path, kind, description,
                                          created_at, updated_at)
                   VALUES (?, ?, ?, 'project', ?, ?, ?)""",
                (str(uuid.uuid4()), p["name"], p["path"], p["description"], ts, ts),
            )
        c.commit()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def session_name(task_id: str, turn: int) -> str:
    return f"task-{task_id[:8]}-t{turn}"


sse_subscribers: dict[str, list[asyncio.Queue]] = {}
spawn_semaphore: Optional[asyncio.Semaphore] = None
# Live dev-previews keyed by task_id: {"port": int, "tmux": str, "started_at": str}.
# Rebuilt from surviving tmux sessions on startup (reconcile_previews) so a
# server restart doesn't orphan a running preview.
active_previews: dict[str, dict] = {}


async def run_cmd(*args: str) -> tuple[int, str, str]:
    """Capture stdout+stderr. NOT safe for tmux control commands that fork
    daemons inheriting stdio — use run_cmd_void for those."""
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")


async def run_cmd_void(*args: str) -> int:
    """Run a command, discard stdio. Safe for tmux new-session / kill-session
    where the tmux server daemon inherits fds and would block PIPE-based reads.
    """
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    return await proc.wait()


async def tmux_new_session(name: str, cwd: str, command: str, width: int = 200) -> None:
    rc = await run_cmd_void(
        "tmux", "new-session", "-d", "-s", name, "-c", cwd,
        "-x", str(width), "-y", "50", command,
    )
    if rc != 0:
        raise RuntimeError(f"tmux new-session failed (rc={rc})")


async def tmux_capture(name: str, join_wrapped: bool = False) -> str:
    args = ["tmux", "capture-pane", "-p"]
    if join_wrapped:
        args.append("-J")
    args += ["-t", name]
    _, out, _ = await run_cmd(*args)
    return out


async def tmux_send_keys(name: str, text: str) -> None:
    await run_cmd_void("tmux", "send-keys", "-t", name, text, "Enter")


async def tmux_kill(name: str) -> None:
    await run_cmd_void("tmux", "kill-session", "-t", name)


async def tmux_list_sessions_for(task_id: str) -> list[str]:
    prefix = f"task-{task_id[:8]}-"
    _, out, _ = await run_cmd("tmux", "list-sessions", "-F", "#{session_name}")
    return [line for line in out.splitlines() if line.startswith(prefix)]


CLAUDE_USER_JSON = Path.home() / ".claude.json"


def trust_project_path(path: str) -> None:
    """Pre-accept Claude Code's per-directory trust dialog by writing into
    ~/.claude.json. Without this, every fresh sandbox dir triggers the
    'Trust this folder?' wizard on first interactive claude launch.
    """
    if not CLAUDE_USER_JSON.exists():
        return
    try:
        with CLAUDE_USER_JSON.open("r+") as f:
            data = json.load(f)
            projects = data.setdefault("projects", {})
            existing = projects.get(path, {})
            projects[path] = {
                "allowedTools": [],
                "mcpContextUris": [],
                "mcpServers": {},
                "enabledMcpjsonServers": [],
                "disabledMcpjsonServers": [],
                "hasTrustDialogAccepted": True,
                "projectOnboardingSeenCount": 0,
                "hasClaudeMdExternalIncludesApproved": False,
                "hasClaudeMdExternalIncludesWarningShown": False,
                **existing,
                "hasTrustDialogAccepted": True,
            }
            f.seek(0)
            json.dump(data, f, indent=2)
            f.truncate()
    except (json.JSONDecodeError, OSError):
        pass  # best-effort; first task may still hit the dialog


def setup_workspace(cwd: Path) -> Path:
    """Per-cwd readiness: ensure dir exists + pre-accept trust dialog in
    ~/.claude.json. Hooks live at user-level on tank now, not per-cwd."""
    cwd.mkdir(parents=True, exist_ok=True)
    trust_project_path(str(cwd))
    return cwd


def _write_claude_user_json_atomic(data: dict) -> None:
    tmp = CLAUDE_USER_JSON.with_suffix(".json.tmp")
    with tmp.open("w") as f:
        json.dump(data, f, indent=2)
    tmp.replace(CLAUDE_USER_JSON)


def untrust_project_path(path: str) -> None:
    """Remove this path's projects.<path> entry from ~/.claude.json."""
    if not CLAUDE_USER_JSON.exists():
        return
    try:
        with CLAUDE_USER_JSON.open("r") as f:
            data = json.load(f)
        projects = data.get("projects", {})
        if path in projects:
            del projects[path]
            _write_claude_user_json_atomic(data)
    except Exception:
        pass


def sweep_orphan_project_entries() -> int:
    """Remove projects.<path> entries from ~/.claude.json whose path no longer
    exists on disk. Stale entries can be left behind when claude's lifecycle
    hooks fire after we've already cleaned up a deleted chat's filesystem.
    Returns count removed."""
    if not CLAUDE_USER_JSON.exists():
        return 0
    try:
        with CLAUDE_USER_JSON.open("r") as f:
            data = json.load(f)
        projects = data.get("projects", {})
        orphans = [
            k for k in projects
            if k.startswith(str(cfg_path("chats_root")) + "/") and not Path(k).exists()
        ]
        for k in orphans:
            del projects[k]
        if orphans:
            _write_claude_user_json_atomic(data)
        return len(orphans)
    except Exception:
        return 0


# ── Image uploads (drag/drop/paste from the dashboard) ───────────────────────
# Each upload lives in its own subdir under UPLOADS_ROOT so we can remove it
# atomically. Referenced from prompts as a bare path; claude reads images via
# the Read tool. Sweeper deletes dirs whose mtime is older than UPLOAD_TTL_SEC.


def _safe_upload_name(name: str, content_type: str) -> str:
    """Sanitise the client-supplied filename and force a known extension.
    Falls back to 'image' if nothing usable came in."""
    base = Path(name or "").name
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(base).stem).strip("._-")
    if not stem:
        stem = "image"
    ext = UPLOAD_EXT_BY_MIME.get(content_type, "")
    return f"{stem[:60]}{ext}"


def sweep_old_uploads() -> int:
    """Remove upload subdirs whose mtime is older than UPLOAD_TTL_SEC.
    Returns count removed."""
    if not UPLOADS_ROOT.exists():
        return 0
    cutoff = time.time() - UPLOAD_TTL_SEC
    removed = 0
    for child in UPLOADS_ROOT.iterdir():
        try:
            if child.is_dir() and child.stat().st_mtime < cutoff:
                shutil.rmtree(child, ignore_errors=True)
                removed += 1
        except OSError:
            continue
    return removed


async def upload_sweeper_loop() -> None:
    while True:
        try:
            sweep_old_uploads()
        except Exception:
            pass
        await asyncio.sleep(UPLOAD_SWEEP_INTERVAL_SEC)


# ── Per-task git worktrees ───────────────────────────────────────────────────
# When a project has isolate_tasks=1, each task gets a fresh worktree branched
# off project.base_branch, so concurrent tasks on the same project don't stomp
# each other's files. PR creation itself is delegated to claude — see
# open_task_pr below — because claude needs to commit unstaged work first
# anyway, and it can write a real PR body.


def _slugify(s: str, maxlen: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")
    return (s[:maxlen].rstrip("-")) or "task"


async def create_worktree(
    project_path: str, task_id: str, base_branch: str, title: str
) -> tuple[str, str]:
    """Add a worktree at worktrees_root/<task_id> on a fresh branch derived
    from base_branch. Returns (worktree_path, branch_name).

    Fetches origin/<base_branch> first and branches off that ref, so new tasks
    always start from the tip on origin even if local <base_branch> is stale.
    """
    branch = f"tank/{_slugify(title)}-{task_id[:8]}"
    wt_path = cfg_path("worktrees_root") / task_id
    rc, out, err = await run_cmd(
        "git", "-C", project_path, "fetch", "origin", base_branch,
    )
    if rc != 0:
        raise RuntimeError(
            f"git fetch origin {base_branch} failed (rc={rc}): "
            f"{(err or out).strip() or 'no output'}"
        )
    rc, out, err = await run_cmd(
        "git", "-C", project_path, "worktree", "add",
        "-b", branch, str(wt_path), f"origin/{base_branch}",
    )
    if rc != 0:
        raise RuntimeError(
            f"git worktree add failed (rc={rc}): {(err or out).strip() or 'no output'}"
        )
    return str(wt_path), branch


async def remove_worktree(project_path: str, worktree_path: str,
                          branch: Optional[str]) -> None:
    """Force-remove the worktree dir + (best-effort) delete the branch. Safe
    to call repeatedly; non-zero rc is swallowed."""
    await run_cmd_void(
        "git", "-C", project_path, "worktree", "remove", "--force", worktree_path
    )
    if branch:
        await run_cmd_void("git", "-C", project_path, "branch", "-D", branch)
    # Final sweep — if .git/worktrees state is corrupt, the dir may still exist.
    if Path(worktree_path).exists():
        shutil.rmtree(worktree_path, ignore_errors=True)


# ── Dev previews ──────────────────────────────────────────────────────────────
# A "preview" runs a project's dev_command from a task's worktree (or cwd) on a
# dedicated port, so the operator can see + click through the change before
# merging. Each preview is a detached tmux session named
# `preview-<task8>-p<port>` — the port lives in the name so a server restart can
# rebuild active_previews from the surviving sessions (reconcile_previews).


def preview_session_name(task_id: str, port: int) -> str:
    return f"preview-{task_id[:8]}-p{port}"


def parse_port_range(spec: str) -> tuple[int, int]:
    """Parse a 'lo-hi' (or single 'n') port range; falls back to the default
    on anything malformed so a bad settings value can't wedge previews."""
    try:
        lo_s, _, hi_s = spec.strip().partition("-")
        lo = int(lo_s)
        hi = int(hi_s) if hi_s else lo
        if lo > hi or lo < 1 or hi > 65535:
            raise ValueError
        return lo, hi
    except (ValueError, AttributeError):
        return parse_port_range(RUNTIME_CONFIG_DEFAULTS["preview_port_range"])


def _port_is_free(port: int) -> bool:
    """True if nothing is bound on the port locally. Best-effort — a preview
    that races another binder will just fail to start and surface its log."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("0.0.0.0", port))
            return True
        except OSError:
            return False


def allocate_preview_port() -> int:
    """Pick a free port in the configured range that no other preview holds.
    Raises if the range is exhausted."""
    lo, hi = parse_port_range(cfg("preview_port_range"))
    taken = {p["port"] for p in active_previews.values()}
    for port in range(lo, hi + 1):
        if port in taken:
            continue
        if _port_is_free(port):
            return port
    raise RuntimeError(
        f"no free port in preview range {lo}-{hi} "
        f"({len(taken)} already held by live previews)"
    )


async def start_preview(task_id: str, cwd: str, dev_command: str) -> dict:
    """Launch dev_command (with $PORT substituted) from `cwd` in a detached
    tmux session and register it. Returns the preview record. Idempotent-ish:
    a task with a live preview keeps it rather than spawning a second."""
    existing = active_previews.get(task_id)
    if existing and await _preview_alive(task_id):
        return existing
    port = allocate_preview_port()
    name = preview_session_name(task_id, port)
    # tmux runs the command via `sh -c`. Export PORT as its own statement (not a
    # `PORT=n cmd` prefix) so the `$PORT` in the template expands to the new
    # value — a prefix assignment is applied *after* the rest of the line is
    # expanded, so `$PORT` there would resolve to empty.
    command = f"export PORT={port}; {dev_command}"
    await tmux_new_session(name, cwd, command)
    rec = {"port": port, "tmux": name, "started_at": now_iso()}
    active_previews[task_id] = rec
    return rec


async def _preview_alive(task_id: str) -> bool:
    rec = active_previews.get(task_id)
    if not rec:
        return False
    rc, _, _ = await run_cmd("tmux", "has-session", "-t", rec["tmux"])
    if rc != 0:
        active_previews.pop(task_id, None)
        return False
    return True


async def stop_preview(task_id: str) -> bool:
    """Kill a task's preview tmux session and drop its registration. Returns
    True if there was one to stop."""
    rec = active_previews.pop(task_id, None)
    if not rec:
        # Belt-and-braces: a preview may exist in tmux even if we lost the dict
        # entry (e.g. partial reconcile). Sweep any matching session.
        for name in await _list_preview_sessions():
            if name.startswith(f"preview-{task_id[:8]}-"):
                await tmux_kill(name)
                return True
        return False
    await tmux_kill(rec["tmux"])
    return True


async def _list_preview_sessions() -> list[str]:
    _, out, _ = await run_cmd("tmux", "list-sessions", "-F", "#{session_name}")
    return [ln for ln in out.splitlines() if ln.startswith("preview-")]


async def reconcile_previews() -> int:
    """Rebuild active_previews from surviving `preview-<task8>-p<port>` tmux
    sessions after a restart, matching each back to its task by id prefix.
    Sessions whose task is gone (deleted) are killed. Returns count adopted."""
    active_previews.clear()
    adopted = 0
    for name in await _list_preview_sessions():
        # name == preview-<task8>-p<port>
        try:
            _, task8, port_part = name.split("-", 2)
            port = int(port_part.lstrip("p"))
        except (ValueError, IndexError):
            continue
        with db_conn() as c:
            row = c.execute(
                "SELECT id FROM tasks WHERE id LIKE ? LIMIT 1", (task8 + "%",)
            ).fetchone()
        if not row:
            await tmux_kill(name)  # orphaned preview for a deleted task
            continue
        active_previews[row["id"]] = {
            "port": port, "tmux": name, "started_at": now_iso(),
        }
        adopted += 1
    if adopted:
        print(f"[reconcile] adopted {adopted} live preview(s)", flush=True)
    return adopted


def claude_session_dir_for(cwd: str) -> Path:
    """Path where claude stores the session JSONL for a given cwd."""
    encoded = cwd.replace("/", "-")
    return Path.home() / ".claude" / "projects" / encoded


async def _spawn_claude(name: str, task_id: str, turn_num: int, session_flag: str,
                        prompt: str, cwd: Path) -> None:
    """Launch claude in a fresh tmux session with `prompt` as its positional
    CLI argument (interactive mode): claude ingests and submits it once it's up,
    so there's no `tmux send-keys` step — sidestepping send-keys truncation, the
    multi-line auto-submit, and the old wait-for-SessionStart delivery race.

    The prompt is NOT inlined into the tmux command string — tmux caps that at
    ~16KB ("command too long"). Instead it goes in a file the spawned shell
    reads into argv (bounded by ARG_MAX, ~2MB) and then deletes. We pin
    TANK_PORT into claude's env so its hook.sh posts lifecycle events back to
    *this* tank (load-bearing for dev previews, which run on their own port; in
    prod PORT==7878, a no-op).
    """
    pf = PROMPTS_ROOT / f"{task_id}-t{turn_num}.txt"
    pf.write_text(prompt)
    # tmux runs this via `sh -c`. Read the prompt into a var, delete the file,
    # then exec claude so the pane *is* claude (terminal attach + the pkill
    # reaper that greps `<session_flag> <id>` in argv both still work). `$(cat)`
    # strips only trailing newlines; "$p" preserves the rest as one argument.
    pf_q = shlex.quote(str(pf))
    cmd = (
        f"export TANK_PORT={PORT}; p=$(cat {pf_q}); rm -f {pf_q}; "
        f"exec claude {session_flag} {task_id} --dangerously-skip-permissions \"$p\""
    )
    try:
        await tmux_new_session(name, str(cwd), cmd)
    except Exception:
        pf.unlink(missing_ok=True)  # shell never ran to self-delete it
        raise


async def spawn_first_turn(task_id: str, prompt: str, cwd: Path) -> None:
    await _spawn_claude(session_name(task_id, 1), task_id, 1, "--session-id", prompt, cwd)


async def spawn_continue_turn(task_id: str, prompt: str, cwd: Path, turn_num: int) -> None:
    # `--resume <id>`, not `--continue` — the latter ignores the positional
    # prompt (anthropics/claude-code#3180).
    await _spawn_claude(session_name(task_id, turn_num), task_id, turn_num,
                        "--resume", prompt, cwd)


async def broadcast(task_id: str, event: dict) -> None:
    for q in list(sse_subscribers.get(task_id, [])):
        with contextlib.suppress(asyncio.QueueFull):
            q.put_nowait(event)
    for q in list(sse_subscribers.get("*", [])):
        with contextlib.suppress(asyncio.QueueFull):
            q.put_nowait({**event, "task_id": task_id})


def read_last_assistant_text(transcript_path: str) -> str:
    if not transcript_path or not os.path.exists(transcript_path):
        return ""
    try:
        with open(transcript_path) as f:
            lines = f.readlines()
    except OSError:
        return ""
    for line in reversed(lines):
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if msg.get("type") != "assistant":
            continue
        content = msg.get("message", {}).get("content", [])
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    return block.get("text", "")
    return ""


def read_last_api_error(transcript_path: str) -> Optional[dict]:
    """If the most recent assistant message in the transcript is a synthetic
    API-error message (claude emits these after exhausting its retry budget
    on 429/529/etc), return {"status": int|None, "message": str}. Otherwise
    return None — meaning the last assistant turn was a normal response.

    Detected via the `isApiErrorMessage` flag claude tags onto those records.
    """
    if not transcript_path or not os.path.exists(transcript_path):
        return None
    try:
        with open(transcript_path) as f:
            lines = f.readlines()
    except OSError:
        return None
    for line in reversed(lines):
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if msg.get("type") != "assistant":
            continue
        if not msg.get("isApiErrorMessage"):
            return None
        text = ""
        content = msg.get("message", {}).get("content", [])
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text = block.get("text", "")
                    break
        return {"status": msg.get("apiErrorStatus"), "message": text}
    return None


async def handle_event(task_id: str, kind: str, payload: dict) -> None:
    with db_conn() as c:
        c.execute(
            "INSERT INTO events (task_id, kind, payload, at) VALUES (?, ?, ?, ?)",
            (task_id, kind, json.dumps(payload), now_iso()),
        )
        if kind == "SessionStart":
            c.execute(
                "UPDATE tasks SET status='running', updated_at=? WHERE id=?",
                (now_iso(), task_id),
            )
        elif kind == "PreToolUse" and payload.get("tool_name") in BLOCKING_TOOLS:
            # Claude is about to block on operator input (a question, or a
            # plan to approve) — surface it as a distinct status so the
            # dashboard can flag "needs you" vs. plain "running".
            c.execute(
                "UPDATE tasks SET status='awaiting_input', updated_at=? WHERE id=?",
                (now_iso(), task_id),
            )
        elif kind == "PostToolUse" and payload.get("tool_name") in BLOCKING_TOOLS:
            c.execute(
                "UPDATE tasks SET status='running', updated_at=? WHERE id=?",
                (now_iso(), task_id),
            )
        c.commit()

    await broadcast(task_id, {"event": "hook", "kind": kind, "at": now_iso()})

    if kind == "Stop":
        await on_stop(task_id, payload)


PR_URL_RE = re.compile(r"https?://\S+?/pulls/\d+")
# "MERGED" alone on a line, or the Forgejo API's "merged": true echo.
MERGED_RE = re.compile(r'(?mi)^\s*MERGED\s*$|"merged"\s*:\s*true')


async def on_stop(task_id: str, payload: dict) -> None:
    transcript_path = payload.get("transcript_path", "")
    last_response = read_last_assistant_text(transcript_path)
    api_err = read_last_api_error(transcript_path)

    # When the main agent spawns Agent(run_in_background=true) subagents and
    # parks itself waiting on them, Claude Code fires Stop with a
    # `background_tasks` array of {id, type, status, ...}. Status: "running"
    # entries mean the session is NOT finished — it will be auto-resumed by
    # claude in the same session_id when the subagents return (verified
    # empirically; the field is undocumented but stable as of 2.1.x). Treat
    # this as an interim Stop: save the wrap-up text as a preview result,
    # flip the task to 'background_waiting', and leave tmux alive. The next
    # Stop (after the synthesis turn) finalizes normally.
    bg_tasks = payload.get("background_tasks") or []
    pending_bg = [t for t in bg_tasks if t.get("status") == "running"]
    if pending_bg and not api_err:
        ts = now_iso()
        with db_conn() as c:
            c.execute(
                """UPDATE turns SET result=?
                   WHERE task_id=? AND finished_at IS NULL""",
                (last_response, task_id),
            )
            c.execute(
                "UPDATE tasks SET status='background_waiting', updated_at=? WHERE id=?",
                (ts, task_id),
            )
            c.commit()
        await broadcast(
            task_id,
            {
                "event": "background_waiting",
                "at": ts,
                "pending": len(pending_bg),
                "tasks": pending_bg,
            },
        )
        return

    if api_err:
        status = "errored"
        error_reason = format_api_error_reason(api_err)
        broadcast_event = "errored"
    else:
        status = "completed"
        error_reason = None
        broadcast_event = "completed"

    # PR/merge detection: only act on the in-flight turn if its prompt is one
    # of our templates, so an unrelated continue-turn that happens to contain
    # the word MERGED doesn't accidentally flip status.
    pr_url_update: str | None = None
    with db_conn() as c:
        active_turn = c.execute(
            """SELECT prompt FROM turns
               WHERE task_id=? AND finished_at IS NULL
               ORDER BY turn_num DESC LIMIT 1""",
            (task_id,),
        ).fetchone()
        task_row = c.execute(
            "SELECT pr_url, branch FROM tasks WHERE id=?", (task_id,)
        ).fetchone()

    if (
        status == "completed"
        and last_response
        and active_turn
        and task_row
        and task_row["branch"]
    ):
        prompt_text = active_turn["prompt"] or ""
        if prompt_text.startswith(SHIP_PROMPT_SENTINEL):
            # "ship it" does both halves in one turn: scrape the PR URL (if not
            # already recorded) AND the MERGED sentinel from the single reply.
            # A half-completed ship (PR opened, merge failed) still records the
            # PR URL, so the UI falls back to the recovery "merge PR" button.
            if not task_row["pr_url"]:
                m = PR_URL_RE.search(last_response)
                if m:
                    pr_url_update = m.group(0)
            if MERGED_RE.search(last_response):
                status = "merged"
                broadcast_event = "merged"
        elif prompt_text.startswith(PR_PROMPT_SENTINEL) and not task_row["pr_url"]:
            m = PR_URL_RE.search(last_response)
            if m:
                pr_url_update = m.group(0)
        elif prompt_text.startswith(MERGE_PROMPT_SENTINEL) and task_row["pr_url"]:
            if MERGED_RE.search(last_response):
                status = "merged"
                broadcast_event = "merged"

    ts = now_iso()
    with db_conn() as c:
        c.execute(
            """UPDATE turns SET result=?, finished_at=?
               WHERE task_id=? AND finished_at IS NULL""",
            (last_response, ts, task_id),
        )
        # A finalizing Stop means the in-flight turn ended — clear any
        # open-PR/merge intent regardless of outcome (the background_waiting
        # interim Stop returns early above, so a PR turn parked on subagents
        # correctly keeps its pending flag).
        if pr_url_update is not None:
            c.execute(
                "UPDATE tasks SET status=?, error_reason=?, pr_url=?, "
                "pr_pending=0, merge_pending=0, updated_at=? WHERE id=?",
                (status, error_reason, pr_url_update, ts, task_id),
            )
        else:
            c.execute(
                "UPDATE tasks SET status=?, error_reason=?, "
                "pr_pending=0, merge_pending=0, updated_at=? WHERE id=?",
                (status, error_reason, ts, task_id),
            )
        c.commit()

    for name in await tmux_list_sessions_for(task_id):
        await tmux_kill(name)

    await broadcast(
        task_id,
        {
            "event": broadcast_event,
            "at": ts,
            "result": last_response,
            "error_reason": error_reason,
        },
    )


def format_api_error_reason(api_err: dict) -> str:
    """Produce a compact one-liner for the dashboard from a read_last_api_error
    result. Examples: "API Error 529: Overloaded" / "API Error: …"."""
    status = api_err.get("status")
    msg = (api_err.get("message") or "").strip().splitlines()[0] if api_err.get("message") else ""
    # Claude's synthetic message already starts with "API Error: <status> <reason>".
    # Strip that prefix so we don't double it up.
    msg = re.sub(r"^API Error:\s*\d*\s*", "", msg).strip()
    base = f"API Error {status}" if status else "API Error"
    return f"{base}: {msg[:160]}" if msg else base


AUTH_TMUX = "tank-auth"

# Plan-usage panel data. Read by GET /usage and cached briefly because the
# upstream is rate-limited (binary logs "fetchUtilization … attempt N") and
# claude.ai itself only refreshes once a minute.
CREDENTIALS_PATH = Path.home() / ".claude" / ".credentials.json"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
USAGE_CACHE_SECONDS = 60
_usage_cache: dict = {"payload": None, "fetched_at": 0.0}


async def fetch_usage() -> dict:
    """Returns {ok, usage?, error?, cached_at}.

    The OAuth access token lives in ~/.claude/.credentials.json (owned by the
    `tank` user — same one running the API, so no perms shenanigans). On 401
    we surface `token_expired` and leave token-refresh to the user re-running
    `claude auth login`."""
    now = time.time()
    cached = _usage_cache["payload"]
    if cached and (now - _usage_cache["fetched_at"]) < USAGE_CACHE_SECONDS:
        return cached
    try:
        creds = json.loads(CREDENTIALS_PATH.read_text())
        token = creds["claudeAiOauth"]["accessToken"]
    except (FileNotFoundError, KeyError, json.JSONDecodeError) as e:
        return {"ok": False, "error": f"no_credentials: {type(e).__name__}",
                "cached_at": now_iso()}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(USAGE_URL, headers={
                "Authorization": f"Bearer {token}",
                "anthropic-beta": "oauth-2025-04-20",
            })
    except httpx.HTTPError as e:
        return {"ok": False, "error": f"network: {type(e).__name__}",
                "cached_at": now_iso()}
    if r.status_code == 401:
        return {"ok": False, "error": "token_expired", "cached_at": now_iso()}
    if r.status_code >= 400:
        return {"ok": False, "error": f"http_{r.status_code}",
                "cached_at": now_iso()}
    payload = {"ok": True, "usage": r.json(), "cached_at": now_iso()}
    _usage_cache["payload"] = payload
    _usage_cache["fetched_at"] = now
    return payload


async def claude_auth_status() -> dict:
    """Returns {authed: bool, raw: str, account: str|None}.

    Reads ~/.claude/.credentials.json directly instead of shelling out to
    `claude auth status`. The dashboard polls this every 30s; each subprocess
    invocation used to fight any concurrent interactive `claude` sessions for
    the OAuth refresh-token rotation (Anthropic issues single-use refresh
    tokens), so whoever lost the race got kicked to /login. A pure file read
    can't trigger or race a refresh.
    """
    if not CREDENTIALS_PATH.exists():
        return {"authed": False, "raw": "no credentials file", "account": None}
    try:
        creds = json.loads(CREDENTIALS_PATH.read_text())
    except (json.JSONDecodeError, OSError) as e:
        return {"authed": False, "raw": f"unreadable: {type(e).__name__}",
                "account": None}
    # OAuth flow: claudeAiOauth.accessToken present = user is signed in.
    # Deliberately don't gate on expiresAt — an expired access token is
    # refreshable, and the dashboard's job is to flag missing/broken creds,
    # not refreshable ones.
    oauth = creds.get("claudeAiOauth") or {}
    if oauth.get("accessToken"):
        return {"authed": True, "raw": "oauth", "account": _account_email()}
    # Long-lived `claude setup-token` path: top-level token field. Exact key
    # name isn't documented and may vary, so accept any non-empty value.
    for k in ("primaryApiKey", "setupToken", "apiKey"):
        if creds.get(k):
            return {"authed": True, "raw": "setup-token",
                    "account": _account_email()}
    return {"authed": False, "raw": "credentials present but no token field",
            "account": None}


def _account_email() -> Optional[str]:
    """Best-effort: pull oauthAccount.emailAddress out of ~/.claude.json so
    the dashboard can show who's signed in. Silent on any read/parse error."""
    try:
        data = json.loads(CLAUDE_USER_JSON.read_text())
        return data.get("oauthAccount", {}).get("emailAddress")
    except Exception:
        return None


def _extract_auth_url(pane: str) -> Optional[str]:
    """Find the auth URL inside a (joined) capture-pane snapshot.

    Only return URLs that look complete (contain `state=`), so we don't return
    a truncated URL while claude is still mid-write.
    """
    for line in pane.splitlines():
        if "https://claude" in line and "/oauth/" in line.lower():
            idx = line.find("https://")
            url = line[idx:].strip()
            # Strip trailing prompt/text after the URL
            url = url.split()[0] if " " in url else url
            if "state=" in url:
                return url
    return None


class CreateTaskBody(BaseModel):
    prompt: str
    project_id: str


class CreateChatBody(BaseModel):
    """A chat is a one-off project of kind='chat' with a disposable cwd. The
    first task is auto-created with the initial prompt. When AI features are
    enabled, both the chat's project name and the task title are auto-generated
    from the prompt (see generate_and_set_title)."""
    prompt: str


class CreateProjectBody(BaseModel):
    name: str
    # Optional. If omitted, we derive projects_root/<name> and create the dir
    # (cloning git_remote if set, else `git init`-ing an empty repo).
    path: Optional[str] = None
    description: Optional[str] = None
    # Doubles as the clone URL when the target path doesn't exist yet.
    git_remote: Optional[str] = None
    # If true and we freshly created an empty dir, drop a runnable Creator Magic
    # starter app into it (FastAPI+HTMX wearing the shared house-style shell,
    # assets vendored from tank's own copy) and register its dev_command so the
    # preview button works immediately. Ignored when registering/cloning into an
    # existing dir, so it never clobbers real code.
    scaffold_ui: Optional[bool] = False


class ContinueTaskBody(BaseModel):
    prompt: str


class QueueItemIn(BaseModel):
    title: str
    detail: Optional[str] = None
    # Comma-separated queue_item ids this item waits on. Optional — items run in
    # seq order on the shared branch anyway, so deps are only needed to force a
    # non-adjacent ordering or to skip dependents when a prerequisite fails.
    depends_on: Optional[str] = None


class QueueAddBody(BaseModel):
    items: list[QueueItemIn]


class QueuePatchBody(BaseModel):
    title: Optional[str] = None
    detail: Optional[str] = None
    status: Optional[str] = None
    depends_on: Optional[str] = None
    seq: Optional[int] = None


class QueueReorderBody(BaseModel):
    order: list[str]


class EventBody(BaseModel):
    kind: str
    payload: dict = {}


# ── Public API response models ─────────────────────────────────────────────────
# These type the integrator-facing routes so /openapi.json + /docs describe real
# response shapes. `extra="allow"` is load-bearing: the row models below describe
# the documented columns, but additive DB migrations (e.g. prev_status) add
# columns that the SPA reads — extra="allow" lets those pass through untouched
# rather than being stripped by response_model serialisation.


class TaskRow(BaseModel):
    """A task — one Claude Code session, one git worktree/branch."""
    model_config = ConfigDict(extra="allow")
    id: str
    project_id: str
    title: str
    cwd: str
    status: str  # queued|running|awaiting_input|background_waiting|done|failed|killed|merged…
    branch: Optional[str] = None
    worktree_path: Optional[str] = None
    pr_url: Optional[str] = None
    created_at: str
    updated_at: str


class TurnRow(BaseModel):
    """One prompt → result exchange within a task."""
    model_config = ConfigDict(extra="allow")
    turn_num: int
    prompt: str
    result: Optional[str] = None
    started_at: str
    finished_at: Optional[str] = None


class EventRecord(BaseModel):
    """A single agent lifecycle / tool-use event in the task timeline."""
    model_config = ConfigDict(extra="allow")
    id: int
    kind: str
    at: str
    summary: str
    turn_num: Optional[int] = None


class PreviewState(BaseModel):
    running: bool
    port: Optional[int] = None
    started_at: Optional[str] = None


class TaskHandle(BaseModel):
    """Returned when a task is spawned or advanced a turn."""
    task_id: str
    status: str
    turn: Optional[int] = None


class TaskDetail(BaseModel):
    """Full task state: the task row, its turns, the tool-use timeline, and the
    current live-preview status."""
    task: TaskRow
    turns: list[TurnRow]
    events: list[EventRecord]
    preview: PreviewState


class StopResult(BaseModel):
    killed_sessions: list[str]


class HealthResult(BaseModel):
    status: str
    ts: str


async def _delayed_sweep() -> None:
    """Re-sweep orphan project entries after delete: claude's lifecycle hooks
    can re-add entries up to a couple of seconds after we kill its tmux."""
    await asyncio.sleep(3.0)
    sweep_orphan_project_entries()


# ── Title auto-generation via the inference endpoint ──────────────────────────
# Tasks are created with a placeholder title (first words of prompt). When AI
# features are enabled (see ai_enabled / the settings cog), a background
# asyncio.create_task hits the configured OpenAI-compatible chat-completions
# endpoint (config: title_model_url / title_model) for a clean title and
# broadcasts an SSE event so the dashboard refreshes. All failures are silent —
# the placeholder stays put.

# A cold model load can take 7-25s on some backends; warm calls return in <1s.
TITLE_TIMEOUT_SEC = 45.0

_TITLE_SYSTEM = "You write short titles for tasks. Reply with only the title."
_TITLE_FEWSHOT = [
    {"role": "user", "content": "add a dark mode toggle to the settings page"},
    {"role": "assistant", "content": "Add dark mode toggle"},
    {"role": "user", "content": "the login button stays disabled after a failed attempt, fix that"},
    {"role": "assistant", "content": "Fix stuck login button"},
    {"role": "user", "content": "can you walk me through how websockets reconnect in the chat code"},
    {"role": "assistant", "content": "Explain websocket reconnect flow"},
]


def placeholder_title(prompt: str) -> str:
    """First ~60 chars of the prompt, trimmed at a word boundary."""
    s = " ".join((prompt or "").split())
    if not s:
        return "New task"
    if len(s) <= 60:
        return s
    cut = s[:60]
    sp = cut.rfind(" ")
    return (cut[:sp] if sp > 30 else cut) + "…"


def _sanitise_title(raw: str) -> str:
    s = raw or ""
    # Strip <think>...</think> defensively in case /no_think is ignored.
    s = re.sub(r"<think>.*?</think>", "", s, flags=re.S)
    # Truncated thinking: an opening <think> with no close means the rest of
    # the output is reasoning that never finished — discard everything from
    # the opening tag onward.
    if "<think>" in s and "</think>" not in s:
        s = s.split("<think>", 1)[0]
    s = s.strip()
    # Reasoning often precedes the answer — take the last non-empty line.
    lines = [l.strip() for l in s.splitlines() if l.strip()]
    s = lines[-1] if lines else s
    s = s.strip()
    # Strip one matched pair of wrapping quotes only. Greedy strip('"\'')
    # mangles titles whose own content contains quotes (e.g. model emits
    # `"Rename button to "ask AI""` → strips 2 right + 1 left → stray open).
    if len(s) >= 2 and s[0] in "\"'" and s[-1] == s[0]:
        s = s[1:-1].strip()
    s = s.strip("* •-").rstrip(".!?")
    return s[:80]


async def _llm_title(prompt: str) -> Optional[str]:
    body = {
        "model": cfg("title_model"),
        "temperature": 0.3,
        # Headroom for a short reasoning preamble plus the title. With 32 the
        # response often hit finish_reason=length and the sanitiser turned
        # the truncated fragment ("They want me…") into the title.
        "max_tokens": 128,
        "messages": [
            {"role": "system", "content": _TITLE_SYSTEM},
            *_TITLE_FEWSHOT,
            # /no_think disables the reasoning channel on models that honour it
            # (e.g. Qwen); harmless to others.
            {"role": "user", "content": prompt[:1000].strip() + " /no_think"},
        ],
    }
    async with httpx.AsyncClient(timeout=TITLE_TIMEOUT_SEC) as client:
        r = await client.post(cfg("title_model_url"), json=body)
        r.raise_for_status()
        data = r.json()
    choice = (data.get("choices") or [{}])[0]
    # finish_reason="length" means the model was cut off mid-output; whatever
    # we got back is a fragment, not a title — keep the placeholder instead.
    if choice.get("finish_reason") == "length":
        return None
    content = choice.get("message", {}).get("content")
    title = _sanitise_title(content) if content else ""
    return title or None


# ── Todo tidy-up via the inference endpoint ───────────────────────────────────
# When the user adds a todo, we run the raw text through the configured model to
# clean it up: imperative phrasing, fix typos, drop filler, preserve the user's
# intent. Reuses the title model. Synchronous on the POST path with a short
# timeout — falls back to the raw text on any error so the add never fails.

TODO_IMPROVE_TIMEOUT_SEC = 12.0

_TODO_SYSTEM = (
    "You rewrite informal todo items into concise, imperative action items. "
    "Use British English. Fix typos and capitalisation. Preserve the user's "
    "intent — do not invent details. If the item is already well-written, "
    "return it unchanged. Reply with only the rewritten todo, no quotes, "
    "no list marker, no trailing punctuation."
)
_TODO_FEWSHOT = [
    {"role": "user", "content": "yeah we should probably get around to refactoring the auth module at some point"},
    {"role": "assistant", "content": "Refactor auth module"},
    {"role": "user", "content": "fix that anoying bug where the modal closes when you click inside it"},
    {"role": "assistant", "content": "Fix modal closing on inside-click"},
    {"role": "user", "content": "ask sam about the new db schema"},
    {"role": "assistant", "content": "Ask Sam about the new DB schema"},
    {"role": "user", "content": "buy milk"},
    {"role": "assistant", "content": "Buy milk"},
]


async def _llm_improve_todo(text: str) -> Optional[str]:
    body = {
        "model": cfg("title_model"),
        "temperature": 0.2,
        "max_tokens": 128,
        "messages": [
            {"role": "system", "content": _TODO_SYSTEM},
            *_TODO_FEWSHOT,
            {"role": "user", "content": text[:1000].strip() + " /no_think"},
        ],
    }
    async with httpx.AsyncClient(timeout=TODO_IMPROVE_TIMEOUT_SEC) as client:
        r = await client.post(cfg("title_model_url"), json=body)
        r.raise_for_status()
        data = r.json()
    choice = (data.get("choices") or [{}])[0]
    if choice.get("finish_reason") == "length":
        return None
    content = choice.get("message", {}).get("content")
    cleaned = _sanitise_todo(content) if content else ""
    return cleaned or None


def _sanitise_todo(raw: str) -> str:
    """Like _sanitise_title but with a higher length cap — todos can be
    longer than 80 chars without being malformed."""
    s = raw or ""
    s = re.sub(r"<think>.*?</think>", "", s, flags=re.S)
    if "<think>" in s and "</think>" not in s:
        s = s.split("<think>", 1)[0]
    s = s.strip()
    lines = [l.strip() for l in s.splitlines() if l.strip()]
    s = lines[-1] if lines else s
    s = s.strip()
    if len(s) >= 2 and s[0] in "\"'" and s[-1] == s[0]:
        s = s[1:-1].strip()
    s = s.strip("* •-").rstrip(".!?")
    return s[:200]


async def generate_and_set_title(
    task_id: str, prompt: str, chat_project_id: Optional[str] = None
) -> None:
    """Background: ask the model for a friendly title, update DB, broadcast SSE.

    When chat_project_id is set, also update projects.name (the chat's
    display label in the sidebar). Falls back to a suffixed name if the
    generated title collides with an existing project. Silent on any
    failure — the synchronous placeholder stays. No-op when AI features are
    disabled or unconfigured.
    """
    if not ai_enabled():
        return
    try:
        title = await _llm_title(prompt)
        if not title:
            return
        ts = now_iso()
        with db_conn() as c:
            c.execute(
                "UPDATE tasks SET title=?, updated_at=? WHERE id=?",
                (title, ts, task_id),
            )
            c.commit()
        if chat_project_id:
            with db_conn() as c:
                try:
                    c.execute(
                        "UPDATE projects SET name=?, updated_at=? WHERE id=?",
                        (title, ts, chat_project_id),
                    )
                    c.commit()
                except sqlite3.IntegrityError:
                    c.execute(
                        "UPDATE projects SET name=?, updated_at=? WHERE id=?",
                        (f"{title} ({chat_project_id[:6]})", ts, chat_project_id),
                    )
                    c.commit()
        await broadcast(task_id, {"event": "title_updated", "at": ts})
    except Exception as e:
        print(f"[title_gen] failed for task {task_id}: {e!r}", flush=True)


# ── AI-suggested project icon ─────────────────────────────────────────────────
# Given a project's name + description, ask the configured model for a handful of
# icon keywords, then map them onto a real Lucide slug (the model can't be trusted
# to name an icon that actually exists, so we validate against the catalogue).
# LUCIDE_VER must match the const in static/index.html.

LUCIDE_VER = "1.17.0"
LUCIDE_TAGS_URL = f"https://cdn.jsdelivr.net/npm/lucide-static@{LUCIDE_VER}/tags.json"
ICON_SUGGEST_TIMEOUT_SEC = 15.0

_lucide_catalog_cache: Optional[dict[str, list[str]]] = None
_lucide_lock = asyncio.Lock()

_ICON_SYSTEM = (
    "You pick an icon for a software project. Given its name and description, "
    "reply with 4 to 8 short lowercase keywords (single words, comma-separated, "
    "best first) naming concrete objects or concepts that would make a good "
    "icon. Prefer concrete nouns over abstractions. Reply with ONLY the "
    "comma-separated keywords on a single line — no explanation, no reasoning."
)
_ICON_FEWSHOT = [
    {"role": "user", "content": "Project name: payments-api\nDescription: Stripe billing and invoicing service /no_think"},
    {"role": "assistant", "content": "credit-card, receipt, banknote, wallet, coins"},
    {"role": "user", "content": "Project name: home-network\nDescription: Umbrella for all home infrastructure work /no_think"},
    {"role": "assistant", "content": "house, network, server, router, wifi"},
    {"role": "user", "content": "Project name: photo-sorter\nDescription: Organises and tags a photo library /no_think"},
    {"role": "assistant", "content": "image, camera, folder, tag, gallery"},
]


async def _lucide_catalog() -> dict[str, list[str]]:
    """Fetch + cache Lucide's name→tags map from jsDelivr. Cached for the
    process lifetime; the icon set only changes when LUCIDE_VER bumps."""
    global _lucide_catalog_cache
    if _lucide_catalog_cache is not None:
        return _lucide_catalog_cache
    async with _lucide_lock:
        if _lucide_catalog_cache is not None:
            return _lucide_catalog_cache
        async with httpx.AsyncClient(timeout=ICON_SUGGEST_TIMEOUT_SEC) as client:
            r = await client.get(LUCIDE_TAGS_URL)
            r.raise_for_status()
            data = r.json()
        _lucide_catalog_cache = {k: list(v or []) for k, v in data.items()}
    return _lucide_catalog_cache


def _match_lucide_icon(keywords: list[str], catalog: dict[str, list[str]]) -> Optional[str]:
    """Resolve model keywords to a real Lucide slug. Priority: exact slug →
    exact tag → slug substring. Catalogue iteration is alphabetical, so ties
    resolve deterministically."""
    names = set(catalog)
    cleaned = [k.strip().lower() for k in keywords if k.strip()]
    for kw in cleaned:               # exact slug
        if kw in names:
            return kw
    for kw in cleaned:               # exact tag
        for name, tags in catalog.items():
            if kw in tags:
                return name
    for kw in cleaned:               # slug substring (longer keywords only)
        if len(kw) >= 3:
            for name in catalog:
                if kw in name:
                    return name
    return None


async def _llm_pick_icon(name: str, description: Optional[str]) -> Optional[str]:
    catalog = await _lucide_catalog()
    desc = (description or "").strip() or "(no description)"
    body = {
        "model": cfg("title_model"),
        "temperature": 0.3,
        # Headroom for a reasoning preamble on models that ignore /no_think; we
        # parse only the final line. Truncation (finish_reason=length) is
        # rejected below rather than parsed as a fragment.
        "max_tokens": 128,
        "messages": [
            {"role": "system", "content": _ICON_SYSTEM},
            *_ICON_FEWSHOT,
            {"role": "user", "content": f"Project name: {name}\nDescription: {desc} /no_think"},
        ],
    }
    async with httpx.AsyncClient(timeout=ICON_SUGGEST_TIMEOUT_SEC) as client:
        r = await client.post(cfg("title_model_url"), json=body)
        r.raise_for_status()
        data = r.json()
    choice = (data.get("choices") or [{}])[0]
    if choice.get("finish_reason") == "length":
        return None
    content = choice.get("message", {}).get("content") or ""
    content = re.sub(r"<think>.*?</think>", "", content, flags=re.S)
    if "<think>" in content and "</think>" not in content:
        content = content.split("<think>", 1)[0]
    # The keyword list is the last non-empty line (any reasoning preamble sits
    # above it). Split on commas; expand multi-word phrases into kebab slugs and
    # their component words so "credit card" can match "credit-card"/"card".
    lines = [l.strip() for l in content.splitlines() if l.strip()]
    last = lines[-1] if lines else ""
    raw = [w.strip().lower() for w in last.split(",") if w.strip()]
    keywords: list[str] = []
    for kw in raw:
        kw = re.sub(r"[^a-z0-9 -]", "", kw).strip()
        if not kw:
            continue
        keywords.append(kw)
        if " " in kw:
            keywords.append(kw.replace(" ", "-"))
            keywords.extend(kw.split())
    return _match_lucide_icon(keywords, catalog)


async def _suggest_and_store_icon(
    project_id: str, name: str, description: Optional[str]
) -> Optional[str]:
    """Pick an icon and persist it. Returns the slug, or None if nothing matched.
    Broadcasts so any open dashboard refreshes its sidebar."""
    icon = await _llm_pick_icon(name, description)
    if not icon:
        return None
    ts = now_iso()
    with db_conn() as c:
        c.execute(
            "UPDATE projects SET icon=?, updated_at=? WHERE id=?", (icon, ts, project_id)
        )
        c.commit()
    await broadcast("*", {"event": "project_updated", "at": ts})
    return icon


async def _auto_suggest_icon(project_id: str, name: str, description: Optional[str]) -> None:
    """Fire-and-forget icon suggestion for a freshly-created project. Silent on
    any failure — the project just keeps the default favicon."""
    try:
        await _suggest_and_store_icon(project_id, name, description)
    except Exception as e:
        print(f"[icon_suggest] auto-suggest failed for {project_id}: {e!r}", flush=True)


async def reconcile_orphan_tasks() -> int:
    """Mark as 'interrupted' any task whose tmux session is gone but whose DB
    row still claims it's live. Runs at startup so service restarts and CT
    reboots can't leave zombies stuck in 'running' forever.

    With KillMode=process on tank.service, a deploy typically leaves tmux +
    claude alive — those tasks get skipped here (tmux session still exists)
    and their hooks keep firing as normal. Reboots kill everything, so this
    catches those.
    """
    with db_conn() as c:
        rows = c.execute(
            """SELECT id FROM tasks
               WHERE status IN ('queued', 'running', 'awaiting_input', 'background_waiting')"""
        ).fetchall()
    if not rows:
        return 0
    count = 0
    for r in rows:
        task_id = r["id"]
        sessions = await tmux_list_sessions_for(task_id)
        if sessions:
            continue  # tmux survived the restart; task is still live
        ts = now_iso()
        with db_conn() as c:
            # Close out the in-flight turn so the UI stops spinning and the
            # next Stop hook (on resume) doesn't retroactively overwrite it.
            c.execute(
                """UPDATE turns SET result=?, finished_at=?
                   WHERE task_id=? AND finished_at IS NULL""",
                ("_[interrupted by server restart]_", ts, task_id),
            )
            c.execute(
                "UPDATE tasks SET status='interrupted', updated_at=? WHERE id=?",
                (ts, task_id),
            )
            c.commit()
        count += 1
    if count:
        print(f"[reconcile] marked {count} task(s) interrupted", flush=True)
    return count


# Watchdog cadence + staleness threshold. A task flagged 'running' that has
# received no hook events for this long is a candidate for transcript
# inspection — likely either an API-error bailout claude retried out of, or
# a normal completion whose Stop hook never reached us.
WATCHDOG_INTERVAL_SEC = 60
WATCHDOG_STALE_SEC = 300  # 5 minutes — generous to avoid racing with long tool calls


async def sweep_stuck_tasks() -> int:
    """One pass of the watchdog. For each task in 'running' whose most recent
    hook event is older than WATCHDOG_STALE_SEC, consult the JSONL transcript
    and decide how to close it out:

    - Transcript's last assistant message is an api-error → status='errored'
      with error_reason set from `apiErrorStatus` + message text. (Handles
      the 529/overloaded case where claude bailed.)
    - Transcript has a normal last assistant turn → status='completed'.
      (Handles the case where the Stop hook never reached us — claude
      finished cleanly but tank kept showing 'running' forever.)
    - No transcript or no assistant turn → leave alone; nothing to claim.

    'awaiting_input' is deliberately excluded — a user could legitimately
    sit on an AskUserQuestion for hours. 'background_waiting' IS swept
    because parked-on-subagents sessions still emit hook events (subagents
    use the same hooks), so 5min of silence really does mean wedged.
    Always reaps leftover tmux on close-out so zombie sessions don't pile
    up.
    """
    cutoff_iso = (datetime.now(timezone.utc).timestamp() - WATCHDOG_STALE_SEC)
    cutoff_iso_str = datetime.fromtimestamp(cutoff_iso, tz=timezone.utc).isoformat()
    with db_conn() as c:
        rows = c.execute(
            """SELECT t.id, t.cwd,
                      COALESCE(
                        (SELECT MAX(e.at) FROM events e WHERE e.task_id = t.id),
                        t.updated_at
                      ) AS last_at
               FROM tasks t
               WHERE t.status IN ('running', 'background_waiting')""",
        ).fetchall()
    closed = 0
    for r in rows:
        task_id = r["id"]
        cwd = r["cwd"]
        last_at = r["last_at"]
        if last_at and last_at > cutoff_iso_str:
            continue  # recent activity — leave alone
        try:
            transcript = claude_session_dir_for(cwd) / f"{task_id}.jsonl"
            if not transcript.exists():
                continue
            api_err = read_last_api_error(str(transcript))
            last_response = read_last_assistant_text(str(transcript))
            if api_err:
                new_status = "errored"
                error_reason = format_api_error_reason(api_err)
                broadcast_event = "errored"
            elif last_response:
                new_status = "completed"
                error_reason = None
                broadcast_event = "completed"
            else:
                continue  # transcript has nothing to claim yet
            ts = now_iso()
            with db_conn() as c:
                c.execute(
                    """UPDATE turns SET result=?, finished_at=?
                       WHERE task_id=? AND finished_at IS NULL""",
                    (last_response, ts, task_id),
                )
                c.execute(
                    "UPDATE tasks SET status=?, error_reason=?, updated_at=? WHERE id=?",
                    (new_status, error_reason, ts, task_id),
                )
                c.commit()
            for name in await tmux_list_sessions_for(task_id):
                await tmux_kill(name)
            await broadcast(
                task_id,
                {
                    "event": broadcast_event,
                    "at": ts,
                    "result": last_response,
                    "error_reason": error_reason,
                },
            )
            closed += 1
            tag = error_reason or "missed Stop"
            print(
                f"[watchdog] task {task_id[:8]} closed as {new_status}: {tag}",
                flush=True,
            )
        except Exception as e:
            print(f"[watchdog] task {task_id[:8]} sweep failed: {e!r}", flush=True)
    return closed


async def watchdog_loop() -> None:
    while True:
        try:
            await sweep_stuck_tasks()
        except Exception as e:
            print(f"[watchdog] loop error: {e!r}", flush=True)
        await asyncio.sleep(WATCHDOG_INTERVAL_SEC)


# ── Build queue (sequential, shared-branch overnight runner) ───────────────────
# A project's queue is an ordered list of queue_items run ONE AT A TIME on a
# single dedicated worktree/branch (tank/queue-<proj8>), each item continuing on
# top of the previous item's auto-committed work — so a dependency chain like
# "schema → API → runner" just works. The runner is a single background loop
# (started in lifespan) that, per running project: checks the in-flight item's
# spawned task, finalizes it (completed→commit+done, errored→failed,
# awaiting_input→blocked+free the worktree), then pops the next eligible pending
# item. Blocked/failed items never stall the rest: their pending dependents are
# marked blocked and the runner moves on. A run is 'done' when nothing is left
# pending/running. `queue_wake` nudges the loop immediately on start/resume.
queue_wake = asyncio.Event()


async def ensure_queue_worktree(project_path: str, project_id: str,
                                base_branch: str) -> tuple[str, str]:
    """Return (worktree_path, branch) for the project's queue, creating the
    worktree if absent. Reuses an existing dir/branch across runs so progress
    (auto-commits) survives a pause/resume or server restart. Falls back through
    origin/<base> → local <base> → HEAD so it also works for projects with no
    remote."""
    branch = f"tank/queue-{project_id[:8]}"
    wt = cfg_path("worktrees_root") / f"queue-{project_id[:8]}"
    if wt.exists():
        return str(wt), branch

    async def _verify(ref: str) -> bool:
        rc, _, _ = await run_cmd("git", "-C", project_path,
                                 "rev-parse", "--verify", "--quiet", ref)
        return rc == 0

    await run_cmd("git", "-C", project_path, "fetch", "origin", base_branch)
    if await _verify(f"refs/heads/{branch}"):
        rc, out, err = await run_cmd(
            "git", "-C", project_path, "worktree", "add", str(wt), branch)
    else:
        base_ref = None
        for cand in (f"origin/{base_branch}", base_branch, "HEAD"):
            if await _verify(cand):
                base_ref = cand
                break
        if base_ref is None:
            raise RuntimeError(
                f"no usable base ref (tried origin/{base_branch}, {base_branch}, HEAD)"
            )
        rc, out, err = await run_cmd(
            "git", "-C", project_path, "worktree", "add", "-b", branch,
            str(wt), base_ref)
    if rc != 0:
        raise RuntimeError(
            f"git worktree add failed (rc={rc}): {(err or out).strip() or 'no output'}"
        )
    setup_workspace(wt)
    return str(wt), branch


def build_queue_prompt(project_id: str, item: sqlite3.Row, wt: str) -> str:
    token = cfg("api_token").strip()
    auth = f" -H 'Authorization: Bearer {token}'" if token else ""
    base = f"http://localhost:{PORT}"
    detail = (item["detail"] or "").strip()
    return f"""You are an automated build agent working through an overnight build \
queue for this project. This is item #{item['seq']}: {item['title']}

{detail}

## Standing instructions (read carefully)
- You are running UNATTENDED as one step of a queue. Do NOT ask the user \
questions, do NOT use AskUserQuestion, and do NOT enter plan mode. Make \
reasonable, well-judged assumptions and proceed. If you are genuinely blocked, \
write the blocker clearly as your final message and stop.
- Before writing code, do a quick web search to confirm the current best \
approach / library / API for this item, and note in a line or two what you found.
- Work ONLY inside this directory: {wt}
- Do NOT run git commit — tank commits your work to the queue branch \
automatically when you finish this item.
- You MAY refine the remaining plan. To add / edit / reorder items that are \
still 'pending', call tank's queue API (this project's id is {project_id}):
    curl -s{auth} {base}/projects/{project_id}/queue
    curl -s{auth} -X POST {base}/projects/{project_id}/queue \\
         -H 'Content-Type: application/json' \\
         -d '{{"items":[{{"title":"...","detail":"..."}}]}}'
    curl -s{auth} -X PATCH {base}/projects/{project_id}/queue/<item_id> \\
         -H 'Content-Type: application/json' -d '{{"detail":"..."}}'
  Only ever touch items whose status is still 'pending'.

When finished, summarise what you changed in your final message."""


def _queue_result_text(task_id: Optional[str]) -> Optional[str]:
    if not task_id:
        return None
    with db_conn() as c:
        r = c.execute(
            "SELECT result FROM turns WHERE task_id=? ORDER BY turn_num DESC LIMIT 1",
            (task_id,),
        ).fetchone()
    return r["result"] if r else None


async def _kill_task_tmux(task_id: Optional[str]) -> None:
    if not task_id:
        return
    for name in await tmux_list_sessions_for(task_id):
        await tmux_kill(name)


async def _queue_commit(wt: str, item: sqlite3.Row) -> bool:
    """Stage + commit everything in the queue worktree. Returns True if a commit
    was created (rc 0); a 'nothing to commit' no-op returns False. Sets an
    explicit identity so it works even where the service user has no git config."""
    await run_cmd("git", "-C", wt, "add", "-A")
    rc, _, _ = await run_cmd(
        "git", "-C", wt,
        "-c", "user.email=tank@local", "-c", "user.name=tank-queue",
        "commit", "-m", f"{item['title']} [tank-queue item {item['seq']}]",
    )
    return rc == 0


def _queue_log(project_id: str, item_id: Optional[str], kind: str, detail: str = "") -> None:
    print(f"[queue] {project_id[:8]} item={item_id} {kind} {detail}".rstrip(), flush=True)


def _next_eligible_item(c: sqlite3.Connection, project_id: str) -> Optional[sqlite3.Row]:
    """Lowest-seq pending item whose deps are all 'done'. Side effect: any pending
    item with a dep that's already failed/blocked is itself marked blocked (this
    is how a failure cascades to its dependents, one tick per level)."""
    items = c.execute(
        "SELECT * FROM queue_items WHERE project_id=? ORDER BY seq, created_at",
        (project_id,),
    ).fetchall()
    done = {i["id"] for i in items if i["status"] == "done"}
    dead = {i["id"] for i in items if i["status"] in ("failed", "blocked")}
    changed = False
    pick = None
    for it in items:
        if it["status"] != "pending":
            continue
        deps = [d.strip() for d in (it["depends_on"] or "").split(",") if d.strip()]
        if any(d in dead for d in deps):
            c.execute(
                "UPDATE queue_items SET status='blocked', result=?, updated_at=? WHERE id=?",
                ("blocked: a prerequisite item failed or is blocked", now_iso(), it["id"]),
            )
            changed = True
            continue
        if pick is None and all(d in done for d in deps):
            pick = it
    if changed:
        c.commit()
    return pick


async def _spawn_queue_item(project_id: str, item: sqlite3.Row, wt: str,
                            branch: str) -> None:
    if spawn_semaphore is None:
        return
    task_id = str(uuid.uuid4())
    ts = now_iso()
    prompt = build_queue_prompt(project_id, item, wt)
    title = f"[queue {item['seq']}] {item['title']}"[:200]
    setup_workspace(Path(wt))
    with db_conn() as c:
        c.execute(
            """INSERT INTO tasks (id, project_id, title, cwd, status,
                                  branch, worktree_path, created_at, updated_at)
               VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, ?)""",
            (task_id, project_id, title, wt, branch, wt, ts, ts),
        )
        c.execute(
            "INSERT INTO turns (task_id, turn_num, prompt, started_at) VALUES (?, 1, ?, ?)",
            (task_id, prompt, ts),
        )
        c.execute(
            "UPDATE queue_items SET status='running', agent_task_id=?, updated_at=? WHERE id=?",
            (task_id, ts, item["id"]),
        )
        c.execute(
            "UPDATE queue_runs SET current_item=?, updated_at=? WHERE project_id=?",
            (item["id"], ts, project_id),
        )
        c.commit()
    try:
        async with spawn_semaphore:
            await spawn_first_turn(task_id, prompt, Path(wt))
    except Exception as e:
        with db_conn() as c:
            c.execute("UPDATE tasks SET status='failed', updated_at=? WHERE id=?",
                      (now_iso(), task_id))
            c.execute(
                "UPDATE queue_items SET status='failed', result=?, updated_at=? WHERE id=?",
                (f"spawn failed: {e}", now_iso(), item["id"]),
            )
            c.execute("UPDATE queue_runs SET current_item=NULL, updated_at=? WHERE project_id=?",
                      (now_iso(), project_id))
            c.commit()
        _queue_log(project_id, item["id"], "spawn_failed", str(e))
        return
    _queue_log(project_id, item["id"], "running", f"task={task_id[:8]}")


async def advance_queue_for_project(run: sqlite3.Row) -> None:
    pid = run["project_id"]
    wt = run["worktree_path"]
    branch = run["branch"]

    # 1) Resolve the in-flight item against its spawned task's status.
    cur_id = run["current_item"]
    if cur_id:
        with db_conn() as c:
            item = c.execute("SELECT * FROM queue_items WHERE id=?", (cur_id,)).fetchone()
        if item and item["status"] == "running":
            tstatus = None
            if item["agent_task_id"]:
                with db_conn() as c:
                    tr = c.execute("SELECT status FROM tasks WHERE id=?",
                                   (item["agent_task_id"],)).fetchone()
                tstatus = tr["status"] if tr else None
            if tstatus in ("queued", "running", "background_waiting"):
                return  # still working — re-check next tick
            if tstatus == "completed":
                committed = await _queue_commit(wt, item)
                with db_conn() as c:
                    c.execute(
                        "UPDATE queue_items SET status='done', result=?, updated_at=? WHERE id=?",
                        (_queue_result_text(item["agent_task_id"]), now_iso(), cur_id),
                    )
                    c.commit()
                _queue_log(pid, cur_id, "done", f"committed={committed}")
            elif tstatus == "awaiting_input":
                # Needs a human. Free the shared worktree so the rest of the list
                # isn't held hostage; the task itself stays resumable.
                await _kill_task_tmux(item["agent_task_id"])
                with db_conn() as c:
                    c.execute(
                        "UPDATE queue_items SET status='blocked', result=?, updated_at=? WHERE id=?",
                        ("blocked: agent asked for input — resume its task to answer",
                         now_iso(), cur_id),
                    )
                    c.commit()
                _queue_log(pid, cur_id, "blocked", "awaiting_input")
            else:  # errored / killed / interrupted / failed / unknown
                with db_conn() as c:
                    c.execute(
                        "UPDATE queue_items SET status='failed', result=?, updated_at=? WHERE id=?",
                        (f"failed: task ended '{tstatus}'", now_iso(), cur_id),
                    )
                    c.commit()
                _queue_log(pid, cur_id, "failed", str(tstatus))
        # Clear the pointer now that this item is resolved (or vanished).
        with db_conn() as c:
            c.execute("UPDATE queue_runs SET current_item=NULL, updated_at=? WHERE project_id=?",
                      (now_iso(), pid))
            c.commit()

    # 2) Pop the next eligible pending item (or finish the run).
    with db_conn() as c:
        nxt = _next_eligible_item(c, pid)
    if not nxt:
        with db_conn() as c:
            remaining = c.execute(
                "SELECT COUNT(*) AS n FROM queue_items "
                "WHERE project_id=? AND status IN ('pending','running')",
                (pid,),
            ).fetchone()["n"]
        if remaining == 0:
            with db_conn() as c:
                c.execute("UPDATE queue_runs SET status='done', updated_at=? WHERE project_id=?",
                          (now_iso(), pid))
                c.commit()
            _queue_log(pid, None, "run_done", "")
        return
    await _spawn_queue_item(pid, nxt, wt, branch)


async def queue_runner_tick() -> None:
    with db_conn() as c:
        runs = c.execute("SELECT * FROM queue_runs WHERE status='running'").fetchall()
    for run in runs:
        try:
            await advance_queue_for_project(run)
        except Exception as e:
            print(f"[queue] advance error for {run['project_id'][:8]}: {e!r}", flush=True)


async def queue_runner_loop() -> None:
    while True:
        try:
            await queue_runner_tick()
        except Exception as e:
            print(f"[queue] loop error: {e!r}", flush=True)
        try:
            await asyncio.wait_for(queue_wake.wait(), timeout=max(2, cfg_int("queue_poll_secs")))
        except asyncio.TimeoutError:
            pass
        queue_wake.clear()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global spawn_semaphore
    db_init()  # creates + seeds config, loads the cfg cache
    spawn_semaphore = asyncio.Semaphore(cfg_int("max_concurrent"))
    # A preview instance shares the real tank's tmux server / uploads / trust
    # file — it must not run any of the housekeeping that mutates that shared
    # state (esp. reconcile_previews, which would reap the very session it runs
    # inside). Serve read-only and skip all of it.
    if PREVIEW_MODE:
        print("[lifespan] TANK_PREVIEW_MODE=1 — skipping reconcile + sweeper loops", flush=True)
        yield
        return
    # Tidy any stale trust entries left from prior runs.
    sweep_orphan_project_entries()
    # Catch tasks whose tmux died while we were down (reboot, crash, etc.).
    await reconcile_orphan_tasks()
    # Re-adopt any dev previews whose tmux survived the restart (and reap any
    # left behind by since-deleted tasks).
    await reconcile_previews()
    # Catch tasks whose claude bailed mid-flight with an API error and whose
    # Stop hook never reached us.
    watchdog_task = asyncio.create_task(watchdog_loop())
    upload_sweeper = asyncio.create_task(upload_sweeper_loop())
    # Drive the build queue: pop eligible items, spawn their tasks, advance.
    queue_runner = asyncio.create_task(queue_runner_loop())
    try:
        yield
    finally:
        watchdog_task.cancel()
        upload_sweeper.cancel()
        queue_runner.cancel()
        for t in (watchdog_task, upload_sweeper, queue_runner):
            with contextlib.suppress(asyncio.CancelledError):
                await t


TAGS_METADATA = [
    {"name": "tasks",
     "description": "Spawn and drive interactive Claude Code agent sessions."},
    {"name": "projects",
     "description": "Projects (repos / workspaces) that an agent task runs inside."},
    {"name": "system",
     "description": "Liveness and service metadata."},
]

API_DESCRIPTION = """\
tank is an HTTP API for running **interactive Claude Code coding agents** on demand.

Each *task* is a real `claude` session running in an isolated git worktree on a
server: you POST a prompt, the agent autonomously edits code and runs tools, and
you poll or stream the task until it finishes. This is **asynchronous agent
execution**, not a chat-completion endpoint — start a task, then watch it.

**Typical loop:** `GET /projects` → `POST /tasks` → `GET /tasks/{id}/stream`
(or poll `GET /tasks/{id}`) → optional `POST /tasks/{id}/continue`.

**Auth.** When the server has an API token configured, send it as
`Authorization: Bearer <token>` on every request. With no token configured the
API is open (the LAN-trust default).

For a compact, paste-into-an-AI-agent version of this reference, see
[`/llms.txt`](/llms.txt).
"""

app = FastAPI(
    title="tank",
    description=API_DESCRIPTION,
    version="1.0.0",
    openapi_tags=TAGS_METADATA,
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ── API-token auth ────────────────────────────────────────────────────────────
# A single shared bearer token gates the whole HTTP surface when `api_token` is
# configured. Empty token = auth off (historical LAN-trust behaviour). External
# programs send `Authorization: Bearer <token>`; the browser SPA stores the
# token and authenticates via the `tank_token` cookie (so same-origin fetch,
# EventSource, and WebSocket all carry it automatically).

def _token_ok(scope, token: str) -> bool:
    """True if `scope` (a Request or WebSocket) presents the configured token by
    any accepted channel."""
    auth = scope.headers.get("authorization", "")
    if auth.startswith("Bearer ") and auth[len("Bearer "):].strip() == token:
        return True
    if scope.headers.get("x-api-token", "").strip() == token:
        return True
    if scope.cookies.get("tank_token") == token:
        return True
    # Query param — the only channel a browser EventSource can use, since it
    # can't set headers. Same-origin EventSource sends the cookie too, so this
    # is mostly for non-browser SSE clients.
    if scope.query_params.get("token") == token:
        return True
    return False


# Reachable without a token even when one is set: liveness, the SPA shell + its
# static assets, the self-describing API docs (so an agent can read how to
# authenticate before it has credentials), and the local hook receiver
# (localhost-only, keyed by an unguessable task UUID; hook.sh carries no token).
_AUTH_EXEMPT_PREFIXES = ("/static/",)
_AUTH_EXEMPT_PATHS = {
    "/", "/health", "/favicon.ico", "/llms.txt",
    "/docs", "/redoc", "/openapi.json",
}


@app.middleware("http")
async def api_token_guard(request: Request, call_next):
    token = cfg("api_token").strip()
    if token:
        path = request.url.path
        exempt = (
            path in _AUTH_EXEMPT_PATHS
            or path.startswith(_AUTH_EXEMPT_PREFIXES)
            or path.endswith("/events")  # hook receiver
        )
        if not exempt and not _token_ok(request, token):
            return JSONResponse(
                {"detail": "missing or invalid API token — send "
                           "'Authorization: Bearer <token>'"},
                status_code=401,
            )
    return await call_next(request)


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def root_index() -> HTMLResponse:
    # no-store so a deploy is picked up immediately — the HTML inlines all
    # CSS+JS, and stale copies stick around long enough for users to see
    # status states the old JS doesn't understand (e.g. awaiting_input
    # rendered as "no result captured" + invisible dot).
    #
    # {{HOUSE_STYLE_BASE}} is the base each house-style <link>/<script> loads
    # from. Blank house_style_url → the vendored /static/house-style/ copy
    # (public-safe, offline, no internal hostname in the repo); set it to a
    # design-system URL to track a live channel. Either way each tag keeps a
    # vendored onerror fallback, so when the base IS the vendored copy the
    # primary == fallback and onerror never fires.
    base = cfg("house_style_url").strip().rstrip("/") or "/static/house-style"
    html = (STATIC_DIR / "index.html").read_text().replace("{{HOUSE_STYLE_BASE}}", base)
    return HTMLResponse(
        content=html,
        headers={"Cache-Control": "no-store, max-age=0"},
    )


@app.get("/favicon.ico", include_in_schema=False)
async def favicon() -> Response:
    # Bare browser requests for /favicon.ico (ignoring the <link> tag) get the
    # SVG mark rather than a 404 in the logs.
    return RedirectResponse(url="/static/favicon.svg")


@app.get("/health", response_model=HealthResult, tags=["system"],
         summary="Liveness probe")
async def health() -> dict:
    return {"status": "ok", "ts": now_iso()}


# A compact, model-readable description of the public API — the thing you paste
# into an AI agent so it knows how to drive tank. {base} and {auth} are filled
# per-request so the doc always shows the real host + whether a token is needed.
LLMS_TXT_TEMPLATE = """\
# tank — an HTTP API for running Claude Code agents

tank spawns real, interactive Claude Code coding agents on demand. Each "task"
is a `claude` session running in an isolated git worktree on a server. You give
it a prompt; it autonomously edits code, runs tools, and reports back. This is
ASYNCHRONOUS agent execution, not a chat/completion endpoint: you start a task,
then poll or stream it until it finishes.

Base URL: {base}
{auth}

## Core loop
1. List projects to get a project_id:        GET  /projects
2. Spawn an agent in that project:            POST /tasks
3. Watch it work (events + result):           GET  /tasks/{{id}}/stream   (SSE)
   or poll:                                    GET  /tasks/{{id}}
4. Send a follow-up turn (optional):          POST /tasks/{{id}}/continue
5. Stop it early (optional):                  POST /tasks/{{id}}/stop

## POST /tasks  — spawn an agent
Body: {{ "project_id": "<id>", "prompt": "<what the agent should do>" }}
Returns: {{ "task_id": "...", "status": "running" }}

## GET /tasks/{{id}}  — current state
Returns: {{ task: {{id, title, status, branch, ...}}, turns: [...], events: [...] }}
status lifecycle: queued -> running -> awaiting_input | background_waiting
                  -> done | failed
- running:            the agent is actively working
- awaiting_input:     it asked a question; reply with POST /tasks/{{id}}/continue
- background_waiting: parked on background subagents; it will resume itself
- done / failed:      terminal

## GET /tasks/{{id}}/stream  — Server-Sent Events
Emits one JSON event per agent tool-use and status change. Subscribe instead of
polling. Each event is a JSON object with an "event" field.

## POST /tasks/{{id}}/continue  — follow-up turn
Body: {{ "prompt": "<next instruction>" }}. Use this to answer an
awaiting_input task or to give the same agent more work on its worktree.

## Build queue  — let tank work through a list unattended
A project can hold an ordered build queue that tank runs ONE ITEM AT A TIME, each
on the same dedicated branch (tank/queue-<proj8>), auto-committing after every
item so later items build on earlier ones. Populate it, press Go, walk away.
- Add items (bulk):   POST   /projects/{{id}}/queue
    Body: {{ "items": [ {{"title":"...","detail":"...optional..."}}, ... ] }}
- List + statuses:    GET    /projects/{{id}}/queue   -> {{ run, items[] }}
- Edit/reorder/remove (pending items only):
    PATCH/DELETE /projects/{{id}}/queue/{{item_id}} ; POST /projects/{{id}}/queue/reorder
- Seed from TODO.md:  POST   /projects/{{id}}/queue/import-todos
- Start (the Go):     POST   /projects/{{id}}/queue/start
- Pause / resume:     POST   /projects/{{id}}/queue/pause | .../resume
- Clear (reset):      DELETE /projects/{{id}}/queue
Item status: pending -> running -> done | failed | blocked. A running item's
agent may itself POST/PATCH the remaining pending items to refine the plan.

## Notes for callers
- Tasks are durable; a task_id stays valid after the agent stops (you can resume).
- One task == one git branch/worktree. To land it: POST /tasks/{{id}}/ship
  (commit + open PR + merge in one turn), or the separate /pr and /merge steps.
- Treat latency like a human coding session (seconds-to-minutes), not an API call.
- Full interactive reference + schemas: {base}docs  (OpenAPI: {base}openapi.json)
"""

_LLMS_AUTH_ON = (
    "Auth: send `Authorization: Bearer <token>` on every request (ask the tank\n"
    "operator for the token)."
)
_LLMS_AUTH_OFF = (
    "Auth: none required (this tank is open on its trusted network)."
)


@app.get("/llms.txt", response_class=PlainTextResponse, include_in_schema=False)
async def llms_txt(request: Request) -> PlainTextResponse:
    """Compact, paste-into-an-AI-agent guide to the public API."""
    auth = _LLMS_AUTH_ON if cfg("api_token").strip() else _LLMS_AUTH_OFF
    body = LLMS_TXT_TEMPLATE.format(base=str(request.base_url), auth=auth)
    return PlainTextResponse(body)


# ── Image uploads ────────────────────────────────────────────────────────────

@app.post("/uploads", include_in_schema=False)
async def create_upload(file: UploadFile = File(...)) -> dict:
    """Accept a single image. Returns the on-disk path the frontend should
    prepend to the prompt body. Validates MIME and size; rejects everything
    else."""
    ctype = (file.content_type or "").lower()
    if ctype not in UPLOAD_ALLOWED_MIME:
        raise HTTPException(415, f"unsupported content type: {ctype or 'unknown'}")
    data = await file.read(UPLOAD_MAX_BYTES + 1)
    if len(data) > UPLOAD_MAX_BYTES:
        raise HTTPException(413, f"file exceeds {UPLOAD_MAX_BYTES} bytes")
    if not data:
        raise HTTPException(400, "empty file")
    upload_id = str(uuid.uuid4())
    dest_dir = UPLOADS_ROOT / upload_id
    dest_dir.mkdir(parents=True, exist_ok=False)
    fname = _safe_upload_name(file.filename or "", ctype)
    dest = dest_dir / fname
    dest.write_bytes(data)
    return {
        "upload_id": upload_id,
        "path": str(dest),
        "name": fname,
        "size": len(data),
        "content_type": ctype,
    }


@app.delete("/uploads/{upload_id}", include_in_schema=False)
async def delete_upload(upload_id: str) -> dict:
    """Remove an upload subdir (user removed the attachment before sending)."""
    try:
        uuid.UUID(upload_id)
    except ValueError:
        raise HTTPException(400, "invalid upload id")
    target = UPLOADS_ROOT / upload_id
    if not target.exists():
        return {"ok": True, "removed": False}
    shutil.rmtree(target, ignore_errors=True)
    return {"ok": True, "removed": True}


@app.get("/auth/status", include_in_schema=False)
async def auth_status() -> dict:
    return await claude_auth_status()


@app.get("/usage", include_in_schema=False)
async def usage() -> dict:
    return await fetch_usage()


# Keys the dashboard settings cog is allowed to read/write. Bootstrap config
# (install dir, port) is deliberately excluded — it lives in env/systemd.
EDITABLE_CONFIG_KEYS = set(RUNTIME_CONFIG_DEFAULTS)


@app.get("/settings", include_in_schema=False)
async def get_settings() -> dict:
    """Current runtime config for the settings cog. Reflects DB → env → default
    resolution for every editable key."""
    return {k: cfg(k) for k in RUNTIME_CONFIG_DEFAULTS}


@app.patch("/settings", include_in_schema=False)
async def update_settings(body: dict) -> dict:
    """Persist edited config keys into the config table and refresh the cache.
    Unknown keys are ignored. Note: max_concurrent only takes effect on the
    next restart (the spawn semaphore is sized once at startup)."""
    updates = {
        k: ("" if v is None else str(v))
        for k, v in body.items()
        if k in EDITABLE_CONFIG_KEYS
    }
    if not updates:
        raise HTTPException(400, "no recognised settings keys in body")
    with db_conn() as c:
        for k, v in updates.items():
            c.execute(
                """INSERT INTO config (key, value) VALUES (?, ?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                (k, v),
            )
        c.commit()
    load_config_cache()
    return {k: cfg(k) for k in RUNTIME_CONFIG_DEFAULTS}


# ── Forgejo repo flattening ───────────────────────────────────
# List repos in the configured Forgejo and flatten any one of them into a single
# text file (all text sources concatenated with path headers). Uses the Forgejo
# API with a base URL + token from runtime config (forgejo_url / forgejo_token),
# independent of git_provider (which only gates the per-task PR/merge buttons).

# Per-file content cap: bigger files are listed but not inlined, so one checked-in
# blob can't dominate the output.
FLATTEN_MAX_FILE_BYTES = 512 * 1024
# Hard cap on total flattened output, so a huge repo can't OOM the box.
FLATTEN_MAX_TOTAL_BYTES = 20 * 1024 * 1024
# Path fragments whose files we never include (VCS noise / vendored / build).
FLATTEN_SKIP_DIRS = ("/.git/", "/node_modules/", "/.venv/", "/__pycache__/")


def forgejo_config() -> tuple[str, str]:
    """(base_url, token) for the Forgejo API, or 400 if unconfigured. base_url is
    normalised with no trailing slash."""
    url = cfg("forgejo_url").strip().rstrip("/")
    token = cfg("forgejo_token").strip()
    if not url or not token:
        raise HTTPException(
            400,
            "Forgejo is not configured — set forgejo_url and forgejo_token in settings.",
        )
    return url, token


def _forgejo_headers(token: str) -> dict:
    # Forgejo/Gitea accept the "token <value>" Authorization scheme.
    return {"Authorization": f"token {token}", "Accept": "application/json"}


async def forgejo_list_repos() -> list[dict]:
    """Repos the token can see, most-recently-updated first. Pages through
    /api/v1/repos/search, capped so a giant instance can't hang us."""
    base, token = forgejo_config()
    repos: list[dict] = []
    async with httpx.AsyncClient(timeout=20.0) as client:
        for page in range(1, 11):  # up to 10 * 50 = 500 repos
            r = await client.get(
                f"{base}/api/v1/repos/search",
                params={"limit": 50, "page": page, "sort": "updated", "order": "desc"},
                headers=_forgejo_headers(token),
            )
            if r.status_code == 401:
                raise HTTPException(401, "Forgejo rejected the token (401).")
            r.raise_for_status()
            batch = (r.json() or {}).get("data") or []
            if not batch:
                break
            for repo in batch:
                repos.append({
                    "full_name": repo.get("full_name"),
                    "name": repo.get("name"),
                    "owner": (repo.get("owner") or {}).get("login"),
                    "default_branch": repo.get("default_branch") or "",
                    "private": bool(repo.get("private")),
                    "empty": bool(repo.get("empty")),
                })
            if len(batch) < 50:
                break
    return repos


def _looks_binary(chunk: bytes) -> bool:
    return b"\x00" in chunk


def flatten_archive_bytes(raw: bytes, repo_label: str, ref: str) -> str:
    """Turn a .tar.gz repo archive into one annotated text blob. Forgejo wraps
    every entry under a top-level "<repo>/" dir, which we strip for clean
    repo-relative paths. Binary and oversized files are noted but not inlined."""
    parts: list[str] = []
    included = skipped = total = 0
    truncated = False
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tf:
        members = sorted(
            (m for m in tf.getmembers() if m.isfile()),
            key=lambda m: m.name,
        )
        for m in members:
            rel = m.name.split("/", 1)[1] if "/" in m.name else m.name
            if not rel:
                continue
            if any(seg in ("/" + rel + "/") for seg in FLATTEN_SKIP_DIRS):
                skipped += 1
                continue
            if m.size > FLATTEN_MAX_FILE_BYTES:
                parts.append(f"===== {rel} ({m.size} bytes, skipped: too large) =====\n")
                skipped += 1
                continue
            f = tf.extractfile(m)
            if f is None:
                skipped += 1
                continue
            data = f.read()
            if _looks_binary(data[:8192]):
                parts.append(f"===== {rel} ({m.size} bytes, skipped: binary) =====\n")
                skipped += 1
                continue
            text = data.decode("utf-8", errors="replace")
            block = f"===== {rel} =====\n{text}\n"
            if total + len(block) > FLATTEN_MAX_TOTAL_BYTES:
                truncated = True
                break
            parts.append(block)
            total += len(block)
            included += 1

    header = [
        f"# Flattened repository: {repo_label}",
        f"# Ref: {ref}",
        f"# Generated by tank at {now_iso()}",
        f"# Files included: {included}, skipped: {skipped}",
    ]
    if truncated:
        header.append(
            f"# NOTE: output truncated at {FLATTEN_MAX_TOTAL_BYTES} bytes — "
            "some files were omitted."
        )
    return "\n".join(header) + "\n\n" + "".join(parts)


@app.get("/forgejo/repos", include_in_schema=False)
async def forgejo_repos() -> dict:
    """List Forgejo repos for the flatten picker."""
    return {"repos": await forgejo_list_repos()}


@app.get("/forgejo/flatten", include_in_schema=False)
async def forgejo_flatten(repo: str, ref: Optional[str] = None) -> Response:
    """Download <repo> (owner/name) as a single flattened text file. Resolves the
    default branch when ref is omitted, fetches the .tar.gz archive from the
    Forgejo API, flattens it, and returns text/plain as an attachment."""
    base, token = forgejo_config()
    if "/" not in repo or ".." in repo:
        raise HTTPException(400, "repo must be in 'owner/name' form")
    owner, _, name = repo.partition("/")
    if not owner or not name:
        raise HTTPException(400, "invalid repo")
    async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
        if not ref:
            info = await client.get(
                f"{base}/api/v1/repos/{owner}/{name}",
                headers=_forgejo_headers(token),
            )
            if info.status_code == 404:
                raise HTTPException(404, "repo not found")
            if info.status_code == 401:
                raise HTTPException(401, "Forgejo rejected the token (401).")
            info.raise_for_status()
            ref = (info.json() or {}).get("default_branch") or "main"
        arc = await client.get(
            f"{base}/api/v1/repos/{owner}/{name}/archive/{ref}.tar.gz",
            headers=_forgejo_headers(token),
        )
        if arc.status_code == 404:
            raise HTTPException(404, f"archive not found for ref '{ref}'")
        if arc.status_code == 401:
            raise HTTPException(401, "Forgejo rejected the token (401).")
        arc.raise_for_status()
        raw = arc.content
    try:
        flat = flatten_archive_bytes(raw, repo, ref)
    except tarfile.TarError as e:
        raise HTTPException(502, f"could not read archive: {e}")
    filename = f"{owner}-{name}-{_slugify(ref)}.txt"
    return Response(
        content=flat,
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/auth/login/start", include_in_schema=False)
async def auth_login_start() -> dict:
    # Kill any prior auth session and orphan claude procs.
    await tmux_kill(AUTH_TMUX)
    await run_cmd("pkill", "-u", SERVICE_USER, "claude")
    await asyncio.sleep(0.3)
    # Spawn `claude auth login` in a wide pty (800 cols) for safety.
    await tmux_new_session(AUTH_TMUX, str(Path.home()), "claude auth login", width=800)
    # Poll for the URL to appear (up to ~30s; first run can be slow).
    # Use -J to join wrapped lines so we see the full URL even if it overflows.
    url: Optional[str] = None
    last_pane = ""
    for _ in range(60):
        await asyncio.sleep(0.5)
        last_pane = await tmux_capture(AUTH_TMUX, join_wrapped=True)
        url = _extract_auth_url(last_pane)
        if url:
            break
    if not url:
        await tmux_kill(AUTH_TMUX)
        raise HTTPException(
            500,
            f"could not extract auth URL from claude output. last pane:\n---\n{last_pane}",
        )
    return {"url": url, "tmux_session": AUTH_TMUX}


class AuthCodeBody(BaseModel):
    code: str


@app.post("/auth/login/code", include_in_schema=False)
async def auth_login_code(body: AuthCodeBody) -> dict:
    code = body.code.strip()
    if not code:
        raise HTTPException(400, "code is required")
    # Send the code to the waiting prompt.
    await tmux_send_keys(AUTH_TMUX, code)
    # Wait for claude to settle, then snapshot the final state.
    await asyncio.sleep(2.0)
    pane = await tmux_capture(AUTH_TMUX)
    # Cross-check by hitting `claude auth status` (independent of the tmux text).
    await asyncio.sleep(0.5)
    status = await claude_auth_status()
    await tmux_kill(AUTH_TMUX)
    return {"final_pane": pane, **status}


@app.post("/auth/login/cancel", include_in_schema=False)
async def auth_login_cancel() -> dict:
    await tmux_kill(AUTH_TMUX)
    return {"ok": True}


@app.post("/auth/logout", include_in_schema=False)
async def auth_logout() -> dict:
    """Run `claude auth logout` to clear ~/.claude/.credentials.json. Unlike
    login this is non-interactive, so no tmux is needed. We kill any
    half-finished login session first, then cross-check the status afterward so
    the dashboard can confirm the creds are actually gone."""
    await tmux_kill(AUTH_TMUX)
    rc, out, err = await run_cmd("claude", "auth", "logout")
    status = await claude_auth_status()
    return {"ok": rc == 0, "detail": (out + err).strip(), **status}


@app.post("/tasks", response_model=TaskHandle, tags=["tasks"],
          summary="Spawn an interactive Claude Code agent",
          description=(
              "Starts a real `claude` session in tmux inside the project's "
              "workspace (an isolated git worktree when the project has "
              "`isolate_tasks` on) and sends `prompt` as the first turn. "
              "Returns immediately with a `task_id` — the agent then works "
              "asynchronously. Poll `GET /tasks/{id}` or subscribe to "
              "`GET /tasks/{id}/stream` to watch tool-use events and collect "
              "the result. This is agent execution, not a chat completion."))
async def create_task(body: CreateTaskBody) -> dict:
    if spawn_semaphore is None:
        raise HTTPException(503, "not ready")

    with db_conn() as c:
        project = c.execute(
            "SELECT id, path, kind, isolate_tasks, base_branch FROM projects WHERE id=?",
            (body.project_id,),
        ).fetchone()
    if not project:
        raise HTTPException(404, "project not found")

    task_id = str(uuid.uuid4())
    ts = now_iso()
    title = placeholder_title(body.prompt)

    # If the project opts into per-task isolation, branch a worktree off
    # base_branch and run the task there instead of in the project root.
    branch: Optional[str] = None
    worktree_path: Optional[str] = None
    if project["isolate_tasks"]:
        # The branch slug is derived from the placeholder title (first words of
        # the prompt). We deliberately do NOT block on the AI title here: a slow
        # or unresponsive local endpoint would stall "Create & Run" for up to
        # TITLE_TIMEOUT_SEC before anything spawns. The background regen below
        # swaps the displayed title in over SSE once it lands; the branch keeps
        # its placeholder slug (cosmetic, and renaming a live worktree branch
        # isn't worth the complexity).
        try:
            worktree_path, branch = await create_worktree(
                project["path"], task_id, project["base_branch"] or "main", title,
            )
        except Exception as e:
            raise HTTPException(500, f"could not create worktree: {e}")
        cwd = setup_workspace(Path(worktree_path))
    else:
        cwd = setup_workspace(Path(project["path"]))
    with db_conn() as c:
        c.execute(
            """INSERT INTO tasks (id, project_id, title, cwd, status,
                                  branch, worktree_path,
                                  created_at, updated_at)
               VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, ?)""",
            (task_id, body.project_id, title, str(cwd), branch, worktree_path, ts, ts),
        )
        c.execute(
            """INSERT INTO turns (task_id, turn_num, prompt, started_at)
               VALUES (?, 1, ?, ?)""",
            (task_id, body.prompt, ts),
        )
        c.execute(
            "UPDATE projects SET updated_at=? WHERE id=?",
            (ts, body.project_id),
        )
        c.commit()

    # Regenerate the title in the background so spawning isn't blocked on the
    # AI endpoint. Silently no-ops if AI features are off or unreachable.
    asyncio.create_task(generate_and_set_title(task_id, body.prompt))

    async with spawn_semaphore:
        try:
            await spawn_first_turn(task_id, body.prompt, cwd)
        except Exception as e:
            with db_conn() as c:
                c.execute(
                    "UPDATE tasks SET status='failed', updated_at=? WHERE id=?",
                    (now_iso(), task_id),
                )
                c.commit()
            raise HTTPException(500, str(e))

    await broadcast(task_id, {"event": "spawned", "at": now_iso()})
    return {"task_id": task_id, "status": "running"}


@app.get("/tasks", response_model=list[TaskRow], tags=["tasks"],
         summary="List tasks (most recently active first, optionally by project)")
async def list_tasks(project_id: Optional[str] = None) -> list[dict]:
    with db_conn() as c:
        if project_id:
            rows = c.execute(
                "SELECT * FROM tasks WHERE project_id=? ORDER BY updated_at DESC LIMIT 200",
                (project_id,),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM tasks ORDER BY updated_at DESC LIMIT 200"
            ).fetchall()
    return [dict(r) for r in rows]


def _search_snippet(text: str, q: str, radius: int = 80) -> str:
    """A ~160-char window of `text` centred on the first case-insensitive hit
    of `q`, with ellipses where it was clipped. Returns raw text; the client
    escapes it and highlights the match."""
    if not text:
        return ""
    idx = text.lower().find(q.lower())
    if idx < 0:
        return text[: radius * 2].strip()
    start = max(0, idx - radius)
    end = min(len(text), idx + len(q) + radius)
    snip = " ".join(text[start:end].split())  # collapse whitespace/newlines
    if start > 0:
        snip = "… " + snip
    if end < len(text):
        snip = snip + " …"
    return snip


# Prefer showing the field the operator most likely searched for: their own
# prompt first, then claude's reply, then the bare title.
_SEARCH_FIELD_RANK = {"prompt": 0, "result": 1, "title": 2}


@app.get("/search", include_in_schema=False)
async def search(q: str = "", limit: int = 50) -> list[dict]:
    """Free-text search across every task title and every turn (prompt +
    result), newest task first. One row per matching task with a snippet of
    the best match. UI helper behind the sidebar search box; substring match
    is plenty at this scale."""
    q = (q or "").strip()
    if len(q) < 2:
        return []
    like = f"%{q}%"
    with db_conn() as c:
        rows = c.execute(
            """
            SELECT t.id AS task_id, t.title, t.status, t.updated_at,
                   t.project_id, p.name AS project_name, p.kind AS project_kind,
                   tn.turn_num, tn.prompt, tn.result
            FROM tasks t
            JOIN projects p ON p.id = t.project_id
            LEFT JOIN turns tn ON tn.task_id = t.id
            WHERE t.title LIKE ? OR tn.prompt LIKE ? OR tn.result LIKE ?
            ORDER BY t.updated_at DESC
            """,
            (like, like, like),
        ).fetchall()

    ql = q.lower()
    best: dict[str, dict] = {}
    for r in rows:
        if r["prompt"] and ql in r["prompt"].lower():
            field, src = "prompt", r["prompt"]
        elif r["result"] and ql in r["result"].lower():
            field, src = "result", r["result"]
        elif r["title"] and ql in r["title"].lower():
            field, src = "title", r["title"]
        else:
            continue  # LEFT JOIN row matched on a sibling turn, not this one
        cur = best.get(r["task_id"])
        if cur and _SEARCH_FIELD_RANK[cur["match_in"]] <= _SEARCH_FIELD_RANK[field]:
            continue
        best[r["task_id"]] = {
            "task_id": r["task_id"],
            "title": r["title"],
            "status": r["status"],
            "updated_at": r["updated_at"],
            "project_id": r["project_id"],
            "project_name": r["project_name"],
            "project_kind": r["project_kind"],
            "match_in": field,
            "turn_num": r["turn_num"],
            "snippet": _search_snippet(src, q),
        }
    results = sorted(best.values(), key=lambda x: x["updated_at"], reverse=True)
    return results[:limit]


# ── Projects ──────────────────────────────────────────────────────────────────

@app.get("/projects", tags=["projects"],
         summary="List projects (each with a live task summary)")
async def list_projects(kind: Optional[str] = None) -> list[dict]:
    """List projects. Optional ?kind=project|chat filter. Each row carries a
    lightweight task summary (count + last activity)."""
    with db_conn() as c:
        if kind:
            rows = c.execute(
                "SELECT * FROM projects WHERE kind=? ORDER BY updated_at DESC",
                (kind,),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM projects ORDER BY kind, updated_at DESC"
            ).fetchall()
        out = []
        for r in rows:
            counts = c.execute(
                "SELECT COUNT(*) AS n FROM tasks WHERE project_id=?", (r["id"],)
            ).fetchone()
            # Live = a task currently demanding the operator's eye. live_status is
            # the highest-priority live status present, so the sidebar dot can
            # colour-match the per-task dots (awaiting > bg-waiting > running).
            live = c.execute(
                "SELECT status, COUNT(*) AS n FROM tasks "
                "WHERE project_id=? AND status IN "
                "('running','awaiting_input','background_waiting') "
                "GROUP BY status",
                (r["id"],),
            ).fetchall()
            live_by = {row["status"]: row["n"] for row in live}
            live_count = sum(live_by.values())
            live_status = next(
                (s for s in ("awaiting_input", "background_waiting", "running")
                 if s in live_by),
                None,
            )
            # Latest task's status — drives the Chats tab status dot/info, where
            # a chat collapses to its (usually single) task and live_status is
            # null once the turn finishes.
            last = c.execute(
                "SELECT status FROM tasks WHERE project_id=? "
                "ORDER BY updated_at DESC LIMIT 1",
                (r["id"],),
            ).fetchone()
            out.append({
                **dict(r),
                "task_count": counts["n"],
                "live_count": live_count,
                "live_status": live_status,
                "last_status": last["status"] if last else None,
            })
    return out


@app.post("/projects/chat", include_in_schema=False)
async def create_chat_project(body: CreateChatBody) -> dict:
    """Create a chat-kind project (disposable cwd under chats_root/<id>/)
    and auto-create its first task with the prompt. Returns project + first
    task."""
    if spawn_semaphore is None:
        raise HTTPException(503, "not ready")

    chat_id = str(uuid.uuid4())
    cwd = cfg_path("chats_root") / chat_id
    setup_workspace(cwd)
    ts = now_iso()
    # The chat's projects.name is UNIQUE — suffix the placeholder with a
    # short chat_id prefix so two chats with the same opening line still
    # insert cleanly. The async title-gen later replaces this with the clean
    # generated title (with its own collision fallback).
    initial = placeholder_title(body.prompt)
    chat_name = f"{initial} ({chat_id[:6]})"
    with db_conn() as c:
        c.execute(
            """INSERT INTO projects (id, name, path, kind, description,
                                      created_at, updated_at)
               VALUES (?, ?, ?, 'chat', NULL, ?, ?)""",
            (chat_id, chat_name, str(cwd), ts, ts),
        )
        c.commit()

    # First task immediately, same flow as create_task.
    task_id = str(uuid.uuid4())
    with db_conn() as c:
        c.execute(
            """INSERT INTO tasks (id, project_id, title, cwd, status,
                                  created_at, updated_at)
               VALUES (?, ?, ?, ?, 'queued', ?, ?)""",
            (task_id, chat_id, initial, str(cwd), ts, ts),
        )
        c.execute(
            """INSERT INTO turns (task_id, turn_num, prompt, started_at)
               VALUES (?, 1, ?, ?)""",
            (task_id, body.prompt, ts),
        )
        c.commit()

    asyncio.create_task(generate_and_set_title(task_id, body.prompt, chat_project_id=chat_id))

    async with spawn_semaphore:
        try:
            await spawn_first_turn(task_id, body.prompt, cwd)
        except Exception as e:
            with db_conn() as c:
                c.execute(
                    "UPDATE tasks SET status='failed', updated_at=? WHERE id=?",
                    (now_iso(), task_id),
                )
                c.commit()
            raise HTTPException(500, str(e))

    await broadcast(task_id, {"event": "spawned", "at": now_iso()})
    return {"project_id": chat_id, "task_id": task_id, "status": "running"}


# ── house-style scaffold ───────────────────────────────────────────────────
# A fresh project can opt into a runnable Creator Magic starter: a minimal
# FastAPI+HTMX app wearing the shared house-style shell, with the design assets
# vendored from tank's own committed copy so the new app is self-contained and
# works offline. `__NAME__` is substituted at write time.

SCAFFOLD_INDEX_HTML = """<!doctype html>
<!-- Scaffolded by tank · Creator Magic house-style. Assets vendored under
     /static/house-style/. To track the live design system instead, repoint the
     three links below at your design-system URL (e.g. a no-cache /latest channel). -->
<html lang="en" data-accent="purple">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>__NAME__ · creator magic</title>
  <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600;700&family=Space+Grotesk:wght@400;500;600;700&display=swap" rel="stylesheet">
  <link rel="stylesheet" href="/static/house-style/tokens.css">
  <link rel="stylesheet" href="/static/house-style/shell.css">
  <script defer src="/static/house-style/shell.js"></script>
</head>
<body>
  <div class="aura"></div>
  <div class="grain"></div>

  <div class="app">
    <header class="topbar">
      <span class="brand"><img src="/static/house-style/logo.png" alt=""><b>__NAME__</b></span>
      <div class="topbar-search"><input type="search" placeholder="Search…" aria-label="Search"></div>
      <span class="spacer"></span>
      <button class="btn btn-spark">✨ Generate</button>
      <button class="icon-btn" title="Open panel" onclick="toggleDrawer('side')">⌸</button>
    </header>

    <div class="body">
      <aside class="rail">
        <div class="tabs"><button class="tab on">items</button><button class="tab">archive</button></div>
        <div class="rail-scroll">
          <button class="new-proj">＋ new item</button>
          <div class="chan on"><span class="ico">◆</span><span class="nm">Example channel</span><span class="ct">3</span></div>
        </div>
      </aside>
      <main class="main">
        <cm-chat endpoint="/chat" placeholder="Ask anything…"></cm-chat>
      </main>
    </div>
  </div>

  <cm-drawer id="side" title="Details">
    <p class="p">Shared <code class="code-chip">&lt;cm-drawer&gt;</code> — toggle with
    <code class="code-chip">toggleDrawer('side')</code>. Refine it once in house-style
    and every fleet app updates.</p>
  </cm-drawer>

  <style>
    .topbar-search{ flex:0 1 420px; }
    .topbar-search input{ width:100%; background:var(--panel-2); border:1px solid var(--border); border-radius:9px; color:var(--text); font-family:var(--font-ui); font-size:12.5px; padding:8px 13px; outline:none; }
    .topbar-search input:focus{ border-color:var(--accent-line); box-shadow:0 0 0 3px var(--accent-soft); }
    .main{ padding:0 40px; }
  </style>
</body>
</html>
"""

SCAFFOLD_API_PY = '''"""__NAME__ — a Creator Magic fleet app. Scaffolded by tank."""
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

ROOT = Path(__file__).parent
app = FastAPI(title="__NAME__")
app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")


@app.get("/health")
def health() -> dict:
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (ROOT / "static" / "index.html").read_text()


class ChatIn(BaseModel):
    message: str


@app.post("/chat")
def chat(body: ChatIn) -> dict:
    # Stub. <cm-chat> POSTs {"message": ...} and renders the {"reply": ...}.
    # Wire this to your model / pipeline.
    return {"reply": f"echo: {body.message}"}
'''

SCAFFOLD_REQUIREMENTS = "fastapi\nuvicorn[standard]\n"

SCAFFOLD_README = """# __NAME__

A Creator Magic fleet app, scaffolded by tank. FastAPI + HTMX, no build step,
wearing the shared **house-style** shell.

## Run

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn api:app --reload --port 8000
```

(In tank, the project's dev_command does this for you — hit the preview button
on any task.)

## Design system

The look comes from house-style, vendored under `static/house-style/`
(`tokens.css`, `shell.css`, `shell.js`, `logo.png`) so this app is
self-contained and works offline. The shared `<cm-chat>` and `<cm-drawer>`
components live in `shell.js`.

To track the live design system instead (auto-pick-up of changes), repoint the
three links in `static/index.html` at your design-system URL (e.g. a `/latest`
channel). To pull a fresh vendored copy, re-copy the files from house-style.
"""

# Self-bootstrapping: creates a venv, installs deps, runs uvicorn on tank's
# preview-assigned $PORT. Idempotent across previews.
SCAFFOLD_DEV_COMMAND = (
    "python3 -m venv .venv 2>/dev/null; "
    ".venv/bin/pip install -q -r requirements.txt; "
    "exec .venv/bin/uvicorn api:app --host 0.0.0.0 --port $PORT"
)


def scaffold_house_style_app(target: Path, name: str) -> str:
    """Write a runnable Creator Magic starter app into `target` and return the
    dev_command to register. Vendors house-style assets from tank's own copy."""
    static = target / "static"
    hs = static / "house-style"
    hs.mkdir(parents=True, exist_ok=True)
    src_hs = STATIC_DIR / "house-style"
    for fn in ("tokens.css", "shell.css", "shell.js", "logo.png"):
        src = src_hs / fn
        if src.exists():
            shutil.copyfile(src, hs / fn)
    (static / "index.html").write_text(SCAFFOLD_INDEX_HTML.replace("__NAME__", name))
    (target / "api.py").write_text(SCAFFOLD_API_PY.replace("__NAME__", name))
    (target / "requirements.txt").write_text(SCAFFOLD_REQUIREMENTS)
    (target / "README.md").write_text(SCAFFOLD_README.replace("__NAME__", name))
    return SCAFFOLD_DEV_COMMAND


@app.post("/projects", tags=["projects"],
          summary="Register or create a project to spawn tasks into")
async def create_project(body: CreateProjectBody) -> dict:
    """Register or create a project.

    Three flows, selected by what's provided:
    - path supplied + already exists → register that path as-is.
    - path omitted, dir at projects_root/<name> doesn't exist, git_remote set
      → `git clone <git_remote>` into projects_root/<name>.
    - path omitted, dir doesn't exist, no git_remote → mkdir + `git init`.

    Returns the new row + an `action` string so the UI can tell what happened.
    """
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(400, "name is required")

    if body.path:
        target = Path(body.path)
    else:
        if not PROJECT_NAME_RE.match(name):
            raise HTTPException(
                400,
                "name must start with a letter or digit and contain only "
                "letters, digits, '.', '_' or '-' (got " + repr(name) + ")",
            )
        target = cfg_path("projects_root") / name

    action: str
    if target.exists():
        if not target.is_dir():
            raise HTTPException(400, f"path exists but is not a directory: {target}")
        action = "registered"
    elif body.git_remote:
        # Clone into target. Parent must exist; the project name is the leaf.
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise HTTPException(500, f"could not create parent directory: {e}")
        rc, out, err = await run_cmd(
            "git", "clone", body.git_remote.strip(), str(target),
        )
        if rc != 0:
            raise HTTPException(
                500,
                f"git clone failed (rc={rc}): {(err or out).strip()[:500] or 'no output'}",
            )
        action = "cloned"
    else:
        try:
            target.mkdir(parents=True, exist_ok=False)
        except OSError as e:
            raise HTTPException(500, f"could not create directory: {e}")
        rc, out, err = await run_cmd("git", "init", str(target))
        if rc != 0:
            raise HTTPException(
                500,
                f"git init failed (rc={rc}): {(err or out).strip()[:500] or 'no output'}",
            )
        action = "initialized"

    pid = str(uuid.uuid4())
    ts = now_iso()
    with db_conn() as c:
        try:
            c.execute(
                """INSERT INTO projects (id, name, path, kind, description,
                                          git_remote, created_at, updated_at)
                   VALUES (?, ?, ?, 'project', ?, ?, ?, ?)""",
                (pid, name, str(target), body.description, body.git_remote, ts, ts),
            )
            c.commit()
        except sqlite3.IntegrityError:
            raise HTTPException(409, f"project name '{name}' already exists")
    setup_workspace(target)

    # Optional: drop a runnable house-style starter into a freshly created dir.
    # Only when we just `git init`-ed an empty tree, so we never clobber cloned
    # or pre-existing code. Best-effort: a scaffold hiccup must not orphan the
    # already-registered project, so we report it without failing the request.
    scaffolded = False
    scaffold_error: Optional[str] = None
    if body.scaffold_ui and action == "initialized":
        try:
            dev_command = scaffold_house_style_app(target, name)
            with db_conn() as c:
                c.execute(
                    "UPDATE projects SET dev_command=?, updated_at=? WHERE id=?",
                    (dev_command, now_iso(), pid),
                )
                c.commit()
            # Initial commit so isolate_tasks worktrees have a base to branch
            # from (an empty repo with no commits can't create a worktree).
            await run_cmd("git", "-C", str(target), "add", "-A")
            await run_cmd(
                "git", "-C", str(target),
                "-c", "user.email=tank@localhost", "-c", "user.name=tank",
                "commit", "-m", "scaffold: house-style starter app",
            )
            scaffolded = True
            action = "scaffolded"
        except Exception as e:
            scaffold_error = str(e)

    # Let the local model pick a fitting icon in the background (best effort).
    if ai_enabled():
        asyncio.create_task(_auto_suggest_icon(pid, name, body.description))
    return {
        "project_id": pid,
        "name": name,
        "path": str(target),
        "action": action,
        "scaffolded": scaffolded,
        **({"scaffold_error": scaffold_error} if scaffold_error else {}),
    }


class UpdateProjectBody(BaseModel):
    description: Optional[str] = None
    isolate_tasks: Optional[bool] = None
    base_branch: Optional[str] = None
    # Shell command to launch the app for a live preview, run from a task's
    # worktree. Use $PORT for the tank-assigned port. Empty string clears it.
    dev_command: Optional[str] = None
    # Lucide icon slug (e.g. "rocket"). Empty string clears it back to the
    # default. Passed straight through to the favicon/sidebar on the client.
    icon: Optional[str] = None


@app.patch("/projects/{project_id}", include_in_schema=False)
async def update_project(project_id: str, body: UpdateProjectBody) -> dict:
    """Mutate a project's editable fields. Currently description, isolate_tasks,
    base_branch, dev_command, icon. Returns the updated row."""
    with db_conn() as c:
        existing = c.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        if not existing:
            raise HTTPException(404, "project not found")
        sets = []
        vals: list = []
        if body.description is not None:
            sets.append("description=?"); vals.append(body.description)
        if body.isolate_tasks is not None:
            sets.append("isolate_tasks=?"); vals.append(1 if body.isolate_tasks else 0)
        if body.base_branch is not None:
            sets.append("base_branch=?"); vals.append(body.base_branch.strip() or "main")
        if body.dev_command is not None:
            sets.append("dev_command=?"); vals.append(body.dev_command.strip() or None)
        if body.icon is not None:
            # Keep only the slug charset Lucide uses; empty clears the icon.
            slug = re.sub(r"[^a-z0-9-]", "", body.icon.strip().lower())
            sets.append("icon=?"); vals.append(slug or None)
        if not sets:
            return dict(existing)
        sets.append("updated_at=?"); vals.append(now_iso())
        vals.append(project_id)
        c.execute(f"UPDATE projects SET {', '.join(sets)} WHERE id=?", vals)
        c.commit()
        row = c.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    return dict(row)


@app.post("/projects/{project_id}/suggest-icon", include_in_schema=False)
async def suggest_project_icon(project_id: str) -> dict:
    """Ask the local model to pick a Lucide icon for this project from its name
    + description, store it, and return the updated row. 409 when AI is off."""
    if not ai_enabled():
        raise HTTPException(409, "AI features are not enabled (settings cog)")
    row = _project_or_404(project_id)
    try:
        icon = await _suggest_and_store_icon(project_id, row["name"], row["description"])
    except Exception as e:
        raise HTTPException(502, f"icon suggestion failed: {e}")
    if not icon:
        raise HTTPException(422, "the model did not return a usable icon")
    with db_conn() as c:
        updated = c.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    return dict(updated)


@app.delete("/projects/{project_id}", include_in_schema=False)
async def delete_project(project_id: str, remove_dir: bool = False) -> dict:
    """Remove a project from tank + all its tasks. Chat-kind projects always
    have their (ephemeral sandbox) cwd removed. Project-kind projects keep their
    cwd — it's a real repo — unless `remove_dir` is set, in which case the
    working directory is deleted from disk too."""
    with db_conn() as c:
        row = c.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        if not row:
            raise HTTPException(404, "project not found")
        kind = row["kind"]
        cwd = row["path"]
        task_ids = [t["id"] for t in c.execute(
            "SELECT id FROM tasks WHERE project_id=?", (project_id,)
        ).fetchall()]

    # Reuse per-task cleanup for each task.
    for tid in task_ids:
        try:
            await delete_task(tid)
        except HTTPException:
            pass

    # Tear down the build queue: stop any in-flight item's tmux and remove the
    # shared queue worktree (it lives under worktrees_root, so it'd be orphaned
    # once the project row is gone). Done before any rmtree of `cwd` because the
    # worktree-remove git command needs the project dir to still exist.
    try:
        await clear_queue(project_id)
    except HTTPException:
        pass

    dir_removed = False
    if kind == "chat" or remove_dir:
        shutil.rmtree(cwd, ignore_errors=True)
        untrust_project_path(cwd)
        dir_removed = True

    with db_conn() as c:
        c.execute("DELETE FROM projects WHERE id=?", (project_id,))
        c.commit()

    await broadcast(project_id, {"event": "project_deleted", "at": now_iso()})
    return {"deleted": True, "project_id": project_id,
            "tasks_removed": len(task_ids), "dir_removed": dir_removed}


# ── Per-project TODO.md ───────────────────────────────────────────────────────
#
# Each project's working dir can have a TODO.md (markdown checklist). Tank
# reads and writes it as the source of truth for "stream of work in this
# project". Line indexes identify a todo across requests — fragile if the
# file's edited externally, but pragmatic for MVP.

TODO_FILENAME = "TODO.md"
TODO_RE = re.compile(r'^(\s*)([-*+])\s*\[([ xX])\]\s*(.*)$')
# Spaces a todo's detail lines sit under, aligning beneath the "- [ ] " marker.
# Continuation lines indented at least this much (and not themselves a
# checkbox) are the todo's `details` body — renders as part of the list item
# in any markdown viewer, so TODO.md stays human-readable.
TODO_DETAIL_INDENT = 6


def _project_or_404(project_id: str) -> dict:
    with db_conn() as c:
        row = c.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    if not row:
        raise HTTPException(404, "project not found")
    return dict(row)


def _todo_path_for(project: dict) -> Path:
    return Path(project["path"]) / TODO_FILENAME


def parse_todos(text: str) -> list[dict]:
    """Return one entry per todo. `line_idx` is the 0-based file line of the
    checkbox (the todo's stable-ish handle). `details` is the dedented body of
    indented continuation lines beneath it (may be empty); `end_idx` is the
    last file line belonging to the todo's block (checkbox + detail lines), so
    callers can rewrite/move/delete the whole block as a unit."""
    lines = text.splitlines()
    n = len(lines)
    out = []
    i = 0
    while i < n:
        m = TODO_RE.match(lines[i])
        if not m:
            i += 1
            continue
        indent = len(m.group(1))
        detail_indent = indent + TODO_DETAIL_INDENT
        detail_lines: list[str] = []
        pending_blanks: list[str] = []
        end_idx = i
        j = i + 1
        while j < n:
            ln = lines[j]
            if not ln.strip():
                # Blank line: buffer it. Only kept if more detail follows;
                # otherwise it ends the block and stays outside it.
                pending_blanks.append("")
                j += 1
                continue
            if TODO_RE.match(ln):
                break  # next todo (incl. a nested checkbox) ends this block
            if len(ln) - len(ln.lstrip(" ")) < detail_indent:
                break  # not indented enough to belong to this todo
            detail_lines.extend(pending_blanks)
            pending_blanks = []
            detail_lines.append(ln[detail_indent:])
            end_idx = j
            j += 1
        out.append({
            "line_idx": i,
            "indent": indent,
            "done": m.group(3).lower() == "x",
            "text": m.group(4).rstrip(),
            "details": "\n".join(detail_lines),
            "end_idx": end_idx,
        })
        i = end_idx + 1
    return out


def _todo_block_lines(indent: str, marker: str, check: str, text: str, details: str) -> list[str]:
    """Render a todo (and its details) to the file lines it occupies: one
    checkbox line, then indented continuation lines for the details body."""
    out = [f"{indent}{marker} [{check}] {text}"]
    body = (details or "").strip("\n")
    if body:
        pad = indent + " " * TODO_DETAIL_INDENT
        for dl in body.replace("\r\n", "\n").split("\n"):
            dl = dl.rstrip()
            out.append(pad + dl if dl else "")
        while len(out) > 1 and out[-1] == "":
            out.pop()
    return out


def _find_todo(text: str, line_idx: int) -> Optional[dict]:
    return next((t for t in parse_todos(text) if t["line_idx"] == line_idx), None)


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    tmp.replace(path)


@app.get("/projects/{project_id}/todos", include_in_schema=False)
async def list_todos(project_id: str) -> dict:
    project = _project_or_404(project_id)
    todo_file = _todo_path_for(project)
    if not todo_file.exists():
        return {"exists": False, "path": str(todo_file), "todos": []}
    text = todo_file.read_text()
    return {"exists": True, "path": str(todo_file), "todos": parse_todos(text)}


class TodoAddBody(BaseModel):
    text: str
    details: Optional[str] = None


@app.post("/projects/{project_id}/todos", include_in_schema=False)
async def add_todo(project_id: str, body: TodoAddBody) -> dict:
    project = _project_or_404(project_id)
    todo_file = _todo_path_for(project)
    text = body.text.strip()
    if not text:
        raise HTTPException(400, "text required")
    existing = todo_file.read_text() if todo_file.exists() else f"# {project['name']} — TODO\n\n"
    if not existing.endswith("\n"):
        existing += "\n"
    # Append at the end of the existing content — line_idx of the new row is
    # the checkbox line, i.e. the number of lines already in `existing`.
    line_idx = len(existing.splitlines())
    block = _todo_block_lines("", "-", " ", text, body.details or "")
    _atomic_write(todo_file, existing + "\n".join(block) + "\n")
    return {"ok": True, "line_idx": line_idx, "text": text}


class TodoTidyBody(BaseModel):
    original_text: str


@app.post("/projects/{project_id}/todos/{line_idx}/tidy", include_in_schema=False)
async def tidy_todo(project_id: str, line_idx: int, body: TodoTidyBody) -> dict:
    """Run a single todo through the model and update the line if it still
    matches. Client calls this in the background after a successful add, so the
    user sees their raw text immediately and a tidied version a moment later.
    Silent no-op when AI features are off, on any error, or if the line
    moved/changed in the interim."""
    project = _project_or_404(project_id)
    todo_file = _todo_path_for(project)
    if not todo_file.exists():
        return {"ok": False, "improved": False}
    if not ai_enabled():
        return {"ok": False, "improved": False}
    original = body.original_text.strip()
    if not original:
        return {"ok": False, "improved": False}
    try:
        improved = await asyncio.wait_for(
            _llm_improve_todo(original), timeout=TODO_IMPROVE_TIMEOUT_SEC,
        )
    except Exception as e:
        print(f"[todo_tidy] model call failed: {e!r}", flush=True)
        return {"ok": False, "improved": False}
    if not improved or improved == original:
        return {"ok": True, "improved": False, "text": original}
    lines = todo_file.read_text().splitlines()
    if line_idx < 0 or line_idx >= len(lines):
        return {"ok": False, "improved": False}
    m = TODO_RE.match(lines[line_idx])
    if not m or m.group(4).rstrip() != original:
        # Line moved or was edited/deleted — leave it alone.
        return {"ok": False, "improved": False}
    indent, marker, check = m.group(1), m.group(2), m.group(3)
    lines[line_idx] = f"{indent}{marker} [{check}] {improved}"
    _atomic_write(todo_file, "\n".join(lines) + "\n")
    return {"ok": True, "improved": True, "text": improved}


class TodoUpdateBody(BaseModel):
    done: Optional[bool] = None
    text: Optional[str] = None
    # None = leave details unchanged; "" = clear them.
    details: Optional[str] = None


@app.patch("/projects/{project_id}/todos/{line_idx}", include_in_schema=False)
async def update_todo(project_id: str, line_idx: int, body: TodoUpdateBody) -> dict:
    project = _project_or_404(project_id)
    todo_file = _todo_path_for(project)
    if not todo_file.exists():
        raise HTTPException(404, "TODO.md not found")
    text = todo_file.read_text()
    lines = text.splitlines()
    todo = _find_todo(text, line_idx)
    if todo is None:
        raise HTTPException(400, "line is not a todo")
    m = TODO_RE.match(lines[line_idx])
    indent, marker, check = m.group(1), m.group(2), m.group(3)
    new_done = body.done if body.done is not None else (check.lower() == "x")
    new_text = body.text.strip() if body.text is not None else todo["text"]
    new_details = body.details if body.details is not None else todo["details"]
    block = _todo_block_lines(indent, marker, "x" if new_done else " ", new_text, new_details)
    # Replace the whole block — its length can change when details are added or
    # cleared — keyed on the parsed span so following todos shift correctly.
    lines[todo["line_idx"]:todo["end_idx"] + 1] = block
    _atomic_write(todo_file, "\n".join(lines) + "\n")
    return {"ok": True}


@app.delete("/projects/{project_id}/todos/{line_idx}", include_in_schema=False)
async def delete_todo(project_id: str, line_idx: int) -> dict:
    project = _project_or_404(project_id)
    todo_file = _todo_path_for(project)
    if not todo_file.exists():
        raise HTTPException(404, "TODO.md not found")
    text = todo_file.read_text()
    lines = text.splitlines()
    todo = _find_todo(text, line_idx)
    if todo is None:
        raise HTTPException(400, "line is not a todo")
    del lines[todo["line_idx"]:todo["end_idx"] + 1]
    _atomic_write(todo_file, "\n".join(lines) + ("\n" if lines else ""))
    return {"ok": True}


class TodoReorderBody(BaseModel):
    # Checkbox line_idx values in the desired new order. Any todo omitted from
    # the list keeps its relative position at the end; unknown idxs are ignored.
    order: list[int]


@app.post("/projects/{project_id}/todos/reorder", include_in_schema=False)
async def reorder_todos(project_id: str, body: TodoReorderBody) -> dict:
    project = _project_or_404(project_id)
    todo_file = _todo_path_for(project)
    if not todo_file.exists():
        raise HTTPException(404, "TODO.md not found")
    text = todo_file.read_text()
    lines = text.splitlines()
    todos = parse_todos(text)
    if not todos:
        return {"exists": True, "path": str(todo_file), "todos": []}
    by_idx = {t["line_idx"]: t for t in todos}
    seen: set[int] = set()
    ordered: list[dict] = []
    for idx in body.order:
        t = by_idx.get(idx)
        if t is not None and idx not in seen:
            ordered.append(t)
            seen.add(idx)
    # Defensive: append any todos the client didn't mention, original order.
    for t in todos:
        if t["line_idx"] not in seen:
            ordered.append(t)
    # Keep everything before the first todo (file header etc.); rewrite the
    # todo blocks in the new order. Each block's raw lines are preserved
    # verbatim, so details ride along untouched.
    preamble = lines[:todos[0]["line_idx"]]
    new_lines = list(preamble)
    for t in ordered:
        new_lines.extend(lines[t["line_idx"]:t["end_idx"] + 1])
    new_text = "\n".join(new_lines) + "\n"
    _atomic_write(todo_file, new_text)
    return {"exists": True, "path": str(todo_file), "todos": parse_todos(new_text)}


# ── Build-queue routes ──────────────────────────────────────────────────────
# The DB-backed queue (distinct from the markdown TODO.md): a seq-ordered list
# the runner works through one item at a time on a shared branch. All routes are
# include_in_schema=False (operator/integrator surface, not the public contract).

QUEUE_EDITABLE_STATUSES = ("pending",)  # only un-started items can be edited/deleted


def _queue_item_dict(r: sqlite3.Row) -> dict:
    return {
        "id": r["id"], "project_id": r["project_id"], "parent_id": r["parent_id"],
        "seq": r["seq"], "title": r["title"], "detail": r["detail"],
        "status": r["status"], "depends_on": r["depends_on"], "result": r["result"],
        "agent_task_id": r["agent_task_id"],
        "created_at": r["created_at"], "updated_at": r["updated_at"],
    }


def _queue_snapshot(project_id: str) -> dict:
    with db_conn() as c:
        run = c.execute("SELECT * FROM queue_runs WHERE project_id=?", (project_id,)).fetchone()
        items = c.execute(
            "SELECT * FROM queue_items WHERE project_id=? ORDER BY seq, created_at",
            (project_id,),
        ).fetchall()
    return {
        "run": (dict(run) if run else {"project_id": project_id, "status": "idle"}),
        "items": [_queue_item_dict(i) for i in items],
    }


@app.get("/projects/{project_id}/queue", include_in_schema=False)
async def get_queue(project_id: str) -> dict:
    _project_or_404(project_id)
    return _queue_snapshot(project_id)


@app.post("/projects/{project_id}/queue", include_in_schema=False)
async def add_queue_items(project_id: str, body: QueueAddBody) -> dict:
    _project_or_404(project_id)
    if not body.items:
        raise HTTPException(400, "no items")
    ts = now_iso()
    with db_conn() as c:
        base = c.execute(
            "SELECT COALESCE(MAX(seq), 0) AS m FROM queue_items WHERE project_id=?",
            (project_id,),
        ).fetchone()["m"]
        for n, it in enumerate(body.items, start=1):
            title = (it.title or "").strip()
            if not title:
                continue
            c.execute(
                """INSERT INTO queue_items
                   (id, project_id, parent_id, seq, title, detail, status,
                    depends_on, created_at, updated_at)
                   VALUES (?, ?, NULL, ?, ?, ?, 'pending', ?, ?, ?)""",
                (str(uuid.uuid4()), project_id, base + n, title,
                 (it.detail or None), (it.depends_on or None), ts, ts),
            )
        c.commit()
    return _queue_snapshot(project_id)


@app.patch("/projects/{project_id}/queue/{item_id}", include_in_schema=False)
async def patch_queue_item(project_id: str, item_id: str, body: QueuePatchBody) -> dict:
    _project_or_404(project_id)
    with db_conn() as c:
        item = c.execute(
            "SELECT * FROM queue_items WHERE id=? AND project_id=?",
            (item_id, project_id),
        ).fetchone()
        if not item:
            raise HTTPException(404, "queue item not found")
        # Content edits are only safe on items the runner hasn't started.
        content_keys = {"title", "detail", "depends_on", "seq"}
        wants_content = any(getattr(body, k) is not None for k in content_keys)
        if wants_content and item["status"] not in QUEUE_EDITABLE_STATUSES:
            raise HTTPException(409, f"item is '{item['status']}', only pending items are editable")
        sets, vals = [], []
        for k in ("title", "detail", "depends_on", "seq", "status"):
            v = getattr(body, k)
            if v is not None:
                sets.append(f"{k}=?")
                vals.append(v)
        if not sets:
            return _queue_snapshot(project_id)
        sets.append("updated_at=?")
        vals.append(now_iso())
        vals.append(item_id)
        c.execute(f"UPDATE queue_items SET {', '.join(sets)} WHERE id=?", vals)
        c.commit()
    return _queue_snapshot(project_id)


@app.delete("/projects/{project_id}/queue/{item_id}", include_in_schema=False)
async def delete_queue_item(project_id: str, item_id: str) -> dict:
    _project_or_404(project_id)
    with db_conn() as c:
        item = c.execute(
            "SELECT status FROM queue_items WHERE id=? AND project_id=?",
            (item_id, project_id),
        ).fetchone()
        if not item:
            raise HTTPException(404, "queue item not found")
        if item["status"] not in QUEUE_EDITABLE_STATUSES:
            raise HTTPException(409, f"item is '{item['status']}', only pending items are deletable")
        c.execute("DELETE FROM queue_items WHERE id=?", (item_id,))
        c.commit()
    return _queue_snapshot(project_id)


@app.post("/projects/{project_id}/queue/reorder", include_in_schema=False)
async def reorder_queue(project_id: str, body: QueueReorderBody) -> dict:
    _project_or_404(project_id)
    ts = now_iso()
    with db_conn() as c:
        ids = {r["id"] for r in c.execute(
            "SELECT id FROM queue_items WHERE project_id=?", (project_id,)).fetchall()}
        seq = 0
        for item_id in body.order:
            if item_id in ids:
                seq += 1
                c.execute("UPDATE queue_items SET seq=?, updated_at=? WHERE id=?",
                          (seq, ts, item_id))
        c.commit()
    return _queue_snapshot(project_id)


@app.post("/projects/{project_id}/queue/import-todos", include_in_schema=False)
async def import_todos_to_queue(project_id: str) -> dict:
    """Seed the queue from the project's TODO.md (un-done items only). The queue
    owns its state from here on — TODO.md stays the human-editable doc."""
    project = _project_or_404(project_id)
    todo_file = _todo_path_for(project)
    if not todo_file.exists():
        raise HTTPException(404, "TODO.md not found")
    todos = [t for t in parse_todos(todo_file.read_text()) if not t["done"]]
    if not todos:
        return _queue_snapshot(project_id)
    ts = now_iso()
    with db_conn() as c:
        base = c.execute(
            "SELECT COALESCE(MAX(seq), 0) AS m FROM queue_items WHERE project_id=?",
            (project_id,),
        ).fetchone()["m"]
        for n, t in enumerate(todos, start=1):
            c.execute(
                """INSERT INTO queue_items
                   (id, project_id, parent_id, seq, title, detail, status,
                    created_at, updated_at)
                   VALUES (?, ?, NULL, ?, ?, ?, 'pending', ?, ?)""",
                (str(uuid.uuid4()), project_id, base + n, t["text"],
                 (t["details"] or None), ts, ts),
            )
        c.commit()
    return _queue_snapshot(project_id)


@app.post("/projects/{project_id}/queue/start", include_in_schema=False)
async def start_queue(project_id: str) -> dict:
    project = _project_or_404(project_id)
    with db_conn() as c:
        run = c.execute("SELECT * FROM queue_runs WHERE project_id=?", (project_id,)).fetchone()
        pending = c.execute(
            "SELECT COUNT(*) AS n FROM queue_items WHERE project_id=? AND status='pending'",
            (project_id,),
        ).fetchone()["n"]
    if pending == 0 and not (run and run["status"] == "paused"):
        raise HTTPException(400, "no pending queue items to run")
    # Create/adopt the shared queue worktree.
    try:
        wt, branch = await ensure_queue_worktree(
            project["path"], project_id, project["base_branch"] or "main")
    except Exception as e:
        raise HTTPException(500, f"could not prepare queue worktree: {e}")
    ts = now_iso()
    with db_conn() as c:
        if run:
            c.execute(
                "UPDATE queue_runs SET status='running', branch=?, worktree_path=?, "
                "started_at=COALESCE(started_at, ?), updated_at=? WHERE project_id=?",
                (branch, wt, ts, ts, project_id),
            )
        else:
            c.execute(
                """INSERT INTO queue_runs
                   (project_id, status, branch, worktree_path, started_at, updated_at)
                   VALUES (?, 'running', ?, ?, ?, ?)""",
                (project_id, branch, wt, ts, ts),
            )
        c.commit()
    _queue_log(project_id, None, "start", f"branch={branch}")
    queue_wake.set()
    return _queue_snapshot(project_id)


@app.post("/projects/{project_id}/queue/pause", include_in_schema=False)
async def pause_queue(project_id: str) -> dict:
    _project_or_404(project_id)
    with db_conn() as c:
        c.execute(
            "UPDATE queue_runs SET status='paused', updated_at=? "
            "WHERE project_id=? AND status='running'",
            (now_iso(), project_id),
        )
        c.commit()
    _queue_log(project_id, None, "pause", "")
    return _queue_snapshot(project_id)


@app.post("/projects/{project_id}/queue/resume", include_in_schema=False)
async def resume_queue(project_id: str) -> dict:
    return await start_queue(project_id)


@app.delete("/projects/{project_id}/queue", include_in_schema=False)
async def clear_queue(project_id: str) -> dict:
    """Stop the run, kill any in-flight item's tmux, remove the queue worktree,
    and delete all items. Used to reset between test runs. Auto-committed work on
    the queue branch is left intact (only the worktree checkout is removed)."""
    project = _project_or_404(project_id)
    with db_conn() as c:
        run = c.execute("SELECT * FROM queue_runs WHERE project_id=?", (project_id,)).fetchone()
        running = c.execute(
            "SELECT agent_task_id FROM queue_items WHERE project_id=? AND status='running'",
            (project_id,),
        ).fetchall()
    for r in running:
        await _kill_task_tmux(r["agent_task_id"])
    if run and run["worktree_path"]:
        await remove_worktree(project["path"], run["worktree_path"], None)
    with db_conn() as c:
        c.execute("DELETE FROM queue_items WHERE project_id=?", (project_id,))
        c.execute("DELETE FROM queue_runs WHERE project_id=?", (project_id,))
        c.commit()
    _queue_log(project_id, None, "cleared", "")
    return _queue_snapshot(project_id)


def _event_summary(kind: str, payload: dict) -> str:
    """Render a one-line, human-readable summary of a hook event."""
    if kind == "PostToolUse" or kind == "PreToolUse":
        tool = payload.get("tool_name", "?")
        ti = payload.get("tool_input") or {}
        if isinstance(ti, dict):
            # Pick the most descriptive field per tool.
            for key in ("command", "file_path", "path", "url", "pattern", "description", "query"):
                if key in ti and ti[key]:
                    val = str(ti[key]).replace("\n", " ")
                    return f"{tool}: {val[:120]}"
            # Fallback: first key=value
            for k, v in ti.items():
                return f"{tool}: {k}={str(v)[:80]}"
        return tool
    if kind == "UserPromptSubmit":
        p = payload.get("prompt") or payload.get("user_prompt") or ""
        return f"prompt: {str(p)[:100]}" if p else "prompt sent"
    if kind == "SessionStart":
        src = payload.get("source", "")
        return f"session started ({src})" if src else "session started"
    if kind == "Stop":
        return "turn complete"
    if kind == "SessionEnd":
        reason = payload.get("reason") or payload.get("exit_reason") or ""
        return f"session ended ({reason})" if reason else "session ended"
    if kind == "SubagentStop":
        return "subagent complete"
    return kind


@app.get("/tasks/{task_id}", response_model=TaskDetail, tags=["tasks"],
         summary="Get full task state (row + turns + event timeline + preview)",
         description=(
             "Status lifecycle: `queued` → `running` → "
             "`awaiting_input` | `background_waiting` → `done` | `failed`. "
             "`awaiting_input` means the agent asked a question — reply with "
             "`POST /tasks/{id}/continue`."))
async def get_task(task_id: str) -> dict:
    with db_conn() as c:
        task_row = c.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not task_row:
            raise HTTPException(404, "task not found")
        turn_rows = c.execute(
            "SELECT * FROM turns WHERE task_id=? ORDER BY turn_num", (task_id,)
        ).fetchall()
        event_rows = c.execute(
            "SELECT id, kind, payload, at FROM events WHERE task_id=? ORDER BY id",
            (task_id,),
        ).fetchall()

    turn_dicts = [dict(r) for r in turn_rows]
    sorted_turns = sorted(turn_dicts, key=lambda tt: tt["turn_num"])
    # [start, next_start) windows for assigning events to turns.
    windows = []
    for i, tt in enumerate(sorted_turns):
        next_start = sorted_turns[i + 1]["started_at"] if i + 1 < len(sorted_turns) else None
        windows.append((tt["turn_num"], tt["started_at"], next_start))

    # Collapse PreToolUse + PostToolUse into one row per tool invocation. Pre
    # and Post share an identical summary, so two rows would just duplicate
    # the line with a different leading glyph (◯ vs ●). FIFO-match Post to
    # the oldest unmatched Pre with the same (turn_num, tool_name) — orphan
    # Pre survives as a "still running" row.
    events = []
    pending_pre: dict[tuple[int | None, str], list[int]] = {}
    for r in event_rows:
        try:
            payload = json.loads(r["payload"])
        except json.JSONDecodeError:
            payload = {}
        turn_num = None
        for tn, start, end in windows:
            if r["at"] >= start and (end is None or r["at"] < end):
                turn_num = tn
                break
        record = {
            "id": r["id"],
            "kind": r["kind"],
            "at": r["at"],
            "summary": _event_summary(r["kind"], payload),
            "turn_num": turn_num,
        }
        if r["kind"] == "PostToolUse":
            key = (turn_num, payload.get("tool_name", "?"))
            queue = pending_pre.get(key)
            if queue:
                events[queue.pop(0)] = record
                continue
        events.append(record)
        if r["kind"] == "PreToolUse":
            key = (turn_num, payload.get("tool_name", "?"))
            pending_pre.setdefault(key, []).append(len(events) - 1)

    return {
        "task": dict(task_row),
        "turns": turn_dicts,
        "events": events,
        "preview": await _preview_state(task_id),
    }


async def _preview_state(task_id: str) -> dict:
    """Current preview status for a task, for the detail pane. The client builds
    the actual URL from window.location.hostname + this port, so a preview is
    reachable however the dashboard itself is being reached."""
    rec = active_previews.get(task_id)
    if rec and await _preview_alive(task_id):
        return {"running": True, "port": rec["port"], "started_at": rec["started_at"]}
    return {"running": False, "port": None}


@app.post("/tasks/{task_id}/preview", include_in_schema=False)
async def start_task_preview(task_id: str) -> dict:
    """Launch (or return the existing) live dev preview for a task. Runs the
    project's dev_command from the task's cwd on an allocated port."""
    with db_conn() as c:
        row = c.execute(
            """SELECT t.cwd, t.status, p.dev_command
               FROM tasks t JOIN projects p ON p.id = t.project_id
               WHERE t.id=?""",
            (task_id,),
        ).fetchone()
    if not row:
        raise HTTPException(404, "task not found")
    dev_command = (row["dev_command"] or "").strip()
    if not dev_command:
        raise HTTPException(
            400,
            "this project has no dev/preview command — set one in the project "
            "settings (e.g. 'uvicorn api:app --host 0.0.0.0 --port $PORT')",
        )
    try:
        rec = await start_preview(task_id, row["cwd"], dev_command)
    except Exception as e:
        raise HTTPException(500, f"could not start preview: {e}")
    return {"running": True, "port": rec["port"], "started_at": rec["started_at"]}


@app.delete("/tasks/{task_id}/preview", include_in_schema=False)
async def stop_task_preview(task_id: str) -> dict:
    stopped = await stop_preview(task_id)
    return {"running": False, "stopped": stopped}


@app.post("/tasks/{task_id}/continue", response_model=TaskHandle, tags=["tasks"],
          summary="Send a follow-up turn to an existing agent",
          description=(
              "Sends another prompt to the same agent session (same worktree, "
              "same git branch). Use this to answer an `awaiting_input` task or "
              "to give a finished agent more work."))
async def continue_task(task_id: str, body: ContinueTaskBody) -> dict:
    if spawn_semaphore is None:
        raise HTTPException(503, "not ready")
    with db_conn() as c:
        t = c.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not t:
            raise HTTPException(404, "task not found")
        last_n = (
            c.execute(
                "SELECT MAX(turn_num) AS n FROM turns WHERE task_id=?", (task_id,)
            ).fetchone()["n"]
            or 0
        )
        new_turn = last_n + 1
        ts = now_iso()
        c.execute(
            """INSERT INTO turns (task_id, turn_num, prompt, started_at)
               VALUES (?, ?, ?, ?)""",
            (task_id, new_turn, body.prompt, ts),
        )
        c.execute(
            "UPDATE tasks SET status='running', updated_at=? WHERE id=?",
            (ts, task_id),
        )
        c.commit()

    cwd = Path(t["cwd"])
    async with spawn_semaphore:
        await spawn_continue_turn(task_id, body.prompt, cwd, new_turn)

    await broadcast(task_id, {"event": "continued", "turn": new_turn, "at": now_iso()})
    return {"task_id": task_id, "turn": new_turn, "status": "running"}


@app.post("/tasks/{task_id}/resume", response_model=TaskHandle, tags=["tasks"],
          summary="Resume an interrupted/errored task")
async def resume_task(task_id: str) -> dict:
    """Spawn `claude --resume <task_id>` for an interrupted/errored task,
    queueing the RESUME_NUDGE prompt. Functionally identical to /continue
    with that prompt, but gated on a recoverable status so the UI can offer
    a one-click button without the user having to type anything."""
    with db_conn() as c:
        t = c.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not t:
            raise HTTPException(404, "task not found")
        if t["status"] not in ("interrupted", "errored"):
            raise HTTPException(
                400, f"task is '{t['status']}', not resumable"
            )
    return await continue_task(task_id, ContinueTaskBody(prompt=RESUME_NUDGE))


@app.post("/tasks/{task_id}/stop", response_model=StopResult, tags=["tasks"],
          summary="Interrupt a running agent (kill its tmux sessions)")
async def stop_task(task_id: str) -> dict:
    killed = []
    for name in await tmux_list_sessions_for(task_id):
        await tmux_kill(name)
        killed.append(name)
    with db_conn() as c:
        c.execute(
            "UPDATE tasks SET status='killed', updated_at=? WHERE id=?",
            (now_iso(), task_id),
        )
        c.commit()
    await broadcast(task_id, {"event": "killed", "at": now_iso()})
    return {"killed_sessions": killed}


# First-line sentinels used by on_stop to recognise which template a turn was
# spawned from. Keep these in sync with the templates below — each template's
# first line MUST start with its sentinel.
#
# The prompts are deliberately terse: claude runs inside the task's worktree
# with full git access and knows how to commit/push/open/merge against Forgejo,
# so we don't spell out the API recipe. The two things it CAN'T infer and which
# are load-bearing stay in every template: (a) the `origin` remote embeds the
# API token, and (b) the exact completion sentinels on_stop greps for (the PR
# html_url and the word MERGED on their own lines).
PR_PROMPT_SENTINEL = "Open a Forgejo pull request"
MERGE_PROMPT_SENTINEL = "Merge the pull request"
SHIP_PROMPT_SENTINEL = "Commit all work on this branch"

PR_PROMPT_TEMPLATE = """\
Open a Forgejo pull request for this branch: commit any uncommitted work (with a \
message reflecting "{title}"), push `{branch}`, and open a PR into `{base}` via \
the Forgejo API (the `origin` remote embeds the API token).

End your reply with the PR's html_url on its own line so the dashboard can spot it.
"""

MERGE_PROMPT_TEMPLATE = """\
Merge the pull request {pr_url} via the Forgejo API, then delete its branch (the \
`origin` remote embeds the API token).

End your reply with the single word MERGED on its own line so the dashboard can \
detect the merge.
"""

SHIP_PROMPT_TEMPLATE = """\
Commit all work on this branch, open a Forgejo pull request into `{base}`, and \
merge it (deleting the branch `{branch}` after). The `origin` remote embeds the \
API token.

End your reply with two lines: the PR's html_url, then the single word MERGED on \
its own line — so the dashboard can record both the PR and the merge.
"""


@app.post("/tasks/{task_id}/pr", tags=["tasks"],
          summary="Ask the agent to open a PR for this task's branch")
async def open_task_pr(task_id: str) -> dict:
    """Ask claude to commit, push, and open a Forgejo PR for this task's
    branch. We delegate to claude rather than do git ourselves because:
    (a) claude needs to commit any unstaged work first anyway, and (b) it
    can write a real PR body. The work happens as a normal continue-turn,
    so progress is visible in the task history. Gated on git_provider."""
    if cfg("git_provider") == "none":
        raise HTTPException(400, "no git provider configured — set one in settings")
    with db_conn() as c:
        row = c.execute(
            """SELECT t.title, t.branch, t.worktree_path, p.base_branch
               FROM tasks t JOIN projects p ON p.id = t.project_id
               WHERE t.id=?""",
            (task_id,),
        ).fetchone()
    if not row:
        raise HTTPException(404, "task not found")
    if not row["branch"] or not row["worktree_path"]:
        raise HTTPException(
            400, "task is not isolated — enable isolate_tasks on the project"
        )

    prompt = PR_PROMPT_TEMPLATE.format(
        title=row["title"], branch=row["branch"], base=row["base_branch"] or "main",
    )
    # Set pr_pending only after the turn is successfully under way, so a failed
    # spawn doesn't leave a stuck flag with no Stop to clear it. on_stop resets
    # it when the turn finishes.
    result = await continue_task(task_id, ContinueTaskBody(prompt=prompt))
    with db_conn() as c:
        c.execute("UPDATE tasks SET pr_pending=1 WHERE id=?", (task_id,))
        c.commit()
    return result


@app.post("/tasks/{task_id}/merge", tags=["tasks"],
          summary="Ask the agent to merge this task's PR")
async def merge_task_pr(task_id: str) -> dict:
    """Ask claude to merge the task's already-opened PR via the Forgejo API.
    Detection of success lives in on_stop: it scrapes 'MERGED' from the reply
    and flips status to 'merged'. Gated on git_provider."""
    if cfg("git_provider") == "none":
        raise HTTPException(400, "no git provider configured — set one in settings")
    with db_conn() as c:
        row = c.execute(
            "SELECT pr_url FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
    if not row:
        raise HTTPException(404, "task not found")
    if not row["pr_url"]:
        raise HTTPException(400, "task has no PR yet — open one first")

    prompt = MERGE_PROMPT_TEMPLATE.format(pr_url=row["pr_url"])
    # See open_task_pr: flag set post-spawn, cleared by on_stop on finish.
    result = await continue_task(task_id, ContinueTaskBody(prompt=prompt))
    with db_conn() as c:
        c.execute("UPDATE tasks SET merge_pending=1 WHERE id=?", (task_id,))
        c.commit()
    return result


@app.post("/tasks/{task_id}/ship", tags=["tasks"],
          summary="Commit, open a PR, and merge it — all in one turn")
async def ship_task(task_id: str) -> dict:
    """One-click 'ship it': commit any work, open a Forgejo PR into the base
    branch, and merge it — all in a single continue-turn. This is the common
    case (the operator just wants the branch landed); the separate /pr and
    /merge routes remain for when you want to pause at the PR for review.
    on_stop scrapes BOTH the PR URL and the MERGED sentinel from the one reply.
    Gated on git_provider."""
    if cfg("git_provider") == "none":
        raise HTTPException(400, "no git provider configured — set one in settings")
    with db_conn() as c:
        row = c.execute(
            """SELECT t.title, t.branch, t.worktree_path, p.base_branch
               FROM tasks t JOIN projects p ON p.id = t.project_id
               WHERE t.id=?""",
            (task_id,),
        ).fetchone()
    if not row:
        raise HTTPException(404, "task not found")
    if not row["branch"] or not row["worktree_path"]:
        raise HTTPException(
            400, "task is not isolated — enable isolate_tasks on the project"
        )

    prompt = SHIP_PROMPT_TEMPLATE.format(
        branch=row["branch"], base=row["base_branch"] or "main",
    )
    # One turn does both halves, so flag both pending; on_stop clears both when
    # the turn finishes (see open_task_pr for why we set the flag post-spawn).
    result = await continue_task(task_id, ContinueTaskBody(prompt=prompt))
    with db_conn() as c:
        c.execute(
            "UPDATE tasks SET pr_pending=1, merge_pending=1 WHERE id=?", (task_id,)
        )
        c.commit()
    return result


@app.post("/tasks/{task_id}/done", response_model=TaskHandle, tags=["tasks"],
          summary="Mark a task done")
async def mark_task_done(task_id: str) -> dict:
    """Manually mark a task as done — same blue UI state as 'merged' but
    without the PR/merge dance. For threads that finished off-platform
    (verified by hand, abandoned, rolled into another task) and just need
    clearing from the 'needs action' (green) bucket."""
    with db_conn() as c:
        t = c.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not t:
            raise HTTPException(404, "task not found")
        if t["status"] in ("running", "awaiting_input", "background_waiting"):
            raise HTTPException(
                400, f"task is '{t['status']}' — stop or wait for it to finish first"
            )
        ts = now_iso()
        # Stash the pre-done status so "undo" can restore it (see
        # /tasks/{id}/undone). prev_status only carries meaning while
        # status='done'; any other transition leaves it stale-but-ignored.
        c.execute(
            "UPDATE tasks SET prev_status=status, status='done', updated_at=? WHERE id=?",
            (ts, task_id),
        )
        c.commit()
    await broadcast(task_id, {"event": "done", "at": ts})
    return {"task_id": task_id, "status": "done"}


@app.post("/tasks/{task_id}/undone", response_model=TaskHandle, tags=["tasks"],
          summary="Reverse a manual 'mark done'")
async def unmark_task_done(task_id: str) -> dict:
    """Reverse a manual 'mark done' — restore the status the task held before
    it was marked done (falling back to 'completed' if that wasn't recorded).
    Only valid on a task that's currently 'done'; 'merged' tasks went through
    the real PR/merge flow and aren't reversible here."""
    with db_conn() as c:
        t = c.execute(
            "SELECT status, prev_status FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
        if not t:
            raise HTTPException(404, "task not found")
        if t["status"] != "done":
            raise HTTPException(
                400, f"task is '{t['status']}', not 'done' — nothing to undo"
            )
        restored = t["prev_status"] or "completed"
        ts = now_iso()
        c.execute(
            "UPDATE tasks SET status=?, prev_status=NULL, updated_at=? WHERE id=?",
            (restored, ts, task_id),
        )
        c.commit()
    await broadcast(task_id, {"event": "undone", "at": ts, "status": restored})
    return {"task_id": task_id, "status": restored}


@app.delete("/tasks/{task_id}", tags=["tasks"],
            summary="Delete a task (kill procs, clean up worktree + DB rows)")
async def delete_task(task_id: str) -> dict:
    """Remove a task: kill tmux + claude procs, remove claude's per-session
    JSONL, drop in-memory state, delete DB rows. Leaves the project's cwd
    and trust entry alone — those belong to the project, not the task."""
    with db_conn() as c:
        row = c.execute(
            """SELECT t.cwd, t.project_id, t.branch, t.worktree_path, p.path AS project_path
               FROM tasks t JOIN projects p ON p.id = t.project_id
               WHERE t.id=?""",
            (task_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "task not found")
        cwd = row["cwd"]
        branch = row["branch"]
        worktree_path = row["worktree_path"]
        project_path = row["project_path"]

    # Stop any live dev preview first (its tmux session has a different name
    # prefix than the task's turn sessions, so tmux_list_sessions_for misses it).
    await stop_preview(task_id)

    # Kill any live tmux sessions for this task.
    for name in await tmux_list_sessions_for(task_id):
        await tmux_kill(name)

    # Forcefully kill any claude process tied to this task. Without this,
    # claude can be alive for a beat after tmux dies and will rewrite state.
    await run_cmd_void("pkill", "-9", "-f", f"--session-id {task_id}")
    await run_cmd_void("pkill", "-9", "-f", f"--resume {task_id}")
    await asyncio.sleep(0.4)

    # Drop in-memory references.
    sse_subscribers.pop(task_id, None)

    # Delete claude's per-session JSONL only — leave cwd alone.
    # The JSONL lives at ~/.claude/projects/-<cwd-hash>/<task_id>.jsonl
    jsonl = claude_session_dir_for(cwd) / f"{task_id}.jsonl"
    with contextlib.suppress(OSError):
        jsonl.unlink()

    # If this task ran in an isolated worktree, tear it down. We always drop
    # the local branch — Forgejo keeps the pushed copy, so a PR (if any) is
    # unaffected.
    if worktree_path:
        with contextlib.suppress(Exception):
            await remove_worktree(project_path, worktree_path, branch)
        untrust_project_path(worktree_path)

    # DB rows (events first to honour foreign keys).
    with db_conn() as c:
        c.execute("DELETE FROM events WHERE task_id=?", (task_id,))
        c.execute("DELETE FROM turns WHERE task_id=?", (task_id,))
        c.execute("DELETE FROM tasks WHERE id=?", (task_id,))
        c.commit()

    # Tell the dashboard to drop this task from its list (goes via global SSE).
    await broadcast(task_id, {"event": "deleted", "at": now_iso()})
    return {"deleted": True, "task_id": task_id}


@app.post("/tasks/{task_id}/events", include_in_schema=False)
async def hook_event(task_id: str, body: EventBody) -> dict:
    await handle_event(task_id, body.kind, body.payload)
    return {"ok": True}


@app.websocket("/tasks/{task_id}/terminal/{turn_num}")
async def terminal_ws(ws: WebSocket, task_id: str, turn_num: int):
    """Attach to the task's tmux session and pipe it bidirectionally to a
    browser xterm.js client. We pty-fork `tmux attach`, then bridge the
    master fd <-> WebSocket text frames."""
    # The HTTP middleware doesn't see WebSocket upgrades, so gate here too.
    # Same-origin browsers carry the tank_token cookie automatically.
    token = cfg("api_token").strip()
    if token and not _token_ok(ws, token):
        await ws.close(code=1008)  # policy violation
        return
    await ws.accept()
    sess = session_name(task_id, turn_num)

    # Confirm the tmux session is still alive.
    rc, _, _ = await run_cmd("tmux", "has-session", "-t", sess)
    if rc != 0:
        await ws.send_text(f"\r\n\x1b[33m[tmux session {sess} not found — task already finished]\x1b[0m\r\n")
        await ws.close()
        return

    pid, fd = pty.fork()
    if pid == 0:
        # Child: become tmux attach. -A creates if missing; -t selects target.
        # systemd's environment has no TERM, so tmux refuses to start with
        # "terminal does not support clear". Set sensible defaults.
        env = os.environ.copy()
        env["TERM"] = "xterm-256color"
        env.setdefault("LANG", "C.UTF-8")
        env.setdefault("LC_ALL", "C.UTF-8")
        try:
            os.execvpe("tmux", ["tmux", "attach", "-t", sess], env)
        except Exception:
            os._exit(127)

    # Parent. Set the pty to a sensible default size; xterm.js will resize.
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 200, 0, 0))
    except OSError:
        pass

    loop = asyncio.get_event_loop()
    closed = asyncio.Event()

    def on_readable():
        try:
            data = os.read(fd, 8192)
        except OSError:
            closed.set()
            return
        if not data:
            closed.set()
            return
        try:
            text = data.decode("utf-8", errors="replace")
        except Exception:
            text = ""
        asyncio.create_task(ws.send_text(text))

    loop.add_reader(fd, on_readable)

    async def from_browser():
        try:
            while True:
                msg = await ws.receive()
                if msg.get("type") == "websocket.disconnect":
                    return
                t = msg.get("text")
                b = msg.get("bytes")
                if t is not None and t.startswith("\x1b__RESIZE__"):
                    # Custom frame from xterm: ESC + __RESIZE__ + JSON
                    try:
                        payload = json.loads(t[len("\x1b__RESIZE__"):])
                        rows = int(payload.get("rows", 40))
                        cols = int(payload.get("cols", 200))
                        fcntl.ioctl(fd, termios.TIOCSWINSZ,
                                    struct.pack("HHHH", rows, cols, 0, 0))
                    except Exception:
                        pass
                    continue
                data = (t.encode() if t is not None else b)
                if data:
                    try:
                        os.write(fd, data)
                    except OSError:
                        return
        except WebSocketDisconnect:
            return
        except Exception:
            return

    try:
        # Race: either the browser disconnects, or the pty closes (tmux died).
        ws_task = asyncio.create_task(from_browser())
        closed_task = asyncio.create_task(closed.wait())
        done, pending = await asyncio.wait(
            {ws_task, closed_task}, return_when=asyncio.FIRST_COMPLETED
        )
        for t in pending:
            t.cancel()
    finally:
        loop.remove_reader(fd)
        # Detach from the tmux session cleanly so we don't leave a dead attach.
        with contextlib.suppress(OSError, ProcessLookupError):
            os.kill(pid, signal.SIGTERM)
        with contextlib.suppress(OSError, ProcessLookupError):
            os.waitpid(pid, 0)
        with contextlib.suppress(OSError):
            os.close(fd)
        with contextlib.suppress(Exception):
            await ws.close()


def _sse_iterator(channel: str, request: Request):
    q: asyncio.Queue = asyncio.Queue(maxsize=128)
    sse_subscribers.setdefault(channel, []).append(q)

    async def gen():
        try:
            while True:
                if await request.is_disconnected():
                    return
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=15)
                    yield {"data": json.dumps(ev)}
                except asyncio.TimeoutError:
                    yield {"event": "ping", "data": ""}
        finally:
            with contextlib.suppress(ValueError, KeyError):
                sse_subscribers[channel].remove(q)

    return gen


@app.get("/tasks/{task_id}/stream", tags=["tasks"],
         summary="Server-Sent Events for one task",
         description=(
             "Server-Sent Events stream. Emits one JSON event per agent "
             "tool-use and status change for this task; `{\"event\":\"ping\"}` "
             "keepalives every ~15s. Subscribe instead of polling "
             "`GET /tasks/{id}`. Browser EventSource carries the auth cookie; "
             "other SSE clients can pass `?token=<token>`."))
async def task_stream(task_id: str, request: Request):
    return EventSourceResponse(_sse_iterator(task_id, request)())


@app.get("/stream", tags=["tasks"],
         summary="Server-Sent Events for all tasks (global feed)")
async def global_stream(request: Request):
    return EventSourceResponse(_sse_iterator("*", request)())
