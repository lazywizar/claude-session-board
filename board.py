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

# Optional: flow (a task manager that binds tasks to Claude sessions). Ignored if absent.
FLOW_DB = os.path.join(HOME, ".flow/flow.db")

EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
NOTIFY_DELAY = 4  # seconds a session must stay blocked before we show a desktop banner
STATE_ORDER = {"waiting": 0, "asked": 1, "busy": 2, "idle": 3}
PAGE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "board.html")


# ---------------------------------------------------------------- small helpers


def connect():
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("pragma journal_mode=wal")
    conn.executescript(
        """
        create table if not exists waiting (
            id text primary key, reason text, agent text, since real);
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


def session_name(session_id):
    """The name Claude Code shows for a session (set with /rename), else a short id."""
    for path in glob.glob(os.path.join(CLAUDE_DIR, "sessions", "*.json")):
        try:
            info = read_json(path)
        except (OSError, ValueError):
            continue
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

    reason = blocked_reason(event)
    with conn:
        if reason:
            inserted = conn.execute(
                "insert into waiting values (?, ?, ?, ?) on conflict (id) do nothing",
                (session_id, reason, agent, now),
            ).rowcount
            return reason, inserted == 1

        if name in ("PostToolUse", "PostToolUseFailure"):
            # The dialog was answered. Only the agent that opened it can clear it,
            # so a background subagent finishing a tool doesn't hide a real prompt.
            conn.execute("delete from waiting where id = ? and agent = ?", (session_id, agent))
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
    if row and row[0] == now:
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

    Transcripts can be hundreds of MB, so only the first 256 KB and last 512 KB are
    read, and results are cached until the file changes.
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
        head = f.read(256 * 1024).decode("utf-8", "ignore")
        size = f.seek(0, os.SEEK_END)
        f.seek(max(0, size - 512 * 1024))
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
        info["last_prompt"] = _user_prompt(record) or info["last_prompt"]

    _transcript_cache[path] = (mtime, info)
    return info


def transcript_path(session_id):
    matches = glob.glob(os.path.join(PROJECTS_DIR, "*", session_id + ".jsonl"))
    return matches[0] if matches else ""


def process_table():
    """All processes as {pid: info}, plus {ppid: [child pids]}."""
    # LC_ALL=C: some locales print CPU as "1,5", which float() can't parse.
    out = subprocess.run(
        ["ps", "-axo", "pid=,ppid=,pcpu=,rss=,tty=,command="],
        capture_output=True,
        text=True,
        env={**os.environ, "LC_ALL": "C"},
    ).stdout
    table, children = {}, {}
    for line in out.splitlines():
        parts = line.split(None, 5)
        if len(parts) < 6:
            continue
        pid, ppid = int(parts[0]), int(parts[1])
        table[pid] = {
            "pid": pid,
            "ppid": ppid,
            "cpu": float(parts[2]),
            "mb": int(parts[3]) // 1024,
            "tty": parts[4],
            "cmd": parts[5],
        }
        children.setdefault(ppid, []).append(pid)
    return table, children


def descendants(pid, children):
    found, stack = [], list(children.get(pid, []))
    while stack:
        child = stack.pop()
        found.append(child)
        stack.extend(children.get(child, []))
    return found


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
        out = subprocess.run([CLAUDE_BIN, "agents", "--json"], capture_output=True, text=True, timeout=20).stdout
        return json.loads(out or "[]")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    sessions = []
    for path in glob.glob(os.path.join(CLAUDE_DIR, "sessions", "*.json")):
        try:
            info = read_json(path)
            os.kill(info["pid"], 0)  # raises if the process is gone
        except (OSError, ValueError, KeyError):
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
    started = {
        agent: ended
        for agent, ended in conn.execute("select agent, ended from subagents where id = ?", (session_id,))
    }
    found = []
    for meta_path in glob.glob(os.path.join(transcript[: -len(".jsonl")], "subagents", "*.meta.json")):
        agent_id = os.path.basename(meta_path)[len("agent-") : -len(".meta.json")]
        try:
            meta = read_json(meta_path)
            last = os.path.getmtime(meta_path[: -len(".meta.json")] + ".jsonl")
        except (OSError, ValueError):
            continue
        # Hook data is exact; for subagents from before the hook existed, guess from recent writes.
        running = started[agent_id] is None if agent_id in started else now - last < 30
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


# ---------------------------------------------------------------- the board snapshot

_snapshot_lock = threading.Lock()
_snapshot = {"at": 0.0, "data": None}
_history_synced_at = [0.0]


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
    if status in ("busy", "shell", "working", "running"):
        return "busy", ""
    last_text = transcript_info.get("last_text", "").rstrip()
    if last_text.endswith("?"):
        # Finished its turn with a question. Start the snippet at a sentence, not mid-word.
        tail = last_text[-260:]
        cut = max(tail.find(". "), tail.find("\n"))
        return "asked", short(tail[cut + 1 :] if 0 <= cut < 160 else tail, 260)
    return "idle", ""


def build_snapshot():
    now, conn = time.time(), connect()
    if now - _history_synced_at[0] > 30:
        sync_history(conn)
        _history_synced_at[0] = now

    procs, children = process_table()
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

        pid = agent.get("pid")
        tree = [pid] + descendants(pid, children) if pid in procs else []
        kids = sorted((procs[p] for p in tree[1:] if p in procs), key=lambda p: -p["cpu"])
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
                "pid": pid,
                "tty": procs[pid]["tty"] if pid in procs else "",
                "cpu": round(sum(procs[p]["cpu"] for p in tree if p in procs), 1),
                "mb": sum(procs[p]["mb"] for p in tree if p in procs),
                "procs": [{"pid": p["pid"], "cpu": p["cpu"], "cmd": short(p["cmd"], 110)} for p in kids[:10]],
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
    return {"now": now, "live": live}


def snapshot():
    """The current board, rebuilt at most every 2 seconds however many tabs poll it."""
    with _snapshot_lock:
        if time.time() - _snapshot["at"] >= 2:
            _snapshot["data"] = build_snapshot()
            _snapshot["at"] = time.time()
        return _snapshot["data"]


def search_history(query, limit=100):
    """Past sessions, newest first, filtered in SQL so the page never loads the whole archive."""
    conn, tasks, query = connect(), flow_tasks(), query.strip()
    live_ids = [s["sid"] for s in snapshot()["live"]]
    task_hits = [sid for sid, task in tasks.items() if query and query.lower() in f"{task['slug']} {task['name']}".lower()]
    like = f"%{query}%"
    sql = f"""
        select * from history
        where id not in ({placeholders(len(live_ids))})
          and (? = '' or title like ? or first_prompt like ? or last_prompt like ? or cwd like ? or id like ?
               or id in ({placeholders(len(task_hits))}))
        order by mtime desc limit ?"""
    rows = conn.execute(sql, (*live_ids, query, like, like, like, like, like, *task_hits, limit))
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
_EDITORS = {"Cursor.app": "Cursor", "Visual Studio Code.app": "Visual Studio Code", "Windsurf.app": "Windsurf"}


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
        for bundle, app in _EDITORS.items():
            if bundle in cmd:
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
        # The board shows your prompts. Refusing other Host headers stops a web page
        # from reading it through DNS rebinding; refusing other Origins stops
        # cross-site POSTs to the focus endpoint.
        origin = self.headers.get("Origin")
        return self.headers.get("Host") in self.allowed_hosts and (
            origin is None or origin in (f"http://{h}" for h in self.allowed_hosts)
        )

    def do_GET(self):
        if not self._trusted():
            return self._send(403, "forbidden")
        url = urlparse(self.path)
        if url.path == "/":
            with open(PAGE_PATH, encoding="utf-8") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")
        elif url.path == "/api/sessions":
            self._send(200, json.dumps(snapshot()), "application/json")
        elif url.path == "/api/history":
            query = parse_qs(url.query).get("q", [""])[0]
            self._send(200, json.dumps(search_history(query)), "application/json")
        else:
            self._send(404, "not found")

    def do_POST(self):
        if not self._trusted():
            return self._send(403, "forbidden")
        url = urlparse(self.path)
        if url.path == "/api/focus":
            self._send(200, focus(parse_qs(url.query).get("sid", [""])[0]))
        else:
            self._send(404, "not found")

    def log_message(self, *args):
        pass


def serve():
    conn = connect()
    two_weeks_ago = time.time() - 14 * 86400
    conn.execute("delete from files where ts < ?", (two_weeks_ago,))
    conn.execute("delete from subagents where started < ?", (two_weeks_ago,))
    conn.execute("delete from waiting where since < ?", (time.time() - 3 * 86400,))
    conn.commit()
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
