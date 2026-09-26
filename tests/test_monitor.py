import copy
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


if __name__ == '__main__':
    unittest.main()
