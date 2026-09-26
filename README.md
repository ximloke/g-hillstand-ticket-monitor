# G Hillstand ticket monitor

Checks the specified **MyKAD Holders Only** ticketing page for **G Hillstand**, using GitHub Actions and Telegram. No purchase, seat selection, cart operation or CAPTCHA bypass is performed. Other ticket allocations or performance IDs are not monitored.

## September 26 improvements

- Records every observation, including unchanged sellouts, with time, diagnostic reason, attempts and workflow run ID. The latest 1,000 observations (about 3.5 days at an actual five-minute cadence) are in `monitor-state/state.json`; older entries are recoverable from branch history while the repository exists.
- Retries one transient/unknown observation after ten seconds, bounded to two attempts. It never repeats a confirmed sold-out check in the same run or bypasses the queue.
- Accepts an unambiguous exact G Hillstand title if its internal ID changes; supports MYR/RM as well as BHD prices and normalized label whitespace. Wrong events, contradictory states and absent prices still fail closed.
- Reports installation/execution failures to Telegram, deduplicated where the state store is accessible. Recovery is reported. If GitHub itself stops scheduling jobs or the repository disappears, this workflow cannot report its own absence.
- Uses current pinned official Actions, a fixed Ubuntu 24.04 runner, and downloads only Chromium's headless shell to reduce setup work.
- Ticket alerts and the run summary display Malaysia time.

## Schedule and cost

The workflow requests one check every five minutes, offset from the hour (:02, :07, :12, …). GitHub can delay or drop scheduled runs during heavy load, and tickets offered between observations may be missed. Public repositories using standard GitHub-hosted runners qualify for free Actions usage. This workflow uses `ubuntu-24.04`, no paid runner, no external hosting, and no uploaded artifacts.

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
