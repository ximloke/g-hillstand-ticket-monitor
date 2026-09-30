# G Hillstand ticket monitor

Checks the specified **MyKAD Holders Only** ticketing page for **G Hillstand**, using GitHub Actions and Telegram. No purchase, seat selection, cart operation or CAPTCHA bypass is performed. Other ticket allocations or performance IDs are not monitored.

## Always-on mode (recommended)

`python monitor.py --daemon` checks continuously, every 5 minutes by default, until stopped. Use this rather than the GitHub Actions schedule below when you need the page checked around the clock.

**Why:** GitHub throttles `schedule` triggers for low-activity repositories. From 26–30 Sept this repository's "every 5 minutes" workflow ran 23 times in 4.5 days (median gap about 4.5 hours, longest about 8), against roughly 1,300 requested. A restock lasting a few minutes can fall entirely between two runs. No cron edit fixes that; the process has to be long-lived.

Run it on any machine that stays on (a VPS, a home server, a Raspberry Pi 4/5 or a spare PC) with Docker:

```sh
cp .env.example .env        # fill in TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID
docker compose up -d --build
docker compose logs -f      # one JSON line per check
```

- Sends a Telegram message on start (so a wrong token fails immediately), a heartbeat every 24 hours (`HEARTBEAT_HOURS`, `0` disables), and the same availability, outage and recovery alerts as the Actions version. **A missing heartbeat means the machine or container is down**; the monitor cannot report its own absence.
- `restart: unless-stopped` brings it back after crashes and reboots. A failed check cycle never ends the loop; three consecutive failed cycles send one alert, and recovery sends one message.
- State (alert de-duplication, last 1,000 observations) is in `./data/state.json`, written atomically, so restarts do not repeat alerts. Keep that directory.
- `CHECK_INTERVAL_SECONDS` defaults to 300 and cannot go below 60. Polling faster gains little and risks an IP block.
- Without Docker: `pip install -r requirements.txt && python -m playwright install --with-deps chromium`, export the variables from `.env.example`, then run `python monitor.py --daemon` under systemd or similar.

Do not run this and the Actions workflow at once unless you want duplicate alerts: they keep separate state. Once the daemon works, disable the workflow under Actions, or leave it as a coarse backup.

## Actions-only mode

The sections below describe the GitHub Actions setup. It needs no server but, per the numbers above, checks far less often than its schedule suggests.

## September 26 improvements

- Records every observation, including unchanged sellouts, with time, diagnostic reason, attempts and workflow run ID. The latest 1,000 observations (about 3.5 days at an actual five-minute cadence) are in `monitor-state/state.json`; older entries are recoverable from branch history while the repository exists.
- Retries one transient/unknown observation after ten seconds, bounded to two attempts. It never repeats a confirmed sold-out check in the same run or bypasses the queue.
- Accepts an unambiguous exact G Hillstand title if its internal ID changes; supports MYR/RM as well as BHD prices and normalized label whitespace. Wrong events, contradictory states and absent prices still fail closed.
- Reports installation/execution failures to Telegram, deduplicated where the state store is accessible. Recovery is reported. If GitHub itself stops scheduling jobs or the repository disappears, this workflow cannot report its own absence.
- Uses current pinned official Actions, a fixed Ubuntu 24.04 runner, and downloads only Chromium's headless shell to reduce setup work.
- Ticket alerts and the run summary display Malaysia time.

## Schedule and cost

The workflow requests one check every five minutes, offset from the hour (:02, :07, :12, …). GitHub can delay or drop scheduled runs (observed: about one run per 4.5 hours), and tickets offered between observations may be missed. Public repositories using standard GitHub-hosted runners qualify for free Actions usage. This workflow uses `ubuntu-24.04`, no paid runner, no external hosting, and no uploaded artifacts.

GitHub may disable scheduled workflows in public repositories after 60 days without repository activity. This is not an uptime guarantee. The target event is in October 2026; disable the workflow when it is no longer useful.

## Setup

1. Use this directory as a public GitHub repository.
2. Store `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` as **repository Actions secrets**. Never commit credentials, local configuration, browser cookies or sessions.
3. Create a `monitor-state` branch containing `state.json`:

```json
{"version":1,"availability_notified":false,"outage_notified":false,"unknown_count":0}
```

4. Run **Actions → G Hillstand ticket monitor → Run workflow**, enable `send_test`, and verify both a successful run and the Telegram test message.
5. Disable any previous local monitor only after this cloud check succeeds.

## Detection and alerts

The monitor checks the event ID, radio ID (or one exact title match), visibility, the independent price-zone container and its sold-out indicator. It observes the same result twice before classifying availability.

- Enabled G Hillstand with a visible price and no unavailability marker: **available**.
- Disabled G Hillstand with an unavailability marker: **sold_out**.
- Queue, CAPTCHA, network failure, changed page or contradictory information: **unknown**; workflow fails visibly instead of silently claiming sold out.

The first available observation sends a Telegram alert. Repeated availability does not spam. A confirmed sellout rearms the next restock alert. Unknown observations do not rearm it. Three consecutive unknown runs send one outage message, and recovery sends one message. Notification state is persisted on `monitor-state`; that branch contains only non-secret flags, observation records and timestamps. State-read failures stop the check and trigger a failure notification attempt. Delivery failures are recorded without marking the ticket alert delivered, so they remain retryable. If delivery succeeds but saving state fails, a duplicate alert is possible on the next run.

The manual `send_test` input sends an explicit cloud test message. Regular sold-out checks stay quiet. To stop, disable the workflow under Actions.

## Tests

```sh
python -m unittest discover -s tests -v
```

## Official references

- [Scheduled workflow limits](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)
- [Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions)
- [Actions secrets](https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/use-secrets)
- [Playwright Python](https://playwright.dev/python/docs/intro)
