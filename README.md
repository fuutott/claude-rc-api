# claude-rc-api — fuutott's fork, with a terminal UI

This is a personal fork of
[ThatCrispyToast/claude-rc-api](https://github.com/ThatCrispyToast/claude-rc-api)
that adds **`claude-rc tui`** — a full-screen terminal control panel for your
Claude Code Remote Control sessions (the claude.ai/code web page, but in your
terminal): session sidebar, live transcript that renders the way the Claude
Code CLI does (Markdown, syntax-highlighted code, tool calls with their output,
thinking, todo checklists), a composer, steering commands, and first-class
permission prompts — approve/deny tool calls (with the **full** tool input
shown, never a clipped command) and answer AskUserQuestion prompts with option
pickers.

**Caveats:** these are the same unofficial, private endpoints the upstream
project reverse-engineered (see the ⚠️ note below) — they can change or break at
any time. The permission-*answer* path this fork adds is validated against the
live-tested implementation in
[g2-claude-remote](https://github.com/ThatCrispyToast/g2-claude-remote) but has
not been exercised against a live worker from this fork itself; `updatedPermissions`
("always allow") and the true-dialog answer shape remain unconfirmed (flagged in
[`API_REFERENCE.md`](./API_REFERENCE.md) §3.2). A few upstream fixes ride along
(`response_shape` unwrapping, newest-slice history, an SSE keep-alive stall).

Install **from this fork**:

```bash
# run the TUI with zero install
uvx --from "git+https://github.com/fuutott/claude-rc-api[cli,tui]" claude-rc tui

# or install it into a venv / project
pip install "claude-rc-api[cli,tui] @ git+https://github.com/fuutott/claude-rc-api"
uv add "claude-rc-api[cli,tui] @ git+https://github.com/fuutott/claude-rc-api"

# then
claude-rc tui                    # pick a session from the sidebar
claude-rc tui cse_abc123         # jump straight into one
```

Prerequisites are unchanged from upstream: log in to Claude Code with a
claude.ai account (`claude` → `/login`), and have a session to drive
(`claude remote-control` in some project). Full TUI docs are in the
[TUI section](#tui) below.

Below is the original readme.

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
uv sync --extra cli --extra tui     # + the terminal UI (`claude-rc tui`)
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
claude-rc repl  <cse_id>         # interactive chat (answers permission prompts)
claude-rc tui   [cse_id]         # terminal control panel (needs the `tui` extra)
claude-rc web                    # browser control panel (all sessions)
```

## TUI

A full-screen terminal control panel (the web page, but in your terminal),
built on [Textual](https://textual.textualize.io/):

```bash
pip install "claude-rc-api[tui] @ git+https://github.com/<you>/claude-rc-api"
claude-rc tui                    # session sidebar + live transcript + composer
claude-rc tui cse_abc123         # jump straight into a session
```

Sessions live in a sidebar with status dots (green idle · yellow running ·
red waiting on you); selecting one loads history and follows the live stream.
Type in the composer to send messages (slash commands like `/effort high` run
on the worker), or use TUI commands: `:model <id>`, `:perm <mode>`,
`:interrupt`, `:archive`, `:q`. Keys: `ctrl+x` interrupt, `ctrl+g` review
pending approvals, `ctrl+r` refresh, `ctrl+q` quit.

**Permission prompts are first-class.** When the agent blocks on a
`can_use_tool` request, a modal shows the tool name and full input: allow
(`a`), always-allow — persisting the CLI's suggested rule (`y`), or deny with
an optional reason (`d`). `esc` defers; unanswered prompts queue up (`ctrl+g`
brings them back), prompts answered from another controller (the web app,
your phone) retire automatically, and a turn ending abandons stale ones.
**AskUserQuestion prompts get their own flow** — the questions render as
pickable option lists (single- and multi-select), answered on the permission
path the way the API actually delivers them.

The answer wire format matches the live-validated implementation in
[`g2-claude-remote`](https://github.com/ThatCrispyToast/g2-claude-remote)
(the glasses app whose bridge first confirmed it) — see
[`API_REFERENCE.md`](./API_REFERENCE.md) §3.2.

## Web UI

A dependency-free browser control panel for your Remote Control sessions: list
them, watch the live event stream, send messages, answer permission prompts
(allow / always allow / deny with a reason), and steer (interrupt / set model /
set permission mode / archive).

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

# answer permission prompts (can_use_tool)
for req in rc.pending_permission_requests(sid):
    if req.is_question:  # AskUserQuestion rides in as a can_use_tool prompt
        rc.answer_question(sid, req.control_request_id,
                           {"Which db?": "postgres"}, req.tool_input,
                           tool_use_id=req.tool_use_id)
        continue
    print(req.tool_name, req.tool_input)
    rc.answer_permission(sid, req.control_request_id, allow=True,
                         updated_input=req.tool_input,
                         tool_use_id=req.tool_use_id)
    # deny instead: allow=False, message="use the sandbox for that"
    # "always allow": updated_permissions=req.permission_suggestions
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
  tui.py           # `claude-rc tui` — Textual terminal control panel
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
