#!/usr/bin/env python3
"""Merge public Tibo reset feeds. No credentials, LLM, scheduler or sender."""

import argparse
import fcntl
import html
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET

API_URL = "https://codex-resets.com/api/resets"
RSS_URL = "https://tibo.watch/feed.xml"
ATOM_URL = "https://tiboreset.com/feed.xml"
ATOM = "{http://www.w3.org/2005/Atom}"
KIND_PRIORITY = {"planned": 4, "completed": 3, "banked": 2, "regular": 1}
LABELS = {
    "planned": "Planned reset",
    "completed": "Reset reported",
    "banked": "Banked reset notice",
    "regular": "Reset notice",
}
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_MESSAGE_UNITS = 1900


@dataclass(frozen=True)
class Event:
    id: str
    text: str
    ts: str
    kind: str
    sources: tuple[str, ...] = ()

    @property
    def url(self):
        return f"https://x.com/thsottiaux/status/{self.id}"


def text(value):
    return value.strip() if isinstance(value, str) else ""


def tweet_id(raw_id, url=""):
    """Normalize synthetic observed-* API IDs using the linked tweet."""
    parsed = urlsplit(text(url))
    if parsed.scheme == "https" and parsed.netloc in (
        "x.com",
        "twitter.com",
        "www.twitter.com",
    ):
        match = re.fullmatch(r"/thsottiaux/status/([0-9]{1,30})/?", parsed.path)
        if match:
            return match.group(1)
    raw = str(raw_id) if isinstance(raw_id, (str, int)) else ""
    return raw if re.fullmatch(r"[0-9]{1,30}", raw) else ""


def timestamp(value, rss=False):
    if not text(value):
        return ""
    try:
        dt = (
            parsedate_to_datetime(value)
            if rss
            else datetime.fromisoformat(value.replace("Z", "+00:00"))
        )
        if dt.tzinfo is None:
            return ""  # Do not guess a timezone.
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, TypeError, OverflowError):
        return ""


def parse_api(raw):
    data = json.loads(raw)
    if not isinstance(data, dict) or not isinstance(data.get("events"), list):
        raise ValueError("expected an object with an events array")
    rows = [(row, False) for row in data["events"]]
    scheduled = data.get("scheduled")
    if scheduled is not None:
        if not isinstance(scheduled, dict):
            raise ValueError("scheduled must be an object or null")
        rows.append((scheduled, True))
    result = {}
    for row, planned in rows:
        if not isinstance(row, dict):
            raise ValueError("event must be an object")
        tid = tweet_id(row.get("tweet_id"), row.get("tweet_url"))
        if not tid:
            continue
        kind = "planned" if planned else text(row.get("reset_type"))
        if kind not in KIND_PRIORITY:
            kind = "regular"
        body = text(row.get("text")) or text(row.get("display_text"))
        body = re.sub(r"\s*https://t\.co/\S+$", "", body)
        result[tid] = Event(tid, body, timestamp(row.get("announced_at")), kind)
    return result


def parse_rss(raw):
    root = ET.fromstring(raw)
    if root.tag != "rss" or root.find("channel") is None:
        raise ValueError("expected RSS with a channel")
    result = {}
    for item in root.findall("channel/item"):
        match = re.fullmatch(r"x:([0-9]{1,30})", text(item.findtext("guid")))
        if not match:
            continue  # Excludes OpenAI incident/status items.
        title = text(item.findtext("title"))
        category = re.match(
            r"^\[(planned|completed|banked)\]\s*(.*)$", title, re.DOTALL
        )
        if not category:
            continue
        tid = match.group(1)
        body = text(item.findtext("description")) or category.group(2)
        result[tid] = Event(
            tid,
            html.unescape(body),
            timestamp(item.findtext("pubDate"), rss=True),
            category.group(1),
        )
    return result


def parse_atom(raw):
    root = ET.fromstring(raw)
    if root.tag != ATOM + "feed":
        raise ValueError("expected Atom feed")
    kinds = {"announce": "planned", "reset": "completed", "banked": "banked"}
    result = {}
    for entry in root.findall(ATOM + "entry"):
        category = entry.find(ATOM + "category")
        kind = kinds.get(category.get("term")) if category is not None else None
        if not kind:
            continue
        via = next(
            (
                link.get("href")
                for link in entry.findall(ATOM + "link")
                if link.get("rel") == "via"
            ),
            "",
        )
        tid = tweet_id("", via)
        if not tid:
            continue  # Cannot deduplicate an entry without a canonical tweet.
        body = text(entry.findtext(ATOM + "summary")) or text(
            entry.findtext(ATOM + "title")
        )
        published = entry.findtext(ATOM + "published") or entry.findtext(
            ATOM + "updated"
        )
        result[tid] = Event(tid, html.unescape(body), timestamp(published), kind)
    return result


SOURCES = (
    ("codex-resets.com", API_URL, parse_api),
    ("tibo.watch", RSS_URL, parse_rss),
    ("tiboreset.com", ATOM_URL, parse_atom),
)


def fetch(url):
    for attempt in range(2):
        try:
            request = urllib.request.Request(
                url, headers={"User-Agent": "tibo-watcher/1.0", "Accept": "*/*"}
            )
            with urllib.request.urlopen(request, timeout=10) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise ValueError("feed exceeds 2 MiB")
            return raw.decode("utf-8")
        except (urllib.error.URLError, TimeoutError, OSError):
            if attempt:
                raise
            time.sleep(1)


def collect():
    events, reports = {}, {}
    for name, url, parser in SOURCES:
        try:
            parsed = parser(fetch(url))
            reports[name] = {"count": len(parsed), "error": None}
            for tid, incoming in parsed.items():
                current = events.get(tid)
                if current is None:
                    events[tid] = replace(incoming, sources=(name,))
                    continue
                kind = max((current.kind, incoming.kind), key=KIND_PRIORITY.__getitem__)
                events[tid] = replace(
                    current,
                    text=current.text or incoming.text,
                    ts=current.ts or incoming.ts,
                    kind=kind,
                    sources=current.sources + (name,),
                )
        except (ValueError, ET.ParseError, urllib.error.URLError, OSError) as exc:
            reports[name] = {"count": None, "error": f"{type(exc).__name__}: {exc}"}
    return events, reports


def load_state(path):
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    if (
        not isinstance(state, dict)
        or type(state.get("version")) is not int
        or state["version"] != 1
        or not isinstance(state.get("ids"), list)
        or any(
            not isinstance(tid, str) or not re.fullmatch(r"[0-9]{1,30}", tid)
            for tid in state["ids"]
        )
    ):
        raise ValueError(
            "invalid state; preserve the file and investigate instead of resetting it"
        )
    return set(state["ids"])


def save_state(path, ids):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as handle:
            temporary = Path(handle.name)
            json.dump({"version": 1, "ids": sorted(ids)}, handle)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def render(event):
    # Feed text is untrusted. Neutralize mentions and keep it inside a quote.
    body = " ".join((event.text or "(no text)").split()).replace("@", "＠")
    if len(body) > 500:
        body = body[:499] + "…"
    when = event.ts or "unknown"
    return (
        f"{LABELS[event.kind]} (feed label) | Posted: {when}\n"
        f"> {body}\n<{event.url}>\n"
        f"Sources: {', '.join(event.sources)}"
    )


def message_batch(events):
    parts, shown = [], []
    for event in sorted(events, key=lambda item: (item.ts, item.id)):
        if len(shown) == 5:
            break
        candidate = "\n\n".join(parts + [render(event)])
        if len(candidate.encode("utf-16-le")) // 2 > MAX_MESSAGE_UNITS:
            break
        parts.append(render(event))
        shown.append(event.id)
    return "\n\n".join(parts), shown


def run(args, path):
    seen = load_state(path) if not args.test else None
    events, reports = collect()
    for name, report in reports.items():
        if report["error"]:
            print(f"warning: {name}: {report['error']}", file=sys.stderr)
    if args.status:
        latest = max(
            events.values(), key=lambda event: (event.ts, event.id), default=None
        )
        print(
            json.dumps(
                {
                    "state_path": str(path),
                    "initialized": seen is not None,
                    "seen": len(seen or ()),
                    "fetched": len(events),
                    "sources": reports,
                    "latest": asdict(latest) if latest else None,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0 if events else 1
    if not events:
        print("error: no usable reset announcements; state unchanged", file=sys.stderr)
        return 1
    if args.test:
        latest = max(events.values(), key=lambda event: (event.ts, event.id))
        print("TEST ONLY — not a new-event notification\n" + render(latest))
        return 0
    if seen is None:
        # A partial first baseline would later replay old posts from a recovered source.
        if any(report["error"] for report in reports.values()):
            print(
                "error: first baseline requires all sources; state unchanged",
                file=sys.stderr,
            )
            return 1
        save_state(path, set(events))
        return 0
    output, shown = message_batch(
        event for tid, event in events.items() if tid not in seen
    )
    if shown:
        # stdout is not a delivery acknowledgement. See README delivery limitations.
        save_state(path, seen | set(shown))
        print(output)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--status",
        action="store_true",
        help="fetch and print JSON diagnostics; no state writes",
    )
    modes.add_argument(
        "--test",
        action="store_true",
        help="preview the latest item; no state writes or sending",
    )
    default_dir = (
        os.environ.get("TIBO_WATCHER_STATE_DIR") or "~/.local/state/tibo-watcher"
    )
    parser.add_argument(
        "--state-dir",
        default=default_dir,
        help="persistent state directory (normal mode only writes)",
    )
    args = parser.parse_args(argv)
    path = Path(args.state_dir).expanduser().resolve() / "seen.json"
    try:
        if args.status or args.test:
            return run(args, path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (path.parent / "run.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print("error: another run holds this state directory", file=sys.stderr)
                return 1
            return run(args, path)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
