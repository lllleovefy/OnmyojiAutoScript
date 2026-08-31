import unittest
from unittest.mock import AsyncMock, patch

from module.server.main_manager import MainManager


class MainManagerStartupTest(unittest.IsolatedAsyncioTestCase):
    async def test_restart_processes_starts_in_order_with_interval(self) -> None:
        events: list[str] = []

        first = AsyncMock()
        first.start.side_effect = lambda: events.append('start:first')
        second = AsyncMock()
        second.start.side_effect = lambda: events.append('start:second')
        third = AsyncMock()
        third.start.side_effect = lambda: events.append('start:third')

        manager = MainManager.__new__(MainManager)
        manager.script_process = {
            'first': first,
            'second': second,
            'third': third,
        }

        async def record_sleep(seconds: float) -> None:
            events.append(f'sleep:{seconds:g}')

        with patch('module.server.main_manager.asyncio.sleep', side_effect=record_sleep):
            await manager.restart_processes(
                ['first', 'second', 'third'],
                startup_interval_seconds=60,
            )

        self.assertEqual(
            [
                'start:first',
                'sleep:60',
                'start:second',
                'sleep:60',
                'start:third',
            ],
            events,
        )

    async def test_restart_processes_can_disable_interval(self) -> None:
        first = AsyncMock()
        second = AsyncMock()
        manager = MainManager.__new__(MainManager)
        manager.script_process = {'first': first, 'second': second}

        with patch('module.server.main_manager.asyncio.sleep') as sleep:
            await manager.restart_processes(
                ['first', 'second'],
                startup_interval_seconds=0,
            )

        first.start.assert_awaited_once_with()
        second.start.assert_awaited_once_with()
        sleep.assert_not_called()


if __name__ == '__main__':
    unittest.main()
