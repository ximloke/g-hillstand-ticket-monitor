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
        self.assertEqual(store.state, INITIAL)
        sent = []
        monitor.process(store, 'available', 'later', sent.append)
        monitor.process(store, 'available', 'later', sent.append)
        self.assertEqual(len(sent), 1)


if __name__ == '__main__':
    unittest.main()
