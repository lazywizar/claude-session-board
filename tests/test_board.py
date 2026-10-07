import json
import os
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
        return dict(self.conn.execute("select id, reason from waiting"))


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
