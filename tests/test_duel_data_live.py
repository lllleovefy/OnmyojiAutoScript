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
from module.duel_data.models import DuelLiveAction, DuelLiveState
from tasks.Duel.live import DuelLivePublisherMixin


class DuelLiveEventBrokerTest(unittest.IsolatedAsyncioTestCase):
    async def test_extended_state_contract_accepts_nullable_opponent_slots(self):
        state = DuelLiveState.model_validate(
            {
                "state": "SELF_PICK",
                "round": 2,
                "opponent_slots": [
                    {
                        "slot": 1,
                        "shishen_id": None,
                        "confidence": 0.41,
                        "source": "portrait",
                        "status": "unresolved",
                    }
                ],
                "pending_own_pick": 596,
                "selected_onmyoji": None,
                "action": "recommend",
            }
        )
        self.assertIsNone(state.opponent_slots[0].shishen_id)
        self.assertEqual(596, state.pending_own_pick)
        action = DuelLiveAction.model_validate(
            {
                "action": "click_candidate",
                "status": "success",
                "round": 2,
                "shishen_id": 596,
                "selected_verified": True,
                "confirmed": False,
                "candidate_rank": 2,
            }
        )
        self.assertEqual("success", action.status)
        self.assertTrue(action.selected_verified)
        self.assertFalse(action.confirmed)
        self.assertEqual(2, action.candidate_rank)

        legacy_action = DuelLiveAction.model_validate(
            {"action": "click_candidate"}
        )
        self.assertIsNone(legacy_action.selected_verified)
        self.assertIsNone(legacy_action.confirmed)
        self.assertIsNone(legacy_action.candidate_rank)

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

    async def test_snapshot_matches_filtered_sse_replay(self):
        broker = DuelLiveEventBroker()
        broker.publish(
            "state",
            {
                "state": "SELF_PICK",
                "round": 2,
                "confidence": 0.991,
                "config_name": "alpha",
            },
        )
        broker.publish(
            "recommendation",
            {
                "target_round": 2,
                "shishen_id": 596,
                "confidence": 0.97,
                "config_name": "alpha",
            },
        )
        broker.publish(
            "state",
            {"state": "BATTLE", "config_name": "beta"},
        )

        snapshot = broker.snapshot(config_name="alpha")

        self.assertEqual("SELF_PICK", snapshot["state"])
        self.assertEqual(2, snapshot["round"])
        self.assertEqual(596, snapshot["shishen_id"])
        self.assertEqual(0.991, snapshot["recognition_confidence"])
        self.assertEqual(0.97, snapshot["confidence"])
        self.assertGreater(snapshot["event_id"], 0)
        self.assertEqual("alpha", snapshot["config_name"])

    async def test_sse_ids_are_monotonic_and_state_clears_old_context(self):
        broker = DuelLiveEventBroker()
        broker.publish("state", {"state": "SELF_PICK"})
        broker.publish("recommendation", {"target_round": 1})
        stream = broker.stream(heartbeat_seconds=0.01)
        state = await anext(stream)
        recommendation = await anext(stream)
        self.assertIn("id: 1", state)
        self.assertIn("id: 2", recommendation)
        await stream.aclose()

        broker.publish("state", {"state": "OPPONENT_PICK"})
        snapshot = broker.snapshot()
        self.assertEqual("OPPONENT_PICK", snapshot["state"])
        self.assertNotIn("target_round", snapshot)

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

    async def test_new_state_clears_stale_action_replay(self):
        broker = DuelLiveEventBroker()
        broker.publish("state", {"state": "SELF_PICK", "config_name": "alpha"})
        broker.publish(
            "action",
            {
                "action": "confirm_candidate",
                "confirmed": True,
                "config_name": "alpha",
            },
        )
        broker.publish("state", {"state": "BAN", "config_name": "alpha"})

        stream = broker.stream(config_name="alpha", heartbeat_seconds=0.01)
        state = await anext(stream)
        self.assertIn('"state":"BAN"', state)
        self.assertEqual(": heartbeat\n\n", await anext(stream))
        await stream.aclose()

    async def test_action_does_not_clear_recommendation_and_replays_after_it(self):
        broker = DuelLiveEventBroker()
        broker.publish("state", {"state": "SELF_PICK", "config_name": "alpha"})
        broker.publish(
            "recommendation",
            {"shishen_id": 101, "config_name": "alpha"},
        )
        broker.publish(
            "action",
            {"action": "click_candidate", "config_name": "alpha"},
        )
        stream = broker.stream(config_name="alpha", heartbeat_seconds=0.01)
        self.assertIn("event: state", await anext(stream))
        self.assertIn("event: recommendation", await anext(stream))
        action = await anext(stream)
        self.assertIn("event: action", action)
        self.assertIn('"action":"click_candidate"', action)
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
