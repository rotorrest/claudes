# claude-monitor (Claudios)

> One command to see what **all** your Claude Code sessions are doing: who's working, who finished, who's blocked waiting for you — and jump to its terminal tab with a single keystroke.

🇬🇧 English · [🇪🇸 Español](README.es.md)

[![CI](https://github.com/rotorrest/claude-monitor/actions/workflows/ci.yml/badge.svg)](https://github.com/rotorrest/claude-monitor/actions/workflows/ci.yml)
[![CodeQL](https://github.com/rotorrest/claude-monitor/actions/workflows/codeql.yml/badge.svg)](https://github.com/rotorrest/claude-monitor/actions/workflows/codeql.yml)
[![Release](https://img.shields.io/github/v/release/rotorrest/claude-monitor)](https://github.com/rotorrest/claude-monitor/releases/latest)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![macOS](https://img.shields.io/badge/macOS-Apple%20Silicon%20%7C%20Intel-lightgrey)
![deps](https://img.shields.io/badge/dependencies-0%20(stdlib)-brightgreen)

<p align="center">
  <img src="docs/demo.svg" alt="claude-monitor TUI showing Claude Code sessions grouped by attention: blocked, waiting for reply, and working" width="900">
</p>

⭐ **If claude-monitor saves you alt-tabs, a star helps others find it.**

You run 5+ Claude Code sessions in parallel and live alt-tabbing to check which one needs you. `claudios` reads the state Claude Code publishes in `~/.claude/sessions/` and shows it sorted by attention: blocked sessions first, then stopped ones (with Claude's last message, so you know *where it left off*), then the ones still working. Press a row's key and it drops you in that session's terminal tab.

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/rotorrest/claude-monitor/main/install.sh | bash
```

or with Homebrew:

```bash
brew install rotorrest/tap/claude-monitor
```

If Homebrew complains about an untrusted tap, run `brew trust rotorrest/tap` and retry.

The installer verifies the release's SHA256 checksums, installs `claudios` + `claude-notify` into `~/.local/bin` and creates the `claude-monitor` alias. Zero dependencies: system Python 3 and nothing else.

## Usage

```bash
claudios                  # snapshot of every session
claudios -w [sec]         # watch mode (refreshes every N sec, default 3)
claude-monitor           # alias: starts straight in watch mode
claudios focus <id>       # focus that session's tab (sessionId or pid)
claudios --json           # JSON snapshot, for scripts/statuslines
claudios update           # self-update from the latest release (SHA256-verified)
```

### Watch-mode keys

| Key | Action |
|-----|--------|
| `↑`/`↓` or `j`/`k` | move the selection cursor between sessions |
| `→` or `l` | open the selected session's conversation, live, without leaving the terminal (`↑↓` scroll, `←`/`Esc` back) |
| `Enter` | jump to the selected session, across Spaces/screens |
| `1-9`, `a`… | jump straight to that row (quick keys) |
| `d` | per-container Docker detail |
| `u` | token usage & cost detail per session |
| `q` | quit |

The jump knows where each session lives: exact tab in **Terminal/iTerm2** (by tty), the project window in **Cursor/VS Code/Windsurf** (detects the editor via the process tree and reuses its window with `open -a`). As far as we know, no other monitor jumps into editor-integrated terminals.

### What it shows

- **Sessions grouped by attention**: blocked waiting for permission → stopped waiting for your reply (with Claude's last message) → working.
- **Background agents**: if a session ended its turn but its subagents are still writing to the transcript, it's flagged `⚙ agentes en background activos` instead of showing as stopped.
- **System metrics**: CPU, load, RAM + memory pressure, swap, disk, per-container Docker, battery + temperature and thermal throttling (so you know whether your Mac can take one more session).
- **Token usage & cost (last 5h)**: totals in/out/cache, estimated API-equivalent cost, burn rate with a 10-minute sparkline, and a per-session top (`u` key) — read from your local JSONLs, zero network, prices for the current model lineup built in.

## Notifications

`claude-notify` turns Claude Code's `Stop`/`Notification` hooks into native macOS notifications. With [terminal-notifier](https://github.com/julienXX/terminal-notifier) installed (`brew install terminal-notifier`), **clicking the notification takes you to that session's tab**.

Add to `~/.claude/settings.json` (merge with your existing hooks, don't replace them):

```json
{
  "hooks": {
    "Stop": [
      { "hooks": [{ "type": "command", "command": "~/.local/bin/claude-notify" }] }
    ],
    "Notification": [
      { "hooks": [{ "type": "command", "command": "~/.local/bin/claude-notify" }] }
    ]
  }
}
```

## JSON mode

`claudios --json` emits raw state to compose with statuslines, SwiftBar/xbar, or anything else:

```json
[
  {
    "pid": 41231,
    "name": "api-payments",
    "cwd": "/Users/you/work/api-payments",
    "sessionId": "1fab6f1a-…",
    "status": "waiting",
    "bg": false,
    "age": 254.3,
    "waitingFor": "permission for Bash(git push)"
  }
]
```

## Auto-update

- `claudios update` downloads the latest GitHub release, **verifies every file against `SHA256SUMS`** and replaces itself atomically. Running watch instances detect the new binary and **restart themselves within seconds** — no stale monitors.
- Watch mode checks (at most once a day) whether a new version exists and says so in the footer.
- `CLAUDIOS_NO_UPDATE_CHECK=1` turns the check off. If you installed via brew you may prefer `brew upgrade claude-monitor`.

## Security & privacy

- **Everything is local.** The tool reads files under your `~/.claude/` and system commands (`ps`, `vm_stat`, `pmset`…). The only network call is the version check against `api.github.com` (1/day, opt-out above).
- **Zero dependencies.** Pure Python stdlib — no supply chain to audit, and CI enforces it with a `stdlib-only` guard that fails if any import leaves the stdlib.
- **Sanitized inputs.** Strings coming from session files/transcripts are stripped of control characters before rendering (no ANSI-escape injection into your terminal), session IDs are validated before being used in paths or commands, and ttys are validated before being interpolated into AppleScript.
- **Pipeline with teeth.** Every push runs `ruff` + `bandit` + CodeQL + shellcheck + the stdlib guard; every release passes the same gate **before** publishing, and the tag must match `__version__`.

## Roadmap (features proudly stolen)

Ideas mapped from the best tools in the ecosystem — credit where due:

- [x] Attention-first ordering (validated by [tmux-claude-session-manager](https://github.com/craftzdog/tmux-claude-session-manager))
- [x] `--json` to compose with other tools ([Claude-Code-Usage-Monitor](https://github.com/Maciek-roboblog/Claude-Code-Usage-Monitor))
- [x] Background-agent detection on "idle" sessions
- [x] Jumping into editor-integrated terminals (Cursor/VS Code/Windsurf) across Spaces
- [ ] Notification polish: debounce, turn duration, suppress when you're already looking at that tab ([claude-ghostty-notify](https://github.com/thejustinwalsh/claude-ghostty-notify), [CCNotify](https://github.com/dazuiba/CCNotify))
- [ ] Ghostty tty-based jumping (today only a title-based fallback on the current Space) ([claude-code-monitor](https://github.com/onikan27/claude-code-monitor))
- [x] Per-session tokens/cost + burn rate from the JSONLs (inspired by [ccusage](https://github.com/ryoppippi/ccusage) and [Claude-Code-Usage-Monitor](https://github.com/Maciek-roboblog/Claude-Code-Usage-Monitor))
- [ ] Remaining-context % per session ([claude-tui](https://github.com/slima4/claude-tui))
- [ ] Per-session CPU/RAM via process tree ([claude-dashboard](https://github.com/seunggabi/claude-dashboard))
- [ ] Auto-compact layout on narrow terminals (ccusage)
- [ ] Mobile web UI with QR + remote permission approval (claude-code-monitor) — the crown jewel


## FAQ / Troubleshooting

**Does it send my code or transcripts anywhere?** No. Everything is read locally; the only network call is a daily version check against `api.github.com` (disable with `CLAUDIOS_NO_UPDATE_CHECK=1`).

**Pressing a key doesn't jump to the session.** The jump uses AppleScript/System Events — grant your terminal app **Accessibility** permission (System Settings → Privacy & Security → Accessibility) the first time macOS asks. For Cursor/VS Code the project folder must be open as the window's workspace root.

**No sessions show up.** The monitor reads `~/.claude/sessions/`, which recent Claude Code versions maintain — update Claude Code if that directory is empty while sessions are running.

**`brew install` says the tap is untrusted.** Run `brew trust rotorrest/tap` and retry (newer Homebrew requires trusting third-party taps).

**Notifications don't focus the tab when clicked.** Install [terminal-notifier](https://github.com/julienXX/terminal-notifier) (`brew install terminal-notifier`); without it notifications fall back to plain osascript (no click action).

## Related tools

| Tool | Focus |
|---|---|
| [ccusage](https://github.com/ryoppippi/ccusage) | costs and tokens from the JSONLs |
| [Claude-Code-Usage-Monitor](https://github.com/Maciek-roboblog/Claude-Code-Usage-Monitor) | plan limits, burn rate, prediction |
| [claude-squad](https://github.com/smtg-ai/claude-squad) | orchestrating agents in tmux + worktrees |
| [ccboard](https://github.com/FlorianBruniaux/ccboard) | all-in-one dashboard (Rust) |
| **claude-monitor** | **who needs my attention right now, and where did it leave off?** |

## Development

```bash
git clone https://github.com/rotorrest/claude-monitor && cd claude-monitor
python3 src/claudios.py -w        # run from source
ruff check src/ && bandit -ll -r src/
python3 tools/screenshot.py docs/demo.svg   # regenerate the README screenshot
```

PRs run the same CI (lint, SAST, CodeQL, macOS smoke test). To release: bump `__version__` in `src/claudios.py`, tag `vX.Y.Z`, push the tag — the pipeline does the rest (security gate → build → release with checksums → brew formula bump).

## Uninstall

```bash
rm ~/.local/bin/claudios ~/.local/bin/claude-notify ~/.local/bin/claude-monitor
# or: brew uninstall claude-monitor
```


## Star history

[![Star History Chart](https://api.star-history.com/svg?repos=rotorrest/claude-monitor&type=Date)](https://star-history.com/#rotorrest/claude-monitor&Date)

## License

[MIT](LICENSE)
