"""``claude-rc tui`` — a Textual terminal UI for Remote Control sessions.

The terminal equivalent of the claude.ai/code web page: a session sidebar, a
live transcript, a composer, steering commands, and first-class handling of
permission prompts (``can_use_tool``) with an approval modal.

Run it with::

    claude-rc tui                # pick a session from the sidebar
    claude-rc tui cse_abc123     # jump straight into one

Keys: **ctrl+x** interrupt · **ctrl+g** review pending approvals ·
**ctrl+r** refresh sessions · **ctrl+q** quit.

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
from collections import deque
from typing import Optional

from rich.markup import escape
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
    ]

    CSS = """
    #sidebar { width: 38; border-right: solid $panel; }
    #sidebar ListView { background: transparent; }
    #transcript { padding: 0 1; }
    #composer { dock: bottom; }
    """

    def __init__(self, client: Optional[RemoteControlClient] = None, session_id: Optional[str] = None):
        super().__init__()
        self._rc = client or RemoteControlClient()
        self._owns_rc = client is None
        self._initial_sid = session_id
        self._sid: Optional[str] = None
        self._stream_rc: Optional[RemoteControlClient] = None
        self._sessions: list[dict] = []
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
                yield RichLog(id="transcript", markup=True, wrap=True, auto_scroll=True)
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
        self._approvals.clear()
        self._answered.clear()
        log = self.query_one("#transcript", RichLog)
        log.clear()
        log.write(f"[dim]· connecting to {escape(sid)}…[/dim]")
        self.run_worker(lambda: self._attach(sid), thread=True, exclusive=True, group="stream")

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
        for ev in history:
            self._render_event(ev)
        for pending in pending_permissions(history):
            self._enqueue_approval(pending)

    def _on_event(self, sid: str, ev: Event) -> None:
        if self._sid != sid:
            return
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
        if ev.role == "user" and text:
            log.write(f"[bold cyan]you[/bold cyan] › {escape(text)}")
        elif ev.role == "assistant":
            if text:
                log.write(f"[bold green]claude[/bold green] › {escape(text)}")
            for tool in ev.tool_uses():
                preview = _arg_preview(tool.get("input"))
                log.write(
                    f"  [yellow]⚙ {escape(tool.get('name') or 'tool')}[/yellow]"
                    f"[dim]{' ' + escape(preview) if preview else ''}[/dim]"
                )
        elif ev.type == "system" and ev.subtype == "init":
            model = ev.payload.get("model") or "?"
            log.write(f"[dim]── session started · {escape(str(model))} ──[/dim]")
        elif ev.type == "result":
            tail = f" · {ev.subtype}" if ev.subtype and ev.subtype != "success" else ""
            log.write(f"[dim]── turn complete{escape(tail)} ──[/dim]")
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
