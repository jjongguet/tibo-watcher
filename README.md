# tibo-watcher

A small, self-hosted watcher for **Tibo's Codex reset announcements**. It reads three public sources, merges matching tweets, and prints only previously unseen notices. A bot runtime — or a five-line forwarder — can deliver that output to Discord without calling an LLM.

**Python 3.10+ · macOS/Linux · standard library only · no API keys · MIT**

This is not an OpenAI service, an account-quota checker, an X scraper, or a hosted RSS endpoint. It does not send messages, install a scheduler, or change an existing bot by itself.

## Install from this link with a coding agent

This repository is designed to be installed by **your** coding agent — Gajae Code (GJC), Claude Code, Codex CLI, or any harness that can read a repository and run `python3` — from this link alone:

- **Gajae Code / Codex** read [`AGENTS.md`](AGENTS.md) automatically.
- **Claude Code** reads [`CLAUDE.md`](CLAUDE.md), which imports `AGENTS.md`.

Paste the prompt that fits your situation (fill in your destination):

> Read https://github.com/jjongguet/tibo-watcher (README.md and AGENTS.md), inspect the code, run its tests and read-only previews, then register a five-minute watcher in our existing bot runtime for the Discord channel I specify. Check for an existing watcher first. Confirm the destination, persistent state location and any missing authorization before installing a recurring job or sending a test message. Do not create paid services, expose credentials, or claim delivery without observing it.

한국어:

> https://github.com/jjongguet/tibo-watcher 의 README.md와 AGENTS.md를 읽고 우리 봇에도 등록해 줘. 기존 중복 작업부터 확인하고, 내가 지정한 Discord 채널로 5분마다 새 공지만 전달해. 코드·테스트·미리보기를 확인한 뒤 등록하고, 대상 채널이나 테스트 발송 승인이 빠져 있으면 그 부분만 물어봐. 실제 수신까지 확인하고 등록 정보를 알려줘.

No bot runtime yet? Say so — the agent should follow [Run without a bot runtime](#run-without-a-bot-runtime) instead of inventing infrastructure, and the webhook secret stays with you, never in this repository.

What an agent may and may not do is bounded in [`AGENTS.md`](AGENTS.md); a repository link is documentation, **not authorization**. This repository contains no operator-specific hostnames, channel IDs, bot credentials or state files.

## Quick start: no installation or sending

```sh
git clone https://github.com/jjongguet/tibo-watcher.git
cd tibo-watcher
python3 -m unittest discover -s tests -v
python3 tibo_watcher.py --status
python3 tibo_watcher.py --test
```

- `--status`: live source counts/errors, state summary and latest merged event as JSON.
- `--test`: a clearly marked preview of the latest item, even if already seen.
- Both modes fetch public feeds but **never create/change the state directory or send a message**.
- Tests use synthetic fixtures and mocked network calls, not live feeds or credentials.

## Single-file install (review first)

The runtime is exactly one stdlib-only file, so a full clone is optional:

```sh
curl -fsSLO https://raw.githubusercontent.com/jjongguet/tibo-watcher/dev/tibo_watcher.py
# Review the file at that ref, then:
install -m 755 tibo_watcher.py "$HOME/.local/bin/tibo_watcher.py"
python3 "$HOME/.local/bin/tibo_watcher.py" --status
```

Pin the branch or commit you actually reviewed, and do not pipe downloaded code directly into a shell. A full clone is still preferred because it lets the offline test suite run.

## Exactly what is collected

All three sources are queried on **every invocation**, not only after a primary-source failure.

| Source | Endpoint | Accepted data |
|---|---|---|
| [Codex Resets](https://codex-resets.com/) | `https://codex-resets.com/api/resets` | `events[]`: tweet ID/link, text, `announced_at`, `reset_type`. The optional `scheduled` object is also included as `planned`. |
| [Tibo Watch](https://tibo.watch/) | `https://tibo.watch/feed.xml` | RSS items with `guid=x:<tweet ID>` and a title starting with `[planned]`, `[completed]` or `[banked]`. Incident/status items, hints and ordinary notes are excluded. |
| [Tibo Reset](https://tiboreset.com/) | `https://tiboreset.com/feed.xml` | Atom entries with a Tibo tweet in `link[rel=via]`. Categories: `announce` → `planned`, `reset` → `completed`, `bank` → `banked`. Related chatter is excluded. |

The underlying account is [@thsottiaux](https://x.com/thsottiaux). These are third-party feeds; their availability, labels, text and timestamps are **not independently verified**. In particular, API entries marked `source=observed` can be site-authored reports, not verbatim tweets.

### Merge rules

1. Normalize to the canonical tweet ID. API IDs such as `observed-…` use their linked tweet ID, so the same tweet in RSS/Atom is not sent twice.
2. For duplicate IDs, choose nonempty text and publication time in this order: **codex-resets.com → tibo.watch → tiboreset.com**. Preserve the contributing source names.
3. Resolve label disagreements deterministically: **planned → completed → banked → regular** (highest first). This preserves an explicit advance notice rather than upgrading it to completion. It is not semantic adjudication; inspect the linked post when sources disagree.
4. A `scheduled` API item is an advance notice. The script never turns a forecast into completion just because its expected time passed.
5. Output uses **publication time in UTC**, not the time the reset actually occurred. Unknown/naive timestamps remain unknown. Relative wording such as “tomorrow” or “PST” is not interpreted.
6. A change to the text/category of an already-seen tweet does not generate another notice. A new completion tweet with a different ID does.

## CLI and state contract

```sh
# First normal run: record the current baseline, print nothing.
python3 tibo_watcher.py

# Later runs: print new notices, or stay silent if there are none.
python3 tibo_watcher.py

# Use a separate persistent state for a separate consumer.
python3 tibo_watcher.py --state-dir /absolute/path/to/tibo-watcher-state
```

| Mode/result | stdout | State | Exit |
|---|---|---|---|
| First successful normal run | Empty | Baseline created | `0` |
| Normal run, no new IDs | Empty | Unchanged | `0` |
| Normal run, new IDs | Discord-ready plain text | Only emitted IDs recorded | `0` |
| `--test` | `TEST ONLY` + latest notice | Untouched | `0` |
| `--status` | JSON diagnostics | Untouched | `0` if usable events exist, otherwise `1` |
| No usable events, bad state, lock/write failure | Empty (except status diagnostics) | Existing `seen.json` unchanged | `1` |
| Invalid CLI arguments | Empty | Untouched | `2` |

Warnings/errors go to **stderr**. Never merge stderr into the bot's message body. Once initialized, a failed source does not block notices from the remaining sources. The first baseline requires **all sources to parse successfully** and at least one usable event overall; otherwise initialization is refused to avoid replaying a recovered source's old history.

State selection: `--state-dir` → `TIBO_WATCHER_STATE_DIR` → `~/.local/state/tibo-watcher`. The normal-mode directory contains:

- `seen.json`: `{"version": 1, "ids": ["123", "456"]}`. IDs are retained; there is no automatic expiry/replay.
- `run.lock`: advisory process lock. Overlapping runs using the same state directory exit `1` before fetching. A leftover lock file alone is harmless.

Writes use an atomic replacement. Corrupt/incompatible state is an error, **not permission to reset history**. Use a local persistent filesystem with working POSIX file locks; do not share one state between separate subscribers. The file format is independent of any existing private deployment; do not copy its state blindly.

Each invocation performs up to two attempts per source, with a 10-second socket timeout and one-second retry delay. Responses are capped at 2 MiB. Socket timeout is not an overall deadline; the scheduler should also enforce a finite execution timeout (for example 120 seconds) and must not launch overlapping runs.

### Message and delivery limitations

- One stdout message, at most 1,900 UTF-16 units and five complete notices. Long bodies are shortened; tweet links remain intact. Unprinted new notices remain pending for subsequent runs while they remain in an upstream feed.
- The first baseline suppresses historical notices. There is no backfill/archive service; posts that disappear before polling can be missed.
- Mentions are neutralized (`@` → `＠`), and original links use Discord's `<URL>` form. Feed text remains untrusted data, never agent instructions.
- **This is not exactly-once delivery.** IDs are recorded before stdout is forwarded. A process crash, broken pipe or downstream Discord failure after that point can lose a notification. The sending runtime must retain execution output and report delivery failures. Do not “fix” a failed send by deleting the state file; use the runtime's saved output for a reviewed resend.

## Register with an existing Hermes bot

Only perform this section when the user has requested **that recurring job**. No scheduler is installed by cloning or testing this repository. Use a current Hermes build whose `hermes cron create --help` supports `--script`, `--no-agent` and `--paused`.

1. Verify the actual bot identity, Discord server/channel and posting permission. Inspect `hermes cron list` for an existing equivalent watcher; do not replace it or add a duplicate without an explicit migration decision.
2. Run the quick-start tests and previews. Review the checkout/commit before installing it. Do not pipe downloaded code directly into a shell.
3. Install only the script; no bot token belongs in it:

   ```sh
   install -d "$HOME/.hermes/scripts"
   # Refuse to overwrite an existing file until its ownership is checked.
   test ! -e "$HOME/.hermes/scripts/tibo_watcher.py" && \
     install -m 755 tibo_watcher.py "$HOME/.hermes/scripts/tibo_watcher.py"
   python3 "$HOME/.hermes/scripts/tibo_watcher.py" --status
   python3 "$HOME/.hermes/scripts/tibo_watcher.py" --test
   ```

4. Establish the baseline by running the installed script normally once, under the **same user, HOME and state-directory environment** that the scheduler will use. Confirm exit `0`, no stdout, and a valid `seen.json`. A second run should be silent unless a genuinely new post arrived. Do not use `--test` as the recurring command: it deliberately repeats the latest notice.
5. With the confirmed channel ID, create the job **paused** so its configuration can be reviewed first:

   ```sh
   # Set CHANNEL_ID to the user's confirmed destination; never guess it.
   : "${CHANNEL_ID:?Set the confirmed Discord channel ID}"
   hermes cron create '*/5 * * * *' \
     --name tibo-watcher \
     --script tibo_watcher.py \
     --no-agent \
     --deliver "discord:${CHANNEL_ID}" \
     --paused --paused-reason 'Review destination and state before enabling'
   ```

   Hermes resolves `--script` under `~/.hermes/scripts/`. `--no-agent` forwards stdout directly and leaves empty output silent. Do not add a summarizing LLM, `--monitor-url`, or a second scheduler.
6. Inspect the returned job ID, schedule, script, execution timeout, destination and failure reporting. After the requested setup is approved, run `hermes cron resume <JOB_ID>`. Verify `hermes cron status`, `hermes cron list` and `hermes cron runs <JOB_ID>` using that installed version's help.
7. If the user approved a test message, send the **marked preview** once through the runtime's existing message-sending path and observe it in the actual channel. Separately verify a real scheduled execution receipt. No new post means a silent tick is expected; do not label silence as observed Discord delivery. Report that a future real notice remains unobserved until one arrives.

Other bot runtimes can run the same script and forward **nonempty stdout on exit `0`**, while recording stderr/failed exits. Tokens and destination configuration stay in the runtime, not this repository.

### Pause, remove, upgrade

- Pause only the recorded owned job: `hermes cron pause <JOB_ID>`.
- Remove only that job: `hermes cron remove <JOB_ID>`. Keep state for a future restart; deletion requires an explicit decision because it changes deduplication history.
- For an upgrade, pause the job, review the new commit, rerun tests/previews, replace only the owned script, preserve state, then resume and inspect the execution receipt. No automatic self-update is installed.

## Run without a bot runtime

Hermes is one example runtime, not a requirement. Any scheduler works if it honors this contract:

- runs `python3 tibo_watcher.py` (normal mode) about every five minutes with a finite execution timeout (for example 120 seconds);
- forwards **nonempty stdout on exit `0`** to the destination, and records stderr / nonzero exits as failures;
- uses one persistent state directory per consumer, under the real execution user's HOME;
- never merges stderr into the message body, and never schedules `--test` or `--status`.

The smallest self-hosted path is cron plus a Discord webhook. The webhook URL is the user's secret and stays **outside** this repository — in a `0600` env file, not in the crontab, command history or logs:

```sh
mkdir -p "$HOME/.config/tibo-watcher" && chmod 700 "$HOME/.config/tibo-watcher"
printf 'TIBO_WEBHOOK=https://discord.com/api/webhooks/YOURS\n' > "$HOME/.config/tibo-watcher/env"
chmod 600 "$HOME/.config/tibo-watcher/env"
install -m 755 tibo_watcher.py "$HOME/.local/bin/tibo_watcher.py"
```

`$HOME/.config/tibo-watcher/notify.sh` (create once, `chmod 700`):

```sh
#!/bin/sh
set -eu
. "$HOME/.config/tibo-watcher/env"
out=$(python3 "$HOME/.local/bin/tibo_watcher.py") || exit 1  # failure: stderr reaches the cron log
[ -n "$out" ] || exit 0
curl -sf --max-time 10 -X POST --data-urlencode "content=$out" "$TIBO_WEBHOOK"
```

Crontab entry (the script's own `run.lock` already rejects overlapping runs):

```cron
*/5 * * * * $HOME/.config/tibo-watcher/notify.sh >> $HOME/.local/state/tibo-watcher-notify.log 2>&1
```

The first scheduled run creates the baseline silently — run one tick manually to confirm exit `0` and a valid `seen.json` before relying on it. A systemd timer or launchd agent is equally fine; only the contract above matters. Treat a failed `curl` as an undelivered notice: the IDs were already recorded (see [limitations](#message-and-delivery-limitations)), so resend from the saved output instead of deleting state. Discord webhook rate limits comfortably allow one request per five minutes.

## Repository layout, CI and branches

- [`tibo_watcher.py`](tibo_watcher.py) — parsing, merge, state and CLI; the only runtime file.
- [`tests/test_tibo_watcher.py`](tests/test_tibo_watcher.py) — offline parser, failure, deduplication, state, locking and CLI tests.
- [`AGENTS.md`](AGENTS.md) (imported by [`CLAUDE.md`](CLAUDE.md)) — the agent install path and change boundaries.
- [`.github/workflows/tests.yml`](.github/workflows/tests.yml) — offline tests on Python 3.10 and 3.13. Every file in a public repository is public, this one included; it touches no live feeds, secrets or sending, and nothing on the install path depends on it.

`dev` is the default and integration branch; focused PRs target it. `main` receives promotion merges from `dev` and has no deployment workflow or hosted service. There is no auto-update: consumers upgrade by reviewing a new ref and replacing the one installed file.

## License

Code: [MIT](LICENSE). Feed content and site names belong to their respective owners; the code license does not relicense upstream posts or promise access to their services.
