import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import board  # noqa: E402


class TempHome(unittest.TestCase):
    """Points the board at a throwaway ~/.claude and database."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = self.tmp.name
        patches = {
            "CLAUDE_DIR": os.path.join(root, ".claude"),
            "PROJECTS_DIR": os.path.join(root, ".claude", "projects"),
            "DATA_DIR": os.path.join(root, "data"),
            "DB_PATH": os.path.join(root, "data", "board.db"),
            "FLOW_DB": os.path.join(root, "no-flow.db"),
        }
        for name, value in patches.items():
            patcher = mock.patch.object(board, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        board._transcript_cache.clear()
        self.conn = board.connect()
        self.addCleanup(self.conn.close)
        self.addCleanup(self.tmp.cleanup)

    def waiting(self):
        return dict(self.conn.execute("select id, reason from waits order by since desc"))


class HookTests(TempHome):
    def event(self, name, **fields):
        return {"session_id": "s1", "hook_event_name": name, **fields}

    def test_question_blocks_until_answered(self):
        ask = self.event("PreToolUse", tool_name="AskUserQuestion",
                         tool_input={"questions": [{"question": "Ship it?"}]})
        reason, newly_blocked = board.record_event(self.conn, ask, time.time())
        self.assertEqual(reason, "asks: Ship it?")
        self.assertTrue(newly_blocked)
        self.assertIn("s1", self.waiting())

        board.record_event(self.conn, self.event("PostToolUse", tool_name="AskUserQuestion"), time.time())
        self.assertEqual(self.waiting(), {})

    def test_second_prompt_does_not_restart_the_wait(self):
        board.record_event(self.conn, self.event("PermissionRequest", tool_name="Bash",
                                                 tool_input={"command": "ls"}), 1.0)
        _, newly_blocked = board.record_event(
            self.conn, self.event("Notification", notification_type="permission_prompt", message="x"), 2.0)
        self.assertFalse(newly_blocked)
        self.assertEqual(self.waiting()["s1"], "approve Bash: ls")

    def test_subagent_tool_does_not_clear_main_prompt(self):
        board.record_event(self.conn, self.event("PermissionRequest", tool_name="Bash",
                                                 tool_input={"command": "rm x"}), time.time())
        board.record_event(self.conn, self.event("PostToolUse", tool_name="Read", agent_id="sub1"), time.time())
        self.assertIn("s1", self.waiting())

    def test_parallel_tool_does_not_clear_open_prompt(self):
        board.record_event(self.conn, self.event("PermissionRequest", tool_name="Bash", tool_use_id="t1",
                                                 tool_input={"command": "rm build"}), 1)
        board.record_event(self.conn, self.event("PostToolUse", tool_name="Read", tool_use_id="t2"), 2)
        self.assertIn("s1", self.waiting())
        board.record_event(self.conn, self.event("PostToolUse", tool_name="Bash", tool_use_id="t1"), 3)
        self.assertNotIn("s1", self.waiting())

    def test_each_open_prompt_is_kept(self):
        _, first = board.record_event(self.conn, self.event("PermissionRequest", tool_name="Bash", agent_id="a",
                                                            tool_use_id="t1", tool_input={"command": "x"}), 1)
        _, second = board.record_event(self.conn, self.event("PermissionRequest", tool_name="Bash", agent_id="b",
                                                             tool_use_id="t2", tool_input={"command": "y"}), 2)
        self.assertEqual((first, second), (True, False))  # one notification per blocked session
        board.record_event(self.conn, self.event("PostToolUse", tool_name="Bash", agent_id="a", tool_use_id="t1"), 3)
        self.assertEqual(self.waiting(), {"s1": "approve Bash: y"})
        board.record_event(self.conn, self.event("PostToolUse", tool_name="Bash", agent_id="b", tool_use_id="t2"), 4)
        self.assertEqual(self.waiting(), {})

    def test_notification_first_then_permission_request(self):
        board.record_event(self.conn, self.event("Notification", notification_type="permission_prompt", message="x"), 1)
        board.record_event(self.conn, self.event("PermissionRequest", tool_name="Bash", tool_use_id="t1",
                                                 tool_input={"command": "x"}), 2)
        board.record_event(self.conn, self.event("PostToolUse", tool_name="Read", tool_use_id="t9"), 3)
        self.assertIn("s1", self.waiting())  # the wait is tied to t1 now, so t9 doesn't clear it

    def test_stop_clears_any_prompt(self):
        board.record_event(self.conn, self.event("PreToolUse", tool_name="ExitPlanMode"), time.time())
        board.record_event(self.conn, self.event("Stop"), time.time())
        self.assertEqual(self.waiting(), {})

    def test_idle_notification_is_not_a_block(self):
        reason, _ = board.record_event(
            self.conn, self.event("Notification", notification_type="idle_prompt"), time.time())
        self.assertIsNone(reason)
        self.assertEqual(self.waiting(), {})

    def test_edits_and_subagents_are_recorded(self):
        board.record_event(self.conn, self.event("PostToolUse", tool_name="Edit",
                                                 tool_input={"file_path": "/a.py"}), 1.0)
        board.record_event(self.conn, self.event("SubagentStart", agent_id="a1", agent_type="Explore"), 1.0)
        board.record_event(self.conn, self.event("SubagentStop", agent_id="a1"), 2.0)
        self.assertEqual(self.conn.execute("select path from files").fetchall(), [("/a.py",)])
        self.assertEqual(self.conn.execute("select type, ended from subagents").fetchall(), [("Explore", 2.0)])


class TranscriptTests(TempHome):
    def write_transcript(self, records):
        folder = os.path.join(board.PROJECTS_DIR, "-repo")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, "abc.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(json.dumps(r) for r in records) + "\n")
        return path

    def test_reads_title_prompts_pr_and_last_reply(self):
        path = self.write_transcript([
            {"type": "user", "cwd": "/repo", "message": {"content": "<command-name>/clear</command-name>"}},
            {"type": "user", "cwd": "/repo", "message": {"content": "Fix the login bug"}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "content": "ok"}]}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "Done. Open a PR?"}]}},
            {"type": "ai-title", "aiTitle": "Login bug fix"},
            {"type": "pr-link", "prUrl": "https://example.com/pr/1"},
        ])
        info = board.read_transcript(path)
        self.assertEqual(info["title"], "Login bug fix")
        self.assertEqual(info["first_prompt"], "Fix the login bug")
        self.assertEqual(info["last_prompt"], "Fix the login bug")
        self.assertEqual(info["pr"], "https://example.com/pr/1")
        self.assertEqual(info["cwd"], "/repo")
        self.assertEqual(board.session_state("abc", "idle", info, {})[0], "asked")

    def test_history_search(self):
        self.write_transcript([
            {"type": "user", "cwd": "/repo", "message": {"content": "Add dark mode"}},
            {"type": "ai-title", "aiTitle": "Dark mode toggle"},
        ])
        with mock.patch.object(board, "snapshot", return_value={"live": []}):
            board.sync_history(self.conn)
            self.assertEqual(board.search_history("dark")["rows"][0]["title"], "Dark mode toggle")
            self.assertEqual(board.search_history("nothing like this")["rows"], [])
            self.assertEqual(board.search_history("' or 1=1 --")["rows"], [])
            self.assertEqual(board.search_history("dark")["total"], 1)


class RepoTests(unittest.TestCase):
    def test_worktree_resolves_to_main_repo(self):
        with tempfile.TemporaryDirectory() as root:
            main = os.path.join(root, "my-app")
            os.makedirs(os.path.join(main, ".git", "worktrees", "feature"))
            tree = os.path.join(root, "trees", "feature")
            os.makedirs(os.path.join(tree, "src"))
            with open(os.path.join(tree, ".git"), "w", encoding="utf-8") as f:
                f.write(f"gitdir: {main}/.git/worktrees/feature\n")
            self.assertEqual(board.repo_of(os.path.join(tree, "src")), "my-app")
            self.assertEqual(board.repo_of(main), "my-app")


class ProcessTests(unittest.TestCase):
    def proc(self, pid, cmd, ppid=1, uid=None):
        uid = os.getuid() if uid is None else uid
        return {"pid": pid, "ppid": ppid, "uid": uid, "cmd": cmd, "cpu": 0.0, "mb": 1, "stat": "S", "started": 0}

    def test_ps_durations(self):
        self.assertEqual(board.ps_seconds("0:01.50"), 1.5)
        self.assertEqual(board.ps_seconds("12:03"), 723)
        self.assertEqual(board.ps_seconds("01:02:03"), 3723)
        self.assertEqual(board.ps_seconds("2-00:00:01"), 2 * 86400 + 1)

    def test_leftovers(self):
        procs = [
            self.proc(10, "node /code/app/node_modules/.bin/vite --port 5173"),
            self.proc(11, "-zsh"),
            self.proc(12, "/Users/me/.cache/gitstatus/gitstatusd-darwin-arm64 -s 1", ppid=11),
            self.proc(13, "/opt/homebrew/bin/bash -c npm test"),
            self.proc(14, "node jest", ppid=13),
            self.proc(15, "/Applications/Cursor.app/Contents/MacOS/Cursor"),
            self.proc(16, "python3 serve.py", uid=os.getuid() + 1),
            self.proc(17, "/opt/homebrew/opt/postgresql/bin/postgres -D /opt/homebrew/var/postgres"),
            self.proc(18, "/Users/me/go/bin/some-daemon --flag"),
            self.proc(19, "/usr/lib/systemd/systemd --user"),
            self.proc(20, "python3 -m http.server", ppid=19),
        ]
        procs = {p["pid"]: p for p in procs}
        children = {11: [12], 13: [14], 19: [20]}
        services = {17}  # started by brew services / launchd on purpose
        leftovers = {pid for pid, p in procs.items() if board.is_leftover(p, procs, children, services)}
        self.assertEqual(leftovers, {10, 13, 20})

    def test_editor_windows_are_named_after_their_folder(self):
        renderer = "/Applications/Cursor.app/Contents/Frameworks/Cursor Helper (Renderer).app/Contents/MacOS/x --type=renderer"
        host = "Cursor Helper (Plugin): extension-host (user) {} [{}-711]"
        procs = {
            1: {"pid": 1, "started": 100.0, "cmd": f"{renderer} --vscode-window-config=vscode:aaaa"},
            2: {"pid": 2, "started": 101.0, "cmd": host.format("my-app", 8)},
            3: {"pid": 3, "started": 107.0, "cmd": f"{renderer} --vscode-window-config=vscode:bbbb"},
            4: {"pid": 4, "started": 108.0, "cmd": host.format("api server", 9)},
            5: {"pid": 5, "started": 109.0, "cmd": f"{renderer} --vscode-window-config=vscode:bbbb"},
        }
        self.assertEqual(board.editor_windows(procs), {1: "my-app", 3: "api server", 5: "api server"})

    def test_cpu_is_measured_between_calls(self):
        def ps(cpu_time, pcpu="3.0"):
            line = f"  42     1   501  {pcpu}  {cpu_time}  05:00  20480  R  ??  node build.js\n"
            return mock.Mock(stdout=line)

        samples = {}
        with mock.patch.object(board.sys, "platform", "darwin"), mock.patch.object(board.subprocess, "run") as run, \
                mock.patch.object(board.time, "time") as clock:
            run.return_value, clock.return_value = ps("0:10.00"), 1000.0
            first, _ = board.process_table(samples)
            run.return_value, clock.return_value = ps("0:13.00"), 1004.0
            second, _ = board.process_table(samples)
        self.assertEqual(first[42]["cpu"], 3.0)  # nothing to compare yet: ps's own number
        self.assertEqual(second[42]["cpu"], 75.0)  # 3 CPU seconds in 4 seconds

    def test_kill_stops_the_whole_tree(self):
        shell = subprocess.Popen(["sh", "-c", "sleep 60; true"])
        self.addCleanup(shell.kill)
        time.sleep(0.3)
        procs, children = board.process_table()
        sleeper = children[shell.pid][0]

        ok, message = board.kill_tree(shell.pid, procs[shell.pid]["started"] - 60)
        self.assertFalse(ok)
        self.assertIsNone(shell.poll())  # a stale start time means a different process: untouched

        ok, _ = board.kill_tree(shell.pid, procs[shell.pid]["started"])
        self.assertTrue(ok)
        shell.wait(timeout=5)
        time.sleep(0.2)
        self.assertNotIn(sleeper, board.process_table()[0])

    def test_kill_refuses_the_boards_own_ancestors(self):
        procs, _ = board.process_table()
        parent = os.getppid()
        ok, message = board.kill_tree(parent, procs[parent]["started"])
        self.assertFalse(ok)
        self.assertIn("board itself", message)

    def test_peer_uid_of_a_local_connection(self):
        server = socket.socket()
        self.addCleanup(server.close)
        server.bind(("127.0.0.1", 0))
        server.listen()
        client = socket.create_connection(server.getsockname())
        self.addCleanup(client.close)
        accepted, _ = server.accept()
        self.addCleanup(accepted.close)
        # In a real request the client is another process (the browser); here it is
        # this test, which peer_uid skips as "the server", so pretend to be pid 0.
        with mock.patch.object(board.os, "getpid", return_value=0):
            uid = board.peer_uid(client.getsockname()[1], server.getsockname()[1])
        self.assertEqual(uid, os.getuid())


class DiscoveryTests(unittest.TestCase):
    def test_failed_discovery_falls_back_to_the_registry(self):
        failed = subprocess.CompletedProcess([], 1, stdout="", stderr="boom")
        registry = [{"sessionId": "s1", "pid": os.getpid()}]
        with mock.patch.object(board.subprocess, "run", return_value=failed), \
                mock.patch.object(board, "session_files", return_value=iter(registry)):
            self.assertEqual(board.live_sessions(), registry)

    def test_empty_discovery_is_trusted(self):
        empty = subprocess.CompletedProcess([], 0, stdout="[]\n", stderr="")
        with mock.patch.object(board.subprocess, "run", return_value=empty):
            self.assertEqual(board.live_sessions(), [])


class ServerTests(unittest.TestCase):
    def check(self, host, origin=None):
        handler = board.Handler.__new__(board.Handler)
        handler.headers = {"Host": host, **({"Origin": origin} if origin else {})}
        return handler._trusted()

    def test_only_local_hosts_and_origins(self):
        self.assertTrue(self.check(f"127.0.0.1:{board.PORT}"))
        self.assertTrue(self.check(f"localhost:{board.PORT}", f"http://localhost:{board.PORT}"))
        self.assertFalse(self.check(f"evil.example:{board.PORT}"))
        self.assertFalse(self.check(f"127.0.0.1:{board.PORT}", "https://evil.example"))


if __name__ == "__main__":
    unittest.main()
