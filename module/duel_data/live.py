from __future__ import annotations

import asyncio
import json
import threading
import uuid
from dataclasses import dataclass
from typing import Any, AsyncIterator

from module.duel_data.security import sanitize_for_storage


LIVE_EVENT_TYPES = frozenset({"state", "recommendation", "match"})
DUEL_LIVE_QUEUE_KEY = "duel_live"


@dataclass
class _Subscriber:
    loop: asyncio.AbstractEventLoop
    queue: asyncio.Queue[tuple[str, Any]]
    config_name: str | None = None


class DuelLiveEventBroker:
    """Small in-process fan-out broker safe to publish from task worker threads."""

    def __init__(self, *, queue_size: int = 100):
        self._queue_size = queue_size
        self._lock = threading.Lock()
        self._subscribers: dict[str, _Subscriber] = {}
        self._latest: dict[tuple[str | None, str], Any] = {}

    def publish(self, event: str, data: Any) -> None:
        if event not in LIVE_EVENT_TYPES:
            raise ValueError(f"Unsupported duel live event: {event}")
        payload = sanitize_for_storage(data)
        event_config = None
        if isinstance(payload, dict) and payload.get("config_name"):
            event_config = str(payload["config_name"])
        with self._lock:
            # Every accepted state/context update invalidates the previous
            # recommendation. A following recommendation event repopulates it
            # for that exact fingerprint.
            if event == "state":
                self._latest.pop((event_config, "recommendation"), None)
            self._latest[(event_config, event)] = payload
            subscribers = list(self._subscribers.items())
        for subscriber_id, subscriber in subscribers:
            if (
                event_config is not None
                and subscriber.config_name is not None
                and event_config != subscriber.config_name
            ):
                continue
            def enqueue(target: _Subscriber = subscriber) -> None:
                if target.queue.full():
                    try:
                        target.queue.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                try:
                    target.queue.put_nowait((event, payload))
                except asyncio.QueueFull:
                    pass

            try:
                subscriber.loop.call_soon_threadsafe(enqueue)
            except RuntimeError:
                with self._lock:
                    self._subscribers.pop(subscriber_id, None)

    async def stream(
        self,
        *,
        heartbeat_seconds: float = 15.0,
        config_name: str | None = None,
    ) -> AsyncIterator[str]:
        normalized_config = str(config_name).strip() if config_name else None
        subscriber_id = uuid.uuid4().hex
        subscriber = _Subscriber(
            loop=asyncio.get_running_loop(),
            queue=asyncio.Queue(maxsize=self._queue_size),
            config_name=normalized_config,
        )
        with self._lock:
            self._subscribers[subscriber_id] = subscriber
            replay = [
                (event, payload)
                for (event_config, event), payload in self._latest.items()
                if event_config is None
                or normalized_config is None
                or event_config == normalized_config
            ]
        try:
            replay.sort(
                key=lambda item: (
                    0 if item[0] == "state" else 1 if item[0] == "recommendation" else 2,
                    item[0],
                )
            )
            if not any(event == "state" for event, _ in replay):
                yield self.encode(
                    "state",
                    {
                        "state": "idle",
                        **(
                            {"config_name": normalized_config}
                            if normalized_config
                            else {}
                        ),
                    },
                )
            for event, payload in replay:
                yield self.encode(event, payload)
            while True:
                try:
                    event, data = await asyncio.wait_for(subscriber.queue.get(), timeout=heartbeat_seconds)
                    yield self.encode(event, data)
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
        finally:
            with self._lock:
                self._subscribers.pop(subscriber_id, None)

    @staticmethod
    def encode(event: str, data: Any) -> str:
        payload = json.dumps(sanitize_for_storage(data), ensure_ascii=False, separators=(",", ":"), default=str)
        return f"event: {event}\ndata: {payload}\n\n"


duel_live_broker = DuelLiveEventBroker()


def publish_live_event(event: str, data: Any, *, event_queue: Any = None) -> None:
    """Publish locally or bridge a sanitized event out of a task process."""
    if event not in LIVE_EVENT_TYPES:
        raise ValueError(f"Unsupported duel live event: {event}")
    payload = sanitize_for_storage(data)
    if event_queue is not None:
        event_queue.put(
            {
                DUEL_LIVE_QUEUE_KEY: {
                    "event": event,
                    "data": payload,
                }
            }
        )
        return
    duel_live_broker.publish(event, payload)


def relay_queued_live_event(envelope: Any) -> bool:
    """Relay a worker-process envelope into the API process SSE broker."""
    if not isinstance(envelope, dict) or DUEL_LIVE_QUEUE_KEY not in envelope:
        return False
    item = envelope.get(DUEL_LIVE_QUEUE_KEY)
    if not isinstance(item, dict):
        return True
    event = item.get("event")
    if event not in LIVE_EVENT_TYPES:
        return True
    duel_live_broker.publish(str(event), item.get("data"))
    return True
