# claude-session-board

A local web page that shows every Claude Code session running on your machine: what each one is
working on, which ones are blocked waiting for you, their subagents and processes, and how to get
back to them.

Built for the "twenty terminal tabs, one of them has been asking me a question for an hour"
problem.

- Sessions grouped by what needs you, with a desktop notification when one gets blocked.
- Click a row for its prompts, subagents, child processes and CPU, edited files and PR.
- **Jump to terminal** for iTerm2 and Terminal, or the editor window for VS Code, Cursor and Windsurf.
- Searchable history of past sessions, each with its resume command.
- `board.py who` tells an agent which other sessions are working in the same repo.

No LLM calls, no tokens, no dependencies: one Python file that reads what Claude Code already
writes to `~/.claude`, plus a small hook for the things those files can't tell it.

## Requirements

- Claude Code
- Python 3.9 or newer
- macOS. Linux works except for **Jump to terminal**; notifications use `notify-send` there.

## Set it up with Claude

Paste this into Claude Code:

```text
Install https://github.com/lazywizar/claude-session-board by following its README's
"Set it up by hand" section: clone to ~/.claude-session-board, merge the hooks into my
~/.claude/settings.json (back it up first, keep my existing hooks), and on macOS install the
launchd job with my paths filled in. Then open http://127.0.0.1:7777 to check it works.
Ask me before the optional steps in that section. Show me what you changed.
```

## Set it up by hand

```sh
git clone https://github.com/lazywizar/claude-session-board ~/.claude-session-board
python3 ~/.claude-session-board/board.py serve      # http://127.0.0.1:7777
```

Then add the hooks from [`examples/settings-hooks.json`](examples/settings-hooks.json) to
`~/.claude/settings.json`, keeping any hooks you already have. To keep the board running on macOS,
use [`examples/launchd.plist`](examples/launchd.plist) (instructions inside the file).

Sessions that were already running pick up the hooks when you open `/hooks` in them once, or when
you resume them. Everything except the "blocked" signal works without the hooks.

Optional:

- Add a line to `~/.claude/CLAUDE.md` asking agents to run `python3 ~/.claude-session-board/board.py who`
  before editing files in a repo.
- Set `"cleanupPeriodDays": 365` in `~/.claude/settings.json`. Claude Code deletes transcripts
  after 30 days by default, and a session can't be resumed without its transcript.

## Using it

| State | Meaning |
|---|---|
| **Blocked** (red) | A permission prompt, `AskUserQuestion` or plan review is open. The session can't continue until you answer. |
| **Asked you** (amber) | It finished its turn and its last message ends with a question. A heuristic. |
| **Working** (green) | Running tools or writing a reply. |
| **Done** | Finished, waiting for your next instruction. |

**Name your sessions** with `/rename <name>`. The board shows that name. To see it on VS Code or
Cursor terminal tabs too, set `"terminal.integrated.tabs.title": "${process}${separator}${sequence}"`
and don't rename tabs by hand (a manual tab name hides the one Claude sets).

**Close sessions freely.** Claude saves the conversation as it goes. Close the tab or press
`Ctrl+D`, and the session moves to History with a resume command (`claude --resume <id>`).

**For agents**, run `python3 ~/.claude-session-board/board.py who` from a repo:

```text
2 other live Claude session(s) in my-app:
- auth-refactor [busy, active 0m ago]  cwd=/Users/you/code/my-app
    topic: Move session handling to middleware
    recently edited: src/middleware/session.ts, src/routes/login.ts
```

## Configuration

| Variable | Default | |
|---|---|---|
| `CLAUDE_BOARD_PORT` | `7777` | Port for the web UI. |
| `CLAUDE_BOARD_DATA` | `~/.claude/session-board` | Where `board.db` lives. |

If you use flow, a task manager that records which Claude session works on which task in
`~/.flow/flow.db`, the board shows each session's task and offers `flow do <task>` as its resume
command. Without it, nothing changes.

## How it works

The board reads, never writes, Claude Code's own files:

| Source | Gives |
|---|---|
| `claude agents --json` | Live sessions, busy or idle, background jobs |
| `~/.claude/projects/*/<session>.jsonl` | Title, first and last prompt, PR link, last reply |
| `~/.claude/projects/*/<session>/subagents/*.meta.json` | Subagent descriptions |
| `~/.claude/jobs/<id>/state.json` | What a blocked background job needs |
| `ps` | Child processes and CPU |

The hook (`board.py hook`, run asynchronously so it never slows a session down) records what
those can't: when a dialog opens and closes, when subagents start and stop, and which files were
edited. It stores that, plus an index of past sessions, in `board.db`.

## Privacy

Everything stays on your machine. The server listens on `127.0.0.1` only and rejects requests whose
`Host` or `Origin` isn't localhost, so web pages can't read your prompts. `board.db` holds short
excerpts of your prompts; delete it any time.

## Uninstall

1. `launchctl bootout gui/$(id -u)/claude-session-board` and delete the `.plist`.
2. Remove the `board.py hook` entries from `~/.claude/settings.json`.
3. Delete `~/.claude-session-board` and `~/.claude/session-board`.

## Development

```sh
python3 -m unittest discover -s tests
```

## License

MIT
