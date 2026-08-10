"""``claude-rc tui`` — a Textual terminal UI for Remote Control sessions.

The terminal equivalent of the claude.ai/code web page: a session sidebar, a
live transcript, a composer, steering commands, and first-class handling of
permission prompts (``can_use_tool``) with an approval modal.

The transcript mirrors the Claude Code CLI's own look — user turns on a
full-width chevron bar, assistant turns as Markdown (syntax-highlighted code
fences, lists, inline code) beside a ``●`` bullet, and tool calls as
``● Name(arg)`` with their output hanging below on a ``└`` connector — rendered
in this project's colour scheme.

Run it with::

    fabio                        # pick a session from the sidebar
    fabio cse_abc123             # jump straight into one
    claude-rc tui [cse_id]       # the same thing under the main CLI

Keys: **ctrl+x** interrupt · **ctrl+g** review pending approvals ·
**ctrl+r** refresh sessions · **ctrl+b** hide/show the session list ·
**ctrl+q** quit.

Select text by dragging over the transcript, then **⌘C / Ctrl+C** to copy it to
the system clipboard (via ``pbcopy`` / ``wl-copy`` / ``xclip``, or OSC 52 when
running remotely).

Composer commands (anything else is sent to the session as a message —
including ``/...`` slash commands, which the worker runs locally):

    :model <model-id>        set the session model
    :perm <mode>             default | plan | acceptEdits | bypassPermissions
    :interrupt               interrupt the running turn
    :archive                 archive the session
    :q                       quit

Requires the ``tui`` extra (``pip install "claude-rc-api[tui]"``).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
from collections import deque
from typing import Optional

from rich.console import Group
from rich.markdown import Markdown
from rich.markup import escape
from rich.padding import Padding
from rich.table import Table
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    OptionList,
    RichLog,
    SelectionList,
    Static,
)
from textual.widgets.option_list import Option

from .client import APIError, RemoteControlClient
from .credentials import CredentialsError
from .events import Event, pending_permissions

_PERM_MODES = ("default", "plan", "acceptEdits", "bypassPermissions")

# Our palette (shared with the web UI's index.html), reused for the transcript
# so it reads like claude.ai/code but in our colours.
ACCENT = "#d97757"   # terracotta — assistant turn bullet
GREEN = "#4fd18b"    # tool-call bullet
BLUE = "#4f8cff"     # user chevron
MUTED = "#8b93a7"    # dividers, connectors
DANGER = "#e0555a"   # tool errors
USER_BG = "#24406b"  # the user-message bar (bright enough to stand out)


def _status_dot(session: dict) -> str:
    worker = (session.get("worker_status") or "").lower()
    status = (session.get("status") or "").lower()
    if status == "archived":
        return "[dim]●[/dim]"
    if worker == "requires_action":
        return "[red]●[/red]"
    if worker == "running":
        return "[yellow]●[/yellow]"
    return "[green]●[/green]"


# Reviewing a permission means seeing EVERYTHING the tool would do — silent
# truncation here is how you approve an `rm` you never saw. The full input is
# always rendered (scrollable); this cap only guards against pathological
# multi-megabyte inputs, and hitting it is announced, never silent.
_FULL_INPUT_CAP = 200_000


def format_full_input(value) -> str:
    """The complete tool input, pretty-printed, with an explicit truncation
    notice on the (rare) input that exceeds the safety cap."""
    text = value if isinstance(value, str) else json.dumps(value, indent=2, default=str)
    text = text or ""
    if len(text) > _FULL_INPUT_CAP:
        omitted = len(text) - _FULL_INPUT_CAP
        text = text[:_FULL_INPUT_CAP] + f"\n… ⚠ TRUNCATED: {omitted:,} more characters not shown"
    return text


def _arg_preview(value, limit: int = 120) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    else:
        for key in ("command", "file_path", "path", "pattern", "query", "url", "description"):
            if isinstance(value, dict) and value.get(key):
                text = str(value[key])
                break
        else:
            text = json.dumps(value)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "…"


# --- transcript renderables (claude.ai/code look, our palette) --------------
def user_bar(text: str, width: int | None = None) -> Group:
    """A user turn: the message on a full-width tinted bar with a ``›`` chevron
    and a blank line above and below, the way the Claude Code CLI stands out
    what you typed. ``Padding`` fills every line's background to the full width —
    including wrapped continuation lines and short messages — so the bar is
    always a solid block, never ragged. ``width`` is accepted for call-site
    compatibility but ignored (the bar expands to the log's width)."""
    body = Text()
    body.append("› ", style=f"bold {BLUE}")
    body.append(text, style="bold")
    bar = Padding(body, (0, 1), style=f"on {USER_BG}", expand=True)
    return Group(Text(""), bar, Text(""))


def assistant_body(text: str) -> Table:
    """An assistant turn: a ``●`` bullet in a gutter with the message rendered as
    Markdown beside it — code fences get syntax highlighting, and lists / bold /
    inline code render the way they do in the Claude Code CLI."""
    grid = Table.grid(expand=True)
    grid.add_column(width=2, no_wrap=True)
    grid.add_column(ratio=1)
    grid.add_row(Text("●", style=f"bold {ACCENT}"), Markdown(text))
    return grid


def tool_call_line(tool: dict) -> Text:
    """A tool call: ``● Name(arg)`` — green bullet, bold name, dim-parenthesised
    argument (the command for Bash, the path for file tools, …)."""
    name = tool.get("name") or "tool"
    arg = _arg_preview(tool.get("input"), limit=80)
    line = Text()
    line.append("● ", style=f"bold {GREEN}")
    line.append(name, style="bold")
    if arg:
        line.append("(", style=MUTED)
        line.append(arg, style=BLUE)
        line.append(")", style=MUTED)
    return line


def _result_text(block: dict) -> tuple[str, bool]:
    """Flatten a ``tool_result`` content block to (text, is_error)."""
    content = block.get("content")
    is_error = bool(block.get("is_error"))
    if isinstance(content, str):
        return content, is_error
    parts = []
    for b in content or []:
        if isinstance(b, dict) and b.get("type") == "text":
            parts.append(b.get("text", ""))
        elif isinstance(b, str):
            parts.append(b)
    return "".join(parts), is_error


def thinking_block(text: str) -> Table:
    """Extended thinking, recessed but rendered in full: a ``✻`` gutter with the
    text dim + italic. Never truncated — the reasoning is how you confirm you're
    on the same page — it just flows into the scrollback."""
    body = Text(text.strip(), style="italic dim")
    grid = Table.grid(expand=True)
    grid.add_column(width=2, no_wrap=True)
    grid.add_column(ratio=1)
    grid.add_row(Text("✻", style=MUTED), body)
    return grid


def todo_list(inp: dict) -> Text:
    """A TodoWrite call as a checklist — done (✔), in-progress (◐), pending (☐) —
    the way the Claude Code CLI surfaces its plan."""
    out = Text()
    out.append("● ", style=f"bold {GREEN}")
    out.append("Update todos", style="bold")
    for todo in inp.get("todos") or []:
        if not isinstance(todo, dict):
            continue
        status = (todo.get("status") or "").lower()
        content = todo.get("content") or todo.get("activeForm") or ""
        if status == "completed":
            mark, mstyle, cstyle = "✔", GREEN, "dim strike"
        elif status == "in_progress":
            mark, mstyle, cstyle = "◐", ACCENT, f"bold {ACCENT}"
        else:
            mark, mstyle, cstyle = "☐", MUTED, MUTED
        out.append(f"\n  {mark} ", style=mstyle)
        out.append(content, style=cstyle)
    return out


def plan_block(inp: dict) -> Table:
    """An ExitPlanMode call: the proposed plan rendered as Markdown under a
    ``●`` bullet, so it reads as the plan it is rather than tool arguments."""
    grid = Table.grid(expand=True)
    grid.add_column(width=2, no_wrap=True)
    grid.add_column(ratio=1)
    grid.add_row(Text("●", style=f"bold {ACCENT}"),
                 Markdown("**Plan**\n\n" + (inp.get("plan") or "")))
    return grid


def tool_render(tool: dict):
    """Dispatch a tool call to its renderer — checklist for TodoWrite, a plan
    for ExitPlanMode, otherwise the compact ``● Name(arg)`` line."""
    name = tool.get("name")
    inp = tool.get("input")
    if name == "TodoWrite" and isinstance(inp, dict) and inp.get("todos"):
        return todo_list(inp)
    if name == "ExitPlanMode" and isinstance(inp, dict) and inp.get("plan"):
        return plan_block(inp)
    return tool_call_line(tool)


def _fmt_n(n) -> str:
    if not n:
        return "0"
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def result_divider(ev: Event) -> Text:
    """The turn-complete divider, carrying the usage footer Claude prints —
    duration, cost, tokens — and turning red when the turn errored."""
    ok = ev.subtype in (None, "success")
    bits = ["turn complete" if ok else (ev.subtype or "error").replace("_", " ")]
    dur = ev.payload.get("duration_ms")
    if dur:
        bits.append(f"{dur / 1000:.1f}s")
    cost = ev.payload.get("total_cost_usd")
    if cost:
        bits.append(f"${cost:.3f}")
    usage = ev.payload.get("usage") or {}
    inp_t, out_t = usage.get("input_tokens"), usage.get("output_tokens")
    if inp_t or out_t:
        bits.append(f"{_fmt_n(inp_t)}↑ {_fmt_n(out_t)}↓")
    line = Text(f"── {' · '.join(bits)} ──", style="dim" if ok else f"bold {DANGER}")
    return line


def tool_result_block(block: dict, max_lines: int = 10) -> Text:
    """A tool result hanging under its call on a ``└`` connector, truncated to
    ``max_lines`` (errors in red)."""
    text, is_error = _result_text(block)
    color = DANGER if is_error else MUTED
    lines = text.rstrip().splitlines() or ["(no output)"]
    shown, extra = lines[:max_lines], len(lines) - max_lines
    out = Text()
    for i, line in enumerate(shown):
        out.append("  └ " if i == 0 else "    ", style=MUTED)
        out.append(line, style=color)
        out.append("\n")
    if extra > 0:
        out.append(f"    … +{extra} more line{'s' if extra != 1 else ''}\n", style=MUTED)
    return out


class ApprovalScreen(ModalScreen[tuple]):
    """Modal for one ``can_use_tool`` prompt.

    Dismisses with ``(behavior, always, message)`` — behavior ``allow`` /
    ``deny`` / ``later`` (keep pending, decide later).
    """

    BINDINGS = [
        Binding("a", "answer('allow')", "Allow"),
        Binding("y", "answer('always')", "Always allow"),
        Binding("d", "answer('deny')", "Deny"),
        Binding("escape", "answer('later')", "Later"),
    ]

    DEFAULT_CSS = """
    ApprovalScreen { align: center middle; }
    ApprovalScreen > Container {
        width: 80%; max-width: 100; max-height: 80%;
        border: round $warning; background: $surface; padding: 1 2;
    }
    ApprovalScreen .title { text-style: bold; color: $warning; }
    ApprovalScreen VerticalScroll { max-height: 14; margin: 1 0; }
    ApprovalScreen .input-json { color: $text-muted; }
    ApprovalScreen Input { margin: 1 0 0 0; }
    ApprovalScreen Horizontal { height: auto; align-horizontal: right; }
    ApprovalScreen Button { margin-left: 2; }
    """

    def __init__(self, event: Event, remaining: int = 0) -> None:
        super().__init__()
        self.event = event
        self.remaining = remaining

    def compose(self) -> ComposeResult:
        ev = self.event
        tail = f"  (+{self.remaining} more waiting)" if self.remaining else ""
        with Container():
            yield Label(f"🔐 Permission: {ev.tool_name or 'tool'}{tail}", classes="title")
            with VerticalScroll():
                yield Static(escape(format_full_input(ev.tool_input)), classes="input-json")
            yield Input(placeholder="deny reason, shown to Claude (optional)", id="deny-message")
            with Horizontal():
                yield Button("Allow (a)", variant="success", id="allow")
                if self.event.permission_suggestions:
                    yield Button("Always (y)", variant="primary", id="always")
                yield Button("Deny (d)", variant="error", id="deny")
                yield Button("Later (esc)", id="later")

    def on_button_pressed(self, message: Button.Pressed) -> None:
        self.action_answer(message.button.id or "later")

    def action_answer(self, behavior: str) -> None:
        message = self.query_one("#deny-message", Input).value.strip()
        if behavior == "always":
            self.dismiss(("allow", True, message))
        else:
            self.dismiss((behavior, False, message))


class QuestionScreen(ModalScreen[tuple]):
    """One AskUserQuestion question (they arrive as ``can_use_tool`` prompts).

    Dismisses with ``("picks", [labels])`` (empty list = skipped) or
    ``("later", None)`` to defer the whole prompt.
    """

    BINDINGS = [
        Binding("escape", "skip", "Skip"),
        Binding("l", "later", "Later"),
    ]

    DEFAULT_CSS = """
    QuestionScreen { align: center middle; }
    QuestionScreen > Container {
        width: 80%; max-width: 100; max-height: 80%;
        border: round $accent; background: $surface; padding: 1 2;
    }
    QuestionScreen .title { text-style: bold; color: $accent; }
    QuestionScreen .q-header { color: $text-muted; }
    QuestionScreen OptionList, QuestionScreen SelectionList { max-height: 14; margin: 1 0; }
    QuestionScreen Horizontal { height: auto; align-horizontal: right; }
    QuestionScreen Button { margin-left: 2; }
    """

    def __init__(self, question: dict, index: int = 0, total: int = 1) -> None:
        super().__init__()
        self.question = question
        self.index = index
        self.total = total
        self._labels: list[str] = []

    def compose(self) -> ComposeResult:
        q = self.question
        multi = bool(q.get("multiSelect") or q.get("multi_select"))
        counter = f"  ({self.index + 1}/{self.total})" if self.total > 1 else ""
        options = []
        for o in q.get("options") or []:
            label = o if isinstance(o, str) else (o.get("label") or "")
            desc = "" if isinstance(o, str) else (o.get("description") or "")
            if not label:
                continue
            self._labels.append(label)
            options.append((label, desc))
        with Container():
            if q.get("header"):
                yield Label(escape(str(q["header"])), classes="q-header")
            yield Label(f"❓ {escape(q.get('question') or '')}{counter}", classes="title")
            if multi:
                yield SelectionList[str](*[(escape(lbl), lbl) for lbl, _ in options])
                with Horizontal():
                    yield Button("Done", variant="primary", id="done")
                    yield Button("Skip (esc)", id="skip")
            else:
                yield OptionList(*[
                    Option(escape(lbl) + (f" [dim]— {escape(desc)}[/dim]" if desc else ""))
                    for lbl, desc in options
                ])

    def on_option_list_option_selected(self, message: OptionList.OptionSelected) -> None:
        self.dismiss(("picks", [self._labels[message.option_index]]))

    def on_button_pressed(self, message: Button.Pressed) -> None:
        if message.button.id == "done":
            picks = list(self.query_one(SelectionList).selected)
            self.dismiss(("picks", picks))
        else:
            self.action_skip()

    def action_skip(self) -> None:
        self.dismiss(("picks", []))

    def action_later(self) -> None:
        self.dismiss(("later", None))


class RemoteControlTUI(App):
    """Terminal control panel for Claude Code Remote Control sessions."""

    TITLE = "claude ● remote control"

    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit"),
        Binding("ctrl+x", "interrupt", "Interrupt"),
        Binding("ctrl+g", "show_approvals", "Approvals"),
        Binding("ctrl+r", "refresh_sessions", "Refresh"),
        Binding("ctrl+b", "toggle_sidebar", "Sidebar"),
    ]

    CSS = """
    /* Thin-line scrollbars everywhere (the 2-cell default reads heavy). */
    * { scrollbar-size-vertical: 1; scrollbar-size-horizontal: 1; }

    #sidebar { width: 38; border-right: solid $panel; }
    #sidebar ListView { background: transparent; }

    /* The sidebar needs two visible states: the cursor row (browsing) and the
       ATTACHED session (the one the transcript shows). Subtle backgrounds —
       enough to orient, not enough to shout. */
    #session-list > ListItem { padding: 0 1; }
    #session-list > ListItem.-highlight { background: $boost; }
    #session-list:focus > ListItem.-highlight { background: $accent 25%; }
    #session-list > ListItem.attached { background: $accent 15%; }
    #session-list > ListItem.attached.-highlight { background: $accent 30%; }

    #transcript { padding: 0 1; }
    #composer { dock: bottom; margin-top: 1; }
    """

    def __init__(self, client: Optional[RemoteControlClient] = None, session_id: Optional[str] = None):
        super().__init__()
        self._rc = client or RemoteControlClient()
        self._owns_rc = client is None
        self._initial_sid = session_id
        self._sid: Optional[str] = None
        self._stream_rc: Optional[RemoteControlClient] = None
        self._stream_thread: Optional[threading.Thread] = None
        self._sessions: list[dict] = []
        # Events rendered for the open session, kept so the transcript can be
        # re-rendered (e.g. re-wrapped on a sidebar toggle) without re-hitting
        # the API. Reset on every session switch.
        self._events: list[Event] = []
        self._approvals: deque[Event] = deque()
        self._answered: set[str] = set()
        self._modal_open = False

    # -- layout ------------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal():
            with Vertical(id="sidebar"):
                yield ListView(id="session-list")
            with Vertical():
                # min_width=20 (default is 78): without this, content refuses to
                # wrap below 78 columns and overflows horizontally on a narrow
                # terminal / phone instead of reflowing to the pane width.
                yield RichLog(
                    id="transcript", markup=True, wrap=True, auto_scroll=True, min_width=20
                )
                yield Input(
                    placeholder="Message the session…  (:model, :perm, :interrupt, :archive, :q)",
                    id="composer",
                )
        yield Footer()

    def on_mount(self) -> None:
        self.action_refresh_sessions()
        self.set_interval(8.0, self.action_refresh_sessions)

    def on_unmount(self) -> None:
        self._close_stream()
        if self._owns_rc:
            self._rc.close()

    # -- session list ------------------------------------------------------
    def action_refresh_sessions(self) -> None:
        self.run_worker(self._fetch_sessions, thread=True, exclusive=True, group="sessions")

    def _fetch_sessions(self) -> None:
        try:
            sessions = self._rc.sessions(limit=50)
        except (APIError, CredentialsError, OSError) as exc:
            self.call_from_thread(self.notify, f"session list failed: {exc}", severity="error")
            return
        self.call_from_thread(self._render_sessions, sessions)

    def _render_sessions(self, sessions: list[dict]) -> None:
        self._sessions = sessions
        lv = self.query_one("#session-list", ListView)
        selected = self._sid
        lv.clear()
        for s in sessions:
            model = (s.get("config") or {}).get("model") or ""
            label = Label(
                f"{_status_dot(s)} [b]{escape((s.get('title') or '(untitled)')[:30])}[/b]\n"
                f"  [dim]{escape(s.get('worker_status') or s.get('status') or '')}"
                f"{' · ' + escape(model) if model else ''}[/dim]"
            )
            item = ListItem(label)
            item.session_id = s.get("id")
            if s.get("id") == selected:
                item.add_class("attached")
            lv.append(item)
        if self._initial_sid:
            sid, self._initial_sid = self._initial_sid, None
            self._select_session(sid)
        elif selected:
            for i, s in enumerate(sessions):
                if s.get("id") == selected:
                    lv.index = i
                    break

    def on_list_view_selected(self, message: ListView.Selected) -> None:
        sid = getattr(message.item, "session_id", None)
        if sid and sid != self._sid:
            self._select_session(sid)

    # -- selecting + streaming a session -----------------------------------
    def _select_session(self, sid: str) -> None:
        self._close_stream()
        self._sid = sid
        self._events = []
        for item in self.query_one("#session-list", ListView).query(ListItem):
            item.set_class(getattr(item, "session_id", None) == sid, "attached")
        self._approvals.clear()
        self._answered.clear()
        log = self.query_one("#transcript", RichLog)
        log.clear()
        log.write(f"[dim]· connecting to {escape(sid)}…[/dim]")
        # Stream on our OWN daemon thread, not a Textual worker: the SSE read
        # blocks until the next heartbeat, and Textual's worker pool would wait
        # for it on quit — making exit hang for seconds. A daemon thread is
        # abandoned instantly at exit (terminal is already restored by then).
        self._stream_thread = threading.Thread(
            target=self._attach, args=(sid,), daemon=True, name="fabio-stream"
        )
        self._stream_thread.start()

    def _rerender(self) -> None:
        """Redraw the transcript from the in-memory event cache — no API call,
        no stream reconnect. Used when only the render width changed (e.g. a
        sidebar toggle); RichLog caches lines at their write-width, so replaying
        the events re-wraps them to the pane's current width."""
        log = self.query_one("#transcript", RichLog)
        log.clear()
        for ev in self._events:
            self._render_event(ev)

    def _close_stream(self) -> None:
        if self._stream_rc is not None:
            try:
                self._stream_rc.close()  # aborts the blocked SSE read
            except Exception:
                pass
            self._stream_rc = None

    def _attach(self, sid: str) -> None:
        """Worker: load header + history, then follow the live stream."""
        try:
            session = self._rc.get_session(sid)
            history = self._rc.list_events(sid, limit=200, sort_order="desc")
        except (APIError, CredentialsError, OSError) as exc:
            self.call_from_thread(self.notify, f"load failed: {exc}", severity="error")
            return
        history.reverse()
        last_seq = max((ev.sequence_num or 0 for ev in history), default=0)
        self.call_from_thread(self._render_attached, sid, session, history)

        # Stream through a dedicated client when we own credentials — closing
        # it is how _close_stream aborts the blocked SSE read on session
        # switch. An injected client (tests, embedding) streams directly.
        if self._owns_rc:
            try:
                stream_rc = RemoteControlClient()
            except CredentialsError as exc:
                self.call_from_thread(self.notify, f"stream failed: {exc}", severity="error")
                return
            self._stream_rc = stream_rc
        else:
            stream_rc = self._rc
        try:
            for ev in stream_rc.stream_events(sid, from_sequence_num=last_seq):
                if self._sid != sid:
                    return
                self.call_from_thread(self._on_event, sid, ev)
        except (APIError, CredentialsError, OSError, RuntimeError) as exc:
            if self._sid == sid:
                self.call_from_thread(self.notify, f"stream ended: {exc}", severity="warning")
        finally:
            if self._stream_rc is stream_rc:
                self._stream_rc = None

    def _render_attached(self, sid: str, session: dict, history: list[Event]) -> None:
        if self._sid != sid:
            return
        model = (session.get("config") or {}).get("model") or ""
        self.sub_title = " · ".join(
            x for x in (session.get("title"), model, session.get("worker_status")) if x
        )
        self._events = list(history)  # seed the cache with history
        for ev in history:
            self._render_event(ev)
        for pending in pending_permissions(history):
            self._enqueue_approval(pending)

    def _on_event(self, sid: str, ev: Event) -> None:
        if self._sid != sid:
            return
        self._events.append(ev)  # keep the cache current with the live stream
        self._render_event(ev)
        if ev.is_blocking_control and ev.control_subtype == "can_use_tool":
            self._enqueue_approval(ev)
        elif ev.type == "control_response" and ev.control_request_id:
            self._retire_approval(ev.control_request_id)
        elif ev.is_turn_end:
            self._approvals.clear()

    # -- transcript rendering ----------------------------------------------
    def _render_event(self, ev: Event) -> None:
        log = self.query_one("#transcript", RichLog)
        text = ev.text().strip()
        if ev.role == "user":
            # A user event carrying tool_result blocks is the worker echoing tool
            # output back — render it as results, not as something you typed.
            results = ev.tool_results()
            if results:
                for block in results:
                    log.write(tool_result_block(block))
            elif text:
                log.write(user_bar(text))
        elif ev.role == "assistant":
            think = ev.thinking().strip()
            if think:
                log.write(thinking_block(think))
            if text:
                log.write(assistant_body(text))
            for tool in ev.tool_uses():
                log.write(tool_render(tool))
        elif ev.type == "system" and ev.subtype == "init":
            model = ev.payload.get("model") or "?"
            log.write(f"[dim]── session started · {escape(str(model))} ──[/dim]")
        elif ev.type == "system" and ev.subtype == "compact_boundary":
            log.write("[dim]── context compacted ──[/dim]")
        elif ev.type == "result":
            log.write(result_divider(ev))
        elif ev.type == "rate_limit_event":
            rl = ev.rate_limit_info() or {}
            if rl.get("level") == "reached":
                reset = rl.get("resets_at")
                tail = f" · resets {reset}" if isinstance(reset, str) else ""
                log.write(f"[bold {DANGER}]⏳ usage limit reached{escape(tail)}[/]")
            elif rl.get("level") == "warning":
                log.write(f"[{MUTED}]⏳ approaching usage limit[/]")
            # a normal status pulse is telemetry — don't clutter the transcript
        elif ev.is_question:
            first = next(
                (q.get("question") for q in (ev.tool_input or {}).get("questions") or []
                 if isinstance(q, dict) and q.get("question")),
                "",
            )
            log.write(f"[bold magenta]❓ question[/bold magenta] {escape(first)}")
        elif ev.is_blocking_control and ev.control_subtype == "can_use_tool":
            preview = _arg_preview(ev.tool_input)
            log.write(
                f"[bold magenta]🔐 permission[/bold magenta] {escape(ev.tool_name or 'tool')}"
                f"[dim]{' ' + escape(preview) if preview else ''}[/dim]"
            )
        elif ev.is_blocking_control:
            log.write(f"[bold magenta]⚠ needs you: {escape(ev.control_subtype or 'input')}[/bold magenta]")

    # -- approvals ----------------------------------------------------------
    def _enqueue_approval(self, ev: Event) -> None:
        rid = ev.control_request_id
        if not rid or rid in self._answered or any(
            p.control_request_id == rid for p in self._approvals
        ):
            return
        self._approvals.append(ev)
        self._show_next_approval()

    def _retire_approval(self, request_id: str) -> None:
        """Someone (maybe another controller) answered — drop it from the queue."""
        self._answered.add(request_id)
        self._approvals = deque(
            p for p in self._approvals if p.control_request_id != request_id
        )

    def _show_next_approval(self) -> None:
        if self._modal_open or not self._approvals:
            return
        ev = self._approvals.popleft()
        rid = ev.control_request_id
        if not rid or rid in self._answered:
            return self._show_next_approval()
        if ev.is_question:
            return self._present_question(ev)
        self._modal_open = True

        def _done(result: Optional[tuple]) -> None:
            self._modal_open = False
            behavior, always, message = result or ("later", False, "")
            if behavior == "later":
                self._approvals.appendleft(ev)
            else:
                self._answered.add(rid)
                self._answer(ev, behavior == "allow", always, message)
                self._show_next_approval()

        self.push_screen(ApprovalScreen(ev, remaining=len(self._approvals)), _done)

    def _present_question(self, ev: Event) -> None:
        """Walk an AskUserQuestion's questions one modal at a time, then answer."""
        rid = ev.control_request_id
        questions = [
            q for q in (ev.tool_input or {}).get("questions") or []
            if isinstance(q, dict) and q.get("question")
        ]
        answers: dict[str, object] = {}
        self._modal_open = True

        def _step(i: int) -> None:
            if i >= len(questions):
                self._modal_open = False
                self._answered.add(rid)
                self._answer_question(ev, answers)
                return self._show_next_approval()

            def _done(result: Optional[tuple]) -> None:
                kind, picks = result or ("later", None)
                if kind == "later":
                    self._modal_open = False
                    self._approvals.appendleft(ev)
                    return
                if picks:
                    q = questions[i]
                    multi = bool(q.get("multiSelect") or q.get("multi_select"))
                    answers[q["question"]] = picks if multi else picks[0]
                _step(i + 1)

            self.push_screen(QuestionScreen(questions[i], index=i, total=len(questions)), _done)

        _step(0)

    def _answer_question(self, ev: Event, answers: dict) -> None:
        sid, rid = self._sid, ev.control_request_id
        log = self.query_one("#transcript", RichLog)
        summary = ", ".join(f"{v}" for v in answers.values()) if answers else "dismissed"
        log.write(f"[dim]  ↳ answered: {escape(summary)}[/dim]")

        def _send() -> None:
            try:
                self._rc.answer_question(
                    sid, rid, answers, ev.tool_input, tool_use_id=ev.tool_use_id
                )
            except (APIError, CredentialsError, OSError) as exc:
                self.call_from_thread(self.notify, f"answer failed: {exc}", severity="error")

        self.run_worker(_send, thread=True, group="control")

    def _answer(self, ev: Event, allow: bool, always: bool, message: str) -> None:
        sid, rid = self._sid, ev.control_request_id
        log = self.query_one("#transcript", RichLog)
        verdict = "allowed (always)" if allow and always else ("allowed" if allow else "denied")
        log.write(f"[dim]  ↳ {verdict}: {escape(ev.tool_name or 'tool')}[/dim]")

        def _send() -> None:
            try:
                self._rc.answer_permission(
                    sid,
                    rid,
                    allow,
                    updated_input=ev.tool_input if allow else None,
                    updated_permissions=ev.permission_suggestions if allow and always else None,
                    message=message,
                    tool_use_id=ev.tool_use_id,
                )
            except (APIError, CredentialsError, OSError) as exc:
                self.call_from_thread(self.notify, f"answer failed: {exc}", severity="error")

        self.run_worker(_send, thread=True, group="control")

    def copy_to_clipboard(self, text: str) -> None:
        """Copy selected text to the system clipboard.

        Overrides Textual's default, which uses OSC 52 — that does not reach the
        clipboard on macOS Terminal (and needs opt-in on some others), so a
        drag-select + ⌘C/Ctrl+C silently failed there. Prefer a local clipboard
        tool (``pbcopy`` / ``wl-copy`` / ``xclip`` / ``xsel``); fall back to
        OSC 52 only when none exists (e.g. a remote/SSH session)."""
        tools = {
            "pbcopy": [],
            "wl-copy": [],
            "xclip": ["-selection", "clipboard"],
            "xsel": ["--clipboard", "--input"],
        }
        for tool, args in tools.items():
            exe = shutil.which(tool)
            if not exe:
                continue
            try:
                subprocess.run([exe, *args], input=text, text=True, timeout=10)
                self.notify(f"copied {len(text)} chars to clipboard")
                return
            except (OSError, subprocess.SubprocessError):
                continue
        super().copy_to_clipboard(text)  # OSC 52 fallback (SSH / remote terminals)

    def action_toggle_sidebar(self) -> None:
        """Hide/show the session list so the transcript can take the full width
        (handy on a narrow terminal / phone). Focus moves to the composer when
        the list is hidden so keys still land somewhere sensible."""
        sidebar = self.query_one("#sidebar")
        sidebar.display = not sidebar.display
        if not sidebar.display:
            self.query_one("#composer", Input).focus()
        # The transcript caches lines at their write-width and doesn't reflow
        # when the pane resizes, so toggling the sidebar would leave the old
        # wrap. Re-render from the event cache (no API call) once the layout has
        # settled to the new width — call_after_refresh defers until then.
        if self._sid:
            self.call_after_refresh(self._rerender)

    def action_show_approvals(self) -> None:
        if not self._approvals and not self._modal_open:
            self.notify("no pending approvals")
        self._show_next_approval()

    # -- composer / steering ------------------------------------------------
    def on_input_submitted(self, message: Input.Submitted) -> None:
        if message.input.id != "composer":
            return
        text = message.value.strip()
        message.input.value = ""
        if not text:
            return
        # /exit and /quit close Fabio itself rather than being sent to the
        # session (where the worker would just run them as slash commands).
        if text.lower() in ("/exit", "/quit"):
            return self.exit()
        if text.startswith(":"):
            return self._command(text[1:].strip())
        if not self._sid:
            return self.notify("select a session first", severity="warning")
        self._steer(lambda: self._rc.send_message(self._sid, text), "send")

    def _command(self, cmd: str) -> None:
        name, _, arg = cmd.partition(" ")
        name, arg = name.lower(), arg.strip()
        if name in ("q", "quit"):
            return self.exit()
        if not self._sid:
            return self.notify("select a session first", severity="warning")
        sid = self._sid
        if name == "model" and arg:
            self._steer(lambda: self._rc.set_model(sid, arg), f"model → {arg}")
        elif name == "perm" and arg in _PERM_MODES:
            self._steer(lambda: self._rc.set_permission_mode(sid, arg), f"permission → {arg}")
        elif name == "interrupt":
            self.action_interrupt()
        elif name == "archive":
            self._steer(lambda: self._rc.archive_session(sid), "archived")
            self._close_stream()
            self._sid = None
            self.sub_title = ""
        else:
            self.notify(
                "commands — :model <id> · :perm default|plan|acceptEdits|bypassPermissions · "
                ":interrupt · :archive · :q",
                timeout=8,
            )

    def action_interrupt(self) -> None:
        if not self._sid:
            return self.notify("select a session first", severity="warning")
        self._steer(lambda: self._rc.interrupt(self._sid), "interrupt sent")

    def _steer(self, fn, label: str) -> None:
        def _run() -> None:
            try:
                fn()
                if label != "send":
                    self.call_from_thread(self.notify, label)
            except (APIError, CredentialsError, OSError) as exc:
                self.call_from_thread(self.notify, f"{label} failed: {exc}", severity="error")

        self.run_worker(_run, thread=True, group="control")


def run(session_id: Optional[str] = None) -> None:
    """Launch the TUI (entry point for ``claude-rc tui``)."""
    RemoteControlTUI(session_id=session_id).run()
