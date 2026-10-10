import queue
import unittest

from server.events import EventBroker


class TestEventBroker(unittest.TestCase):
    def test_publish_reaches_subscriber(self):
        broker = EventBroker()
        q = broker.subscribe()
        self.assertEqual(broker.subscriber_count, 1)
        broker.publish({"kind": "reading", "score": 1.0})
        self.assertEqual(broker.drain(q, timeout=0.1)["score"], 1.0)
        broker.unsubscribe(q)
        self.assertEqual(broker.subscriber_count, 0)

    def test_drain_times_out_cleanly(self):
        broker = EventBroker()
        q = broker.subscribe()
        self.assertIsNone(broker.drain(q, timeout=0.01))

    def test_slow_subscriber_does_not_block_or_raise(self):
        broker = EventBroker(max_queue=2)
        q = broker.subscribe()
        for i in range(20):
            broker.publish({"i": i})  # must never raise
        self.assertLessEqual(q.qsize(), 2)

    def test_multiple_subscribers_each_get_the_event(self):
        broker = EventBroker()
        a, b = broker.subscribe(), broker.subscribe()
        broker.publish({"kind": "roast", "text": "Sit up."})
        self.assertEqual(broker.drain(a, 0.1)["text"], "Sit up.")
        self.assertEqual(broker.drain(b, 0.1)["text"], "Sit up.")


if __name__ == "__main__":
    unittest.main()
