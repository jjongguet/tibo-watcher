import contextlib
import fcntl
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError

import tibo_watcher as watcher


API = json.dumps(
    {
        "events": [
            {
                "tweet_id": "observed-101",
                "tweet_url": "https://x.com/thsottiaux/status/101",
                "text": "Original announcement",
                "announced_at": "2026-10-23T17:00:00.000Z",
                "reset_type": "regular",
            }
        ],
        "scheduled": {
            "tweet_id": "102",
            "text": "By midnight",
            "announced_at": "2026-10-25T03:40:04Z",
            "scheduled_for": "2026-10-25T07:00:00Z",
        },
    }
)
RSS = """<rss><channel>
<item><guid>x:101</guid><title>[planned] Tomorrow</title><description>RSS announcement</description><pubDate>Sun, 25 Oct 2026 03:40:04 GMT</pubDate></item>
<item><guid>status:outage</guid><title>[completed] Incident resolved</title></item>
<item><guid>x:999</guid><title>[note] Unrelated chatter</title></item>
<item><guid>x:103</guid><title>[banked] Gift</title><pubDate>invalid date</pubDate></item>
</channel></rss>"""
ATOM = """<feed xmlns="http://www.w3.org/2005/Atom">
<entry><id>https://tiboreset.com/r/101</id><link rel="via" href="https://x.com/thsottiaux/status/101"/><category term="reset"/><summary>Atom summary</summary><published>2026-10-23T17:00:00Z</published><updated>2026-10-25T17:00:00Z</updated></entry>
<entry><link rel="via" href="https://x.com/thsottiaux/status/104"/><category term="announce"/><title>Upcoming</title><updated>2026-10-25T05:00:00Z</updated></entry>
<entry><link rel="via" href="https://x.com/thsottiaux/status/998"/><category term="related"/></entry>
<entry><link rel="via" href="https://evil.example/status/997"/><category term="reset"/></entry>
</feed>"""
PAYLOADS = dict(
    zip((watcher.API_URL, watcher.RSS_URL, watcher.ATOM_URL), (API, RSS, ATOM))
)


def event(tid, body="Synthetic test notice"):
    return watcher.Event(
        str(tid), body, "2026-10-25T03:00:00Z", "planned", ("tibo.watch",)
    )


class ParserTests(unittest.TestCase):
    def test_api_includes_scheduled_without_calling_it_completed(self):
        result = watcher.parse_api(API)
        self.assertEqual(set(result), {"101", "102"})
        self.assertEqual(result["102"].kind, "planned")
        self.assertEqual(result["102"].ts, "2026-10-25T03:40:04Z")

    def test_api_observed_alias_uses_canonical_tweet(self):
        result = watcher.parse_api(API)
        self.assertEqual(result["101"].url, "https://x.com/thsottiaux/status/101")
        self.assertEqual(result["101"].ts, "2026-10-23T17:00:00Z")

    def test_rss_filters_noise_and_keeps_unknown_time_unknown(self):
        result = watcher.parse_rss(RSS)
        self.assertEqual(set(result), {"101", "103"})
        self.assertEqual(result["101"].kind, "planned")
        self.assertEqual(result["103"].kind, "banked")
        self.assertEqual(result["103"].ts, "")

    def test_atom_filters_noise_and_prefers_publication_not_edit_time(self):
        result = watcher.parse_atom(ATOM)
        self.assertEqual(set(result), {"101", "104"})
        self.assertEqual(result["101"].ts, "2026-10-23T17:00:00Z")
        self.assertEqual(result["104"].kind, "planned")
        self.assertEqual(result["104"].text, "Upcoming")

    def test_structurally_wrong_sources_are_not_empty_successes(self):
        cases = (
            (watcher.parse_api, "{}"),
            (watcher.parse_api, '{"events":[null]}'),
            (watcher.parse_api, '{"events":[],"scheduled":[]}'),
            (watcher.parse_rss, "<html/>"),
            (watcher.parse_atom, "<rss/>"),
        )
        for parser, raw in cases:
            with (
                self.subTest(parser=parser.__name__, raw=raw),
                self.assertRaises(ValueError),
            ):
                parser(raw)

    def test_untrusted_links_cannot_become_output_links(self):
        self.assertEqual(
            watcher.tweet_id("", "https://x.com.evil.example/thsottiaux/status/1"), ""
        )
        self.assertEqual(watcher.tweet_id("", "https://x.com/other/status/1"), "")
        self.assertEqual(
            watcher.tweet_id("observed-1", "https://x.com/thsottiaux/status/1"), "1"
        )

    def test_timestamp_normalizes_offsets_but_does_not_guess_naive_time(self):
        self.assertEqual(
            watcher.timestamp("2026-10-25T12:00:00+09:00"), "2026-10-25T03:00:00Z"
        )
        self.assertEqual(watcher.timestamp("2026-10-25T12:00:00"), "")
        self.assertEqual(watcher.timestamp(None), "")


class CollectionTests(unittest.TestCase):
    def test_three_sources_merge_ids_text_priority_and_category_priority(self):
        with patch.object(watcher, "fetch", side_effect=PAYLOADS.__getitem__) as fetch:
            result, reports = watcher.collect()
        self.assertEqual(fetch.call_count, 3)
        self.assertEqual(set(result), {"101", "102", "103", "104"})
        self.assertEqual(result["101"].text, "Original announcement")
        self.assertEqual(result["101"].kind, "planned")
        self.assertEqual(
            result["101"].sources, ("codex-resets.com", "tibo.watch", "tiboreset.com")
        )
        self.assertTrue(all(report["error"] is None for report in reports.values()))

    def test_one_source_failure_does_not_discard_other_feeds(self):
        def fetch(url):
            if url == watcher.API_URL:
                raise URLError("offline fixture")
            return PAYLOADS[url]

        with patch.object(watcher, "fetch", side_effect=fetch):
            result, reports = watcher.collect()
        self.assertEqual(result["101"].text, "RSS announcement")
        self.assertIn("offline fixture", reports["codex-resets.com"]["error"])
        self.assertIsNone(reports["codex-resets.com"]["count"])

    def test_invalid_xml_is_reported_without_aborting_collection(self):
        payloads = {**PAYLOADS, watcher.RSS_URL: "<broken"}
        with patch.object(watcher, "fetch", side_effect=payloads.__getitem__):
            result, reports = watcher.collect()
        self.assertIn("102", result)
        self.assertIn("ParseError", reports["tibo.watch"]["error"])

    def test_fetch_retry_is_bounded(self):
        with (
            patch.object(
                watcher.urllib.request, "urlopen", side_effect=URLError("offline")
            ) as request,
            patch.object(watcher.time, "sleep"),
        ):
            with self.assertRaises(URLError):
                watcher.fetch(watcher.API_URL)
        self.assertEqual(request.call_count, 2)

    def test_fetch_enforces_response_size(self):
        with (
            patch.object(watcher, "MAX_RESPONSE_BYTES", 3),
            patch.object(
                watcher.urllib.request, "urlopen", return_value=io.BytesIO(b"1234")
            ),
        ):
            with self.assertRaisesRegex(ValueError, "exceeds"):
                watcher.fetch(watcher.API_URL)


class StateAndCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "state"
        self.path = self.directory / "seen.json"
        self.events = {"101": event(101)}
        self.reports = {
            name: {"count": 1, "error": None} for name, _, _ in watcher.SOURCES
        }

    def invoke(self, *args, events=None, reports=None):
        out, err = io.StringIO(), io.StringIO()
        with (
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
            patch.object(
                watcher,
                "collect",
                return_value=(
                    self.events if events is None else events,
                    self.reports if reports is None else reports,
                ),
            ),
        ):
            code = watcher.main(["--state-dir", str(self.directory), *args])
        return code, out.getvalue(), err.getvalue()

    def seed(self, ids):
        self.directory.mkdir(exist_ok=True)
        watcher.save_state(self.path, set(ids))

    def test_first_run_baselines_and_repeat_is_silent(self):
        self.assertEqual(self.invoke(), (0, "", ""))
        self.assertEqual(watcher.load_state(self.path), {"101"})
        before = self.path.read_bytes()
        self.assertEqual(self.invoke(), (0, "", ""))
        self.assertEqual(self.path.read_bytes(), before)

    def test_new_event_emits_once(self):
        self.seed({"100"})
        code, out, err = self.invoke()
        self.assertEqual(code, 0)
        self.assertIn("/status/101", out)
        self.assertEqual(err, "")
        self.assertEqual(self.invoke(), (0, "", ""))

    def test_test_and_status_do_not_create_directory(self):
        code, out, _ = self.invoke("--test")
        self.assertEqual(code, 0)
        self.assertIn("TEST ONLY", out)
        code, out, _ = self.invoke("--status")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["fetched"], 1)
        self.assertFalse(self.directory.exists())

    def test_preview_does_not_consume_new_items(self):
        self.seed({"100"})
        before = self.path.read_bytes()
        self.invoke("--test")
        self.invoke("--status")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertIn("/status/101", self.invoke()[1])

    def test_all_sources_empty_or_failed_leave_state_unchanged(self):
        self.seed({"100"})
        before = self.path.read_bytes()
        code, out, err = self.invoke(events={})
        self.assertEqual((code, out), (1, ""))
        self.assertIn("state unchanged", err)
        self.assertEqual(self.path.read_bytes(), before)

    def test_initial_partial_failure_does_not_create_baseline(self):
        reports = {**self.reports, "tibo.watch": {"count": None, "error": "offline"}}
        code, out, err = self.invoke(reports=reports)
        self.assertEqual((code, out), (1, ""))
        self.assertIn("first baseline requires all sources", err)
        self.assertFalse(self.path.exists())

    def test_established_state_keeps_working_during_partial_failure(self):
        self.seed({"100"})
        reports = {**self.reports, "tibo.watch": {"count": None, "error": "offline"}}
        code, out, err = self.invoke(reports=reports)
        self.assertEqual(code, 0)
        self.assertIn("/status/101", out)
        self.assertIn("warning: tibo.watch", err)

    def test_corrupt_state_is_not_silently_reset(self):
        self.directory.mkdir()
        for broken in (
            "{",
            '{"version":1,"ids":"101"}',
            '{"version":true,"ids":[]}',
            '{"version":1,"ids":[null]}',
        ):
            with self.subTest(broken=broken):
                self.path.write_text(broken)
                code, out, err = self.invoke()
                self.assertEqual((code, out), (1, ""))
                self.assertTrue(err)
                self.assertEqual(self.path.read_text(), broken)

    def test_failed_atomic_replace_preserves_previous_state(self):
        self.seed({"100"})
        before = self.path.read_bytes()
        with patch.object(
            watcher.os, "replace", side_effect=OSError("fixture disk failure")
        ):
            code, out, err = self.invoke()
        self.assertEqual((code, out), (1, ""))
        self.assertIn("fixture disk failure", err)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(
            {p.name for p in self.directory.iterdir()}, {"seen.json", "run.lock"}
        )

    def test_overlapping_run_fails_without_fetching(self):
        self.directory.mkdir()
        with (self.directory / "run.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            code, out, err = self.invoke()
        self.assertEqual((code, out), (1, ""))
        self.assertIn("another run", err)
        self.assertFalse(self.path.exists())

    def test_message_budget_leaves_unprinted_items_pending(self):
        self.seed(set())
        events = {str(i): event(i, "Long message " * 80) for i in range(101, 108)}
        emitted = set()
        for _ in range(7):
            code, out, err = self.invoke(events=events)
            self.assertEqual((code, err), (0, ""))
            self.assertLessEqual(len(out.rstrip("\n").encode("utf-16-le")) // 2, 1900)
            current = watcher.load_state(self.path)
            new = current - emitted
            for tid in new:
                self.assertIn(f"/status/{tid}", out)
            emitted = current
        self.assertEqual(emitted, set(events))
        self.assertEqual(self.invoke(events=events), (0, "", ""))

    def test_render_neutralizes_mentions_and_keeps_posted_time_explicit(self):
        rendered = watcher.render(event(101, "@everyone <@123>\nTest"))
        self.assertNotIn("@", rendered)
        self.assertIn("Posted: 2026-10-25T03:00:00Z", rendered)
        self.assertIn("(feed label)", rendered)

    def test_installed_cli_rejects_unknown_arguments_without_network(self):
        result = subprocess.run(
            [
                sys.executable,
                str(Path(watcher.__file__).resolve()),
                "--not-a-real-option",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("unrecognized arguments", result.stderr)
        self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
