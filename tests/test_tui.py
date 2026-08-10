"""Offline TUI tests (skipped unless the `tui` extra is installed)."""
import asyncio
import json

import pytest

textual = pytest.importorskip("textual")

from claude_rc.events import Event  # noqa: E402
from claude_rc.tui import ApprovalScreen, RemoteControlTUI, _arg_preview, _status_dot  # noqa: E402


class FakeRC:
    """Stands in for RemoteControlClient — records calls, serves canned data."""

    def __init__(self):
        self.calls = []
        self.session = {
            "id": "cse_1", "title": "demo", "status": "active",
            "worker_status": "requires_action",
            "config": {"model": "claude-opus-4-8"},
        }
        self.history = [
            {"sequence_num": 1, "payload": {"type": "system", "subtype": "init",
                                            "model": "claude-opus-4-8"}},
            {"sequence_num": 2, "payload": {"type": "user", "message": {
                "role": "user", "content": [{"type": "text", "text": "run ls"}]}}},
            {"sequence_num": 3, "payload": {
                "type": "control_request", "request_id": "req-1",
                "request": {"subtype": "can_use_tool", "tool_name": "Bash",
                            "input": {"command": "ls"}}}},
        ]

    # -- client surface used by the TUI -----------------------------------
    def sessions(self, **kw):
        return [self.session]

    def get_session(self, sid):
        return self.session

    def list_events(self, sid, **kw):
        evs = [Event.from_wire(e) for e in self.history]
        if kw.get("sort_order") == "desc":
            evs.reverse()
        return evs

    def stream_events(self, sid, **kw):
        return iter(())  # empty live stream

    def answer_permission(self, sid, rid, allow, **kw):
        self.calls.append(("answer", sid, rid, allow, kw))
        return {"ok": True}

    def answer_question(self, sid, rid, answers, original_input, **kw):
        self.calls.append(("answer_question", sid, rid, answers, original_input, kw))
        return {"ok": True}

    def send_message(self, sid, text):
        self.calls.append(("send", sid, text))
        return {"ok": True}

    def interrupt(self, sid):
        self.calls.append(("interrupt", sid))
        return {"ok": True}

    def set_model(self, sid, model):
        self.calls.append(("set_model", sid, model))
        return {"ok": True}

    def set_permission_mode(self, sid, mode):
        self.calls.append(("set_permission_mode", sid, mode))
        return {"ok": True}

    def archive_session(self, sid):
        self.calls.append(("archive", sid))
        return True

    def close(self):
        pass


def test_transcript_renderables():
    """The claude.ai/code-style renderables produce sane output and never throw."""
    from rich.console import Console
    from claude_rc.tui import assistant_body, tool_call_line, tool_result_block, user_bar

    console = Console(width=60, no_color=False)

    def render(obj):
        with console.capture() as cap:
            console.print(obj)
        return cap.get()

    # user bar: chevron + text, on a background that fills the full width even
    # for a short message, with a blank line above and below
    narrow = Console(width=40)
    with narrow.capture() as cap:
        narrow.print(user_bar("hi"))
    bar_out = cap.get()
    assert "› hi" in bar_out
    body_lines = [ln for ln in bar_out.split("\n") if "hi" in ln]
    assert body_lines and len(body_lines[0]) == 40, "bar background must fill the full width"
    # blank line above and below (Group wraps the bar in empty lines)
    assert bar_out.startswith("\n") or bar_out.split("\n")[0].strip() == ""

    # assistant body renders markdown — a fenced code block keeps its content
    md = render(assistant_body("Here is code:\n\n```python\nprint('hi')\n```"))
    assert "print" in md and "●" in md

    # tool call: ● Name(arg)
    call = render(tool_call_line({"name": "Bash", "input": {"command": "date"}}))
    assert "Bash" in call and "date" in call

    # tool result: connector + output; errors still render
    ok = render(tool_result_block({"content": "Mon Aug 10", "is_error": False}))
    assert "└" in ok and "Mon Aug 10" in ok
    err = render(tool_result_block({"content": "boom", "is_error": True}))
    assert "boom" in err
    # long output is truncated with a notice
    many = render(tool_result_block({"content": "\n".join(str(i) for i in range(40))}))
    assert "more line" in many


def test_thinking_todos_and_usage_renderables():
    from rich.console import Console
    from claude_rc.tui import result_divider, thinking_block, todo_list, tool_render
    from claude_rc.events import Event as _E

    console = Console(width=70)

    def render(obj):
        with console.capture() as cap:
            console.print(obj)
        return cap.get()

    # thinking: recessed but rendered in full (never truncated)
    assert "reasoning" in render(thinking_block("some reasoning here"))
    full = render(thinking_block("\n".join(f"line{i}" for i in range(50))))
    assert "line0" in full and "line49" in full and "more line" not in full

    # TodoWrite dispatches to a checklist with status marks
    todos = {"todos": [
        {"content": "done thing", "status": "completed"},
        {"content": "doing thing", "status": "in_progress"},
        {"content": "later thing", "status": "pending"},
    ]}
    out = render(tool_render({"name": "TodoWrite", "input": todos}))
    assert "✔" in out and "◐" in out and "☐" in out and "doing thing" in out

    # ExitPlanMode renders the plan
    plan = render(tool_render({"name": "ExitPlanMode", "input": {"plan": "# Step one"}}))
    assert "Plan" in plan and "Step one" in plan

    # result divider carries the usage footer, red-flagged on error
    ok = _E.from_wire({"payload": {"type": "result", "subtype": "success",
        "duration_ms": 4300, "total_cost_usd": 0.0123,
        "usage": {"input_tokens": 1200, "output_tokens": 340}}})
    line = render(result_divider(ok))
    assert "turn complete" in line and "4.3s" in line and "$0.012" in line and "1.2k" in line
    err = _E.from_wire({"payload": {"type": "result", "subtype": "error_max_turns"}})
    assert "error max turns" in render(result_divider(err))


def test_render_event_handles_tool_result(monkeypatch):
    """A user event carrying tool_result blocks renders as output, not a prompt."""
    async def scenario():
        app = RemoteControlTUI(client=FakeRC())
        async with app.run_test() as pilot:
            await pilot.pause(0.2)
            app._sid = "cse_1"
            log = app.query_one("#transcript")
            before = len(log.lines)
            # assistant tool_use, then the user event that echoes its result
            app._render_event(Event.from_wire({"payload": {"type": "assistant", "message": {
                "role": "assistant", "content": [
                    {"type": "tool_use", "name": "Bash", "input": {"command": "date"}, "id": "t1"}]}}}))
            app._render_event(Event.from_wire({"payload": {"type": "user", "message": {
                "role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "t1", "content": "Mon Aug 10"}]}}}))
            await pilot.pause(0.1)
            assert len(log.lines) > before

    asyncio.run(scenario())


def test_status_dot_and_arg_preview():
    assert "red" in _status_dot({"worker_status": "requires_action"})
    assert "yellow" in _status_dot({"worker_status": "running"})
    assert "green" in _status_dot({"worker_status": "idle", "status": "active"})
    assert "dim" in _status_dot({"status": "archived"})
    assert _arg_preview({"command": "ls -la"}) == "ls -la"
    assert _arg_preview("x" * 200).endswith("…")
    assert _arg_preview(None) == ""


def _pending_event():
    return Event.from_wire({"sequence_num": 3, "payload": {
        "type": "control_request", "request_id": "req-1",
        "request": {"subtype": "can_use_tool", "tool_name": "Bash",
                    "input": {"command": "ls"},
                    "permission_suggestions": [{"type": "addRules"}]}}})


def test_tui_selects_session_and_surfaces_pending_approval():
    async def scenario():
        fake = FakeRC()
        app = RemoteControlTUI(client=fake)
        async with app.run_test() as pilot:
            await pilot.pause(0.3)  # sessions worker + auto attach on select
            app._select_session("cse_1")
            for _ in range(20):  # wait for the attach worker to finish
                await pilot.pause(0.1)
                if app._modal_open:
                    break
            # history contained an unanswered can_use_tool → modal is up
            assert isinstance(app.screen, ApprovalScreen)
            assert app.screen.event.tool_name == "Bash"
            await pilot.press("a")  # allow
            await pilot.pause(0.3)
            answers = [c for c in fake.calls if c[0] == "answer"]
            assert answers and answers[0][2] == "req-1" and answers[0][3] is True
            assert answers[0][4]["updated_input"] == {"command": "ls"}

    asyncio.run(scenario())


def test_tui_deny_with_message_and_always_allow():
    async def scenario():
        fake = FakeRC()
        app = RemoteControlTUI(client=fake)
        async with app.run_test() as pilot:
            await pilot.pause(0.2)
            app._sid = "cse_1"
            app._enqueue_approval(_pending_event())
            await pilot.pause(0.2)
            assert isinstance(app.screen, ApprovalScreen)
            await pilot.press("y")  # always allow → carries the suggestions
            await pilot.pause(0.3)
            kind, sid, rid, allow, kw = fake.calls[-1]
            assert (kind, allow) == ("answer", True)
            assert kw["updated_permissions"] == [{"type": "addRules"}]

            # a second prompt, denied
            ev2 = Event.from_wire({"sequence_num": 4, "payload": {
                "type": "control_request", "request_id": "req-2",
                "request": {"subtype": "can_use_tool", "tool_name": "Write",
                            "input": {"file_path": "x"}}}})
            app._enqueue_approval(ev2)
            await pilot.pause(0.2)
            assert isinstance(app.screen, ApprovalScreen)
            await pilot.press("d")
            await pilot.pause(0.3)
            kind, sid, rid, allow, kw = fake.calls[-1]
            assert (kind, rid, allow) == ("answer", "req-2", False)

    asyncio.run(scenario())


def test_tui_composer_commands():
    async def scenario():
        fake = FakeRC()
        app = RemoteControlTUI(client=fake)
        async with app.run_test() as pilot:
            await pilot.pause(0.2)
            app._sid = "cse_1"
            composer = app.query_one("#composer")
            composer.focus()
            await pilot.pause(0.1)
            for text, expected in [
                (":model claude-sonnet-5", ("set_model", "cse_1", "claude-sonnet-5")),
                (":perm acceptEdits", ("set_permission_mode", "cse_1", "acceptEdits")),
                ("hello there", ("send", "cse_1", "hello there")),
            ]:
                composer.value = text
                await pilot.press("enter")
                await pilot.pause(0.2)
                assert fake.calls[-1] == expected

    asyncio.run(scenario())


def test_tui_question_flow():
    from claude_rc.tui import QuestionScreen

    q_input = {"questions": [{"question": "Which db?", "header": "DB",
                              "options": [{"label": "postgres", "description": "the big one"},
                                          {"label": "sqlite"}],
                              "multiSelect": False}]}
    ev = Event.from_wire({"sequence_num": 9, "payload": {
        "type": "control_request", "request_id": "req-q",
        "request": {"subtype": "can_use_tool", "tool_name": "AskUserQuestion",
                    "tool_use_id": "toolu_q", "input": q_input}}})

    async def scenario():
        fake = FakeRC()
        app = RemoteControlTUI(client=fake)
        async with app.run_test() as pilot:
            await pilot.pause(0.2)
            app._sid = "cse_1"
            app._enqueue_approval(ev)
            await pilot.pause(0.2)
            assert isinstance(app.screen, QuestionScreen)
            await pilot.press("enter")  # pick the highlighted (first) option
            await pilot.pause(0.3)
            kind, sid, rid, answers, original, kw = fake.calls[-1]
            assert kind == "answer_question" and rid == "req-q"
            assert answers == {"Which db?": "postgres"}
            assert original == q_input
            assert kw["tool_use_id"] == "toolu_q"

    asyncio.run(scenario())


def test_approval_modal_shows_full_command():
    """The whole point of the prompt: no silent truncation of the tool input.

    (The mobile app clips the command; the modal here must show all of it.)
    """
    from textual.widgets import Static
    from claude_rc.tui import format_full_input

    # a long-but-realistic command: far over the old 4000-char clip
    command = "git commit -m " + "x" * 8000 + " && echo SENTINEL_END"
    ev = Event.from_wire({"sequence_num": 7, "payload": {
        "type": "control_request", "request_id": "req-long",
        "request": {"subtype": "can_use_tool", "tool_name": "Bash",
                    "input": {"command": command}}}})

    async def scenario():
        app = RemoteControlTUI(client=FakeRC())
        async with app.run_test() as pilot:
            await pilot.pause(0.2)
            app._sid = "cse_1"
            app._enqueue_approval(ev)
            await pilot.pause(0.2)
            assert isinstance(app.screen, ApprovalScreen)
            body = str(app.screen.query_one(".input-json", Static).content)
            assert "SENTINEL_END" in body            # the tail survived
            assert "TRUNCATED" not in body           # nothing was clipped

    asyncio.run(scenario())

    # the safety cap announces itself instead of clipping silently
    huge = format_full_input({"command": "y" * 300_000})
    assert "⚠ TRUNCATED" in huge and "more characters not shown" in huge


def test_sidebar_marks_attached_session():
    from textual.widgets import ListItem

    async def scenario():
        fake = FakeRC()
        app = RemoteControlTUI(client=fake)
        async with app.run_test() as pilot:
            await pilot.pause(0.3)  # session list loads
            app._select_session("cse_1")
            await pilot.pause(0.2)
            marked = [i for i in app.query(ListItem) if i.has_class("attached")]
            assert len(marked) == 1
            assert marked[0].session_id == "cse_1"
            # a later list refresh must keep the mark
            app._render_sessions(fake.sessions())
            await pilot.pause(0.1)
            marked = [i for i in app.query(ListItem) if i.has_class("attached")]
            assert len(marked) == 1 and marked[0].session_id == "cse_1"

    asyncio.run(scenario())


def test_fabio_entry_point(monkeypatch):
    """The `fabio` command forwards to the TUI, with an optional session id."""
    import pytest as _pytest
    import claude_rc.tui as tui_mod
    from claude_rc.cli import fabio_main

    seen = {}
    monkeypatch.setattr(tui_mod, "run", lambda session_id=None: seen.__setitem__("sid", session_id))

    assert fabio_main([]) == 0
    assert seen["sid"] is None
    assert fabio_main(["cse_abc"]) == 0
    assert seen["sid"] == "cse_abc"
    with _pytest.raises(SystemExit):        # --version exits via argparse
        fabio_main(["--version"])


def test_copy_to_clipboard_prefers_local_tool(monkeypatch):
    """Selection-copy uses a local clipboard tool (reliable on macOS), not the
    OSC 52 path that fails on macOS Terminal."""
    import claude_rc.tui as tui_mod

    calls = []
    monkeypatch.setattr(tui_mod.shutil, "which",
                        lambda t: f"/usr/bin/{t}" if t == "pbcopy" else None)
    monkeypatch.setattr(tui_mod.subprocess, "run",
                        lambda args, **k: calls.append(args) or type("P", (), {"returncode": 0})())
    app = RemoteControlTUI(client=FakeRC())
    monkeypatch.setattr(app, "notify", lambda *a, **k: None)
    # OSC 52 fallback must NOT run when a local tool exists
    monkeypatch.setattr(type(app).__mro__[1], "copy_to_clipboard",
                        lambda self, text: calls.append(["OSC52"]))
    app.copy_to_clipboard("hello there")
    assert calls and calls[0][0].endswith("pbcopy")
    assert ["OSC52"] not in calls
    # input piped to the tool
    assert calls[0] == ["/usr/bin/pbcopy"]


def test_exit_quit_commands_close_the_app(monkeypatch):
    """/exit and /quit close Fabio instead of being sent to the session."""
    async def scenario():
        fake = FakeRC()
        app = RemoteControlTUI(client=fake)
        async with app.run_test() as pilot:
            await pilot.pause(0.2)
            app._sid = "cse_1"
            exited = []
            monkeypatch.setattr(app, "exit", lambda *a, **k: exited.append(True))
            composer = app.query_one("#composer")
            composer.focus()
            for cmd in ("/exit", "/QUIT"):
                exited.clear()
                composer.value = cmd
                await pilot.press("enter")
                await pilot.pause(0.05)
                assert exited, f"{cmd} should quit"
            assert not any(c[0] == "send" for c in fake.calls)  # never sent onward

    asyncio.run(scenario())


def test_toggle_sidebar():
    async def scenario():
        app = RemoteControlTUI(client=FakeRC())
        async with app.run_test() as pilot:
            await pilot.pause(0.2)
            sidebar = app.query_one("#sidebar")
            assert sidebar.display is True
            await pilot.press("ctrl+b")
            await pilot.pause(0.1)
            assert sidebar.display is False   # hidden → transcript takes the width
            await pilot.press("ctrl+b")
            await pilot.pause(0.1)
            assert sidebar.display is True

    asyncio.run(scenario())


def test_toggle_sidebar_rerenders_from_cache(monkeypatch):
    """With a session open, toggling the sidebar re-renders from the in-memory
    cache (no API refetch) so the transcript re-wraps to the new pane width."""
    async def scenario():
        app = RemoteControlTUI(client=FakeRC())
        async with app.run_test() as pilot:
            await pilot.pause(0.2)
            app._sid = "cse_1"
            reloaded, rerendered = [], []
            monkeypatch.setattr(app, "_select_session", lambda sid: reloaded.append(sid))
            monkeypatch.setattr(app, "_rerender", lambda: rerendered.append(True))
            await pilot.press("ctrl+b")
            await pilot.pause(0.2)  # let call_after_refresh fire
            assert rerendered and not reloaded  # cache, not API

    asyncio.run(scenario())


def test_event_cache_populates_on_load():
    """History (and live events) are cached so the transcript can be redrawn
    without re-hitting the API."""
    async def scenario():
        app = RemoteControlTUI(client=FakeRC())
        async with app.run_test() as pilot:
            await pilot.pause(0.2)
            app._select_session("cse_1")
            for _ in range(20):
                await pilot.pause(0.1)
                if app._events:
                    break
            # FakeRC history has 3 events (init, user, control_request)
            assert len(app._events) == 3
            # a fresh switch resets the cache
            app._select_session("cse_1")
            assert app._events == []

    asyncio.run(scenario())


def test_retire_approval_drops_queued_prompt():
    fake = FakeRC()
    app = RemoteControlTUI(client=fake)
    app._modal_open = True  # block auto-show so the queue is inspectable
    app._enqueue_approval(_pending_event())
    assert len(app._approvals) == 1
    app._retire_approval("req-1")
    assert len(app._approvals) == 0
    # once answered elsewhere, the same request can't be re-queued
    app._enqueue_approval(_pending_event())
    assert len(app._approvals) == 0
