#!/usr/bin/env python3
"""Reel Inbox worker.

Watches the iCloud folder the phone Shortcut writes into. For each new item it
downloads the video (yt-dlp), transcribes it locally (whisper.cpp), picks the
important frames (ffmpeg scene changes + Vision similarity), and reads on-screen
text (Vision OCR). Output: ~/ReelInbox/<id>/reel.md, meta.json, frames/*.jpg.

Stdlib only. Run by launchd (install.sh sets it up). Safe to run by hand any time:
    python3 worker/reel_worker.py            # process everything pending
    python3 worker/reel_worker.py --url URL --note "why"   # process one URL directly
"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

HOME = Path.home()
SUPPORT = Path.home() / "Library/Application Support/ReelInbox"
# install.sh writes config.json with the folders it detected; an environment variable overrides it.
try:
    CONFIG = json.loads((SUPPORT / "config.json").read_text())
except (OSError, ValueError):
    CONFIG = {}
SHORTCUTS_ICLOUD = "Library/Mobile Documents/iCloud~is~workflow~my~workflows/Documents"  # Apple's, same on every Mac


def setting(env, key, default):
    return Path(os.environ.get(env) or CONFIG.get(key) or default).expanduser()


INBOX = setting("REEL_INBOX_SRC", "inbox", HOME / SHORTCUTS_ICLOUD / "ReelInbox")
OUT_ROOT = setting("REEL_INBOX_OUT", "output", HOME / "ReelInbox")
MODEL = setting("REEL_INBOX_MODEL", "model", SUPPORT / "models/ggml-large-v3-turbo-q5_0.bin")
REPO = Path(__file__).resolve().parent.parent
REELVISION = REPO / "bin/reelvision"
ICLOUDREAD = REPO / "bin/icloudread"
LOG = SUPPORT / "worker.log"

BREW = "/opt/homebrew/bin"
YTDLP = shutil.which("yt-dlp") or f"{BREW}/yt-dlp"
FFMPEG = shutil.which("ffmpeg") or f"{BREW}/ffmpeg"
FFPROBE = shutil.which("ffprobe") or f"{BREW}/ffprobe"
WHISPER = shutil.which("whisper-cli") or f"{BREW}/whisper-cli"

MAX_FRAMES = 12
MAX_ATTEMPTS = 4
URL_RE = re.compile(r"https?://[^\s\"'<>]+")


def log(msg: str) -> None:
    SUPPORT.mkdir(parents=True, exist_ok=True)
    line = f"{dt.datetime.now().isoformat(timespec='seconds')} {msg}"
    print(line, flush=True)
    with LOG.open("a") as f:
        f.write(line + "\n")


def run(cmd: list[str], timeout: int = 600) -> subprocess.CompletedProcess:
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        tail = (p.stderr or p.stdout).strip().splitlines()[-6:]
        raise RuntimeError(f"{Path(cmd[0]).name} failed: " + " | ".join(tail))
    return p


# ---------------------------------------------------------------- inbox items

def parse_item(raw: str) -> dict:
    """The Shortcut saves a JSON dictionary, but be lenient: any text with a URL works."""
    data: dict = {}
    try:
        loaded = json.loads(raw)
        if isinstance(loaded, dict):
            data = loaded
    except json.JSONDecodeError:
        pass
    url = data.get("url")
    if isinstance(url, list):
        url = url[0] if url else None
    if not url or not URL_RE.search(str(url)):
        # No link in the url field: look in everything else that was shared (e.g. shared_text).
        hay = " ".join(str(v) for v in data.values()) if data else raw
        m = URL_RE.search(hay)
        url = m.group(0) if m else None
    else:
        url = URL_RE.search(str(url)).group(0)
    note = data.get("note")
    if note is None and not data:
        note = URL_RE.sub("", raw).strip()
    return {
        "url": url,
        "note": (str(note).strip() if note else ""),
        "sent_at": str(data.get("sent_at") or data.get("timestamp") or ""),
        "attempts": int(data.get("attempts") or 0),
        "raw": data or raw,
    }


def read_item(path: Path) -> str:
    """Files from the phone land as iCloud placeholders; a plain read hangs or fails, so go through
    NSFileCoordinator (bin/icloudread), which downloads it first."""
    p = subprocess.run([str(ICLOUDREAD), str(path)], capture_output=True, timeout=120)
    if p.returncode != 0:
        raise RuntimeError(f"couldn't download from iCloud yet: {p.stderr.decode(errors='replace').strip()}")
    return p.stdout.decode(errors="replace")


def materialize_icloud_placeholders() -> None:
    """With 'Optimize Mac Storage' on, iCloud shows '.name.json.icloud' stubs. Ask for the real file."""
    for stub in INBOX.glob(".*.icloud"):
        real = stub.parent / stub.name[1:-len(".icloud")]
        subprocess.run(["/usr/bin/brctl", "download", str(real)], capture_output=True)


def pending_items() -> list[Path]:
    if not INBOX.exists():
        return []
    materialize_icloud_placeholders()
    # Any visible file at the top level counts (the phone writes .json; be forgiving about the rest).
    items = [p for p in INBOX.iterdir() if p.is_file() and not p.name.startswith(".")]
    return sorted(items, key=sent_order)


def sent_order(path: Path) -> str:
    """Oldest first by when it was sent on the phone (not file time: retries rewrite the file).
    Phone files are named reel-yyyyMMdd-HHmmss.json, so the name is the fallback."""
    try:
        rec = json.loads(read_item(path))
        if isinstance(rec, dict) and rec.get("sent_at"):
            return dt.datetime.fromisoformat(str(rec["sent_at"])).astimezone(dt.timezone.utc).isoformat()
    except Exception:
        pass
    return dt.datetime.fromtimestamp(path.stat().st_mtime, dt.timezone.utc).isoformat()


def online() -> bool:
    """Right after the Mac wakes, Wi-Fi can lag. Don't burn retries on that: just wait for the next sweep."""
    import socket
    for host in ("www.instagram.com", "www.tiktok.com", "www.apple.com"):
        try:
            socket.create_connection((host, 443), timeout=5).close()
            return True
        except OSError:
            continue
    return False


def backing_off(path: Path) -> bool:
    """A failed item waits 5 min after its 1st failure, 10 after its 2nd, before it's tried again."""
    try:
        rec = json.loads(read_item(path))
    except Exception:
        return False
    if not isinstance(rec, dict) or rec.get("status") != "failed":
        return False
    wait = 300 * int(rec.get("attempts") or 1)
    return time.time() - float(rec.get("last_attempt") or 0) < wait


def slugify(text: str, n: int = 6) -> str:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return "-".join(words[:n]) or "reel"


# ---------------------------------------------------------------- media steps

def explain_download_error(err: str) -> str:
    e = err.lower()
    if "empty media response" in e or "login" in e or "cookies" in e or "private" in e:
        hint = ("Instagram/TikTok would not hand over this video without a login. Usually a private, "
                "deleted or age-restricted post, or a temporary block. Open the link in a browser to check. "
                "If it plays there while logged out, try again later; Reel Inbox can't fetch posts that need "
                "a login.")
    elif "unsupported url" in e:
        hint = "That link isn't a video yt-dlp knows how to download (photo post, profile or story link?)."
    elif "no video" in e or "no formats" in e:
        hint = "No video in this post (probably a photo or carousel post)."
    elif "unable to download" in e or "timed out" in e or "network" in e or "resolve" in e:
        hint = "Network problem while downloading; it will be retried."
    else:
        hint = "Download failed."
    return f"{hint} (yt-dlp said: {err[:300]})"


def download(url: str, work: Path) -> tuple[Path, dict]:
    try:
        _download(url, work)
    except RuntimeError as e:
        raise RuntimeError(explain_download_error(str(e))) from None
    vids = [p for p in work.glob("video.*") if p.suffix not in (".json", ".part")]
    if not vids:
        raise RuntimeError("No video in this post (probably a photo or carousel post).")
    info = {}
    info_path = work / "video.info.json"
    if info_path.exists():
        info = json.loads(info_path.read_text())
    return vids[0], info


def _download(url: str, work: Path) -> None:
    run([YTDLP, "--no-playlist", "--no-progress", "-f", "b[ext=mp4]/bv*+ba/b",
         "--merge-output-format", "mp4", "--write-info-json",
         "-o", str(work / "video.%(ext)s"), url], timeout=300)


def duration_of(video: Path) -> float:
    p = run([FFPROBE, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(video)])
    try:
        return float(p.stdout.strip())
    except ValueError:
        return 0.0


def has_audio(video: Path) -> bool:
    p = run([FFPROBE, "-v", "error", "-select_streams", "a", "-show_entries", "stream=codec_type",
             "-of", "csv=p=0", str(video)])
    return bool(p.stdout.strip())


def transcribe(video: Path, work: Path, prompt: str) -> list[dict]:
    if not has_audio(video):
        return []
    wav = work / "audio.wav"
    run([FFMPEG, "-v", "error", "-y", "-i", str(video), "-ar", "16000", "-ac", "1", str(wav)])
    cmd = [WHISPER, "-m", str(MODEL), "-f", str(wav), "-oj", "-of", str(work / "transcript"),
           "-np", "-l", "auto", "--suppress-nst"]
    if prompt:
        cmd += ["--prompt", prompt[:400]]
    run(cmd, timeout=900)
    data = json.loads((work / "transcript.json").read_text(errors="replace"))
    segs = []
    for s in data.get("transcription", []):
        text = s.get("text", "").strip()
        if not text:
            continue
        segs.append({"start": s["offsets"]["from"] / 1000, "end": s["offsets"]["to"] / 1000, "text": text})
    return segs


def extract_candidates(video: Path, work: Path, duration: float) -> list[tuple[Path, float]]:
    """Scene-change frames plus a steady sample (every ~1.5s) so slow screen recordings aren't missed."""
    cand = work / "cand"
    cand.mkdir()
    probe = run([FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=avg_frame_rate",
                 "-of", "csv=p=0", str(video)]).stdout.strip()
    try:
        num, den = probe.split("/")
        fps = float(num) / float(den)
    except Exception:
        fps = 30.0
    step = max(1, round(fps * (1.5 if duration < 120 else duration / 80)))
    vf = f"select='gt(scene,0.25)+not(mod(n\\,{step}))',showinfo"
    p = subprocess.run([FFMPEG, "-v", "info", "-i", str(video), "-vf", vf, "-fps_mode", "vfr",
                        "-q:v", "2", str(cand / "c_%05d.jpg")], capture_output=True, text=True, timeout=600)
    if p.returncode != 0:
        raise RuntimeError("ffmpeg frame extraction failed: " + p.stderr.strip().splitlines()[-1])
    times = [float(m) for m in re.findall(r"pts_time:([0-9.]+)", p.stderr)]
    files = sorted(cand.glob("c_*.jpg"))
    return list(zip(files, times + [0.0] * (len(files) - len(times))))


def vision(paths: list[Path]) -> dict:
    p = run([str(REELVISION)] + [str(x) for x in paths], timeout=600)
    return json.loads(p.stdout)


def pick_frames(cands: list[tuple[Path, float]], vis: dict, cap: int = MAX_FRAMES) -> list[int]:
    """Cover the whole video, favouring frames with readable text (code, terminals, repo pages).

    1. Drop blank/fade frames (tiny JPEGs with no text).
    2. Split the timeline into equal windows (about one per 3s, at most cap-2) and take the
       wordiest frame in each window (ties: the most detailed image).
    3. Spend the last slots on the wordiest remaining frames anywhere (two code screens in a row).
    4. Never keep two frames that look alike; keep the wordier of the pair.
    """
    n = len(cands)
    if n == 0:
        return []
    dist = vis["distance"]
    chars = [im["chars"] for im in vis["images"]]
    size = [c[0].stat().st_size for c in cands]
    times = [c[1] for c in cands]
    med = sorted(size)[n // 2]
    usable = [i for i in range(n) if chars[i] > 40 or size[i] > 0.35 * med] or list(range(n))
    score = lambda i: (chars[i], size[i])  # noqa: E731
    alike = 0.6

    def add(i: int, kept: list[int]) -> bool:
        twins = [k for k in kept if dist[i][k] < alike]
        if not twins:
            kept.append(i)
            return True
        k = twins[0]
        if chars[i] > chars[k] * 1.5 + 30:  # same scene, but this one shows more text
            kept[kept.index(k)] = i
        return False

    span = (max(times) - min(times)) or 1.0
    windows = max(1, min(cap - 2, round(span / 3) + 1))
    kept: list[int] = []
    t0 = min(times)
    for w in range(windows):
        lo, hi = t0 + span * w / windows, t0 + span * (w + 1) / windows
        inside = [i for i in usable if lo <= times[i] <= hi]
        for i in sorted(inside, key=score, reverse=True):
            if add(i, kept):
                break
    for i in sorted(usable, key=score, reverse=True):
        if len(kept) >= cap or chars[i] < 80:
            break
        if i not in kept:
            add(i, kept)
    return sorted(set(kept))[:cap]


# ---------------------------------------------------------------- OCR cleanup

def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def clean_ocr(frames_text: list[str], transcript: str) -> list[list[str]]:
    """Per frame: drop burned-in subtitle lines (short lines that are just the spoken words) and lines
    already shown on an earlier frame, so the OCR section is mostly new on-screen text and code."""
    spoken = " " + _norm(transcript) + " "
    seen: set[str] = set()
    out = []
    for text in frames_text:
        lines = []
        for line in text.splitlines():
            n = _norm(line)
            if len(n.replace(" ", "")) <= 2:  # stray letters (on-screen keyboards, icons)
                continue
            if len(n.split()) <= 7 and f" {n} " in spoken:
                continue
            if n in seen:
                continue
            seen.add(n)
            lines.append(line)
        out.append(lines)
    return out


# ---------------------------------------------------------------- output

def fmt_t(sec: float) -> str:
    return f"{int(sec // 60)}:{int(sec % 60):02d}"


def write_reel_md(out: Path, meta: dict, segs: list[dict]) -> None:
    L = [f"# Reel: {meta.get('title') or meta['id']}", ""]
    L += [f"- **Sender's note:** {meta['note'] or '(no note)'}",
          f"- **URL:** {meta['url']}",
          f"- **Sent:** {meta.get('sent_at') or '?'}  ·  **Processed:** {meta.get('processed_at')}",
          f"- **Creator:** {meta.get('uploader') or '?'}  ·  **Length:** {fmt_t(meta.get('duration') or 0)}",
          f"- **Status:** {meta['status']}" + (f" — {meta['error']}" if meta.get("error") else ""), ""]
    if meta.get("caption"):
        L += ["## Post caption", "", meta["caption"].strip(), ""]
    L += ["## Transcript", ""]
    if segs:
        L += [f"[{fmt_t(s['start'])}] {s['text']}" for s in segs]
    else:
        L.append("(no speech found)" if meta["status"] == "ok" else "(not available)")
    L.append("")
    if meta.get("frames"):
        L += ["## Frames and on-screen text", "",
              "Subtitle lines that just repeat the speech, and lines already shown on an earlier frame, "
              "are left out.", ""]
        for f in meta["frames"]:
            L.append(f"### {f['file']} at {fmt_t(f['t'])}")
            if f["ocr"]:
                L += ["```", *f["ocr"], "```"]
            else:
                L.append("(no new text)")
            L.append("")
    (out / "reel.md").write_text("\n".join(L))


def process_url(url: str, note: str, sent_at: str = "", source: dict | str | None = None,
                rid: str | None = None) -> Path:
    """rid reuses an earlier failed attempt's folder, so retries don't pile up duplicates."""
    now = dt.datetime.now()
    rid = rid or f"{now:%Y-%m-%d-%H%M%S}-{slugify(note or url)}"
    out = OUT_ROOT / rid
    (out / "frames").mkdir(parents=True, exist_ok=True)
    for old in (out / "frames").glob("*.jpg"):
        old.unlink()
    meta: dict = {"id": rid, "url": url, "note": note, "sent_at": sent_at,
                  "processed_at": now.isoformat(timespec="seconds"), "status": "processing",
                  "error": None, "done": False, "frames": []}
    if source is not None:
        (out / "source.json").write_text(json.dumps(source, indent=2) if isinstance(source, dict) else source)
    segs: list[dict] = []
    t0 = time.time()
    with tempfile.TemporaryDirectory(prefix="reelinbox-") as tmp:
        work = Path(tmp)
        try:
            video, info = download(url, work)
            meta["uploader"] = info.get("channel") or info.get("uploader")
            meta["caption"] = info.get("description") or ""
            meta["title"] = (meta["caption"].split("\n")[0][:80] if meta["caption"] else None) or info.get("title")
            # No note: the folder was named after the link. Name it after the post instead.
            url_slug = slugify(url)
            better = slugify(meta["title"] or meta["uploader"] or "")
            if not note and out.name.endswith(url_slug) and better != "reel":
                renamed = out.with_name(out.name[: -len(url_slug)] + better)
                if not renamed.exists():
                    out = out.rename(renamed)
                    rid = meta["id"] = out.name
            duration = duration_of(video)
            meta["duration"] = duration
            prompt = " ".join(filter(None, [meta.get("uploader"), meta["caption"][:300]]))
            segs = transcribe(video, work, prompt)
            transcript = " ".join(s["text"] for s in segs)
            cands = extract_candidates(video, work, duration)
            if cands:
                vis = vision([c[0] for c in cands])
                kept = pick_frames(cands, vis)
                ocr = clean_ocr([vis["images"][i]["text"] for i in kept], transcript)
                for n, (i, lines) in enumerate(zip(kept, ocr), 1):
                    name = f"{n:02d}_{fmt_t(cands[i][1]).replace(':', 'm')}s.jpg"
                    shutil.copy(cands[i][0], out / "frames" / name)
                    meta["frames"].append({"file": f"frames/{name}", "t": round(cands[i][1], 2), "ocr": lines})
            meta["status"] = "ok"
            meta["transcript"] = segs
        except Exception as e:  # noqa: BLE001 — every failure is written into the output, never swallowed
            meta["status"] = "failed"
            meta["error"] = str(e)[:800]
            log(f"FAILED {url}: {e}\n{traceback.format_exc()}")
    meta["seconds"] = round(time.time() - t0, 1)
    (out / "meta.json").write_text(json.dumps(meta, indent=2))
    write_reel_md(out, meta, segs)
    log(f"{meta['status'].upper()} {rid} ({meta['seconds']}s, {len(meta['frames'])} frames)")
    return out


def handle_item(path: Path) -> None:
    raw = read_item(path)
    item = parse_item(raw)
    if not item["url"]:
        err = "No link found in what the Shortcut sent."
        out = failed_stub(item, err)
        item_fail(path, item, err, output=str(out), final=True)
        return
    prev = item["raw"].get("output") if isinstance(item["raw"], dict) else None
    rid = Path(prev).name if prev and Path(prev).is_dir() else None
    out = process_url(item["url"], item["note"], item["sent_at"], item["raw"], rid=rid)
    meta = json.loads((out / "meta.json").read_text())
    if meta["status"] == "ok":
        record = dict(item["raw"]) if isinstance(item["raw"], dict) else {"text": item["raw"]}
        record.update({"status": "ok", "output": str(out), "processed_at": meta["processed_at"]})
        move_to(path, "done", record)
    else:
        item_fail(path, item, meta["error"], output=str(out))


def failed_stub(item: dict, error: str) -> Path:
    """An output folder for an item that never got as far as downloading, so list_reels shows it."""
    now = dt.datetime.now()
    rid = f"{now:%Y-%m-%d-%H%M%S}-{slugify(item['note'] or 'no-link')}"
    out = OUT_ROOT / rid
    out.mkdir(parents=True, exist_ok=True)
    meta = {"id": rid, "url": item["url"] or "(none)", "note": item["note"], "sent_at": item["sent_at"],
            "processed_at": now.isoformat(timespec="seconds"), "status": "failed", "error": error,
            "done": False, "frames": []}
    (out / "source.json").write_text(json.dumps(item["raw"], indent=2) if isinstance(item["raw"], dict)
                                     else str(item["raw"]))
    (out / "meta.json").write_text(json.dumps(meta, indent=2))
    write_reel_md(out, meta, [])
    log(f"FAILED {rid}: {error}")
    return out


def item_fail(path: Path, item: dict, error: str, output: str | None = None, final: bool = False) -> None:
    record = dict(item["raw"]) if isinstance(item["raw"], dict) else {"text": item["raw"]}
    record["attempts"] = item["attempts"] + 1
    record["status"] = "failed"
    record["error"] = error
    record["last_attempt"] = time.time()
    if output:
        record["output"] = output
    if final or record["attempts"] >= MAX_ATTEMPTS:
        move_to(path, "failed", record)
    else:
        path.write_text(json.dumps(record, indent=2))  # stays in the inbox; retried on the next run


def move_to(path: Path, sub: str, record: dict) -> None:
    dest_dir = INBOX / sub
    dest_dir.mkdir(exist_ok=True)
    dest = dest_dir / (path.stem + ".json")
    dest.write_text(json.dumps(record, indent=2))
    path.unlink()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url")
    ap.add_argument("--note", default="")
    args = ap.parse_args()
    SUPPORT.mkdir(parents=True, exist_ok=True)
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    if args.url:
        print(process_url(args.url, args.note, dt.datetime.now().isoformat(timespec="seconds")))
        return 0
    lock = open(SUPPORT / "worker.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return 0  # another run is already working through the inbox
    INBOX.mkdir(parents=True, exist_ok=True)
    if pending_items() and not online():
        log("offline; leaving the inbox for the next sweep")
        return 0
    tried: set[Path] = set()
    while True:
        items = [p for p in pending_items() if p not in tried and not backing_off(p)]
        if not items:
            break
        for p in items:
            tried.add(p)
            try:
                handle_item(p)
            except Exception as e:  # noqa: BLE001
                log(f"ERROR on {p.name}: {e}\n{traceback.format_exc()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
