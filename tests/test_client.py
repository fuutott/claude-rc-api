"""Offline tests for the reverse-engineered client (no network)."""
import json
from pathlib import Path

import pytest

from claude_rc.sse import parse_sse, SSEFrame
from claude_rc.events import (
    Event,
    cli_user_message,
    cli_control_request,
    user_message,
)
from claude_rc.credentials import OAuthCredentials, load_credentials, CredentialsError


# --- SSE parser ------------------------------------------------------------
def test_sse_basic_frame():
    lines = ["event: client_event", "id: 42", 'data: {"a":1}', ""]
    frames = list(parse_sse(lines))
    assert len(frames) == 1
    assert frames[0].event == "client_event"
    assert frames[0].id == "42"
    assert json.loads(frames[0].data) == {"a": 1}


def test_sse_multiline_data_and_heartbeat():
    lines = ["data: line1", "data: line2", "", ": keep-alive", ""]
    frames = list(parse_sse(lines))
    # comment-only block yields no frame; the data block joins with newline
    assert len(frames) == 1
    assert frames[0].data == "line1\nline2"
    assert frames[0].event is None


def test_sse_strips_single_leading_space():
    frames = list(parse_sse(["data:  two-spaces", ""]))
    assert frames[0].data == " two-spaces"  # only the first space is stripped


# --- Event model -----------------------------------------------------------
def test_event_rc_wrapped_unwraps_payload_and_int_seq():
    wire = {"sequence_num": "362", "payload": {"type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "hi"}]}}}
    e = Event.from_wire(wire)
    assert e.type == "assistant"
    assert e.role == "assistant"
    assert e.sequence_num == 362 and isinstance(e.sequence_num, int)
    assert e.text() == "hi"


def test_event_rc_string_content():
    wire = {"sequence_num": 1, "payload": {"type": "user",
            "message": {"role": "user", "content": "just a string"}}}
    e = Event.from_wire(wire)
    assert e.text() == "just a string"


def test_event_managed_agents_shape():
    wire = {"type": "agent.message", "id": "sevt_1",
            "content": [{"type": "text", "text": "cloud"}], "processed_at": "2026-01-01T00:00:00Z"}
    e = Event.from_wire(wire)
    assert e.type == "agent.message"
    assert e.text() == "cloud"
    assert e.sequence_num is None


def test_event_turn_end_and_terminal():
    assert Event.from_wire({"payload": {"type": "result", "subtype": "success"}}).is_turn_end
    assert Event.from_wire({"payload": {"type": "result", "subtype": "error_max_turns"}}).is_terminal
    assert Event.from_wire({"type": "session.status_idle"}).is_turn_end


def test_event_blocking_control():
    e = Event.from_wire({"payload": {"type": "control_request",
                                     "request": {"subtype": "can_use_tool"}}})
    assert e.is_blocking_control


def test_event_tool_uses():
    e = Event.from_wire({"payload": {"type": "assistant", "message": {"role": "assistant",
        "content": [{"type": "tool_use", "name": "Bash", "input": {"cmd": "ls"}, "id": "toolu_1"}]}}})
    tus = e.tool_uses()
    assert len(tus) == 1 and tus[0]["name"] == "Bash"


# --- builders --------------------------------------------------------------
def test_cli_user_message_shape():
    m = cli_user_message("hello")
    assert m["type"] == "user"
    assert m["message"] == {"role": "user", "content": [{"type": "text", "text": "hello"}]}


def test_cli_control_request_shape():
    r = cli_control_request("set_model", "rid-1", model="claude-opus-4-8")
    assert r["type"] == "control_request"
    assert r["request"] == {"subtype": "set_model", "model": "claude-opus-4-8"}


def test_cli_control_request_effort_shape():
    # Effort rides apply_flag_settings (there is no set_effort subtype); an
    # explicit null effortLevel means "clear back to auto", so it must survive.
    r = cli_control_request(
        "apply_flag_settings", "rid-2", settings={"effortLevel": None, "ultracode": False}
    )
    assert r["request"] == {
        "subtype": "apply_flag_settings",
        "settings": {"effortLevel": None, "ultracode": False},
    }


# --- set_effort verdict handling (offline: transport methods stubbed) -------
def _bare_rc() -> "RemoteControlClient":
    from claude_rc.client import RemoteControlClient

    return RemoteControlClient.__new__(RemoteControlClient)  # skip __init__ / creds


def test_set_effort_control_accepted(monkeypatch):
    rc = _bare_rc()
    sent = []
    monkeypatch.setattr(rc, "send_events", lambda sid, evs: sent.append(list(evs)) or {"ok": 1})
    monkeypatch.setattr(
        rc, "wait_control_response", lambda sid, rid, timeout: {"subtype": "success", "request_id": rid}
    )
    out = rc.set_effort("cse_x", "high", wait=1.0, command_fallback=True)
    assert out["via"] == "control"
    assert sent[0][0]["request"]["settings"] == {"effortLevel": "high", "ultracode": False}


def test_set_effort_command_fallback(monkeypatch):
    # Remote-control REPL workers refuse apply_flag_settings — the fallback
    # injects the /effort slash command (which they execute as a local command).
    rc = _bare_rc()
    messages = []
    monkeypatch.setattr(rc, "send_events", lambda sid, evs: {})
    monkeypatch.setattr(
        rc,
        "wait_control_response",
        lambda sid, rid, timeout: {
            "subtype": "error",
            "error": "REPL bridge does not handle control_request subtype: apply_flag_settings",
            "request_id": rid,
        },
    )
    monkeypatch.setattr(rc, "send_message", lambda sid, text: messages.append(text) or {})
    assert rc.set_effort("cse_x", "high", wait=1.0, command_fallback=True)["via"] == "command"
    assert messages == ["/effort high"]
    assert rc.set_effort("cse_x", None, wait=1.0, command_fallback=True)["via"] == "command"
    assert messages[-1] == "/effort auto"


def test_set_effort_rejected_without_fallback(monkeypatch):
    from claude_rc.client import ControlRejected

    rc = _bare_rc()
    monkeypatch.setattr(rc, "send_events", lambda sid, evs: {})
    monkeypatch.setattr(
        rc, "wait_control_response", lambda sid, rid, timeout: {"subtype": "error", "error": "nope"}
    )
    try:
        rc.set_effort("cse_x", "low", wait=1.0)
        raise AssertionError("expected ControlRejected")
    except ControlRejected as exc:
        assert "nope" in str(exc)


def test_set_effort_timeout_no_fallback(monkeypatch):
    # No verdict within `wait` → assume applied, do NOT double-apply via command.
    rc = _bare_rc()
    messages = []
    monkeypatch.setattr(rc, "send_events", lambda sid, evs: {})
    monkeypatch.setattr(rc, "wait_control_response", lambda sid, rid, timeout: None)
    monkeypatch.setattr(rc, "send_message", lambda sid, text: messages.append(text) or {})
    assert rc.set_effort("cse_x", "high", wait=0.1, command_fallback=True)["via"] == "control_unconfirmed"
    assert messages == []


def test_managed_agents_user_message_shape():
    assert user_message("x") == {"type": "user.message", "content": [{"type": "text", "text": "x"}]}


def test_managed_agents_stream_raises_apierror_on_non_200(monkeypatch):
    """A failed stream must surface as APIError.

    The body has to be read before it is formatted: httpx raises
    ``ResponseNotRead`` if ``.text`` is touched on a still-streaming response,
    which would mask the real status behind an unrelated exception.
    """
    from claude_rc.client import APIError, ManagedAgentsClient

    class _Resp:
        status_code = 503
        headers = {"request-id": "req_1"}
        read_called = False

        def read(self):
            type(self).read_called = True
            return b"upstream unavailable"

        @property
        def text(self):
            raise AssertionError("must not read .text of an unread stream")

        def iter_lines(self):
            raise AssertionError("must not parse the body of an error response")

    class _Stream:
        def __enter__(self):
            return _Resp()

        def __exit__(self, *exc):
            return False

    ma = ManagedAgentsClient(api_key="k")
    monkeypatch.setattr(ma._http, "stream", lambda *a, **kw: _Stream())
    with pytest.raises(APIError) as excinfo:
        list(ma.stream_events("sesn_1"))
    assert excinfo.value.status == 503
    assert excinfo.value.request_id == "req_1"
    assert "upstream unavailable" in excinfo.value.body
    assert _Resp.read_called
    ma.close()


# --- permission prompts (can_use_tool) --------------------------------------
def _permission_request(rid="req-1", tool="Bash", seq=10, suggestions=None):
    req = {"subtype": "can_use_tool", "tool_name": tool, "input": {"command": "ls"}}
    if suggestions is not None:
        req["permission_suggestions"] = suggestions
    return Event.from_wire({"sequence_num": seq, "payload": {
        "type": "control_request", "request_id": rid, "request": req}})


def test_event_permission_accessors():
    e = _permission_request(suggestions=[{"type": "addRules", "rules": []}])
    assert e.control_request_id == "req-1"
    assert e.control_subtype == "can_use_tool"
    assert e.tool_name == "Bash"
    assert e.tool_input == {"command": "ls"}
    assert e.permission_suggestions == [{"type": "addRules", "rules": []}]
    # control_response side: request_id resolves from the response object
    r = Event.from_wire({"payload": {"type": "control_response",
                                     "response": {"subtype": "success", "request_id": "req-1"}}})
    assert r.control_request_id == "req-1"


def test_cli_control_response_shapes():
    from claude_rc.events import cli_control_response, permission_allow, permission_deny

    ok = cli_control_response("rid-1", permission_allow({"command": "ls"}), tool_use_id="toolu_1")
    assert ok == {"type": "control_response", "response": {
        "subtype": "success", "request_id": "rid-1", "tool_use_id": "toolu_1",
        "response": {"behavior": "allow", "updatedInput": {"command": "ls"}}}}

    # an allow must always carry updatedInput (confirmed live) — None falls back to {}
    assert permission_allow(None) == {"behavior": "allow", "updatedInput": {}}

    always = permission_allow({"command": "ls"}, updated_permissions=[{"type": "addRules"}])
    assert always["updatedPermissions"] == [{"type": "addRules"}]

    deny = cli_control_response("rid-2", permission_deny("not now", interrupt=True))
    assert deny["response"]["response"] == {"behavior": "deny", "message": "not now", "interrupt": True}
    assert "tool_use_id" not in deny["response"]
    # an empty deny message is replaced with a stock line (matching live answers)
    assert permission_deny("")["message"] == "Denied by controller"

    err = cli_control_response("rid-3", error="boom")
    assert err["response"] == {"subtype": "error", "request_id": "rid-3", "error": "boom"}


def test_question_input_and_detection():
    from claude_rc.events import question_input

    q_input = {"questions": [{"question": "Which db?", "header": "DB",
                              "options": [{"label": "postgres"}, {"label": "sqlite"}],
                              "multiSelect": False}]}
    ev = Event.from_wire({"sequence_num": 5, "payload": {
        "type": "control_request", "request_id": "req-q",
        "request": {"subtype": "can_use_tool", "tool_name": "AskUserQuestion",
                    "tool_use_id": "toolu_q", "input": q_input}}})
    assert ev.is_question
    assert ev.tool_use_id == "toolu_q"
    # a normal permission is NOT a question
    assert not _permission_request().is_question

    # answers ride in updatedInput; questions must be echoed (tool crashes otherwise)
    out = question_input(q_input, {"Which db?": "postgres"})
    assert out["questions"] == q_input["questions"]
    assert out["answers"] == {"Which db?": "postgres"}
    # empty map = graceful dismiss; questions still echoed
    dismissed = question_input(q_input, None)
    assert dismissed["answers"] == {} and dismissed["questions"]


def test_answer_question_wire_format(monkeypatch):
    rc = _bare_rc()
    sent = []
    monkeypatch.setattr(rc, "send_events", lambda sid, evs: sent.append(list(evs)) or {"ok": 1})
    q_input = {"questions": [{"question": "Which db?", "options": []}]}
    rc.answer_question("cse_x", "req-q", {"Which db?": "postgres"}, q_input, tool_use_id="toolu_q")
    resp = sent[0][0]["response"]
    assert resp["subtype"] == "success" and resp["tool_use_id"] == "toolu_q"
    inner = resp["response"]
    assert inner["behavior"] == "allow"
    assert inner["updatedInput"]["answers"] == {"Which db?": "postgres"}
    assert inner["updatedInput"]["questions"] == q_input["questions"]


def test_get_session_unwraps_response_shape(monkeypatch):
    rc = _bare_rc()

    class _Resp:
        status_code = 200
        content = b"x"
        headers = {}

        def json(self):
            return {"response_shape": {"id": "cse_1", "status": "active"}}

    monkeypatch.setattr(rc, "_request", lambda *a, **kw: _Resp())
    monkeypatch.setattr(rc, "_url", lambda *p: "u")
    assert rc.get_session("cse_1") == {"id": "cse_1", "status": "active"}


def test_answer_permission_wire_format(monkeypatch):
    rc = _bare_rc()
    sent = []
    monkeypatch.setattr(rc, "send_events", lambda sid, evs: sent.append(list(evs)) or {"ok": 1})

    rc.answer_permission("cse_x", "req-1", True, updated_input={"command": "ls"})
    allow = sent[0][0]
    assert allow["type"] == "control_response"
    assert allow["response"]["subtype"] == "success"
    assert allow["response"]["response"] == {"behavior": "allow", "updatedInput": {"command": "ls"}}

    rc.answer_permission("cse_x", "req-2", False, message="nope")
    deny = sent[1][0]
    assert deny["response"]["response"] == {"behavior": "deny", "message": "nope"}


def test_pending_permissions_lifecycle():
    from claude_rc.events import pending_permissions

    answered = Event.from_wire({"sequence_num": 11, "payload": {
        "type": "control_response",
        "response": {"subtype": "success", "request_id": "req-1"}}})
    result = Event.from_wire({"sequence_num": 12, "payload": {"type": "result", "subtype": "success"}})

    # answered → gone; unanswered in the current turn → pending
    assert pending_permissions([_permission_request("req-1"), answered]) == []
    still_open = _permission_request("req-2", seq=13)
    assert pending_permissions([_permission_request("req-1"), answered, still_open]) == [still_open]
    # a turn boundary abandons everything before it
    assert pending_permissions([_permission_request("req-1"), result]) == []
    assert pending_permissions([_permission_request("req-1"), result, still_open]) == [still_open]


def test_client_pending_permission_requests(monkeypatch):
    rc = _bare_rc()
    # newest-first, as list_events(sort_order="desc") returns
    history_desc = [_permission_request("req-2", seq=13),
                    Event.from_wire({"sequence_num": 12, "payload": {"type": "result", "subtype": "success"}}),
                    _permission_request("req-1", seq=10)]
    monkeypatch.setattr(rc, "list_events", lambda sid, **kw: list(history_desc))
    pending = rc.pending_permission_requests("cse_x")
    assert [e.control_request_id for e in pending] == ["req-2"]


# --- webui permission endpoint ----------------------------------------------
def test_webui_event_to_dict_permission_fields():
    from claude_rc.webui import event_to_dict

    d = event_to_dict(_permission_request(suggestions=[{"type": "addRules"}]))
    assert d["is_blocking_control"] and d["blocking_subtype"] == "can_use_tool"
    assert d["request_id"] == "req-1"
    assert d["tool_name"] == "Bash"
    assert d["tool_input"] == {"command": "ls"}
    assert d["has_suggestions"] is True


def test_webui_event_to_dict_thinking_results_usage():
    from claude_rc.webui import event_to_dict

    # thinking blocks surface for the web UI
    a = Event.from_wire({"payload": {"type": "assistant", "message": {"role": "assistant",
        "content": [{"type": "thinking", "thinking": "hmm"},
                    {"type": "text", "text": "hi"}]}}})
    da = event_to_dict(a)
    assert da["thinking"] == "hmm" and da["text"] == "hi"

    # tool_result blocks (on a user event) flatten to text + error flag
    u = Event.from_wire({"payload": {"type": "user", "message": {"role": "user",
        "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "out", "is_error": True}]}}})
    du = event_to_dict(u)
    assert du["tool_results"] == [{"text": "out", "is_error": True}]

    # result usage footer
    r = Event.from_wire({"payload": {"type": "result", "subtype": "success",
        "duration_ms": 4300, "total_cost_usd": 0.0123,
        "usage": {"input_tokens": 1200, "output_tokens": 340}}})
    dr = event_to_dict(r)
    assert dr["usage"]["cost_usd"] == 0.0123 and dr["usage"]["input_tokens"] == 1200


def test_event_thinking_accessor():
    e = Event.from_wire({"payload": {"type": "assistant", "message": {"role": "assistant",
        "content": [{"type": "thinking", "thinking": "step "},
                    {"type": "redacted_thinking", "data": "xx"},
                    {"type": "text", "text": "answer"}]}}})
    assert e.thinking() == "step [redacted thinking]"
    assert e.text() == "answer"


# --- credentials -----------------------------------------------------------
def test_load_credentials_from_file(tmp_path: Path):
    p = tmp_path / ".credentials.json"
    p.write_text(json.dumps({"claudeAiOauth": {
        "accessToken": "a" * 108, "refreshToken": "r" * 108,
        "expiresAt": 9999999999999, "scopes": ["user:inference"],
        "subscriptionType": "max", "clientId": "cid"}}))
    c = load_credentials(p)
    assert c.access_token == "a" * 108
    assert c.client_id == "cid"
    assert not c.is_expired()


def test_load_credentials_expired(tmp_path: Path):
    p = tmp_path / ".credentials.json"
    p.write_text(json.dumps({"claudeAiOauth": {"accessToken": "t", "expiresAt": 1}}))
    assert load_credentials(p).is_expired()


def test_load_credentials_missing_block(tmp_path: Path):
    p = tmp_path / ".credentials.json"
    p.write_text(json.dumps({"somethingElse": {}}))
    with pytest.raises(CredentialsError):
        load_credentials(p)


# --- rotated-token recovery (long-lived clients) -----------------------------
def _write_creds(p: Path, token: str, refresh: str, expires_at_ms: int) -> None:
    p.write_text(json.dumps({"claudeAiOauth": {
        "accessToken": token, "refreshToken": refresh,
        "expiresAt": expires_at_ms, "scopes": ["user:inference"],
        "subscriptionType": "max"}}))


def _client_for(p: Path):
    from claude_rc.client import RemoteControlClient
    return RemoteControlClient(credentials_path=str(p), org_uuid="org-x")


def test_token_prefers_rotated_disk_copy(tmp_path: Path, monkeypatch):
    """Expired in memory + fresh tokens on disk (another process refreshed)
    → take the disk copy; never spend our own (already-dead) refresh token."""
    p = tmp_path / ".credentials.json"
    _write_creds(p, "old-token", "old-refresh", 1)  # long expired
    client = _client_for(p)

    def _no_refresh(*a, **kw):
        raise AssertionError("network refresh must not run when disk has fresh tokens")

    monkeypatch.setattr("claude_rc.client.refresh_credentials", _no_refresh)
    _write_creds(p, "new-token", "new-refresh", 9999999999999)  # rotated by "someone else"
    assert client._token() == "new-token"
    client.close()


def test_token_reloads_disk_after_invalid_grant(tmp_path: Path, monkeypatch):
    """Our refresh attempt fails (single-use token already spent) but fresh
    tokens landed on disk meanwhile → recover from disk instead of raising."""
    p = tmp_path / ".credentials.json"
    _write_creds(p, "old-token", "old-refresh", 1)
    client = _client_for(p)

    def _invalid_grant(*a, **kw):
        _write_creds(p, "new-token", "new-refresh", 9999999999999)
        raise CredentialsError("token refresh failed: HTTP 400 invalid_grant")

    monkeypatch.setattr("claude_rc.client.refresh_credentials", _invalid_grant)
    assert client._token() == "new-token"
    client.close()


def test_token_raises_when_no_recovery_possible(tmp_path: Path, monkeypatch):
    """Refresh fails and the disk still holds the same dead tokens → raise."""
    p = tmp_path / ".credentials.json"
    _write_creds(p, "old-token", "old-refresh", 1)
    client = _client_for(p)

    def _invalid_grant(*a, **kw):
        raise CredentialsError("token refresh failed: HTTP 400 invalid_grant")

    monkeypatch.setattr("claude_rc.client.refresh_credentials", _invalid_grant)
    with pytest.raises(CredentialsError):
        client._token()
    client.close()
