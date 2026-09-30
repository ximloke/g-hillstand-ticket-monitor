import copy
import io
import os
import tempfile
import unittest
from unittest.mock import patch
import monitor


INITIAL = {'version': 1, 'availability_notified': False,
           'outage_notified': False, 'unknown_count': 0}


class AvailabilityTests(unittest.TestCase):
    def snap(self, **kw):
        result = dict(correct_event=True, visible=True, title='G Hillstand',
                      radio_count=1, disabled=False, text='G Hillstand BHD 18.85')
        result.update(kw)
        return result

    def test_available_and_sold_out(self):
        self.assertEqual(monitor.classify(self.snap()), 'available')
        self.assertEqual(monitor.classify(self.snap(disabled=True, text='G Hillstand BHD 18.85 (Sold out)')), 'sold_out')

    def test_fail_closed(self):
        for changes in [dict(visible=False), dict(correct_event=False), dict(title='K2 Hillstand'),
                        dict(radio_count=2), dict(disabled=True), dict(text='G Hillstand'),
                        dict(text='G Hillstand BHD 18.85 Sold out'), dict(disabled=None)]:
            with self.subTest(changes=changes):
                self.assertEqual(monitor.classify(self.snap(**changes)), 'unknown')
        self.assertEqual(monitor.classify({}), 'unknown')

    def test_event_identity(self):
        self.assertTrue(monitor.correct_event(monitor.URL))
        self.assertFalse(monitor.correct_event(monitor.URL.replace(monitor.PERFORMANCE, 'wrong')))
        self.assertFalse(monitor.correct_event(monitor.URL.replace('tickets.bahraingp.com', 'audienceview.queue-it.net')))

    def test_local_currency_and_label_whitespace(self):
        self.assertEqual(monitor.classify(self.snap(title=' G  Hillstand ', text='G Hillstand RM 200.00')), 'available')
        self.assertEqual(monitor.classify(self.snap(text='G Hillstand MYR 200.00 Sold out')), 'unknown')

    def test_retry_only_unknown(self):
        checker = unittest.mock.Mock(side_effect=[('unknown', 'waiting_room'), ('sold_out', 'zone_verified')])
        sleep = unittest.mock.Mock()
        self.assertEqual(monitor.observe(checker, sleep), ('sold_out', 'zone_verified', 2))
        sleep.assert_called_once_with(10)
        checker = unittest.mock.Mock(return_value=('available', 'zone_verified'))
        self.assertEqual(monitor.observe(checker, sleep), ('available', 'zone_verified', 1))
        self.assertEqual(checker.call_count, 1)

    def test_retry_is_bounded(self):
        checker = unittest.mock.Mock(return_value=('unknown', 'waiting_room'))
        self.assertEqual(monitor.observe(checker, lambda _: None), ('unknown', 'waiting_room', 2))
        self.assertEqual(checker.call_count, 2)


class StateTests(unittest.TestCase):
    def test_dedup_restock_and_unknown(self):
        state = copy.deepcopy(INITIAL)
        messages = []
        for status in ['sold_out', 'available', 'available', 'unknown', 'available']:
            state, out = monitor.transition(state, status, 'now')
            messages += out
        self.assertEqual(len(messages), 1)
        state, _ = monitor.transition(state, 'sold_out', 'later')
        state, out = monitor.transition(state, 'available', 'later')
        self.assertEqual(len(out), 1)

    def test_outage_and_recovery(self):
        state = copy.deepcopy(INITIAL)
        messages = []
        for _ in range(10):
            state, out = monitor.transition(state, 'unknown', 'now')
            messages += out
        self.assertEqual(len(messages), 1)
        state, out = monitor.transition(state, 'sold_out', 'later')
        self.assertEqual(len(out), 1)
        self.assertFalse(state['outage_notified'])

    def test_unchanged_state_does_not_need_commit(self):
        state, _ = monitor.transition(INITIAL, 'sold_out', 'first')
        same, out = monitor.transition(state, 'sold_out', 'second')
        self.assertEqual(same, state)
        self.assertEqual(out, [])

    def test_failed_delivery_is_retryable(self):
        class Store:
            def __init__(self): self.state = copy.deepcopy(INITIAL)
            def load(self): return copy.deepcopy(self.state)
            def save(self, value): self.state = value
        store = Store()
        def fail(message): raise monitor.SafeError('simulated delivery failure')
        with self.assertRaises(monitor.SafeError):
            monitor.process(store, 'available', 'now', fail)
        self.assertFalse(store.state['availability_notified'])
        self.assertTrue(store.state['history'][-1]['notification_failed'])
        sent = []
        monitor.process(store, 'available', 'later', sent.append)
        monitor.process(store, 'available', 'later', sent.append)
        self.assertEqual(len(sent), 1)

    def test_audit_preserves_unknown_and_bounds_history(self):
        state = copy.deepcopy(INITIAL)
        monitor.audit(state, 'sold_out', 'first', 'zone_verified', 1, 0)
        for i in range(monitor.HISTORY_LIMIT + 5):
            monitor.audit(state, 'unknown', str(i), 'waiting_room', 2, 0)
        self.assertEqual(len(state['history']), monitor.HISTORY_LIMIT)
        self.assertEqual(state['last_successful_check_at'], 'first')
        self.assertEqual(state['history'][-1]['reason'], 'waiting_room')

    def test_unchanged_checks_are_still_audited(self):
        class Store:
            state = copy.deepcopy(INITIAL)
            def load(self): return copy.deepcopy(self.state)
            def save(self, value): self.state = value
        store = Store()
        sent = []
        monitor.process(store, 'sold_out', 'first', sent.append)
        monitor.process(store, 'sold_out', 'second', sent.append)
        self.assertEqual(len(store.state['history']), 2)
        self.assertEqual(store.state['last_checked_at'], 'second')
        self.assertEqual(sent, [])

    def test_failure_notice_deduplicated(self):
        with patch('monitor.Path.exists', return_value=False), patch('monitor.GithubState') as factory, patch('monitor.send_telegram') as send:
            factory.return_value.load.return_value = {'version': 1, 'pipeline_failure_notified': True}
            monitor.failure_notice()
            send.assert_not_called()


class FileStateTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, 'nested', 'state.json')

    def test_missing_file_starts_fresh_and_roundtrips(self):
        store = monitor.FileState(self.path)
        self.assertEqual(store.load(), INITIAL)
        store.save({'version': 1, 'unknown_count': 2})
        self.assertEqual(monitor.FileState(self.path).load(), {'version': 1, 'unknown_count': 2})
        self.assertEqual(os.listdir(os.path.dirname(self.path)), ['state.json'])

    def test_corrupt_or_foreign_state_is_never_reset(self):
        os.makedirs(os.path.dirname(self.path))
        for content in ['{"version": 1', '[]', '{"version": 2}']:
            with self.subTest(content=content):
                with open(self.path, 'w') as f:
                    f.write(content)
                with self.assertRaises(monitor.SafeError):
                    monitor.FileState(self.path).load()

    def test_env_int(self):
        with patch.dict(os.environ, {'X': '120'}):
            self.assertEqual(monitor.env_int('X', 300, 60), 120)
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(monitor.env_int('X', 300, 60), 300)
        for bad in ['abc', '59']:
            with patch.dict(os.environ, {'X': bad}), self.assertRaises(monitor.SafeError):
                monitor.env_int('X', 300, 60)


class DaemonTests(unittest.TestCase):
    class Stop:
        """Stands in for threading.Event: runs `cycles` loop iterations, then stops."""
        def __init__(self, cycles, clock=None, step=300):
            self.cycles, self.waits, self.clock, self.step = cycles, [], clock, step
        def is_set(self): return len(self.waits) >= self.cycles
        def wait(self, seconds):
            self.waits.append(seconds)
            if self.clock: self.clock.now += self.step

    class Clock:
        now = 0.0
        def __call__(self): return self.now

    def go(self, observations, cycles, heartbeat_hours=0, store=None, sender=None):
        store = store or monitor.FileState(os.path.join(self.dir.name, 'state.json'))
        sent = []
        clock = self.Clock()
        stop = self.Stop(cycles, clock)
        observer = unittest.mock.Mock(side_effect=observations)
        monitor.run_daemon(store, 300, heartbeat_hours, stop, observer=observer,
                           sender=sender or sent.append, clock=clock)
        return store, sent, stop, observer

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        for stream in ('stdout', 'stderr'):  # The daemon logs every cycle.
            patcher = patch('sys.' + stream, io.StringIO())
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_alerts_once_and_keeps_history_across_cycles(self):
        obs = [('sold_out', 'zone_verified', 1), ('available', 'zone_verified', 1),
               ('available', 'zone_verified', 1), ('sold_out', 'zone_verified', 1),
               ('available', 'zone_verified', 1)]
        store, sent, stop, _ = self.go(obs, 5)
        self.assertEqual(len(sent), 2)
        self.assertTrue(all('可选' in m for m in sent))
        self.assertEqual(len(store.load()['history']), 5)
        self.assertEqual(stop.waits, [300] * 5)

    def test_state_survives_restart(self):
        store, sent, _, _ = self.go([('available', 'zone_verified', 1)], 1)
        _, sent2, _, _ = self.go([('available', 'zone_verified', 1)], 1, store=store)
        self.assertEqual((len(sent), len(sent2)), (1, 0))

    def test_cycle_error_does_not_stop_loop_and_escalates_once(self):
        ok = ('sold_out', 'zone_verified', 1)
        obs = [RuntimeError('boom')] * 4 + [ok]
        store, sent, _, observer = self.go(obs, 5)
        self.assertEqual(observer.call_count, 5)
        self.assertEqual([('运行失败' in m, '已恢复' in m) for m in sent], [(True, False), (False, True)])
        self.assertEqual(len(sent), 2)

    def test_failure_alert_retried_until_delivered(self):
        calls = []
        def flaky(message):
            calls.append(message)
            if len(calls) == 1:
                raise monitor.SafeError('telegram down')
        _, _, _, _ = self.go([RuntimeError('x')] * 5, 5, sender=flaky)
        self.assertEqual(len(calls), 2)  # cycle 3's alert fails, cycle 4 delivers, cycle 5 is quiet

    def test_unknown_uses_outage_policy_not_loop_failure(self):
        obs = [('unknown', 'waiting_room', 2)] * 4
        _, sent, _, _ = self.go(obs, 4)
        self.assertEqual(len(sent), 1)
        self.assertIn('无法确认库存', sent[0])

    def test_heartbeat_reports_counts_and_resets(self):
        obs = [('sold_out', 'zone_verified', 1)] * 3 + [('unknown', 'waiting_room', 2)] * 1
        # Checks run at fake t=0, 300, 600, 900; the heartbeat is due at t=800, so on the 4th.
        store, sent, _, _ = self.go(obs, 4, heartbeat_hours=800 / 3600)
        beats = [m for m in sent if '💓' in m]
        self.assertEqual(len(beats), 1)
        self.assertIn('检查 4 次', beats[0])
        self.assertIn('售罄 3', beats[0])
        self.assertIn('无法确认 1', beats[0])

    def test_sleep_accounts_for_check_duration(self):
        clock = self.Clock()
        def slow():
            clock.now += 100
            return ('sold_out', 'zone_verified', 1)
        stop = self.Stop(1)
        monitor.run_daemon(monitor.FileState(os.path.join(self.dir.name, 's.json')), 300, 0, stop,
                           observer=slow, sender=lambda m: None, clock=clock)
        self.assertEqual(stop.waits, [200])


if __name__ == '__main__':
    unittest.main()
