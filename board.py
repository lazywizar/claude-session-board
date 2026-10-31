#!/usr/bin/env python3
"""claude-session-board: one local page for every Claude Code session on this machine.

Usage:
    board.py serve        start the web UI (default http://127.0.0.1:7777)
    board.py who [PATH]   list other live sessions working in PATH's repo
    board.py hook         Claude Code hook entry point (reads the event JSON on stdin)

Everything is read from files Claude Code already writes. The hook only adds what
those files can't tell us: whether a session is blocked on you right now, which
subagents are running, and which files it edited. Standard library only.
"""

import glob
import json
import os
import re
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HOME = os.path.expanduser("~")
CLAUDE_DIR = os.path.join(HOME, ".claude")
PROJECTS_DIR = os.path.join(CLAUDE_DIR, "projects")
DATA_DIR = os.environ.get("CLAUDE_BOARD_DATA", os.path.join(CLAUDE_DIR, "session-board"))
DB_PATH = os.path.join(DATA_DIR, "board.db")
PORT = int(os.environ.get("CLAUDE_BOARD_PORT", "7777"))
CLAUDE_BIN = shutil.which("claude") or os.path.join(HOME, ".local/bin/claude")
LSOF_BIN = shutil.which("lsof") or "/usr/sbin/lsof"  # launchd's default PATH has no /usr/sbin

# Optional: flow (a task manager that binds tasks to Claude sessions). Ignored if absent.
FLOW_DB = os.path.join(HOME, ".flow/flow.db")

EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
NOTIFY_DELAY = 4  # seconds a session must stay blocked before we show a desktop banner
HEAD_BYTES = 256 * 1024  # enough to find the first prompt
TAIL_BYTES = 512 * 1024  # enough to find the latest title, prompt and reply
STATE_ORDER = {"waiting": 0, "asked": 1, "busy": 2, "idle": 3}
HOT_CPU = 80  # percent of one core
HOT_SECONDS = 120  # how long a process has to stay above HOT_CPU before we call it out
PAGE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "board.html")


# ---------------------------------------------------------------- small helpers


def connect():
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("pragma journal_mode=wal")
    conn.executescript(
        """
        create table if not exists waiting (
            id text primary key, reason text, agent text, since real, tool_use_id text);
        create table if not exists subagents (
            id text, agent text, type text, started real, ended real,
            primary key (id, agent));
        create table if not exists files (
            id text, path text, ts real, primary key (id, path));
        create table if not exists history (
            id text primary key, cwd text, title text, first_prompt text,
            last_prompt text, pr text, mtime real);
        """
    )
    try:  # databases created before tool_use_id was tracked
        conn.execute("alter table waiting add column tool_use_id text")
    except sqlite3.OperationalError:
        pass
    return conn


def read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def short(text, limit=160):
    """Collapse whitespace and cut to `limit` characters."""
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def placeholders(n):
    """'?,?,?' for an IN clause; a never-matching '' when the list is empty."""
    return ",".join("?" * n) or "''"


def repo_of(cwd):
    """Name of the git repo containing `cwd`. Worktrees resolve to their main repo."""
    path = cwd or ""
    while path and path != os.path.dirname(path):
        dot_git = os.path.join(path, ".git")
        if os.path.isdir(dot_git):
            return os.path.basename(path)
        if os.path.isfile(dot_git):
            # A worktree: ".git" is a file pointing at <main repo>/.git/worktrees/<name>.
            with open(dot_git, encoding="utf-8") as f:
                match = re.search(r"gitdir:\s*(.+)", f.read())
            if match and "/.git/worktrees/" in match.group(1):
                return os.path.basename(match.group(1).split("/.git/worktrees/")[0])
            return os.path.basename(path)
        path = os.path.dirname(path)
    return os.path.basename(cwd or "") or "?"


def session_files():
    """Claude Code's per-process registry: one JSON file per running session."""
    for path in glob.glob(os.path.join(CLAUDE_DIR, "sessions", "*.json")):
        try:
            yield read_json(path)
        except (OSError, ValueError):
            continue


def session_name(session_id):
    """The name Claude Code shows for a session (set with /rename), else a short id."""
    for info in session_files():
        if info.get("sessionId") == session_id:
            return info.get("name") or session_id[:8]
    return session_id[:8]


def notify(title, message):
    """Best-effort desktop notification."""
    if sys.platform == "darwin":
        script = 'on run argv\ndisplay notification (item 1 of argv) with title (item 2 of argv) sound name "Glass"\nend run'
        subprocess.run(["osascript", "-e", script, message, title], capture_output=True)
    elif shutil.which("notify-send"):
        subprocess.run(["notify-send", title, message], capture_output=True)


# ---------------------------------------------------------------- hook


def blocked_reason(event):
    """If this event means the session is now waiting on the user, say why."""
    name, tool = event.get("hook_event_name"), event.get("tool_name") or ""
    tool_input = event.get("tool_input") or {}
    if name == "PreToolUse" and tool == "AskUserQuestion":
        questions = tool_input.get("questions")
        first = questions[0] if isinstance(questions, list) and questions and isinstance(questions[0], dict) else {}
        return "asks: " + short(first.get("question") or "a question", 140)
    if name == "PreToolUse" and tool == "ExitPlanMode":
        return "plan ready for your review"
    if name == "PermissionRequest":
        target = tool_input.get("command") or tool_input.get("file_path") or tool_input.get("url")
        return f"approve {tool}: " + short(target or json.dumps(tool_input), 120)
    if name == "Notification" and event.get("notification_type") in ("permission_prompt", "elicitation_dialog"):
        return short(event.get("message") or "needs your input", 140)
    return None


def record_event(conn, event, now):
    """Apply one hook event to the database. Returns (reason, newly_blocked)."""
    session_id, name = event.get("session_id"), event.get("hook_event_name")
    agent = event.get("agent_id") or ""
    tool = event.get("tool_name") or ""
    tool_input = event.get("tool_input") or {}
    tool_use_id = event.get("tool_use_id")

    reason = blocked_reason(event)
    with conn:
        if reason:
            inserted = conn.execute(
                "insert into waiting (id, reason, agent, since, tool_use_id) values (?, ?, ?, ?, ?)"
                " on conflict (id) do nothing",
                (session_id, reason, agent, now, tool_use_id),
            ).rowcount
            if not inserted and tool_use_id:
                # A Notification (which has no tool_use_id) may have opened this wait first.
                conn.execute(
                    "update waiting set tool_use_id = ? where id = ? and tool_use_id is null",
                    (tool_use_id, session_id),
                )
            return reason, inserted == 1

        if name in ("PostToolUse", "PostToolUseFailure"):
            # The dialog was answered once the tool call that opened it finishes. Other
            # tools running in parallel, or in a background subagent, don't count.
            conn.execute(
                "delete from waiting where id = ? and agent = ? and (tool_use_id is null or tool_use_id = ?)",
                (session_id, agent, tool_use_id),
            )
            path = tool_input.get("file_path") or tool_input.get("notebook_path")
            if name == "PostToolUse" and tool in EDIT_TOOLS and path:
                conn.execute(
                    "insert into files values (?, ?, ?) on conflict (id, path) do update set ts = excluded.ts",
                    (session_id, path, now),
                )
        elif name in ("UserPromptSubmit", "Stop", "SessionStart", "SessionEnd"):
            conn.execute("delete from waiting where id = ?", (session_id,))
        elif name == "SubagentStart":
            conn.execute(
                "insert or replace into subagents values (?, ?, ?, ?, null)",
                (session_id, agent, event.get("agent_type"), now),
            )
        elif name == "SubagentStop":
            conn.execute("update subagents set ended = ? where id = ? and agent = ?", (now, session_id, agent))
    return None, False


def run_hook():
    try:
        event = json.load(sys.stdin)
    except ValueError:
        return
    if not event.get("session_id"):
        return
    now = time.time()
    conn = connect()
    reason, newly_blocked = record_event(conn, event, now)
    if not newly_blocked:
        return
    # Most permission prompts in auto mode resolve on their own. Only bother the
    # user if this one is still open after a few seconds.
    time.sleep(NOTIFY_DELAY)
    row = conn.execute("select since from waiting where id = ?", (event["session_id"],)).fetchone()
    if row and row[0] == now:  # still the same wait this hook started, not a newer one
        notify(f"Claude needs you: {session_name(event['session_id'])}", reason)


# ---------------------------------------------------------------- reading Claude Code's files

_transcript_cache = {}


def _json_lines(blob):
    for line in blob.splitlines():
        try:
            yield json.loads(line)
        except ValueError:
            pass


def _user_prompt(record):
    """The text the user typed, or '' for tool results, slash commands and injected context."""
    if record.get("type") != "user" or record.get("isMeta") or record.get("isSidechain"):
        return ""
    content = (record.get("message") or {}).get("content")
    if isinstance(content, list):
        blocks = [b for b in content if isinstance(b, dict)]
        if any(b.get("type") == "tool_result" for b in blocks):
            return ""
        content = " ".join(b.get("text", "") for b in blocks)
    text = str(content or "")
    if re.match(r"\s*(<(command-|local-command|system-reminder|task-notification)|Caveat:)", text):
        return ""
    return short(re.sub(r"<[^>]+>", " ", text), 300)


def read_transcript(path):
    """Title, first and last prompt, PR link and Claude's last reply from a transcript.

    Transcripts can be hundreds of MB, so only the head and tail are read, and
    results are cached until the file changes.
    """
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return {}
    cached = _transcript_cache.get(path)
    if cached and cached[0] == mtime:
        return cached[1]

    info = {"title": "", "first_prompt": "", "last_prompt": "", "pr": "", "last_text": "", "cwd": "", "mtime": mtime}
    with open(path, "rb") as f:
        head = f.read(HEAD_BYTES).decode("utf-8", "ignore")
        size = f.seek(0, os.SEEK_END)
        f.seek(max(0, size - TAIL_BYTES))
        tail = f.read().decode("utf-8", "ignore")

    for record in _json_lines(head):
        info["cwd"] = info["cwd"] or record.get("cwd", "")
        info["first_prompt"] = info["first_prompt"] or _user_prompt(record)
        if info["first_prompt"] and info["cwd"]:
            break

    for record in _json_lines(tail):
        kind = record.get("type")
        info["cwd"] = record.get("cwd") or info["cwd"]
        if kind == "ai-title":
            info["title"] = record.get("aiTitle") or info["title"]
        elif kind == "custom-title":
            info["title"] = record.get("customTitle") or info["title"]
        elif kind == "last-prompt":
            info["last_prompt"] = short(record.get("lastPrompt"), 300)
        elif kind == "pr-link":
            info["pr"] = record.get("prUrl") or info["pr"]
        elif kind == "assistant" and not record.get("isSidechain"):
            blocks = (record.get("message") or {}).get("content") or []
            text = " ".join(b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text")
            if text.strip():
                info["last_text"] = text.strip()[-400:]
        # Records are in order, so whichever comes last wins: Claude's own "last-prompt"
        # record, or a user message we can read directly.
        info["last_prompt"] = _user_prompt(record) or info["last_prompt"]

    _transcript_cache[path] = (mtime, info)
    return info


def transcript_path(session_id):
    matches = glob.glob(os.path.join(PROJECTS_DIR, "*", session_id + ".jsonl"))
    return matches[0] if matches else ""


def ps_seconds(text):
    """Seconds from a ps duration such as "12:03.51", "01:02:03" or "3-04:05:06"."""
    days, _, clock = text.rpartition("-")
    seconds = 0.0
    for part in clock.split(":"):
        seconds = seconds * 60 + float(part)
    return seconds + int(days or 0) * 86400


def linux_cpu_seconds(pid):
    """CPU time from /proc. Linux ps rounds it to whole seconds, too coarse to sample every few seconds."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            # The command name can contain spaces and parentheses, so split after its closing ")".
            fields = f.read().rsplit(b")", 1)[1].split()
        return (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")  # utime + stime
    except (OSError, IndexError, ValueError):
        return None


def process_table(cpu_samples=None):
    """All processes as {pid: info}, plus {ppid: [child pids]}.

    By default "cpu" is ps's own %cpu. On macOS that is a decaying average: it lags by
    a minute and smooths spikes away, which hides exactly the runaway build you are
    looking for. Pass a dict that you keep between calls as `cpu_samples` and "cpu"
    becomes the share of one core each process really used since the previous call.
    """
    # LC_ALL=C: some locales print CPU as "1,5", which float() can't parse.
    out = subprocess.run(
        ["ps", "-axo", "pid=,ppid=,uid=,pcpu=,time=,etime=,rss=,stat=,tty=,command="],
        capture_output=True,
        text=True,
        env={**os.environ, "LC_ALL": "C"},
    ).stdout
    now, table, children = time.time(), {}, {}
    for line in out.splitlines():
        parts = line.split(None, 9)
        if len(parts) < 10:
            continue
        pid, ppid = int(parts[0]), int(parts[1])
        cpu = float(parts[3])
        if cpu_samples is not None:
            used = linux_cpu_seconds(pid) if sys.platform.startswith("linux") else None
            used = ps_seconds(parts[4]) if used is None else used
            previous = cpu_samples.get(pid)
            if not previous or now - previous[1] >= 1:  # shorter gaps are too noisy to measure
                if previous:
                    cpu = max(0.0, 100 * (used - previous[0]) / (now - previous[1]))
                cpu_samples[pid] = (used, now)
        table[pid] = {
            "pid": pid,
            "ppid": ppid,
            "uid": int(parts[2]),
            "cpu": round(cpu, 1),
            "started": now - ps_seconds(parts[5]),
            "mb": int(parts[6]) // 1024,
            "stat": parts[7],
            "tty": parts[8],
            "cmd": parts[9],
        }
        children.setdefault(ppid, []).append(pid)
    if cpu_samples is not None:
        for pid in cpu_samples.keys() - table.keys():
            del cpu_samples[pid]
    return table, children


def process_row(proc, label="", killable=False):
    """What the page needs to show one process. Kill is only offered for your own
    session children and leftovers, never for apps or the system."""
    return {
        "pid": proc["pid"],
        "cpu": proc["cpu"],
        "mb": proc["mb"],
        "started": proc["started"],
        "cmd": short(proc["cmd"], 140),
        "label": label,
        "killable": killable and proc["uid"] == os.getuid(),
    }


def descendants(pid, children):
    found, stack = [], list(children.get(pid, []))
    while stack:
        child = stack.pop()
        found.append(child)
        stack.extend(children.get(child, []))
    return found


def process_stats(pid, procs, children):
    """CPU and memory for a session's whole process tree, plus its busiest children."""
    if pid not in procs:
        return {"tty": "", "cpu": 0, "mb": 0, "procs": []}
    tree = [procs[p] for p in [pid, *descendants(pid, children)] if p in procs]
    busiest = sorted(tree[1:], key=lambda p: -p["cpu"])[:10]
    return {
        "tty": procs[pid]["tty"],
        "cpu": round(sum(p["cpu"] for p in tree), 1),
        "mb": sum(p["mb"] for p in tree),
        "procs": [process_row(p, killable=True) for p in busiest],
    }


def flow_tasks():
    """{session_id: task} from flow's database, or {} if flow isn't installed."""
    if not os.path.exists(FLOW_DB):
        return {}
    try:
        flow = sqlite3.connect(f"file:{FLOW_DB}?mode=ro", uri=True, timeout=2)
        rows = flow.execute("select session_id, slug, name, status from tasks where session_id is not null")
        return {sid: {"slug": slug, "name": name, "status": status} for sid, slug, name, status in rows}
    except sqlite3.Error:
        return {}


def live_sessions():
    """Running sessions from `claude agents --json`, or the per-process registry as a fallback."""
    try:
        result = subprocess.run([CLAUDE_BIN, "agents", "--json"], capture_output=True, text=True, timeout=20)
        if result.returncode == 0 and result.stdout.strip():
            return json.loads(result.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    sessions = []
    for info in session_files():
        try:
            os.kill(info["pid"], 0)  # raises if the process is gone
        except (OSError, KeyError, TypeError):
            continue
        sessions.append(info)
    return sessions


def background_need(job_id):
    """What a blocked background job is waiting for, from its state file."""
    try:
        job = read_json(os.path.join(CLAUDE_DIR, "jobs", job_id or "", "state.json"))
    except (OSError, ValueError):
        return ""
    return short(job.get("needs") or job.get("detail"), 160)


def subagents_of(session_id, transcript, conn, now):
    """Subagents a session spawned, newest first, running ones on top."""
    if not transcript:
        return []
    ended_at = dict(conn.execute("select agent, ended from subagents where id = ?", (session_id,)))
    found = []
    for meta_path in glob.glob(os.path.join(transcript[: -len(".jsonl")], "subagents", "*.meta.json")):
        agent_id = os.path.basename(meta_path)[len("agent-") : -len(".meta.json")]
        try:
            meta = read_json(meta_path)
            last = os.path.getmtime(meta_path[: -len(".meta.json")] + ".jsonl")
        except (OSError, ValueError):
            continue
        if agent_id in ended_at:
            running = ended_at[agent_id] is None
        else:
            # Started before the hook was installed: guess from recent writes.
            running = now - last < 30
        found.append(
            {"desc": short(meta.get("description"), 120), "type": meta.get("agentType", ""), "running": running, "last": last}
        )
    found.sort(key=lambda s: (not s["running"], -s["last"]))
    return found[:20]


def resume_command(session_id, cwd, task=None, background_id=None):
    if background_id:
        return f"claude attach {background_id}"
    if task:
        return f"flow do {task['slug']}"
    return f"cd {shlex.quote(cwd or HOME)} && claude --resume {session_id}"


# ---------------------------------------------------------------- the whole machine

# Programs a coding session typically leaves running: dev servers, test runners, watchers, builds.
DEV_TOOL = re.compile(
    r"node|python[\d.]*|Python|ruby|java|go|cargo|rustc|make|gradle|deno|bun|npm|npx|pnpm|yarn|"
    r"tsc|vite|esbuild|webpack|jest|vitest|pytest|uvicorn|gunicorn|celery|playwright|"
    r"chrome|Chromium|chrome-headless-shell|headless_shell|mongod|redis-server|postgres"
)
SHELL = re.compile(r"(ba|z|da|fi)?sh")
# Never leftovers: apps, the OS, Claude Code's own background daemon, and this board.
NOT_LEFTOVER = re.compile(r"^/(System|Applications)/|^/Library/Apple/|ClaudeCode\.app|--bg-pty-host|--bg-spare|board\.py")
# Where orphans end up: pid 1, or on Linux desktops usually the per-user systemd.
SUBREAPER = re.compile(r"\bsystemd --user\b")
# Shells re-parent the prompt helper they started (powerlevel10k's gitstatusd); on its own it means nothing is running.
PROMPT_HELPER = re.compile(r"gitstatusd")
# VS Code and its forks (Cursor, Windsurf) run one renderer per window, which draws the
# editor and every terminal tab in it. Its command line carries a window id but no folder.
WINDOW_ID = re.compile(r"--vscode-window-config=vscode:([0-9a-f-]+)")
# ...while each window's extension host is titled with its folder: "extension-host (user) my-app [10-719]".
EXTENSION_HOST = re.compile(r"extension-host \(user\) (.+?) \[\d+-")
HOT_CHECK_SECONDS = 15  # how often the server samples CPU when nobody has the page open

_hot_since, _hot_notified = {}, set()


def program_name(cmd):
    """"node" for "/usr/local/bin/node server.js", "zsh" for a login shell's "-zsh"."""
    first = cmd.split(None, 1)[0] if cmd.strip() else ""
    return os.path.basename(first).lstrip("-")


def service_pids(candidates):
    """Pids started by the OS service manager (brew services, systemd units): running on purpose."""
    if sys.platform == "darwin":
        out = subprocess.run(["launchctl", "list"], capture_output=True, text=True).stdout
        return {int(line.split()[0]) for line in out.splitlines() if line.split()[:1] and line.split()[0].isdigit()}
    services = set()
    for pid in candidates:
        try:
            with open(f"/proc/{pid}/cgroup", encoding="utf-8") as f:
                if f.read().strip().endswith(".service"):
                    services.add(pid)
        except OSError:
            pass
    return services


def orphaned(proc, procs):
    parent = procs.get(proc["ppid"])
    return proc["ppid"] == 1 or bool(parent and SUBREAPER.search(parent["cmd"]))


def is_leftover(proc, procs, children, services=frozenset()):
    """True for one of your dev tools whose parent is gone.

    When a terminal tab or Claude session closes, whatever it started is re-parented
    and keeps running: a dev server, a test watcher, a build stuck in a loop. A
    re-parented shell only counts if something is still running under it.
    """
    if proc["uid"] != os.getuid() or proc["pid"] in services or NOT_LEFTOVER.search(proc["cmd"]):
        return False
    if not orphaned(proc, procs):
        return False
    name = program_name(proc["cmd"])
    if SHELL.fullmatch(name):
        return any(c in procs and not PROMPT_HELPER.search(procs[c]["cmd"]) for c in children.get(proc["pid"], []))
    return bool(DEV_TOOL.fullmatch(name))


def editor_windows(procs):
    """{renderer pid: folder name} for VS Code-style editor windows.

    Nothing links a renderer to its folder directly, but a window starts its renderer
    and then, a few seconds later, its extension host. So pair each window, oldest
    first, with the first unclaimed extension host that started within a minute of it.
    """
    windows = {}  # window id -> (start time, [renderer pids])
    for proc in procs.values():
        match = WINDOW_ID.search(proc["cmd"])
        if match:
            start, pids = windows.get(match.group(1), (proc["started"], []))
            windows[match.group(1)] = (min(start, proc["started"]), pids + [proc["pid"]])
    hosts = sorted(
        (p["started"], m.group(1)) for p in procs.values() for m in [EXTENSION_HOST.search(p["cmd"])] if m
    )
    folders, claimed = {}, set()
    for start, pids in sorted(windows.values()):
        for i, (host_start, folder) in enumerate(hosts):
            if i not in claimed and -2 <= host_start - start <= 60:
                claimed.add(i)
                folders.update(dict.fromkeys(pids, folder))
                break
    return folders


def working_dirs(pids):
    """{pid: current directory} for a few processes, in one lsof call. {} if lsof isn't available."""
    if not pids:
        return {}
    try:
        out = subprocess.run(
            [LSOF_BIN, "-a", "-d", "cwd", "-Fpn", "-p", ",".join(map(str, pids))],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    dirs, pid = {}, None
    for line in out.splitlines():
        if line.startswith("p"):
            pid = int(line[1:])
        elif line.startswith("n") and pid:
            dirs[pid] = line[1:]
    return dirs


def machine_report(procs, children, sessions, now):
    """Load, the busiest processes, leftovers, zombies, and anything that has been hot for a while."""
    owner = {}  # pid -> name of the live session it belongs to
    for s in sessions:
        if s["pid"] in procs:
            for pid in [s["pid"], *descendants(s["pid"], children)]:
                owner[pid] = s["name"]
    session_pids = {s["pid"] for s in sessions}
    windows = editor_windows(procs)
    services = service_pids([p["pid"] for p in procs.values() if orphaned(p, procs)])
    leftover_roots = {pid for pid, p in procs.items() if is_leftover(p, procs, children, services)}

    def describe(proc):
        """(label, killable): who a process belongs to, and whether the page offers Kill for it."""
        if proc["pid"] in windows:
            app = re.search(r"/([^/]+)\.app/", proc["cmd"])
            return f"{app.group(1) if app else 'editor'} window: {windows[proc['pid']]}", False
        ancestor, seen = proc, set()
        while ancestor and ancestor["pid"] not in seen:
            if ancestor["pid"] in owner:
                # Not the session's own claude process: close the session for that.
                return "session: " + owner[ancestor["pid"]], proc["pid"] not in session_pids
            if ancestor["pid"] in leftover_roots:
                return "leftover", True
            seen.add(ancestor["pid"])
            ancestor = procs.get(ancestor["ppid"])
        app = re.search(r"/([^/]+)\.app/", proc["cmd"])
        return (app.group(1) if app else "system"), False

    leftovers = []
    for pid in leftover_roots:
        # Report the whole tree: a stuck build is usually a parent and many workers.
        tree = [procs[p] for p in [pid, *descendants(pid, children)] if p in procs]
        row = process_row(procs[pid], "leftover", killable=True)
        row.update(cpu=round(sum(p["cpu"] for p in tree), 1), mb=sum(p["mb"] for p in tree), count=len(tree))
        leftovers.append(row)
    leftovers = sorted(leftovers, key=lambda r: (-r["cpu"], -r["mb"]))[:30]
    dirs = working_dirs([r["pid"] for r in leftovers])
    session_dirs = {s["cwd"]: s["name"] for s in sessions if s["cwd"]}
    for row in leftovers:
        row["cwd"] = dirs.get(row["pid"], "")
        if row["cwd"] in session_dirs:
            row["label"] = "leftover, likely from " + session_dirs[row["cwd"]]

    def row(proc):
        result = process_row(proc, *describe(proc))
        if proc["pid"] in windows:
            # The window's CPU is mostly its terminals, so name the sessions likely running in it.
            folder = windows[proc["pid"]].lower()
            result["sessions"] = [{"name": s["name"], "state": s["state"]} for s in sessions if s["repo"].lower() == folder]
        return result

    busiest = sorted(procs.values(), key=lambda p: -p["cpu"])[:10]
    return {
        "load": round(os.getloadavg()[0], 1),
        "cores": os.cpu_count() or 1,
        "top": [row(p) for p in busiest],
        "hot": track_hot(procs, row, now),
        "leftovers": leftovers,
        "zombies": [
            {"pid": p["pid"], "parent": p["ppid"], "parent_cmd": short(procs.get(p["ppid"], {}).get("cmd", "?"), 100)}
            for p in procs.values()
            if p["stat"].startswith("Z")
        ],
    }


def track_hot(procs, row, now):
    """Processes above HOT_CPU for HOT_SECONDS. Sends one desktop notification for any that just crossed the line."""
    hot, new = [], []
    for proc in procs.values():
        if proc["cpu"] < HOT_CPU:
            _hot_since.pop(proc["pid"], None)
            continue
        since = _hot_since.setdefault(proc["pid"], now)
        if now - since < HOT_SECONDS:
            continue
        hot.append({**row(proc), "hot_for": now - since})
        if proc["pid"] not in _hot_notified:
            _hot_notified.add(proc["pid"])
            new.append(proc)
    for pid in _hot_since.keys() - procs.keys():
        del _hot_since[pid]
    _hot_notified.intersection_update(procs.keys())
    if new:
        worst = max(new, key=lambda p: p["cpu"])
        title = f"High CPU: {row(worst)['label']}" if len(new) == 1 else f"High CPU: {len(new)} processes"
        message = f"{worst['cpu']:.0f}% for {HOT_SECONDS // 60}+ min: {short(worst['cmd'], 80)}"
        threading.Thread(target=notify, args=(title, message), daemon=True).start()
    return sorted(hot, key=lambda h: -h["cpu"])


def kill_tree(pid, started, force=False):
    """Stop a process and everything it started. Returns (ok, message).

    `started` is the start time the page showed. If it doesn't match, the pid now
    belongs to a different process and we leave it alone.
    """
    if os.getuid() == 0:
        return False, "The board is running as root, so Kill is turned off."
    procs, children = process_table()
    proc = procs.get(pid)
    if not proc or abs(proc["started"] - started) > 2:
        return False, "That process has already exited."
    if proc["uid"] != os.getuid():
        return False, "Can't kill that one: it belongs to another user."
    board_and_ancestors, ancestor = set(), os.getpid()
    while ancestor in procs and ancestor not in board_and_ancestors:
        board_and_ancestors.add(ancestor)
        ancestor = procs[ancestor]["ppid"]
    tree = [pid, *descendants(pid, children)]
    if pid <= 1 or board_and_ancestors & set(tree):
        return False, "Can't kill that one: the board itself is running inside it."
    tree = [p for p in tree if procs.get(p, {}).get("uid") == os.getuid()]

    def send(sig):
        for target in tree:
            try:
                os.kill(target, sig)
            except OSError:  # exited in the meantime
                pass

    # Freeze the whole tree first, so a watcher such as nodemon can't restart a child
    # in the moment between that child's signal and its own.
    send(signal.SIGSTOP)
    send(signal.SIGKILL if force else signal.SIGTERM)
    if force:
        return True, f"Force-killed {len(tree)} process(es)."
    send(signal.SIGCONT)  # a stopped process only acts on SIGTERM once it runs again
    return True, f"Asked {len(tree)} process(es) to stop. If it's still listed in a few seconds, use Force kill."


def sample_forever():
    """Keep CPU samples and hot alerts current even when no browser has the board open."""
    while True:
        try:
            snapshot()
        except Exception as error:  # keep sampling; one bad read shouldn't end alerts for good
            print(f"sampling failed: {error!r}", file=sys.stderr, flush=True)
        time.sleep(HOT_CHECK_SECONDS)


# ---------------------------------------------------------------- the board snapshot

_cache_lock = threading.Lock()
_cache = {"at": 0.0, "data": None, "history_synced_at": 0.0}
_cpu_samples = {}  # only snapshot() uses this, under _cache_lock, so the CPU baseline stays steady


def sync_history(conn):
    """Index every transcript so past topics stay searchable even after Claude deletes old transcripts."""
    known = dict(conn.execute("select id, mtime from history"))
    for path in glob.glob(os.path.join(PROJECTS_DIR, "*", "*.jsonl")):
        session_id = os.path.basename(path)[: -len(".jsonl")]
        if session_id.startswith("agent-"):
            continue
        try:
            if known.get(session_id) == os.path.getmtime(path):
                continue
        except OSError:
            continue
        t = read_transcript(path)
        if t.get("first_prompt") or t.get("title"):
            conn.execute(
                "insert or replace into history values (?, ?, ?, ?, ?, ?, ?)",
                (session_id, t["cwd"], t["title"], t["first_prompt"], t["last_prompt"], t["pr"], t["mtime"]),
            )
    conn.commit()


def session_state(session_id, status, transcript_info, waiting):
    """One of waiting / asked / busy / idle, plus the line to show for it."""
    if session_id in waiting:
        return "waiting", waiting[session_id]
    if status == "blocked":  # a background job waiting on the user
        return "waiting", ""
    # Interactive sessions report busy/idle/shell; background jobs report working/running.
    if status in ("busy", "shell", "working", "running"):
        return "busy", ""
    last_text = transcript_info.get("last_text", "").rstrip()
    if last_text.endswith("?"):
        # Finished its turn with a question. Start the snippet at a sentence, not mid-word.
        tail = last_text[-260:]
        cut = max(tail.find(". "), tail.find("\n"))
        return "asked", short(tail[cut + 1 :] if 0 <= cut < 160 else tail, 260)
    return "idle", ""


def build_snapshot(cpu_samples=None, with_machine=False):
    now, conn = time.time(), connect()
    if now - _cache["history_synced_at"] > 30:
        sync_history(conn)
        _cache["history_synced_at"] = now

    procs, children = process_table(cpu_samples)
    tasks = flow_tasks()
    waiting = dict(conn.execute("select id, reason from waiting"))
    live = []
    for agent in live_sessions():
        session_id = agent.get("sessionId")
        if not session_id:
            continue
        transcript = transcript_path(session_id)
        t = read_transcript(transcript) if transcript else {}
        cwd = agent.get("cwd") or t.get("cwd", "")
        background = agent.get("kind") == "background"
        state, reason = session_state(session_id, agent.get("status") or agent.get("state") or "", t, waiting)
        if background and not reason:
            reason = background_need(agent.get("id"))

        files = conn.execute("select path from files where id = ? order by ts desc limit 15", (session_id,))
        live.append(
            {
                "sid": session_id,
                "name": agent.get("name") or session_id[:8],
                "cwd": cwd,
                "repo": repo_of(cwd),
                "kind": "background" if background else "interactive",
                "state": state,
                "reason": reason,
                "pid": agent.get("pid"),
                **process_stats(agent.get("pid"), procs, children),
                "title": t.get("title", ""),
                "first_prompt": t.get("first_prompt", ""),
                "last_prompt": t.get("last_prompt", ""),
                "pr": t.get("pr", ""),
                "flow": tasks.get(session_id),
                "last_active": t.get("mtime") or (agent.get("startedAt") or 0) / 1000,
                "subagents": subagents_of(session_id, transcript, conn, now),
                "files": [row[0] for row in files],
                "resume": resume_command(session_id, cwd, tasks.get(session_id), agent.get("id") if background else None),
            }
        )
    live.sort(key=lambda s: (STATE_ORDER[s["state"]], -s["last_active"]))
    board = {"now": now, "live": live}
    if with_machine:
        board["machine"] = machine_report(procs, children, live, now)
    return board


def snapshot():
    """The current board, rebuilt at most every 2 seconds however many tabs poll it."""
    with _cache_lock:
        if time.time() - _cache["at"] >= 2:
            _cache["data"] = build_snapshot(_cpu_samples, with_machine=True)
            _cache["at"] = time.time()
        return _cache["data"]


def search_history(query, limit=100):
    """Past sessions, newest first, filtered in SQL so the page never loads the whole archive."""
    conn, tasks, query = connect(), flow_tasks(), query.strip()
    live_ids = [s["sid"] for s in snapshot()["live"]]
    where, params = f"id not in ({placeholders(len(live_ids))})", list(live_ids)
    if query:
        columns = ("title", "first_prompt", "last_prompt", "cwd", "id")
        task_hits = [sid for sid, task in tasks.items() if query.lower() in f"{task['slug']} {task['name']}".lower()]
        matches = [f"{column} like ?" for column in columns] + [f"id in ({placeholders(len(task_hits))})"]
        where += f" and ({' or '.join(matches)})"
        params += [f"%{query}%"] * len(columns) + task_hits
    rows = conn.execute(f"select * from history where {where} order by mtime desc limit ?", (*params, limit))
    results = [
        {
            "sid": sid,
            "cwd": cwd,
            "repo": repo_of(cwd),
            "title": title,
            "first_prompt": first,
            "last_prompt": last,
            "pr": pr,
            "flow": tasks.get(sid),
            "last_active": mtime,
            "resumable": bool(transcript_path(sid)),
            "resume": resume_command(sid, cwd, tasks.get(sid)),
        }
        for sid, cwd, title, first, last, pr, mtime in rows
    ]
    total = conn.execute(
        f"select count(*) from history where id not in ({placeholders(len(live_ids))})", live_ids
    ).fetchone()[0]
    return {"rows": results, "total": total}


# ---------------------------------------------------------------- `who`, for agents


def print_who(path):
    repo = repo_of(os.path.abspath(path)).lower()
    procs, _ = process_table()
    mine, pid = set(), os.getpid()
    while pid in procs and pid not in mine:  # skip the session that is asking
        mine.add(pid)
        pid = procs[pid]["ppid"]

    others = [s for s in build_snapshot()["live"] if s["repo"].lower() == repo and s["pid"] not in mine]
    if not others:
        print(f"No other live Claude sessions in {repo}.")
        return
    print(f"{len(others)} other live Claude session(s) in {repo}:")
    for s in others:
        minutes = int((time.time() - s["last_active"]) / 60)
        task = f" task:{s['flow']['slug']}" if s["flow"] else ""
        print(f"- {s['name']} [{s['state']}, active {minutes}m ago]{task}  cwd={s['cwd']}")
        print(f"    topic: {s['title'] or s['first_prompt'][:120]}")
        if s["last_prompt"]:
            print(f"    last ask: {s['last_prompt'][:160]}")
        if s["files"]:
            print(f"    recently edited: {', '.join(s['files'][:8])}")
        running = [a["desc"] for a in s["subagents"] if a["running"]]
        if running:
            print(f"    subagents running: {'; '.join(running)}")


# ---------------------------------------------------------------- jump to a session's terminal

_SELECT_TAB_BY_TTY = {
    "iTerm2": """tell application "iTerm2"
  repeat with w in windows
    repeat with t in tabs of w
      repeat with s in sessions of t
        if tty of s is "/dev/{tty}" then
          select w
          tell t to select
          tell s to select
          activate
          return "ok"
        end if
      end repeat
    end repeat
  end repeat
end tell
return "tab not found in iTerm2" """,
    "Terminal": """tell application "Terminal"
  repeat with w in windows
    repeat with t in tabs of w
      if tty of t is "/dev/{tty}" then
        set selected of t to true
        set index of w to 1
        activate
        return "ok"
      end if
    end repeat
  end repeat
end tell
return "tab not found in Terminal" """,
}

# Editors whose integrated terminal can't be targeted from outside: we open the folder's window.
_EDITORS = ("Cursor", "Visual Studio Code", "Windsurf")


def focus(session_id):
    if sys.platform != "darwin":
        return "Jumping to a terminal is only supported on macOS. Use the resume command."
    session = next((s for s in snapshot()["live"] if s["sid"] == session_id), None)
    if not session or not session.get("pid"):
        return "No terminal found for this session. Use the resume command."

    procs, _ = process_table()
    pid = session["pid"]
    while pid in procs and pid > 1:
        cmd = procs[pid]["cmd"]
        if "iTerm" in cmd:
            return _select_tab("iTerm2", session["tty"])
        if "Terminal.app" in cmd:
            return _select_tab("Terminal", session["tty"])
        for app in _EDITORS:
            if f"{app}.app" in cmd:
                subprocess.run(["open", "-a", app, session["cwd"]], timeout=10)
                return f"Opened its {app} window. Pick the terminal tab there."
        pid = procs[pid]["ppid"]
    return "Can't jump to this terminal app. Use the resume command."


def _select_tab(app, tty):
    if not re.fullmatch(r"ttys\d+", tty or ""):
        return "No terminal found for this session."
    script = _SELECT_TAB_BY_TTY[app].replace("{tty}", tty)
    result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=10)
    return (result.stdout or result.stderr).strip()


# ---------------------------------------------------------------- web server


def peer_uid(client_port, server_port):
    """The uid that owns the client end of a local TCP connection, or None if we can't tell."""
    if sys.platform.startswith("linux"):
        # /proc/net/tcp lists every socket with its owner; ports are hex.
        wanted = (f":{client_port:04X}", f":{server_port:04X}")
        for table in ("/proc/net/tcp", "/proc/net/tcp6"):
            try:
                with open(table, encoding="ascii") as f:
                    for line in list(f)[1:]:
                        fields = line.split()
                        if fields[1].endswith(wanted[0]) and fields[2].endswith(wanted[1]):
                            return int(fields[7])
            except (OSError, IndexError, ValueError):
                continue
        return None
    try:
        out = subprocess.run(
            [LSOF_BIN, "-nP", f"-iTCP@127.0.0.1:{client_port}", "-sTCP:ESTABLISHED", "-Fpu"],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    # lsof prints "p<pid>" then "u<uid>" for every process with that socket open,
    # including this server (whose remote end is the client port). Skip ourselves.
    pid = None
    for line in out.splitlines():
        if line.startswith("p"):
            pid = int(line[1:])
        elif line.startswith("u") and pid != os.getpid():
            return int(line[1:])
    return None



class Handler(BaseHTTPRequestHandler):
    allowed_hosts = (f"127.0.0.1:{PORT}", f"localhost:{PORT}")

    def _send(self, code, body, content_type="text/plain; charset=utf-8"):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _trusted(self):
        # The board shows your prompts and can kill processes. Refusing other Host
        # headers stops a web page from reading it through DNS rebinding; refusing
        # other Origins stops cross-site POSTs to the focus and kill endpoints.
        origin = self.headers.get("Origin")
        return self.headers.get("Host") in self.allowed_hosts and (
            origin is None or origin in (f"http://{h}" for h in self.allowed_hosts)
        )

    def _from_this_user(self):
        # Any account on this machine can reach 127.0.0.1. The board shows your
        # prompts and can kill your processes, so only serve connections that come
        # from one of your own processes.
        uid = peer_uid(self.client_address[1], PORT)
        return uid is not None and uid == os.getuid()

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def _route(self, method):
        if not self._trusted():
            return self._send(403, "forbidden")
        if not self._from_this_user():
            return self._send(403, "Only the user running the board can open it.")
        url = urlparse(self.path)
        query = parse_qs(url.query)
        if method == "GET" and url.path == "/":
            with open(PAGE_PATH, encoding="utf-8") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")
        elif method == "GET" and url.path == "/api/sessions":
            self._send(200, json.dumps(snapshot()), "application/json")
        elif method == "GET" and url.path == "/api/history":
            self._send(200, json.dumps(search_history(query.get("q", [""])[0])), "application/json")
        elif method == "POST" and url.path == "/api/focus":
            self._send(200, focus(query.get("sid", [""])[0]))
        elif method == "POST" and url.path == "/api/kill":
            try:
                pid, started = int(query["pid"][0]), float(query["started"][0])
            except (KeyError, ValueError):
                return self._send(400, "pid and started are required")
            ok, message = kill_tree(pid, started, force=query.get("force") == ["1"])
            self._send(200 if ok else 409, message)
        else:
            self._send(404, "not found")

    def log_message(self, *args):
        pass


def serve():
    conn = connect()
    now = time.time()
    # Edits and subagents only matter while a session is fresh. A "blocked" flag older
    # than a few days is almost certainly from a session that died mid-prompt.
    conn.execute("delete from files where ts < ?", (now - 14 * 86400,))
    conn.execute("delete from subagents where started < ?", (now - 14 * 86400,))
    conn.execute("delete from waiting where since < ?", (now - 3 * 86400,))
    conn.commit()
    threading.Thread(target=sample_forever, daemon=True).start()
    print(f"claude-session-board on http://127.0.0.1:{PORT}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


def main(argv):
    command = argv[1] if len(argv) > 1 else "serve"
    if command == "hook":
        try:
            run_hook()
        except Exception:  # never break a Claude session because of the board
            pass
    elif command == "who":
        print_who(argv[2] if len(argv) > 2 else os.getcwd())
    elif command == "serve":
        serve()
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
