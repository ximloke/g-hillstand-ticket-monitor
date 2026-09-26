"""Read G Hillstand availability once; send deduplicated Telegram alerts."""
import base64
import copy
import datetime as dt
import json
import os
import re
import sys
import time
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request

PERFORMANCE = '45C89B1D-FC79-4F87-AC66-A9DB18A17A39'
ZONE = '3C179698-85A0-48A5-8CF2-CA8C8D7078B7'
URL = ('https://tickets.bahraingp.com/Online/seatSelect.asp?createBO%3A%3AWSmap=1'
       '&BOparam%3A%3AWSmap%3A%3AloadBestAvailable%3A%3Aperformance_ids=' + PERFORMANCE)
STATE_BRANCH = 'monitor-state'
STATE_PATH = 'state.json'
HISTORY_LIMIT = 1000
MYT = dt.timezone(dt.timedelta(hours=8))


def local_time(iso):
    try:
        return dt.datetime.fromisoformat(iso).astimezone(MYT).strftime('%Y-%m-%d %H:%M:%S MYT')
    except ValueError:
        return iso


class SafeError(Exception):
    """Only constant, non-secret diagnostic messages may be raised here."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request_json(url, headers=None, data=None, method=None):
    request = urllib.request.Request(url, headers=headers or {}, method=method,
                                     data=None if data is None else json.dumps(data).encode())
    request.add_header('Content-Type', 'application/json')
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=25) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        raise SafeError('Remote API returned HTTP ' + str(exc.code)) from None
    except (OSError, ValueError):
        # urllib exceptions can contain credential-bearing Telegram URLs.
        raise SafeError('Remote API connection or response failed') from None


def required(name):
    value = os.environ.get(name, '').strip()
    if not value:
        raise SafeError('Missing required setting: ' + name)
    return value


class GithubState:
    def __init__(self):
        repo = required('GITHUB_REPOSITORY')
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo):
            raise SafeError('Invalid repository name')
        self.url = 'https://api.github.com/repos/' + repo + '/contents/' + STATE_PATH
        self.headers = {'Authorization': 'Bearer ' + required('GH_STATE_TOKEN'),
                        'Accept': 'application/vnd.github+json',
                        'X-GitHub-Api-Version': '2022-11-28',
                        'User-Agent': 'g-hillstand-ticket-monitor'}
        self.sha = None

    def load(self):
        result = request_json(self.url + '?ref=' + STATE_BRANCH, self.headers)
        self.sha = result['sha']
        state = json.loads(base64.b64decode(result['content']))
        if state.get('version') != 1:
            raise SafeError('Unrecognized monitor state; refusing to reset alert history')
        return state

    def save(self, state):
        content = json.dumps(state, indent=2, sort_keys=True) + '\n'
        result = request_json(self.url, self.headers, {
            'message': 'Update ticket notification state',
            'branch': STATE_BRANCH, 'sha': self.sha,
            'content': base64.b64encode(content.encode()).decode()}, 'PUT')
        self.sha = result['content']['sha']


def send_telegram(text):
    token = required('TELEGRAM_BOT_TOKEN')
    chat_id = required('TELEGRAM_CHAT_ID')
    if not re.fullmatch(r'\d+:[A-Za-z0-9_-]+', token):
        raise SafeError('Telegram token format is invalid')
    result = request_json('https://api.telegram.org/bot' + token + '/sendMessage', data={
        'chat_id': chat_id, 'text': text, 'link_preview_options': {'is_disabled': True}})
    if result.get('ok') is not True:
        raise SafeError('Telegram did not confirm delivery')


def correct_event(url):
    parts = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qs(parts.query)
    return (parts.scheme == 'https' and parts.hostname == 'tickets.bahraingp.com'
            and parts.path.lower() == '/online/seatselect.asp'
            and query.get('BOparam::WSmap::loadBestAvailable::performance_ids') == [PERFORMANCE])


def classify(snapshot):
    """Fail closed on missing, hidden, mismatched or contradictory page data."""
    if (not snapshot.get('correct_event') or not snapshot.get('visible')
            or ' '.join(snapshot.get('title', '').split()).casefold() != 'g hillstand'
            or snapshot.get('radio_count') != 1
            or type(snapshot.get('disabled')) is not bool):
        return 'unknown'
    text = ' '.join(snapshot.get('text', '').split())
    if not re.search(r'\bg\s+hillstand\b', text, re.I):
        return 'unknown'
    negative = bool(re.search(r'sold\s*out|unavailable|not\s+available', text, re.I))
    if snapshot['disabled'] and negative:
        return 'sold_out'
    if not snapshot['disabled'] and not negative and re.search(r'(?:BHD|MYR|RM)\s*\d', text, re.I):
        return 'available'
    return 'unknown'


def read_zone(page):
    zone = page.locator('input[id="' + ZONE + '"]')
    if zone.count() != 1:
        # A renamed price-zone ID must still have one unambiguous exact title.
        zone = page.locator('input[type="radio"][title="G Hillstand" i]')
    if zone.count() != 1:
        return 'unknown', 'zone_missing_or_ambiguous'
    snap = zone.evaluate('''el => {
      const box = el.closest('.item-box-item');
      return {title: el.getAttribute('title') || '',
        disabled: el.disabled || el.matches(':disabled'),
        text: box ? box.innerText : '',
        radio_count: box ? box.querySelectorAll('input[type=radio]').length : 0};
    }''')
    snap['visible'] = zone.is_visible()
    snap['correct_event'] = correct_event(page.url)
    status = classify(snap)
    return status, 'zone_verified' if status != 'unknown' else 'zone_ambiguous'


def observe_once():
    # Do not bypass queue or CAPTCHA; retry transient failures in a fresh browser.
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as BrowserTimeout
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(viewport={'width': 1280, 'height': 900})
            page = context.new_page()
            response = page.goto(URL, wait_until='domcontentloaded', timeout=45000)
            if response is not None and response.status >= 400:
                return 'unknown', 'ticket_http_' + str(response.status)
            try:
                page.locator('input[type="radio"][name="priceZone"]').first.wait_for(state='attached', timeout=25000)
            except BrowserTimeout:
                if 'queue-it.net' in urllib.parse.urlsplit(page.url).hostname:
                    return 'unknown', 'waiting_room'
                return 'unknown', 'ticket_page_timeout'
            first, reason = read_zone(page)
            page.wait_for_timeout(1500)
            second, reason = read_zone(page)
            browser.close()
            if first == second:
                return second, reason
            return 'unknown', 'zone_changed_during_read'
    except Exception:
        # Do not emit browser HTML, cookies, queue tokens, or untrusted exception text.
        return 'unknown', 'browser_queue_or_page_error'


def observe(checker=None, sleeper=time.sleep):
    checker = checker or observe_once
    for attempt in range(2):
        status, reason = checker()
        if status != 'unknown':
            return status, reason, attempt + 1
        if attempt == 0:
            sleeper(10)
    return status, reason, 2


def transition(state, status, now):
    """Return new non-secret state and messages. Unknown never rearms ticket alerts."""
    if status not in {'available', 'sold_out', 'unknown'}:
        raise SafeError('Invalid observation')
    new = copy.deepcopy(state)
    messages = []
    new['last_status'] = status
    if status == 'unknown':
        new['unknown_count'] = min(3, new.get('unknown_count', 0) + 1)
        if new['unknown_count'] >= 3 and not new.get('outage_notified', False):
            messages.append('⚠️ G Hillstand 云端监控连续 3 次无法确认库存。可能是排队、验证码或页面变化；这不代表售罄。\n' + URL)
            new['outage_notified'] = True
    else:
        was_outage = new.get('outage_notified', False)
        new['unknown_count'] = 0
        new['outage_notified'] = False
        if status == 'sold_out':
            new['availability_notified'] = False
        elif not new.get('availability_notified', False):
            messages.append('🎟 G Hillstand 页面显示可选！MyKAD Holders Only。请尽快确认并购票。\n检查时间：' + local_time(now) + '\n' + URL)
            new['availability_notified'] = True
            new['last_ticket_alert_at'] = now
        if was_outage and not messages:
            messages.append('✅ G Hillstand 云端监控已恢复。当前状态：' + ('售罄' if status == 'sold_out' else '页面显示可选') + '。')
    if new != state:
        new['last_change_at'] = now
    return new, messages


def audit(state, status, now, reason, attempts, sent, failed=False):
    state['last_checked_at'] = now
    state['last_check_reason'] = reason
    if status != 'unknown':
        state['last_successful_check_at'] = now
    entry = {'at': now, 'status': status, 'reason': reason,
             'attempts': attempts, 'notifications_sent': sent}
    if failed:
        entry['notification_failed'] = True
    run_id = os.environ.get('GITHUB_RUN_ID', '')
    if run_id.isdigit():
        entry['run_id'] = run_id
    state['history'] = (state.get('history', []) + [entry])[-HISTORY_LIMIT:]


def process(store, status, now, sender=send_telegram, reason='zone_verified', attempts=1):
    previous = store.load()
    updated, messages = transition(previous, status, now)
    sent = 0
    try:
        for message in messages:
            sender(message)
            sent += 1
    except SafeError:
        # Retain previous delivery flags so the alert can be retried next run.
        audit(previous, status, now, reason, attempts, sent, failed=True)
        store.save(previous)
        raise
    audit(updated, status, now, reason, attempts, sent)
    if updated.get('pipeline_failure_notified'):
        sender('✅ G Hillstand 检查程序已恢复运行。')
        updated['pipeline_failure_notified'] = False
    # Save an audit entry even for unchanged sold-out results.
    store.save(updated)
    return len(messages)


def failure_notice():
    # An ordinary unknown result has its own three-check outage policy.
    if Path('.observation-recorded').exists():
        print('Observation already recorded; outage policy handles this failure.')
        return 0
    store = GithubState()
    state = None
    try:
        state = store.load()
    except Exception:
        pass
    if state and state.get('pipeline_failure_notified'):
        print('Pipeline failure already reported.')
        return 0
    send_telegram('⚠️ G Hillstand 云端检查程序运行失败，本轮可能没有完成查票。请查看 GitHub Actions；这不表示售罄。\n'
                  + 'https://github.com/' + required('GITHUB_REPOSITORY') + '/actions')
    if state is not None:
        state['pipeline_failure_notified'] = True
        store.save(state)
    return 0


def main():
    required('TELEGRAM_BOT_TOKEN')
    required('TELEGRAM_CHAT_ID')
    store = GithubState()
    store.load()  # Verify durable state access before polling the ticket site.
    status, reason, attempts = observe()
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')
    count = process(store, status, now, reason=reason, attempts=attempts)
    Path('.observation-recorded').touch()
    print(json.dumps({'checked_at': now, 'status': status, 'reason': reason,
                      'attempts': attempts, 'notifications_sent': count}))
    if os.environ.get('SEND_TEST') == 'true':
        label = {'sold_out': '售罄', 'available': '页面显示可选', 'unknown': '无法确认'}[status]
        send_telegram('✅ GitHub Actions 云端测试完成。G Hillstand 当前：' + label
                      + '。计划每 5 分钟检查；GitHub 调度可能延迟。\n' + URL)
        print('Cloud Telegram test delivered')
    summary = os.environ.get('GITHUB_STEP_SUMMARY')
    if summary:
        with open(summary, 'a') as f:
            f.write('G Hillstand (MyKAD): **' + status + '**\n\n' + local_time(now)
                    + '\n\nReason: ' + reason + '; attempts: ' + str(attempts) + '\n')
    if status == 'unknown':
        return 2
    return 0


if __name__ == '__main__':
    try:
        sys.exit(failure_notice() if '--failure-notice' in sys.argv else main())
    except SafeError as exc:
        print('Monitor failed: ' + str(exc), file=sys.stderr)
        sys.exit(1)
    except Exception:
        print('Monitor failed unexpectedly; notification/state is not confirmed.', file=sys.stderr)
        sys.exit(1)
