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


def _selected_text(app) -> str:
    """Select every transcript widget through Textual's real selection machinery
    and extract the text — the exact path a drag + ctrl+c copy takes. This is
    the regression guard for 'transcript text must be copy-pasteable'."""
    from textual.selection import SELECT_ALL

    app.screen.selections = {w: SELECT_ALL for w in app.query("#transcript *")}
    return app.screen.get_selected_text() or ""


def test_transcript_widgets_render_and_select():
    """Every transcript widget type renders AND its text survives selection
    extraction (the transcript is widgets, not a RichLog, precisely so that
    drag-select + copy works)."""
    from claude_rc.tui import assistant_body, tool_call_line, tool_result_block, user_bar

    async def scenario():
        app = RemoteControlTUI(client=FakeRC())
        async with app.run_test() as pilot:
            await pilot.pause(0.2)
            t = app.query_one("#transcript")
            await t.mount(
                user_bar("hi"),
                assistant_body("Here is code:\n\n```python\nprint('hi')\n```"),
                tool_call_line({"name": "Bash", "input": {"command": "date"}}),
                tool_result_block({"content": "Mon Aug 10", "is_error": False}),
                tool_result_block({"content": "boom", "is_error": True}),
                tool_result_block({"content": "\n".join(str(i) for i in range(40))}),
            )
            await pilot.pause(0.3)
            text = _selected_text(app)
            assert "› hi" in text                       # user bar
            assert "Here is code" in text               # assistant markdown prose
            assert "print('hi')" in text                # fenced code is selectable too
            assert "Bash" in text and "date" in text    # ● Name(arg)
            assert "└" in text and "Mon Aug 10" in text  # tool result + connector
            assert "boom" in text                        # error result
            assert "+30 more line" in text               # truncation notice

    asyncio.run(scenario())


def test_thinking_todos_and_usage_widgets():
    from claude_rc.tui import result_divider, thinking_block, tool_render
    from claude_rc.events import Event as _E

    ok = _E.from_wire({"payload": {"type": "result", "subtype": "success",
        "duration_ms": 4300, "total_cost_usd": 0.0123,
        "usage": {"input_tokens": 1200, "output_tokens": 340}}})
    err = _E.from_wire({"payload": {"type": "result", "subtype": "error_max_turns"}})
    todos = {"todos": [
        {"content": "done thing", "status": "completed"},
        {"content": "doing thing", "status": "in_progress"},
        {"content": "later thing", "status": "pending"},
    ]}

    async def scenario():
        app = RemoteControlTUI(client=FakeRC())
        async with app.run_test() as pilot:
            await pilot.pause(0.2)
            t = app.query_one("#transcript")
            await t.mount(
                thinking_block("some reasoning here"),
                thinking_block("\n".join(f"line{i}" for i in range(50))),
                tool_render({"name": "TodoWrite", "input": todos}),
                tool_render({"name": "ExitPlanMode", "input": {"plan": "# Step one"}}),
                result_divider(ok),
                result_divider(err),
            )
            await pilot.pause(0.3)
            text = _selected_text(app)
            # thinking: recessed but rendered in full (never truncated)
            assert "some reasoning here" in text
            assert "line0" in text and "line49" in text and "more line" not in text
            # TodoWrite dispatches to a checklist with status marks
            assert "✔" in text and "◐" in text and "☐" in text and "doing thing" in text
            # ExitPlanMode renders the plan
            assert "Plan" in text and "Step one" in text
            # result divider carries the usage footer, on error too
            assert "turn complete" in text and "4.3s" in text
            assert "$0.012" in text and "1.2k" in text
            assert "error max turns" in text

    asyncio.run(scenario())


def test_render_event_handles_tool_result():
    """A user event carrying tool_result blocks renders as output, not a prompt."""
    async def scenario():
        app = RemoteControlTUI(client=FakeRC())
        async with app.run_test() as pilot:
            await pilot.pause(0.2)
            app._sid = "cse_1"
            t = app.query_one("#transcript")
            before = len(t.children)
            # assistant tool_use, then the user event that echoes its result
            app._render_event(Event.from_wire({"payload": {"type": "assistant", "message": {
                "role": "assistant", "content": [
                    {"type": "tool_use", "name": "Bash", "input": {"command": "date"}, "id": "t1"}]}}}))
            app._render_event(Event.from_wire({"payload": {"type": "user", "message": {
                "role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "t1", "content": "Mon Aug 10"}]}}}))
            await pilot.pause(0.2)
            assert len(t.children) == before + 2  # ● Bash(date), └ Mon Aug 10
            assert "Mon Aug 10" in _selected_text(app)

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
            await pilot.press("ctrl+o")
            await pilot.pause(0.1)
            assert sidebar.display is False   # hidden → transcript takes the width
            await pilot.press("ctrl+o")
            await pilot.pause(0.1)
            assert sidebar.display is True

    asyncio.run(scenario())


def test_composer_grows_and_submits_multiline():
    """The composer wraps long input and grows (Input scrolled horizontally and
    rendered black-on-black past the width); ctrl+j inserts a newline; Enter
    submits the whole thing."""
    async def scenario():
        fake = FakeRC()
        app = RemoteControlTUI(client=fake)
        async with app.run_test(size=(60, 24)) as pilot:
            await pilot.pause(0.2)
            app._sid = "cse_1"
            composer = app.query_one("#composer")
            composer.focus()
            await pilot.pause(0.1)
            h1 = composer.styles.height.value
            composer.value = "x" * 200  # far wider than the 60-col screen
            await pilot.pause(0.2)
            assert composer.styles.height.value > h1, "composer must grow when text wraps"
            # capped, not unbounded
            composer.value = "y" * 5000
            await pilot.pause(0.2)
            assert composer.styles.height.value <= composer.MAX_LINES + 2

            # ctrl+j = newline, enter = submit the multi-line message intact
            composer.value = "line one"
            await pilot.press("end", "ctrl+j")
            await pilot.pause(0.1)
            for ch in "two":
                await pilot.press(ch)
            await pilot.press("enter")
            await pilot.pause(0.2)
            assert fake.calls[-1] == ("send", "cse_1", "line one\ntwo")
            assert composer.value == ""  # cleared after send

    asyncio.run(scenario())


def test_ctrl_c_copies_transcript_selection_while_composer_focused(monkeypatch):
    """The composer (TextArea) binds ctrl+c to copy ITS OWN selection, and it
    holds focus nearly all the time — so a transcript drag-select + ctrl+c
    silently copied nothing. The composer must hand ctrl+c to the screen's
    copy action whenever the transcript owns the live selection."""
    from claude_rc.tui import user_bar

    async def scenario():
        app = RemoteControlTUI(client=FakeRC())
        async with app.run_test() as pilot:
            await pilot.pause(0.2)
            t = app.query_one("#transcript")
            await t.mount(user_bar("copy me please"))
            await pilot.pause(0.2)
            copied = []
            monkeypatch.setattr(app, "copy_to_clipboard", lambda text: copied.append(text))
            app.query_one("#composer").focus()
            await pilot.pause(0.1)
            assert _selected_text(app)  # transcript selection is live
            await pilot.press("ctrl+c")
            await pilot.pause(0.1)
            assert copied and "copy me please" in copied[0]

            # but text selected INSIDE the composer still copies composer text
            copied.clear()
            composer = app.query_one("#composer")
            composer.value = "mine"
            await pilot.pause(0.1)
            composer.select_all()
            await pilot.press("ctrl+c")
            await pilot.pause(0.1)
            assert copied and copied[0] == "mine"

    asyncio.run(scenario())


def test_copy_on_select(monkeypatch):
    """Finishing a selection drag copies it immediately (the Mac path: ⌘C
    never reaches a TUI, and iTerm's own default is copy-on-selection — so
    drag must equal copy). A click that clears the selection must NOT clobber
    the clipboard."""
    from textual import events as tevents
    from claude_rc.tui import user_bar

    async def scenario():
        app = RemoteControlTUI(client=FakeRC())
        async with app.run_test() as pilot:
            await pilot.pause(0.2)
            await app.query_one("#transcript").mount(user_bar("drag equals copy"))
            await pilot.pause(0.2)
            copied = []
            monkeypatch.setattr(app, "copy_to_clipboard", lambda text: copied.append(text))

            _selected_text(app)  # a live selection, as after a drag
            app.screen.post_message(tevents.TextSelected())
            await pilot.pause(0.1)
            assert copied and "drag equals copy" in copied[-1]

            # a plain click clears the selection, then fires TextSelected —
            # nothing must be copied over the clipboard
            copied.clear()
            app.screen.clear_selection()
            app.screen.post_message(tevents.TextSelected())
            await pilot.pause(0.1)
            assert copied == []

    asyncio.run(scenario())


def test_transcript_survives_session_switch():
    """Selecting a session clears the transcript and loads its history as
    selectable widgets; switching again resets cleanly."""
    async def scenario():
        app = RemoteControlTUI(client=FakeRC())
        async with app.run_test() as pilot:
            await pilot.pause(0.2)
            app._select_session("cse_1")
            for _ in range(20):
                await pilot.pause(0.1)
                if len(app.query_one("#transcript").children) > 1:
                    break
            # FakeRC history: init divider, the user's "run ls", a permission line
            text = _selected_text(app)
            assert "session started" in text
            assert "run ls" in text

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
