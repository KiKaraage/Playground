# SPDX-License-Identifier: Apache-2.0
import threading
import unittest

from luma_playground.core import events


class EventBusTests(unittest.TestCase):
    def test_topic_and_wildcard(self):
        bus = events.EventBus()
        seen, everything = [], []
        bus.subscribe("goal.changed", lambda **p: seen.append(p))
        bus.subscribe(None, lambda topic, **p: everything.append((topic, p)))
        bus.publish("goal.changed", goal_id="a")
        bus.publish("activity.appended", entry={})
        self.assertEqual(seen, [{"goal_id": "a"}])
        self.assertEqual([t for t, _ in everything], ["goal.changed", "activity.appended"])

    def test_unsubscribe(self):
        bus = events.EventBus()
        seen = []
        token = bus.subscribe("x", lambda **p: seen.append(p))
        bus.unsubscribe(token)
        bus.unsubscribe(token)  # twice is harmless
        bus.publish("x")
        self.assertEqual(seen, [])

    def test_a_failing_listener_does_not_stop_others(self):
        bus = events.EventBus()
        seen = []

        def broken(**_):
            raise RuntimeError("boom")

        bus.subscribe("x", broken)
        bus.subscribe("x", lambda **p: seen.append(1))
        with self.assertLogs("luma_playground.core.events", level="ERROR"):
            bus.publish("x")
        self.assertEqual(seen, [1])

    def test_listener_may_unsubscribe_while_called(self):
        bus = events.EventBus()
        calls = []
        token = None

        def once(**_):
            calls.append(1)
            bus.unsubscribe(token)

        token = bus.subscribe("x", once)
        bus.publish("x")
        bus.publish("x")
        self.assertEqual(calls, [1])

    def test_threads(self):
        bus = events.EventBus()
        count = []
        lock = threading.Lock()

        def add(**_):
            with lock:
                count.append(1)

        bus.subscribe("x", add)
        workers = [threading.Thread(target=lambda: [bus.publish("x") for _ in range(200)]) for _ in range(8)]
        for w in workers:
            w.start()
        for w in workers:
            w.join()
        self.assertEqual(len(count), 1600)

    def test_module_bus_exists(self):
        self.assertIsInstance(events.bus, events.EventBus)


if __name__ == "__main__":
    unittest.main()
