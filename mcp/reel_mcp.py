#!/usr/bin/env python3
"""Reel Inbox MCP server (stdio, stdlib only).

Tools:
  list_reels  - newest first, with the sender's note and status
  get_reel    - one reel (newest by default): note, transcript, on-screen text, and the frames as images
  mark_done   - hide a reel from the default list once it has been used

Reads ~/ReelInbox/<id>/{meta.json, reel.md, frames/*.jpg} written by worker/reel_worker.py.
"""
from __future__ import annotations

import base64
import datetime as dt
import json
import os
import sys
from pathlib import Path

SUPPORT = Path.home() / "Library/Application Support/ReelInbox"
# install.sh writes config.json with the folders it detected; an environment variable overrides it.
try:
    CONFIG = json.loads((SUPPORT / "config.json").read_text())
except (OSError, ValueError):
    CONFIG = {}
SHORTCUTS_ICLOUD = "Library/Mobile Documents/iCloud~is~workflow~my~workflows/Documents"  # Apple's, same on every Mac


def setting(env, key, default):
    return Path(os.environ.get(env) or CONFIG.get(key) or default).expanduser()


OUT_ROOT = setting("REEL_INBOX_OUT", "output", Path.home() / "ReelInbox")
INBOX = setting("REEL_INBOX_SRC", "inbox", Path.home() / SHORTCUTS_ICLOUD / "ReelInbox")
# Optional: if you keep a learnings library as a Claude skill, every reel gets filed into it.
LEARNINGS = Path.home() / ".claude/skills/reel-learnings"
FILE_LINE = ("After reading, file this reel in ~/.claude/skills/reel-learnings/ (see its SKILL.md)."
             if (LEARNINGS / "SKILL.md").exists() else "")
SERVER = {"name": "reel-inbox", "version": "1.0.0"}

TOOLS = [
    {
        "name": "list_reels",
        "description": "List Instagram/TikTok reels the user sent from their phone with 'Send to Claude', newest "
                       "first, with the note about why each one was sent. Done reels are hidden unless "
                       "include_done is true.",
        "inputSchema": {"type": "object", "properties": {
            "limit": {"type": "integer", "description": "How many to list (default 10)."},
            "include_done": {"type": "boolean", "description": "Also list reels marked done."}}},
    },
    {
        "name": "get_reel",
        "description": "Get a reel the user sent from their phone: their note (why they sent it / which project), the "
                       "full transcript, on-screen text and code (OCR), and the key frames as images. With no "
                       "id it returns the newest one. Use this when the user says 'the reel I sent', 'the reel I "
                       "just shared', 'check my reel inbox', etc.",
        "inputSchema": {"type": "object", "properties": {
            "id": {"type": "string", "description": "Reel id from list_reels, or any words from its note or "
                                                    "title, or 'next' for the oldest one not opened yet "
                                                    "(for working through a stack). Omit for the newest reel."},
            "include_frames": {"type": "boolean", "description": "Return frame images (default true)."},
            "max_frames": {"type": "integer", "description": "Max frame images to return (default 12)."}}},
    },
    {
        "name": "mark_done",
        "description": "Mark a reel as handled so list_reels hides it by default. Nothing is deleted.",
        "inputSchema": {"type": "object", "properties": {
            "id": {"type": "string", "description": "Reel id from list_reels."}}, "required": ["id"]},
    },
]


# ------------------------------------------------------------------ data

def all_reels() -> list[dict]:
    reels = []
    if not OUT_ROOT.exists():
        return reels
    for d in OUT_ROOT.iterdir():
        mp = d / "meta.json"
        if d.is_dir() and mp.exists():
            try:
                m = json.loads(mp.read_text())
            except json.JSONDecodeError:
                continue
            m["_dir"] = d
            reels.append(m)
    reels.sort(key=sent_key, reverse=True)
    return reels


def sent_key(m: dict) -> str:
    for k in ("sent_at", "processed_at"):
        try:
            return dt.datetime.fromisoformat(str(m[k])).astimezone(dt.timezone.utc).isoformat()
        except Exception:
            continue
    return m["id"]


def is_new(m: dict) -> bool:
    return not m.get("opened_at") and not m.get("done")


def new_count_line() -> str:
    n = sum(1 for m in all_reels() if is_new(m))
    return f"{n} new reel(s) not opened yet." if n else "No new reels."


def waiting_line() -> str:
    """Items still in the iCloud inbox: sent from the phone but not processed yet."""
    if not INBOX.exists():
        return ""
    waiting = [p for p in INBOX.iterdir() if p.is_file() and (p.suffix == ".json" or p.name.endswith(".icloud"))]
    if not waiting:
        return ""
    retrying = 0
    for p in waiting:
        try:
            if p.stat().st_flags & 0x40000000:  # still an iCloud placeholder: reading it could hang
                continue
            if json.loads(p.read_text()).get("status") == "failed":
                retrying += 1
        except Exception:
            pass
    s = f"Note: {len(waiting)} reel(s) sent from the phone are still waiting to be processed on the Mac"
    if retrying:
        s += f" ({retrying} failed once and will be retried)"
    return s + ". If the user means one they just sent, it may need another minute; check again shortly."


def find(query: str | None) -> dict | None:
    reels = all_reels()
    if not reels:
        return None
    if not query:
        return reels[0]
    q = query.strip().lower()
    if q in ("next", "next unread", "oldest unread"):
        unread = [m for m in reels if is_new(m)]
        return unread[-1] if unread else None
    for m in reels:
        if m["id"].lower() == q:
            return m
    for m in reels:
        if m["id"].lower().startswith(q):
            return m
    words = q.split()
    for m in reels:
        hay = " ".join(str(m.get(k) or "") for k in ("id", "note", "title", "caption", "uploader", "url")).lower()
        if all(w in hay for w in words):
            return m
    return None


def summary(m: dict) -> str:
    status = m.get("status")
    s = f"- {m['id']}"
    if is_new(m):
        s += "  [NEW]"
    if m.get("done"):
        s += "  [done]"
    if status != "ok":
        s += f"  [{status.upper()}: {m.get('error') or ''}]"
    s += f"\n  note: {m.get('note') or '(no note)'}"
    s += f"\n  from: {m.get('uploader') or '?'} · {m.get('title') or m.get('url')}"
    s += f"\n  sent: {m.get('sent_at') or m.get('processed_at')} · {len(m.get('frames') or [])} frames"
    return s


# ------------------------------------------------------------------ tools

def list_reels(args: dict) -> list[dict]:
    limit = int(args.get("limit") or 10)
    reels = [m for m in all_reels() if args.get("include_done") or not m.get("done")]
    lines = [summary(m) for m in reels[:limit]]
    head = new_count_line() + f" {len(reels)} reel(s)" + ("" if args.get("include_done") else " not marked done")
    text = head + ", newest first. To work through a stack oldest-first, call get_reel with id 'next'.\n" + ("\n".join(lines) if lines else "(none)")
    w = waiting_line()
    return [{"type": "text", "text": text + ("\n\n" + w if w else "")}]


def get_reel(args: dict) -> list[dict]:
    m = find(args.get("id"))
    w = waiting_line()
    if not m:
        if str(args.get("id") or "").strip().lower().startswith(("next", "oldest")):
            msg = "No unread reels left."
        else:
            msg = "No reel found" + (f" matching '{args.get('id')}'." if args.get("id") else " yet.")
        return [{"type": "text", "text": msg + ("\n\n" + w if w else "")}]
    d: Path = m["_dir"]
    mark_opened(d)
    md = (d / "reel.md").read_text() if (d / "reel.md").exists() else json.dumps(m, indent=2, default=str)
    header = f"Reel id: {m['id']}\nFolder: {d}\n"
    if w:
        header += w + "\n"
    out = [{"type": "text", "text": header + "\n" + md + ("\n\n" + FILE_LINE if FILE_LINE else "")}]
    if args.get("include_frames", True) is not False:
        frames = m.get("frames") or []
        cap = int(args.get("max_frames") or 12)
        if len(frames) > cap:
            step = len(frames) / cap
            frames = [frames[int(i * step)] for i in range(cap)]
        for f in frames:
            p = d / f["file"]
            if not p.exists():
                continue
            t = f.get("t", 0)
            out.append({"type": "text", "text": f"Frame {f['file']} at {int(t // 60)}:{int(t % 60):02d}"})
            out.append({"type": "image", "mimeType": "image/jpeg",
                        "data": base64.b64encode(p.read_bytes()).decode()})
    return out


def mark_opened(d: Path) -> None:
    """A reel only stops counting as new once Claude has actually opened it."""
    mp = d / "meta.json"
    data = json.loads(mp.read_text())
    if not data.get("opened_at"):
        data["opened_at"] = dt.datetime.now().isoformat(timespec="seconds")
        mp.write_text(json.dumps(data, indent=2))


def mark_done(args: dict) -> list[dict]:
    m = find(args.get("id"))
    if not m:
        return [{"type": "text", "text": f"No reel matching '{args.get('id')}'."}]
    mp = m["_dir"] / "meta.json"
    data = json.loads(mp.read_text())
    data["done"] = True
    mp.write_text(json.dumps(data, indent=2))
    return [{"type": "text", "text": f"Marked done: {m['id']}"}]


def session_instructions() -> str:
    base = ("Reels the user shared from their phone with a note. 'The reel I sent' = get_reel with no id; "
            "'go through my reels' = get_reel id 'next' repeatedly. Read their note first: it says why they "
            "sent it. " + FILE_LINE)
    try:
        n = sum(1 for m in all_reels() if is_new(m))
        w = waiting_line()
    except Exception:
        return base
    if n or w:
        base += (f" STATUS AT SESSION START: {n} new reel(s) not opened yet. {w} "
                 "Mention this to the user in one short line in your first reply, then carry on with what "
                 "they asked. Don't open them unless they ask.")
    return base


HANDLERS = {"list_reels": list_reels, "get_reel": get_reel, "mark_done": mark_done}


# ------------------------------------------------------------------ JSON-RPC over stdio

def reply(msg_id, result=None, error=None) -> None:
    msg = {"jsonrpc": "2.0", "id": msg_id}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def handle(req: dict) -> None:
    method, msg_id = req.get("method"), req.get("id")
    if msg_id is None:  # notification (initialized, cancelled, ...)
        return
    params = req.get("params") or {}
    if method == "initialize":
        reply(msg_id, {"protocolVersion": params.get("protocolVersion", "2025-06-18"),
                       "capabilities": {"tools": {}}, "serverInfo": SERVER,
                       "instructions": session_instructions()})
    elif method == "ping":
        reply(msg_id, {})
    elif method == "tools/list":
        reply(msg_id, {"tools": TOOLS})
    elif method == "tools/call":
        name = params.get("name")
        fn = HANDLERS.get(name)
        if not fn:
            reply(msg_id, error={"code": -32602, "message": f"unknown tool {name}"})
            return
        try:
            reply(msg_id, {"content": fn(params.get("arguments") or {}), "isError": False})
        except Exception as e:  # noqa: BLE001
            reply(msg_id, {"content": [{"type": "text", "text": f"reel-inbox error: {e}"}], "isError": True})
    else:
        reply(msg_id, error={"code": -32601, "message": f"method not found: {method}"})


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        handle(req)


if __name__ == "__main__":
    main()
