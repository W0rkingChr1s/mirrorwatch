# mirrorwatch

Watch HTTP endpoints for new and changed files, mirror them locally, and get notified — with the file attached.

Built for the annoying case: a publisher drops PDFs on a web server, tells nobody, and offers no feed, no listing, and no sane 404. mirrorwatch notices anyway, and brings you a copy.

- **No dependencies.** Python standard library only. The Docker image is `python:3.12-alpine` plus one package directory.
- **Content-aware.** Compares SHA-256, not just `Last-Modified`. A re-upload of identical bytes stays quiet.
- **Server quirks live in config, not code.** Detection rules are data, so a server that answers `HTTP 200` for missing files is a config entry rather than a fork.
- **Mirrors and archives.** Every version is kept, so you can diff last year's flyer against this year's.
- **Telegram, webhook, ntfy.** Files under the size limit are sent as attachments.

---

## Quick start

One line scaffolds a ready-to-edit deployment directory (compose file, an
example config, and an `.env` template):

```bash
curl -fsSL https://raw.githubusercontent.com/W0rkingChr1s/mirrorwatch/main/install.sh | sh
```

Then edit `mirrorwatch/config.json` and `mirrorwatch/.env`, and `docker compose
up -d`. That's it — the pre-built image is pulled from GHCR, no build step.

<details>
<summary>Prefer to do it by hand?</summary>

```bash
git clone https://github.com/W0rkingChr1s/mirrorwatch
cd mirrorwatch
mkdir -p config
cp examples/html-index.json config/config.json   # then edit it
cp .env.example .env                             # then edit it

python -m mirrorwatch check   -c config/config.json   # validate
python -m mirrorwatch targets -c config/config.json   # what would be watched
python -m mirrorwatch once    -c config/config.json --dry-run

docker compose up -d
docker compose logs -f
```
</details>

---

## Deployment

### Pre-built image

Every push to `main` runs the test suite and, if it passes, publishes a
multi-arch (`amd64` + `arm64`) image to the GitHub Container Registry:

```
ghcr.io/w0rkingchr1s/mirrorwatch:latest
```

Tags: `latest` tracks `main`; `vX.Y.Z` / `X.Y` are cut from git tags; a
`sha-<short>` tag pins any exact build.

```bash
docker pull ghcr.io/w0rkingchr1s/mirrorwatch:latest
```

### Run it

The bundled `docker-compose.yml` pulls the pre-built image and mounts a config
directory and a state volume:

```bash
mkdir -p config
cp examples/html-index.json config/config.json   # then edit it
cp .env.example .env                              # Telegram token etc.

docker compose up -d
docker compose logs -f          # watch for "baseline established"
```

mirrorwatch only makes outbound requests, so the container maps no ports. State
lives in the `mirrorwatch-data` volume and survives image updates, so nothing is
re-notified after an upgrade.

### Portainer / paste-only stack

`stack.yml` needs no host files at all: the whole config travels in the
`MIRRORWATCH_CONFIG_JSON` environment variable. In Portainer, add a stack, paste
`stack.yml`, and set three env vars — `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`, and
`MIRRORWATCH_CONFIG_JSON` (your config as one-line JSON). Deploy.

`MIRRORWATCH_CONFIG_JSON` works anywhere: when set, it overrides the config
file, so a plain `docker run` needs no mount either:

```bash
docker run -d --name mirrorwatch \
  -e TELEGRAM_TOKEN=... -e TELEGRAM_CHAT_ID=... \
  -e MIRRORWATCH_CONFIG_JSON='{"sources":[...],"notifiers":{...}}' \
  -v mirrorwatch-data:/data \
  ghcr.io/w0rkingchr1s/mirrorwatch:latest
```

### Optional: auto-updates

The container carries the `com.centurylinklabs.watchtower.enable=true` label.
If you run [Watchtower](https://containrrr.dev/watchtower/), it rolls out each
new `:latest` automatically; if you don't, the label is simply ignored. To pin a
version instead, replace `:latest` with a `vX.Y.Z` tag and update deliberately.

---

## How it decides what changed

For every target, mirrorwatch sends a `HEAD` request and classifies the answer as **file**, **directory**, **missing**, or **error**.

| Situation | What happens |
|---|---|
| Headers unchanged since last run | Nothing. No download. |
| Headers moved, bytes identical | State updated, no notification. |
| Bytes differ | Previous version archived, new one mirrored, notification sent. |
| Previously present, now missing | `gone` notification. The mirror copy is kept. |
| Network error | Nothing. An unreachable server is never reported as a deletion. |

Directories are compared by `Last-Modified` alone, because there is nothing to hash. On a normal POSIX filesystem that timestamp moves whenever a file inside is added, replaced, or removed — which is often the only way to learn that *something* appeared on a server with no listing.

---

## Sources

### `index` — scrape an overview page

The one to reach for first. Fetches HTML, extracts `<a href>`, keeps what matches. New files are discovered on their own.

```json
{
  "name": "council-minutes",
  "type": "index",
  "url": "https://example.org/publications/",
  "match": "\\.pdf$",
  "exclude": "/archive/|draft",
  "recursive": { "depth": 1, "match": "/publications/[0-9]{4}/$" }
}
```

`match` and `exclude` are regular expressions tested against the absolute URL. Recursion only follows links matching `recursive.match`, bounded by `depth`. Off-host links are skipped unless `same_host_only` is `false`.

### `probe` — ask about paths one by one

For servers with no listing at all. You supply known paths and templates; mirrorwatch checks which exist.

```json
{
  "name": "some-hub",
  "type": "probe",
  "base_url": "https://example.net/files.php?file=",
  "dirs":  ["docs/de/flyer"],
  "files": ["docs/de/flyer/spring2026.pdf"],
  "probes": [
    { "template": "docs/de/flyer/spring{yyyy}.pdf", "years": { "from": 2026, "to": 2028 } }
  ]
}
```

Placeholders: `{yyyy}`, `{yy}`, `{mm}`, and `{v}` with a `values` list. A `base_url` ending in `=`, `?`, or `&` is concatenated directly; otherwise paths are joined with `/`.

Probing is guessing. Use it only when there is genuinely no index page.

### `urls` — a fixed list

```json
{ "name": "releases", "type": "urls", "urls": ["https://example.org/latest.zip"] }
```

Set `"download": false` on any source to track changes from headers alone, without fetching the body. Useful for multi-gigabyte files.

---

## Detection rules

Servers disagree about how to say "not found". Instead of hardcoding one server's behaviour, describe it:

```json
"detect": {
  "missing":   [{ "content_type": "text/html" }],
  "directory": [{ "content_type": "directory" }],
  "default":   "file"
}
```

A rule matches when **all** of its criteria match. Available criteria:

| Criterion | Meaning |
|---|---|
| `status` | int or list, exact match |
| `status_range` | `[min, max]`, inclusive |
| `content_type` | string or list, prefix match, case insensitive |
| `max_size` / `min_size` | bounds on `Content-Length`; ignored when the server sends none |
| `url_suffix` | string or list; URL ends with one of them |

Defaults treat `404/403/410/451` as missing and any other `2xx` as a file, which is right for most servers.

`examples/prowin-hub.json` documents a real endpoint that answers `HTTP 200` with a 31-byte HTML body for every path that does not exist, making status codes useless — exactly the case these rules exist for.

---

## Notifiers

Secrets never belong in the config file. Use `env:NAME` or `file:/path`.

```json
"notifiers": {
  "telegram": {
    "type": "telegram",
    "token": "env:TELEGRAM_TOKEN",
    "chat_id": "env:TELEGRAM_CHAT_ID",
    "send_files": true,
    "max_mb": 45
  }
}
```

| Type | Notes |
|---|---|
| `telegram` | Sends the file as a document with a caption. Falls back to a text message when the file exceeds `max_mb` (Telegram's bot upload ceiling is 50 MB). Honours `retry_after` on rate limits. |
| `webhook` | `POST`s a JSON document with the full event list. Custom headers supported. |
| `ntfy` | Plain text push, optional bearer token. |
| `stdout` | Logs only. Handy while tuning a config. |

Route per source with `"notify": ["telegram"]`. Omit it and every notifier gets everything.

**Telegram setup:** create a bot with [@BotFather](https://t.me/BotFather), add it to the channel as an administrator with permission to post, then find the chat id:

```bash
curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates" \
  | python3 -c "import json,sys;[print(u.get('channel_post',{}).get('chat')) for u in json.load(sys.stdin)['result']]"
```

---

## The first run

A first run would otherwise announce everything it finds. `bootstrap_notify` controls that:

| Value | Behaviour |
|---|---|
| `summary` (default) | One message: how many targets, how many files mirrored. |
| `full` | Every discovery reported individually. |
| `none` | Silence. Baseline only. |

---

## When the check runs

Out of the box `mirrorwatch run` works on an interval: check, wait `interval_seconds`, check again. The first check happens right away, so the clock times drift with every restart.

For checks at fixed times of day, set `check_times` instead. It replaces `interval_seconds` entirely.

```json
{
  "check_times": ["06:00", "12:00", "18:00"],
  "timezone": "Europe/Berlin"
}
```

- Times are 24-hour `HH:MM`, in any order — they get sorted, and duplicates dropped.
- `timezone` is an IANA name. Leave it out and the machine's local time is used, which inside a container means UTC. In Docker the image ships tzdata, so any zone resolves.
- Daylight saving is handled by the zone: `06:00` stays `06:00` across the switch.
- On startup mirrorwatch waits for the next slot rather than checking immediately, so restarts and image updates do not trigger extra runs. Set `run_on_start` to `true` if you would rather have one check at boot as well, or to `false` in interval mode to wait out the first interval.

The same thing through the environment, which is what the container stacks use:

```
MIRRORWATCH_CHECK_TIMES=06:00,18:00
MIRRORWATCH_TIMEZONE=Europe/Berlin
TZ=Europe/Berlin
```

`TZ` is worth setting alongside: it is the container's own clock, which is what the log timestamps use. Without it a line like `11:37 … next check at 17:00` compares a UTC timestamp against a Berlin time and looks two hours off.

`mirrorwatch status` prints the schedule and the next due run, and the log says `next check at …` after every pass.

One thing to adjust with sparse schedules: the container healthcheck fails when the last run is older than `MIRRORWATCH_HEALTH_MAX_AGE` (default 45000s, half a day). Checking once a day means raising it above the longest gap between two check times, e.g. `MIRRORWATCH_HEALTH_MAX_AGE=100000`.

---

## Commands

```
mirrorwatch run                 # loop forever on check_times, or interval_seconds
mirrorwatch once [--dry-run]    # single pass; dry-run writes nothing anywhere
mirrorwatch check               # validate config, exit non-zero on problems
mirrorwatch targets             # resolve and print every target, without requests to files
mirrorwatch status [--json] [--max-age SECONDS]
```

`status --max-age` is what the container healthcheck uses: it fails when the last completed run is older than the given number of seconds.

---

## Configuration reference

| Key | Default | Meaning |
|---|---|---|
| `interval_seconds` | `21600` | Time between runs in `run` mode; ignored when `check_times` is set |
| `check_times` | `[]` | Fixed times of day for `run` mode, e.g. `["06:00", "18:00"]` |
| `timezone` | `null` | IANA zone the check times are read in; `null` is the machine's local time |
| `run_on_start` | `null` | Check once at startup? `null` means yes on an interval, no with check times |
| `request_delay_ms` | `250` | Pause between requests; be kind to other people's servers |
| `timeout` | `60` | Per-request timeout in seconds |
| `retries` | `2` | Retries on network errors, with linear backoff |
| `max_download_mb` | `200` | Bodies larger than this are refused |
| `user_agent` | `mirrorwatch/0.1 …` | Set something identifiable with contact details |
| `state_file` | `./data/state.json` | Written atomically |
| `mirror.enabled` | `true` | Turn off to notify without storing |
| `mirror.dir` | `./data/mirror` | Layout: `<mirror>/<source>/<path>` |
| `mirror.archive_dir` | `./data/archive` | Previous versions, timestamp-suffixed |
| `mirror.keep_versions` | `true` | Off means overwrite in place |
| `bootstrap_notify` | `summary` | `summary`, `full`, or `none` |

These environment variables override the file, which is what you want in a container: `MIRRORWATCH_CONFIG`, `MIRRORWATCH_STATE_FILE`, `MIRRORWATCH_MIRROR_DIR`, `MIRRORWATCH_ARCHIVE_DIR`, `MIRRORWATCH_INTERVAL`, `MIRRORWATCH_CHECK_TIMES`, `MIRRORWATCH_TIMEZONE`, `MIRRORWATCH_RUN_ON_START`, `MIRRORWATCH_USER_AGENT`, `MIRRORWATCH_REQUEST_DELAY_MS`, `MIRRORWATCH_BOOTSTRAP_NOTIFY`, `MIRRORWATCH_LOG_LEVEL`.

`MIRRORWATCH_CONFIG_JSON` goes one step further: set it to the whole config as JSON and mirrorwatch skips the file entirely. This is what makes a one-paste container stack possible — see [Deployment](#portainer--paste-only-stack).

---

## Please be a good citizen

mirrorwatch makes requests to servers you do not own.

- Keep `request_delay_ms` sane. The default is deliberately unhurried.
- Poll hourly at most unless you know the publisher is fine with more.
- Put real contact details in `user_agent` so an administrator can reach you instead of blocking you.
- Check the site's terms and `robots.txt`. Probe sources in particular walk a line between "checking a URL" and "enumerating someone's filesystem" — use them only where no index exists, and keep the candidate list small.
- Mirrored files stay under their original copyright. A local mirror is not a licence to redistribute.

---

## Development

```bash
python -m unittest discover -s tests -v
```

26 tests, no network access required — a local HTTP server covers the full lifecycle: discovery, unchanged runs, header-only changes, real content changes, archiving, deletion, and the guarantee that a network failure is never reported as a deletion.

---

## License

MIT. See [LICENSE](LICENSE).
