# G Hillstand ticket monitor

Checks the specified Bahrain GP ticketing page for **G Hillstand**, using GitHub Actions and Telegram. No purchase, seat selection, cart operation or CAPTCHA bypass is performed.

## Schedule and cost

The workflow requests one check every five minutes, offset from the hour (:02, :07, :12, …). GitHub can delay or drop scheduled runs during heavy load. Public repositories using standard GitHub-hosted runners qualify for free Actions usage. This workflow uses `ubuntu-latest`, no paid runner, no external hosting, and no uploaded artifacts.

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

The monitor checks the event ID, exact radio ID and title, visibility, the independent price-zone container and its sold-out indicator. It observes the same result twice before classifying availability.

- Enabled G Hillstand with a visible price and no unavailability marker: **available**.
- Disabled G Hillstand with an unavailability marker: **sold_out**.
- Queue, CAPTCHA, network failure, changed page or contradictory information: **unknown**; workflow fails visibly instead of silently claiming sold out.

The first available observation sends a Telegram alert. Repeated availability does not spam. A confirmed sellout rearms the next restock alert. Unknown observations do not rearm it. Three consecutive unknown checks send one outage message, and recovery sends one message. Notification state is persisted on `monitor-state`; that branch contains only non-secret flags and timestamps. State-read failures stop the check. Delivery failures are retryable. If delivery succeeds but saving state fails, a duplicate alert is possible on the next run.

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
