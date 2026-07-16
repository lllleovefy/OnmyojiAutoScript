"""Lightweight worker-process bridge for Duel live events."""

from module.logger import logger


class DuelLivePublisherMixin:
    def publish_bp_live_event(self, event: str, data: dict) -> None:
        try:
            from module.duel_data import publish_live_event
        except (ImportError, ModuleNotFoundError):
            return
        try:
            payload = dict(data)
            config = getattr(self, "config", None)
            config_name = getattr(config, "config_name", None)
            if config_name:
                payload.setdefault("config_name", str(config_name))
            publish_live_event(
                event,
                payload,
                event_queue=getattr(self, "duel_event_queue", None),
            )
        except Exception as exc:
            # Telemetry must never interrupt an active duel.
            logger.warning(f"Unable to publish Duel BP live event: {exc!r}")
