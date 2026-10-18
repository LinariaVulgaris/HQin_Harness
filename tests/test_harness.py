import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import config
import main
import tools
import workspace


def _tool_call(call_id: str, name: str, arguments: str = "{}"):
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


class WorkspaceTestCase(unittest.TestCase):
    def setUp(self):
        self.original_cwd = os.getcwd()
        # 测试不写文件，直接使用受控项目目录，避免受限环境拒绝切换到系统临时目录。
        workspace.set_workspace(str(Path(__file__).resolve().parents[1]))

    def tearDown(self):
        workspace.set_workspace(self.original_cwd)


class ContextSafetyTests(unittest.TestCase):
    def test_summary_boundary_keeps_tool_result_with_its_call(self):
        messages = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "first task"},
            {"role": "assistant", "tool_calls": [{"id": "call_1"}]},
            {"role": "tool", "tool_call_id": "call_1", "content": "result"},
            {"role": "user", "content": "latest task"},
        ]

        self.assertEqual(main._summary_end_at_turn_boundary(messages, 3), 4)

    def test_hard_trim_removes_a_whole_user_turn(self):
        messages = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "old request" * 20},
            {"role": "assistant", "tool_calls": [{"id": "call_1"}]},
            {"role": "tool", "tool_call_id": "call_1", "content": "old result" * 20},
            {"role": "user", "content": "new request"},
            {"role": "assistant", "content": "new response"},
        ]
        with patch.object(config, "MAX_CHARS", 30):
            main._trim_messages_hard(messages)

        self.assertEqual([message["role"] for message in messages], ["system", "user", "assistant"])
        self.assertEqual(messages[1]["content"], "new request")


class ToolExecutionTests(WorkspaceTestCase):
    def test_mutating_tool_calls_are_serial(self):
        calls = [_tool_call("1", "edit_file"), _tool_call("2", "read_file")]
        seen = []

        def execute(name, args):
            seen.append(name)
            return "Success", 0.0

        stats = main._TaskStats()
        with patch.object(main, "_execute_tool", side_effect=execute), patch.object(
            main.console, "print"
        ), patch.object(main, "_print_tool_call"):
            result = main._run_tools(calls, stats, [0])

        self.assertEqual(seen, ["edit_file", "read_file"])
        self.assertEqual([message["tool_call_id"] for message in result], ["1", "2"])

    def test_invalid_tool_json_is_returned_as_a_tool_error(self):
        stats = main._TaskStats()
        with patch.object(main.console, "print"), patch.object(main, "_print_tool_call"):
            result = main._run_tools([_tool_call("1", "read_file", "not json")], stats, [0])

        self.assertTrue(result[0]["content"].startswith("Error:"))

    def test_git_runner_uses_argument_list_not_shell(self):
        completed = SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
        with patch("tools.subprocess.run", return_value=completed) as run:
            code, output = tools._git_run(["diff", "--", "a && not-a-command"])

        self.assertEqual((code, output), (0, ""))
        self.assertEqual(run.call_args.args[0], ["git", "diff", "--", "a && not-a-command"])
        self.assertFalse(run.call_args.kwargs["shell"])

    def test_failed_command_is_an_error(self):
        completed = SimpleNamespace(returncode=1, stdout=b"", stderr=b"failed")
        with patch("tools.Confirm.ask", return_value=True), patch(
            "tools.subprocess.run", return_value=completed
        ), patch.object(tools.console, "print"):
            result = tools.run_bash("exit 1")

        self.assertTrue(result.startswith("Error:"))


if __name__ == "__main__":
    unittest.main()
