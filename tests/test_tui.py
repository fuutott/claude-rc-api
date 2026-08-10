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
