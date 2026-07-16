from __future__ import annotations

import asyncio
import queue
import threading
import unittest
from unittest.mock import patch

from module.duel_data.live import (
    DuelLiveEventBroker,
    publish_live_event,
    relay_queued_live_event,
)
from tasks.Duel.live import DuelLivePublisherMixin


class DuelLiveEventBrokerTest(unittest.IsolatedAsyncioTestCase):
    async def test_thread_safe_publish_and_sanitized_sse(self):
        broker = DuelLiveEventBroker(queue_size=2)
        stream = broker.stream(heartbeat_seconds=60)
        initial = await anext(stream)
        self.assertIn("event: state", initial)

        thread = threading.Thread(
            target=broker.publish,
            args=("recommendation", {"pick": 101, "Authorization": "Bearer secret-value"}),
        )
        thread.start()
        thread.join()
        event = await asyncio.wait_for(anext(stream), timeout=1)
        self.assertIn("event: recommendation", event)
        self.assertIn('"pick":101', event)
        self.assertNotIn("secret-value", event)
        self.assertNotIn("Authorization", event)
        await stream.aclose()

    async def test_rejects_unknown_event_type(self):
        broker = DuelLiveEventBroker()
        with self.assertRaises(ValueError):
            broker.publish("unknown", {})

    async def test_reconnect_replays_last_value_and_filters_other_configs(self):
        broker = DuelLiveEventBroker()
        broker.publish("state", {"state": "SELF_PICK", "config_name": "alpha"})
        broker.publish(
            "recommendation",
            {"shishen_id": 101, "config_name": "alpha"},
        )
        broker.publish("state", {"state": "BATTLE", "config_name": "beta"})

        alpha_stream = broker.stream(config_name="alpha", heartbeat_seconds=0.01)
        self.assertIn('"state":"SELF_PICK"', await anext(alpha_stream))
        self.assertIn('"shishen_id":101', await anext(alpha_stream))
        await alpha_stream.aclose()

        beta_stream = broker.stream(config_name="beta", heartbeat_seconds=0.01)
        beta_state = await anext(beta_stream)
        self.assertIn('"state":"BATTLE"', beta_state)
        self.assertNotIn("alpha", beta_state)
        broker.publish(
            "recommendation",
            {"shishen_id": 102, "config_name": "alpha"},
        )
        self.assertEqual(": heartbeat\n\n", await anext(beta_stream))
        await beta_stream.aclose()

    async def test_result_clears_stale_recommendation_replay(self):
        broker = DuelLiveEventBroker()
        broker.publish("state", {"state": "SELF_PICK", "config_name": "alpha"})
        broker.publish(
            "recommendation",
            {"shishen_id": 101, "config_name": "alpha"},
        )
        broker.publish("state", {"state": "RESULT", "config_name": "alpha"})
        stream = broker.stream(config_name="alpha", heartbeat_seconds=0.01)
        self.assertIn('"state":"RESULT"', await anext(stream))
        self.assertEqual(": heartbeat\n\n", await anext(stream))
        await stream.aclose()

    async def test_worker_queue_bridge_sanitizes_and_relays_in_api_process(self):
        event_queue = queue.Queue()
        publish_live_event(
            "state",
            {"state": "SELF_PICK", "Authorization": "Bearer private-value"},
            event_queue=event_queue,
        )
        envelope = event_queue.get_nowait()
        self.assertNotIn("private-value", repr(envelope))
        self.assertNotIn("Authorization", repr(envelope))
        with patch("module.duel_data.live.duel_live_broker.publish") as publish:
            self.assertTrue(relay_queued_live_event(envelope))
        publish.assert_called_once_with("state", {"state": "SELF_PICK"})

    async def test_non_duel_state_queue_payload_is_not_consumed(self):
        self.assertFalse(relay_queued_live_event({"schedule": []}))

    async def test_duel_task_instance_publishes_to_injected_process_queue(self):
        task = DuelLivePublisherMixin()
        task.config = type("Config", (), {"config_name": "fixture"})()
        task.duel_event_queue = queue.Queue()
        task.publish_bp_live_event("state", {"state": "BAN"})
        envelope = task.duel_event_queue.get_nowait()
        self.assertEqual("state", envelope["duel_live"]["event"])
        self.assertEqual("BAN", envelope["duel_live"]["data"]["state"])
        self.assertEqual("fixture", envelope["duel_live"]["data"]["config_name"])


if __name__ == "__main__":
    unittest.main()
