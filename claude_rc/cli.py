"""Command-line interface for the Claude Remote Control API.

    claude-rc whoami                 # show login / org / token status
    claude-rc list                   # list your Remote Control sessions
    claude-rc get <session_id>       # session details
    claude-rc events <session_id>    # recent events (history)
    claude-rc watch <session_id>     # stream events live (read-only)
    claude-rc send <session_id> "run the tests"   # send a message, print the reply
    claude-rc repl <session_id>      # interactive chat with a live session
    claude-rc tui [session_id]       # terminal control panel (needs the `tui` extra)
    claude-rc web                    # browser control panel for all sessions

`send`, `repl`, `tui`, and `web` steer live sessions — only use them on
sessions you started with `claude remote-control`.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import sys
from typing import Optional

from . import __version__
from .client import RemoteControlClient
from .credentials import CredentialsError, load_credentials, load_org_uuid

try:  # optional pretty output
    from rich.console import Console
    from rich.table import Table

    _console: Optional["Console"] = Console()
except Exception:  # pragma: no cover - rich optional
    _console = None


def _print(*a, **k):
    if _console:
        _console.print(*a, **k)
    else:
        print(*[str(x) for x in a], **k)


def _fmt_time(iso: Optional[str]) -> str:
    if not iso:
        return "-"
    try:
        t = _dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
        delta = _dt.datetime.now(_dt.timezone.utc) - t
        secs = int(delta.total_seconds())
        for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
            if secs >= n:
                return f"{secs // n}{unit} ago"
        return f"{secs}s ago"
    except Exception:
        return iso


# --- commands --------------------------------------------------------------
def cmd_whoami(args) -> int:
    try:
        creds = load_credentials()
    except CredentialsError as e:
        _print(f"not logged in: {e}")
        return 1
    org = load_org_uuid()
    _print(f"access token : {'present' if creds.access_token else 'MISSING'} (len {len(creds.access_token)})")
    _print(f"expired      : {creds.is_expired()}")
    _print(f"scopes       : {', '.join(creds.scopes) or '(unknown)'}")
    _print(f"subscription : {creds.subscription_type or '-'}")
    _print(f"org uuid     : {'present' if org else 'MISSING — set CLAUDE_RC_ORG_UUID'}")
    return 0


def cmd_list(args) -> int:
    with RemoteControlClient() as rc:
        sessions = rc.sessions(limit=args.limit)
    if _console:
        table = Table(title=f"{len(sessions)} Remote Control sessions")
        for col in ("id", "title", "status", "worker", "model", "last activity"):
            table.add_column(col, overflow="fold")
        for s in sessions:
            table.add_row(
                s.get("id", ""),
                (s.get("title") or "")[:50],
                str(s.get("status", "")),
                str(s.get("worker_status", "")),
                (s.get("config") or {}).get("model", ""),
                _fmt_time(s.get("last_event_at")),
            )
        _console.print(table)
    else:
        for s in sessions:
            print(f"{s.get('id')}\t{s.get('status')}\t{(s.get('title') or '')[:50]}")
    return 0


def cmd_get(args) -> int:
    import json

    with RemoteControlClient() as rc:
        print(json.dumps(rc.get_session(args.session_id), indent=2))
    return 0


def cmd_events(args) -> int:
    with RemoteControlClient() as rc:
        evs = rc.list_events(args.session_id, limit=args.limit, sort_order="asc")
    for e in evs:
        _render_event(e)
    return 0


def cmd_watch(args) -> int:
    _print("streaming (Ctrl-C to stop)…")
    with RemoteControlClient() as rc:
        try:
            for e in rc.stream_events(args.session_id, from_sequence_num=args.from_seq):
                _render_event(e)
        except KeyboardInterrupt:
            return 0
    return 0


def cmd_send(args) -> int:
    with RemoteControlClient() as rc:
        for e in rc.send_and_collect(args.session_id, args.message):
            _render_event(e)
    return 0


def cmd_repl(args) -> int:
    _print("REPL — type a message and press enter; Ctrl-D to quit.")
    with RemoteControlClient() as rc:
        while True:
            try:
                text = input("you> ")
            except (EOFError, KeyboardInterrupt):
                print()
                return 0
            if not text.strip():
                continue
            events = rc.send_and_collect(args.session_id, text)
            for e in events:
                _render_event(e)
            _handle_blocking(rc, args.session_id, events[-1] if events else None)


def _handle_blocking(rc: RemoteControlClient, sid: str, last) -> None:
    """Answer permission prompts interactively, then follow the turn to its end."""
    import json

    while last is not None and last.is_blocking_control:
        if last.control_subtype != "can_use_tool" or not last.control_request_id:
            _print(f"⚠ session blocked on: {last.control_subtype} (answer it elsewhere)")
            return
        if last.is_question:
            answers = {}
            for q in (last.tool_input or {}).get("questions") or []:
                if not isinstance(q, dict) or not q.get("question"):
                    continue
                labels = [
                    o if isinstance(o, str) else (o.get("label") or "")
                    for o in q.get("options") or []
                ]
                labels = [x for x in labels if x]
                _print(f"❓ {q['question']}")
                for i, label in enumerate(labels, 1):
                    _print(f"   {i}. {label}")
                try:
                    raw = input("pick number(s), or free text (empty = skip): ").strip()
                except (EOFError, KeyboardInterrupt):
                    print()
                    return
                if not raw:
                    continue
                picks = []
                for part in raw.replace(",", " ").split():
                    if part.isdigit() and 1 <= int(part) <= len(labels):
                        picks.append(labels[int(part) - 1])
                answers[q["question"]] = (
                    (picks if q.get("multiSelect") else picks[0]) if picks else raw
                )
            rc.answer_question(
                sid, last.control_request_id, answers, last.tool_input,
                tool_use_id=last.tool_use_id,
            )
        else:
            # Print the FULL input — approving a command you only saw part of
            # defeats the point of the prompt. Only a pathological input is
            # clipped, with an explicit notice.
            pretty = last.tool_input
            pretty = pretty if isinstance(pretty, str) else json.dumps(pretty, indent=2, default=str)
            pretty = pretty or ""
            if len(pretty) > 200_000:
                omitted = len(pretty) - 200_000
                pretty = pretty[:200_000] + f"\n… ⚠ TRUNCATED: {omitted:,} more characters not shown"
            _print(f"🔐 permission: {last.tool_name or 'tool'}")
            for line in pretty.splitlines():
                _print(f"   {line}")
            try:
                answer = input("allow? [y]es / [n]o / anything else = deny with that reason: ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return
            allow = answer.lower() in ("y", "yes")
            rc.answer_permission(
                sid,
                last.control_request_id,
                allow,
                updated_input=last.tool_input if allow else None,
                message="" if allow or answer.lower() in ("n", "no") else answer,
                tool_use_id=last.tool_use_id,
            )
        # Follow the rest of the turn from where it blocked.
        start = last.sequence_num or 0
        last = None
        for ev in rc.stream_events(sid, from_sequence_num=start, reconnect=False):
            if (ev.sequence_num or 0) <= start:
                continue
            _render_event(ev)
            if ev.is_turn_end or ev.is_terminal or ev.is_blocking_control:
                last = ev
                break


def cmd_tui(args) -> int:
    try:
        from .tui import run
    except ImportError:
        _print(
            "the TUI needs the `tui` extra:  pip install 'claude-rc-api[tui]'  "
            "(or `uv sync --extra tui` in a checkout)"
        )
        return 1
    run(session_id=args.session_id)
    return 0


def cmd_web(args) -> int:
    from .webui import serve

    serve(
        host=args.host,
        port=args.port,
        open_browser=not args.no_open,
        verbose=args.verbose,
    )
    return 0


def _render_event(e) -> None:
    t = e.type
    if e.role == "assistant":
        body = e.text().strip()
        if body:
            _print(f"[green]claude[/green]> {body}" if _console else f"claude> {body}")
        for tu in e.tool_uses():
            _print(f"  [yellow]· tool[/yellow] {tu.get('name')}" if _console else f"  · tool {tu.get('name')}")
    elif e.role == "user":
        body = e.text().strip()
        if body:
            _print(f"[cyan]user[/cyan]> {body}" if _console else f"user> {body}")
    elif t == "result":
        _print(f"[dim]— turn complete ({e.subtype})[/dim]" if _console else f"— turn complete ({e.subtype})")
    elif t == "system" and e.subtype == "init":
        model = e.payload.get("model", "?")
        _print(f"[dim]· session init (model={model})[/dim]" if _console else f"· session init (model={model})")
    elif t == "control_request" and e.is_blocking_control:
        sub = (e.payload.get("request") or {}).get("subtype")
        _print(f"[magenta]⚠ needs you: {sub}[/magenta]" if _console else f"⚠ needs you: {sub}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="claude-rc", description="Claude Remote Control API client")
    p.add_argument("--version", action="version", version=f"claude-rc {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("whoami", help="show login / org / token status").set_defaults(func=cmd_whoami)

    pl = sub.add_parser("list", help="list Remote Control sessions")
    pl.add_argument("--limit", type=int, default=None)
    pl.set_defaults(func=cmd_list)

    pg = sub.add_parser("get", help="session details (JSON)")
    pg.add_argument("session_id")
    pg.set_defaults(func=cmd_get)

    pe = sub.add_parser("events", help="recent events (history)")
    pe.add_argument("session_id")
    pe.add_argument("--limit", type=int, default=30)
    pe.set_defaults(func=cmd_events)

    pw = sub.add_parser("watch", help="stream events live (read-only)")
    pw.add_argument("session_id")
    pw.add_argument("--from-seq", type=int, default=0, dest="from_seq")
    pw.set_defaults(func=cmd_watch)

    ps = sub.add_parser("send", help="send a message and print the reply")
    ps.add_argument("session_id")
    ps.add_argument("message")
    ps.set_defaults(func=cmd_send)

    pr = sub.add_parser("repl", help="interactive chat with a live session")
    pr.add_argument("session_id")
    pr.set_defaults(func=cmd_repl)

    pt = sub.add_parser("tui", help="terminal control panel (requires the `tui` extra)")
    pt.add_argument("session_id", nargs="?", default=None)
    pt.set_defaults(func=cmd_tui)

    pweb = sub.add_parser("web", help="launch the browser control panel")
    pweb.add_argument("--host", default="127.0.0.1", help="bind address (default: 127.0.0.1)")
    pweb.add_argument("--port", type=int, default=8765, help="port (default: 8765)")
    pweb.add_argument("--no-open", action="store_true", help="don't open a browser window")
    pweb.add_argument("--verbose", action="store_true", help="log HTTP requests")
    pweb.set_defaults(func=cmd_web)
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except CredentialsError as e:
        _print(f"auth error: {e}")
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
