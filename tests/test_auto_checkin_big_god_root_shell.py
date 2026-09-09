import shlex
import unittest
from unittest.mock import Mock

from tasks.AutoCheckinBigGod.script_task import ScriptTask


class AutoCheckinBigGodRootShellTest(unittest.TestCase):
    def test_su_runs_entire_compound_command_as_root(self) -> None:
        task = ScriptTask.__new__(ScriptTask)
        task._frida_root_mode = 'su'
        task._adb_shell = Mock(return_value='ok')
        command = (
            'rm -f /data/local/tmp/frida-server.log; '
            'nohup /data/local/tmp/frida-server '
            '>/data/local/tmp/frida-server.log 2>&1 </dev/null &'
        )

        result = task._adb_root_shell(command, timeout=5)

        self.assertEqual('ok', result)
        task._adb_shell.assert_called_once_with(
            [f'su 0 sh -c {shlex.quote(command)}'],
            timeout=5,
        )

    def test_adb_root_keeps_command_in_single_shell_argument(self) -> None:
        task = ScriptTask.__new__(ScriptTask)
        task._frida_root_mode = 'adb'
        task._adb_shell = Mock(return_value='uid=0(root)')
        command = 'id; id'

        result = task._adb_root_shell(command)

        self.assertEqual('uid=0(root)', result)
        task._adb_shell.assert_called_once_with([command], timeout=15)


if __name__ == '__main__':
    unittest.main()
