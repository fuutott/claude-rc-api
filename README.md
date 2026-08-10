# claude-rc-api — fuutott's fork, with a terminal UI

This is a personal fork of
[ThatCrispyToast/claude-rc-api](https://github.com/ThatCrispyToast/claude-rc-api)
that adds **`claude-rc tui`** — a full-screen terminal control panel for your
Claude Code Remote Control sessions: the claude.ai/code web page, but in your
terminal. Everything below the preamble is the upstream README, unchanged; this
section covers only what the fork adds.

**Caveats:** these are the same unofficial, private endpoints the upstream
project reverse-engineered (see the ⚠️ note in the original readme below) — they
can change or break at any time. The permission-*answer* path this fork adds is
validated against the live-tested implementation in
[g2-claude-remote](https://github.com/ThatCrispyToast/g2-claude-remote) but has
not been exercised against a live worker from this fork itself; `updatedPermissions`
("always allow") and the true-dialog answer shape remain unconfirmed (flagged in
[`API_REFERENCE.md`](./API_REFERENCE.md) §3.2). A few upstream-side fixes ride
along (`response_shape` unwrapping, newest-slice history, an SSE keep-alive stall).

## Install (from this fork)

```bash
# run the TUI with zero install
uvx --from "git+https://github.com/fuutott/claude-rc-api[cli,tui]" fabio

# or install it into a venv / project
pip install "claude-rc-api[cli,tui] @ git+https://github.com/fuutott/claude-rc-api"
uv add "claude-rc-api[cli,tui] @ git+https://github.com/fuutott/claude-rc-api"
```

The TUI installs a **`fabio`** command (a friendly alias for `claude-rc tui`).
(Heads-up: an unrelated load balancer also ships a `fabio` binary — if you have
it installed, one will shadow the other on your `PATH`.)

Prerequisites are unchanged from upstream: log in to Claude Code with a
claude.ai account (`claude` → `/login`), and have a session to drive
(`claude remote-control` in some project).

## TUI

```bash
fabio                            # pick a session from the sidebar
fabio cse_abc123                 # jump straight into one
# `claude-rc tui [cse_id]` does exactly the same thing
```

A full-screen control panel built on [Textual](https://textual.textualize.io/):
a session sidebar (status dots — green idle · yellow running · red waiting on
you), a live transcript, and a composer. Selecting a session loads its history
and follows the live stream; the sidebar marks the attached session distinctly
from the cursor row.

The transcript renders the way the Claude Code CLI does, in this fork's colour
scheme: user turns on a chevron bar, assistant turns as Markdown (syntax-
highlighted code fences, lists, inline code), tool calls as `● Name(arg)` with
their output hanging below on a `└` connector, extended **thinking** shown in
full (recessed, never truncated), **TodoWrite** as a live `✔ / ◐ / ☐` checklist,
and a per-turn usage footer (duration · cost · tokens).

Type in the composer to send a message (slash commands like `/effort high` run
on the worker), or use TUI commands: `:model <id>`, `:perm <mode>`,
`:interrupt`, `:archive`, `:q`. Keys: `ctrl+x` interrupt, `ctrl+g` review
pending approvals, `ctrl+r` refresh, `ctrl+b` hide/show the session list,
`ctrl+q` quit.

**Selecting & copying text.** Drag over the transcript to select, then **⌘C /
Ctrl+C** to copy to the system clipboard. (Fabio copies via `pbcopy` / `wl-copy`
/ `xclip`, falling back to OSC 52 for remote/SSH sessions — Textual's default
OSC 52 alone doesn't reach the clipboard on macOS Terminal, which is why the
local tool is preferred.) `claude-rc events <cse_id>` also prints a session's
history as plain text from outside the app.

**Permission prompts are first-class.** When the agent blocks on a
`can_use_tool` request, a modal shows the tool name and the **full** tool input
(scrollable, never a clipped command): allow (`a`), always-allow — persisting
the suggested rule (`y`), or deny with an optional reason (`d`). `esc` defers;
unanswered prompts queue up (`ctrl+g` brings them back), prompts answered from
another controller (the web app, your phone) retire automatically, and a turn
ending abandons stale ones. **AskUserQuestion** prompts get their own flow — the
questions render as pickable option lists (single- and multi-select), answered
on the permission path the way the API delivers them.

The `claude-rc web`, `repl`, and `send` surfaces answer permission prompts too.

---

Below is the original upstream readme, unchanged.

---

# claude-rc-api

Unofficial Python client for the Claude Code Remote Control web service,
reverse-engineered from the Claude Code CLI and exercised against the live API.

It lets your own programs do what `claude.ai/code` and the Claude mobile app do:
list, observe, and steer Claude Code sessions running on a machine. It talks to
the same `/v1/code/sessions` endpoints and claude.ai OAuth token the web app
uses.

> ⚠️ **Unofficial and unsupported.** These are private endpoints
> (`api.anthropic.com`), reconstructed from the CLI binary. They can change or
> break at any time. Use your own account, at your own risk. Full protocol
> writeup in [`API_REFERENCE.md`](./API_REFERENCE.md).

---

## How it works

Remote Control relays, it doesn't run cloud compute. Your local
`claude remote-control` process runs Claude and holds an outbound connection to
Anthropic; the web app and mobile app act as a controller that posts messages
and reads the session's event stream. This library plays that controller role.

```
your script ──► POST /v1/code/sessions/{id}/events    (send a message)
            ◄── GET  /v1/code/sessions/{id}/events/stream  (SSE: read the reply)
   (claude.ai OAuth Bearer + x-organization-uuid, from ~/.claude)
```

It also ships a `ManagedAgentsClient` for the sibling public Sessions API
(`/v1/sessions`, `x-api-key`) that runs agents in Anthropic's cloud.

## Install

As a library / CLI, straight from the repo:

```bash
uv add "claude-rc-api @ git+https://github.com/ThatCrispyToast/claude-rc-api"   # into a uv project
pip install "git+https://github.com/ThatCrispyToast/claude-rc-api"              # into any venv
uvx --from "git+https://github.com/ThatCrispyToast/claude-rc-api[cli]" claude-rc list   # run the CLI, zero install
```

For development in a checkout, use [uv](https://docs.astral.sh/uv/):

```bash
uv sync --extra cli                 # runtime + pretty CLI
uv sync --extra cli --extra test    # + pytest
```

## Prerequisites

1. Log in to Claude Code with a claude.ai account (Pro/Max/Team/Enterprise):
   `claude` → `/login`. (API-key logins can't use Remote Control.)
2. Have a session to drive: run `claude remote-control` (or
   `claude --remote-control`) in some project. That registers a session you can
   then control from here.

Credentials load automatically from `~/.claude/.credentials.json` (OAuth token)
and `~/.claude.json` (`organizationUuid`). Override them with
`CLAUDE_RC_ACCESS_TOKEN` / `CLAUDE_CODE_OAUTH_TOKEN` and `CLAUDE_RC_ORG_UUID`.

## CLI

```bash
claude-rc whoami                 # login / org / token status
claude-rc list                   # your Remote Control sessions
claude-rc get   <cse_id>         # session details (JSON)
claude-rc events <cse_id>        # recent history
claude-rc watch <cse_id>         # stream live (read-only)
claude-rc send  <cse_id> "run the tests"   # send a message, print the reply
claude-rc repl  <cse_id>         # interactive chat
claude-rc web                    # browser control panel (all sessions)
```

## Web UI

A dependency-free browser control panel for your Remote Control sessions: list
them, watch the live event stream, send messages, and steer (interrupt / set
model / set permission mode / archive).

```bash
claude-rc web                    # serves http://127.0.0.1:8765 and opens it
claude-rc web --port 9000 --no-open
```

It's a standard-library `http.server` (no new dependencies) that proxies the
`RemoteControlClient`. It speaks to the private API with *your* OAuth token and
does no auth of its own, so it binds to `127.0.0.1` by default. Pass
`--host 0.0.0.0` only if you accept that anyone who can reach it can steer your
sessions. You can also run it programmatically:

```python
from claude_rc import serve_webui
serve_webui(host="127.0.0.1", port=8765, open_browser=True)
```

## Library

```python
from claude_rc import RemoteControlClient

rc = RemoteControlClient()                 # reads ~/.claude, refreshes token as needed

# discover sessions
for s in rc.sessions():
    print(s["id"], s["status"], s["title"])

sid = rc.sessions()[0]["id"]

# ask and wait for the answer (sends, streams from the pre-send cursor, stops on `result`)
for ev in rc.send_and_collect(sid, "summarize the current diff", print_stream=True):
    pass

# observe read-only
for ev in rc.stream_events(sid):
    if ev.role == "assistant":
        print(ev.text())
    if ev.type == "result":
        break

# steer
rc.interrupt(sid)
rc.set_model(sid, "claude-opus-4-8")
rc.set_permission_mode(sid, "acceptEdits")
rc.set_effort(sid, "high")  # low|medium|high|xhigh, or None = auto
```

Cloud (Managed Agents) mode:

```python
from claude_rc import ManagedAgentsClient
ma = ManagedAgentsClient(api_key="sk-ant-...")
agent = ma.create_agent(name="A", model="claude-opus-4-8",
                        tools=[{"type": "agent_toolset_20260401"}])
env = ma.create_environment(name="e", config={"type": "cloud",
                            "networking": {"type": "unrestricted"}})
sess = ma.create_session(agent=agent["id"], environment_id=env["id"])
ma.send_message(sess["id"], "hello")
for ev in ma.stream_events(sess["id"]):
    if ev.type == "agent.message": print(ev.text())
    if ev.type == "session.status_idle": break
```

See [`examples/`](./examples).

## Events

`Event` normalizes both wire formats. For Remote Control the important payload
types are `user`, `assistant`, `result` (turn complete), `control_request`
(steering / permission prompts), and `system` (init). Helpers:

```python
ev.type          # payload type
ev.role          # "user" | "assistant" | None
ev.text()        # concatenated text blocks (handles str or block-list content)
ev.tool_uses()   # tool_use content blocks
ev.sequence_num  # RC ordering/resume cursor (int)
ev.is_turn_end   # a `result` (RC) or `session.status_idle` (managed agents)
ev.is_blocking_control  # a permission prompt waiting on you
```

## Safety

- `list`/`get`/`events`/`watch` and all `RemoteControlClient` read methods are
  read-only. `send`/`repl`/`send_message`/`interrupt` inject into a live
  session, so only use them on sessions you own.
- Token refresh writes the refreshed token back to
  `~/.claude/.credentials.json` (same as the CLI). Pass `persist_refresh=False`
  to keep refreshes in memory.
- This project never prints or transmits your tokens.

## Used by

[`claude-remote-bridge`](https://github.com/ThatCrispyToast/g2-claude-remote)
(the `server/` half of the Claude Remote glasses app) depends on this package
straight from git and wraps `RemoteControlClient` in a JSON + SSE HTTP bridge. It
imports `client`, `credentials`, and `events`, and resolves this repo's default
branch on each fresh `uv` install — so keep those three backwards-compatible.

## Project layout

```
claude_rc/
  credentials.py   # OAuth load + refresh, org uuid, ~/.claude parsing
  client.py        # RemoteControlClient (mode A) + ManagedAgentsClient (mode B)
  events.py        # Event model + builders for both wire formats
  sse.py           # dependency-free SSE parser
  cli.py           # `claude-rc` command line
  webui.py         # `claude-rc web` — stdlib http.server control panel
  static/          # the web UI single-page app
API_REFERENCE.md   # full reverse-engineered protocol reference
examples/          # runnable examples
tests/             # offline unit tests (pytest)
```

## Tests

```bash
uv run --extra test pytest -q
```

All tests run offline (no network, no credentials required).
