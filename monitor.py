"""Read G Hillstand availability; send deduplicated Telegram alerts.

Default: one check per run (GitHub Actions). With --daemon: check forever.
"""
import base64
import collections
import copy
import datetime as dt
import json
import os
import re
import signal
import sys
import threading
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
MIN_INTERVAL = 10  # Seconds between checks; polling this fast risks an IP block or throttling.
FAILURES_BEFORE_ALERT = 3
TICKET_ALERT_PREFIX = '🎟'  # Only messages starting with this reach subscribers.
MYT = dt.timezone(dt.timedelta(hours=8))


def local_time(iso):
    try:
        return dt.datetime.fromisoformat(iso).astimezone(MYT).strftime('%Y-%m-%d %H:%M:%S MYT')
    except ValueError:
        return iso


class SafeError(Exception):
    """Only constant, non-secret diagnostic messages may be raised here."""


class RemoteError(SafeError):
    def __init__(self, code):
        super().__init__('Remote API returned HTTP ' + str(code))
        self.code = code


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
        raise RemoteError(exc.code) from None
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


class FileState:
    """Same interface as GithubState, backed by a local JSON file."""

    def __init__(self, path):
        self.path = Path(path)

    def load(self):
        try:
            state = json.loads(self.path.read_text())
        except FileNotFoundError:
            return {'version': 1, 'availability_notified': False,
                    'outage_notified': False, 'unknown_count': 0}
        except ValueError:
            raise SafeError('State file is corrupt; refusing to reset alert history') from None
        if not isinstance(state, dict) or state.get('version') != 1:
            raise SafeError('Unrecognized monitor state; refusing to reset alert history')
        return state

    def save(self, state):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + '.tmp')
        tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + '\n')
        os.replace(tmp, self.path)  # Atomic: a kill mid-write cannot corrupt state.


def telegram_call(method, data):
    token = required('TELEGRAM_BOT_TOKEN')
    if not re.fullmatch(r'\d+:[A-Za-z0-9_-]+', token):
        raise SafeError('Telegram token format is invalid')
    return request_json('https://api.telegram.org/bot' + token + '/' + method, data=data)


def send_telegram(text, chat_id=None):
    """Send to `chat_id`, or to the owner (TELEGRAM_CHAT_ID) when omitted."""
    result = telegram_call('sendMessage', {
        'chat_id': chat_id or required('TELEGRAM_CHAT_ID'), 'text': text,
        'link_preview_options': {'is_disabled': True}})
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
            messages.append(TICKET_ALERT_PREFIX + ' G Hillstand 页面显示可选！MyKAD Holders Only。请尽快确认并购票。\n检查时间：' + local_time(now) + '\n' + URL)
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


def env_int(name, default, minimum):
    raw = os.environ.get(name, '').strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        raise SafeError('Setting must be an integer: ' + name) from None
    if value < minimum:
        raise SafeError('Setting ' + name + ' must be at least ' + str(minimum))
    return value


WELCOME = ('✅ 已订阅 G Hillstand（巴林 F1）购票通知：页面出现可选票时会立刻通知你。'
           '发送 /stop 可取消订阅。\n注意：这只是提醒，不会替你购票，也不保证抢到。')
HELP = '发送 /start 订阅 G Hillstand（巴林 F1）有票通知，发送 /stop 取消订阅。'


class Subscribers:
    """Friends who pressed /start. Persisted so restarts keep them; safe across threads."""

    def __init__(self, path, owner_id, limit):
        self.path, self.owner_id, self.limit = Path(path), str(owner_id), limit
        self.lock = threading.Lock()
        try:
            data = json.loads(self.path.read_text())
            self.people = dict(data.get('subscribers', {}))
            self.offset = int(data.get('offset', 0))
        except FileNotFoundError:
            self.people, self.offset = {}, 0
        except (ValueError, TypeError, AttributeError):
            raise SafeError('Subscriber file is corrupt; refusing to drop subscribers') from None

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + '.tmp')
        tmp.write_text(json.dumps({'version': 1, 'offset': self.offset, 'subscribers': self.people},
                                  indent=2, sort_keys=True) + '\n')
        os.replace(tmp, self.path)

    def ids(self):
        with self.lock:
            return [chat_id for chat_id in self.people if chat_id != self.owner_id]

    def count(self):
        return len(self.ids())

    def add(self, chat_id, name):
        with self.lock:
            if chat_id in self.people:
                return 'exists'
            if len(self.people) >= self.limit:
                return 'full'
            self.people[chat_id] = {'name': name, 'since': dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')}
            self._save()
            return 'added'

    def remove(self, chat_id):
        with self.lock:
            if self.people.pop(chat_id, None) is None:
                return False
            self._save()
            return True

    def advance(self, offset):
        with self.lock:
            self.offset = offset
            self._save()


def alert_sender(subscribers, send=send_telegram):
    """Ticket alerts also reach subscribers; every other message is for the owner only."""
    def sender(text):
        send(text)  # Owner first: if this raises, process() retries the whole alert next cycle.
        if not text.startswith(TICKET_ALERT_PREFIX):
            return
        for chat_id in subscribers.ids():
            try:
                send(text, chat_id)
            except SafeError as exc:
                # One unreachable friend must not block the others or re-alert the owner.
                if isinstance(exc, RemoteError) and exc.code == 403:  # Blocked the bot.
                    subscribers.remove(chat_id)
                print('Subscriber delivery failed: ' + str(exc), file=sys.stderr, flush=True)
    return sender


def handle_command(subscribers, chat, command, send):
    chat_id = str(chat['id'])
    name = str(chat.get('first_name') or chat.get('username') or chat_id)[:40]
    if chat_id == subscribers.owner_id:
        send('你是管理员，本来就会收到全部通知，无需订阅。', chat_id)
    elif command == '/start':
        outcome = subscribers.add(chat_id, name)
        if outcome == 'added':
            send(WELCOME, chat_id)
            send('👤 新订阅者：' + name + '（' + chat_id + '）。当前共 ' + str(subscribers.count()) + ' 位朋友订阅。')
        elif outcome == 'exists':
            send('你已经订阅了。发送 /stop 可取消。', chat_id)
        else:
            send('抱歉，订阅人数已满。', chat_id)
    elif command == '/stop':
        send('已取消订阅。' if subscribers.remove(chat_id) else '你还没有订阅。', chat_id)
    else:
        send(HELP, chat_id)


def handle_updates(subscribers, call=telegram_call, send=send_telegram):
    """Long-poll Telegram once and answer /start and /stop from private chats."""
    result = call('getUpdates', {'offset': subscribers.offset, 'timeout': 15,
                                 'allowed_updates': ['message']})
    if result.get('ok') is not True:
        raise SafeError('Telegram did not return updates')
    for update in result.get('result', []):
        message = update.get('message') or {}
        chat = message.get('chat') or {}
        if chat.get('type') == 'private' and 'id' in chat:
            words = (message.get('text') or '').split()
            command = words[0].split('@')[0].lower() if words else ''
            try:
                handle_command(subscribers, chat, command, send)
            except SafeError as exc:
                print('Subscriber reply failed: ' + str(exc), file=sys.stderr, flush=True)
        subscribers.advance(update['update_id'] + 1)  # Per update: a crash never replays more than one.


def listen_for_subscribers(subscribers, stop, poll=handle_updates):
    """Answer /start and /stop as they arrive. Errors never end the loop."""
    last_error = None
    while not stop.is_set():
        try:
            poll(subscribers)
            last_error = None
            stop.wait(1)  # Guards against a hot loop if a proxy answers long-polls instantly.
        except Exception as exc:
            message = str(exc) if isinstance(exc, SafeError) else 'unexpected error'
            if message != last_error:  # Log a persistent failure once, not every 10 seconds.
                print('Subscription listener: ' + message, file=sys.stderr, flush=True)
                last_error = message
            stop.wait(10)


def try_send(sender, text):
    try:
        sender(text)
        return True
    except SafeError as exc:
        print('Notification not delivered: ' + str(exc), file=sys.stderr, flush=True)
        return False


def run_daemon(store, interval, heartbeat_hours, stop, observer=observe, sender=send_telegram,
               clock=time.monotonic):
    """Check every `interval` seconds until `stop` is set; one bad cycle never ends the loop."""
    tally = collections.Counter()
    failures = 0
    failure_alerted = False
    next_heartbeat = clock() + heartbeat_hours * 3600 if heartbeat_hours else None
    while not stop.is_set():
        started = clock()
        try:
            status, reason, attempts = observer()
            now = dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')
            sent = process(store, status, now, sender=sender, reason=reason, attempts=attempts)
            tally[status] += 1
            failures = 0
            print(json.dumps({'checked_at': now, 'status': status, 'reason': reason,
                              'attempts': attempts, 'notifications_sent': sent}), flush=True)
            if failure_alerted and try_send(sender, '✅ G Hillstand 常驻监控已恢复正常。'):
                failure_alerted = False
        except SafeError as exc:
            failures += 1
            print('Check cycle failed: ' + str(exc), file=sys.stderr, flush=True)
        except Exception:
            failures += 1
            print('Check cycle failed unexpectedly.', file=sys.stderr, flush=True)
        if failures >= FAILURES_BEFORE_ALERT and not failure_alerted:
            # Retried each cycle until delivered, e.g. if Telegram itself is what is failing.
            failure_alerted = try_send(sender, '⚠️ G Hillstand 常驻监控连续 ' + str(failures)
                                       + ' 轮运行失败，可能没有完成查票。请查看运行日志；这不表示售罄。')
        if next_heartbeat is not None and clock() >= next_heartbeat:
            summary = '，'.join(label + ' ' + str(tally[key]) for key, label in
                               (('sold_out', '售罄'), ('available', '可选'), ('unknown', '无法确认')))
            if try_send(sender, '💓 G Hillstand 监控运行正常。过去 ' + format(heartbeat_hours, 'g')
                        + ' 小时检查 ' + str(sum(tally.values())) + ' 次：' + summary + '。'):
                tally.clear()
                next_heartbeat = clock() + heartbeat_hours * 3600
        stop.wait(max(0, interval - (clock() - started)))


def daemon_main():
    required('TELEGRAM_BOT_TOKEN')
    required('TELEGRAM_CHAT_ID')
    interval = env_int('CHECK_INTERVAL_SECONDS', 300, MIN_INTERVAL)
    heartbeat_hours = env_int('HEARTBEAT_HOURS', 24, 0)
    max_subscribers = env_int('MAX_SUBSCRIBERS', 20, 0)
    state_path = Path(os.environ.get('STATE_FILE', 'state.json'))
    store = FileState(state_path)
    store.load()  # Fail now, not hours later, if the state file is unusable.
    subscribers = Subscribers(state_path.with_name('subscribers.json'),
                              required('TELEGRAM_CHAT_ID'), max_subscribers)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    # A failing send here means bad Telegram settings: exit loudly instead of running deaf.
    send_telegram('✅ G Hillstand 常驻监控已启动：每 ' + str(interval) + ' 秒检查一次，'
                  + ('每 ' + str(heartbeat_hours) + ' 小时发送一次心跳。' if heartbeat_hours else '心跳已关闭。')
                  + ('\n朋友订阅：给 bot 发 /start 即可（当前 ' + str(subscribers.count()) + ' 位，上限 '
                     + str(max_subscribers) + '）。' if max_subscribers else '\n朋友订阅已关闭。')
                  + '\n' + URL)
    if max_subscribers:
        threading.Thread(target=listen_for_subscribers, args=(subscribers, stop), daemon=True).start()
    run_daemon(store, interval, heartbeat_hours, stop, sender=alert_sender(subscribers))
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
        if '--daemon' in sys.argv:
            sys.exit(daemon_main())
        sys.exit(failure_notice() if '--failure-notice' in sys.argv else main())
    except SafeError as exc:
        print('Monitor failed: ' + str(exc), file=sys.stderr)
        sys.exit(1)
    except Exception:
        print('Monitor failed unexpectedly; notification/state is not confirmed.', file=sys.stderr)
        sys.exit(1)
