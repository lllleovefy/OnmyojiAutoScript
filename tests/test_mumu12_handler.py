import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from module.device.platform2.handlers.mumu12 import MuMu12Handler


class MuMu12InstanceDiscoveryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.vms_dir = Path(self.temp_dir.name)
        self.emulator = Mock()
        self.emulator.path = "D:/Program Files/Netease/MuMu/nx_main/MuMuNxMain.exe"
        self.emulator.list_folder.side_effect = self._list_folders

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _list_folders(self, folder: str, *, is_dir: bool = False) -> list[str]:
        self.assertEqual(folder, "../vms")
        self.assertTrue(is_dir)
        return [str(path) for path in self.vms_dir.iterdir() if path.is_dir()]

    def test_discovers_stopped_instance_without_nemu_file(self) -> None:
        (self.vms_dir / "MuMuPlayer-15.0-0").mkdir()

        instances = list(MuMu12Handler().iter_instances(self.emulator))

        self.assertEqual(len(instances), 1)
        self.assertEqual(instances[0].name, "MuMuPlayer-15.0-0")
        self.assertEqual(instances[0].serial, "127.0.0.1:16384")

    def test_prefers_serial_from_nemu_file(self) -> None:
        instance_dir = self.vms_dir / "MuMuPlayer-15.0-2"
        instance_dir.mkdir()
        (instance_dir / "MuMuPlayer-15.0-2.nemu").write_text(
            '<Forwarding name="adb" hostport="20000" guestport="5555"/>',
            encoding="utf-8",
        )

        instances = list(MuMu12Handler().iter_instances(self.emulator))

        self.assertEqual(len(instances), 1)
        self.assertEqual(instances[0].serial, "127.0.0.1:20000")

    def test_ignores_unrecognized_directory_without_nemu_file(self) -> None:
        (self.vms_dir / "backup").mkdir()

        instances = list(MuMu12Handler().iter_instances(self.emulator))

        self.assertEqual(instances, [])


if __name__ == "__main__":
    unittest.main()
