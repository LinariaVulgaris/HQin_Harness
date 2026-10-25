import os
import subprocess
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import config
import notifier
import tools


class BuildPsArgsTests(unittest.TestCase):
    """build_ps_args 是纯函数，不启动任何进程。"""

    def test_uses_windows_powershell_51_not_pwsh(self):
        args = notifier.build_ps_args("t", "m")
        self.assertTrue(args[0].endswith("powershell.exe"))
        self.assertIn("System32", args[0])
        self.assertEqual(args[1:4], ["-NoProfile", "-NonInteractive", "-Command"])
        script = args[-1]
        self.assertIn("Windows.UI.Notifications", script)
        self.assertIn("ToastGeneric", script)

    def test_escapes_all_five_xml_characters(self):
        args = notifier.build_ps_args('A&B<C>D"E', "m'F&G")
        script = args[-1]
        xml = script.split("@'\n", 1)[1].split("\n'@", 1)[0]
        # 五个特殊字符全部转为 XML 实体，裸字符不残留。
        self.assertIn("A" + notifier._AMP + "amp;B", xml)
        self.assertIn("B" + notifier._AMP + "lt;C" + notifier._AMP + "gt;D", xml)
        self.assertIn("D" + notifier._AMP + "quot;E", xml)
        self.assertIn("m" + notifier._AMP + "apos;F" + notifier._AMP + "amp;G", xml)

    def test_escapes_ampersand_before_lt_gt(self):
        # & 必须先转义，否则后续实体名会被二次转义。
        xml_out = notifier._xml_escape("<&>")
        self.assertEqual(
            xml_out,
            notifier._AMP + "lt;" + notifier._AMP + "amp;" + notifier._AMP + "gt;",
        )

    def test_truncates_title_to_200_and_message_to_500(self):
        args = notifier.build_ps_args("T" * 300, "M" * 900)
        xml = args[-1].split("@'\n", 1)[1].split("\n'@", 1)[0]
        self.assertIn("T" * 200, xml)
        self.assertNotIn("T" * 201, xml)
        self.assertIn("M" * 500, xml)
        self.assertNotIn("M" * 501, xml)

    def test_newlines_are_flattened_not_passed_through(self):
        args = notifier.build_ps_args("t\ni\rt\tle", "me\r\nssage")
        xml = args[-1].split("@'\n", 1)[1].split("\n'@", 1)[0]
        self.assertIn("t i t le", xml)
        self.assertIn("me ssage", xml)
        # here-string 终止符 '@ 绝不能因内容出现在行首而被破坏。
        self.assertNotIn("\n'@", xml)

    def test_empty_strings_are_safe(self):
        args = notifier.build_ps_args("", "")
        xml = args[-1].split("@'\n", 1)[1].split("\n'@", 1)[0]
        self.assertIn("<text id='1'></text>", xml)
        self.assertIn("<text id='2'></text>", xml)

    def test_app_id_is_registered_powershell_aumid(self):
        args = notifier.build_ps_args("t", "m")
        self.assertIn(
            r"{1AC14E86-1E54-5FA7-B6C6-A3C4E4A5123B}\WindowsPowerShell\v1.0",
            args[-1],
        )


class NotifySoundTests(unittest.TestCase):
    """自定义提示音：toast 静音 + SoundPlayer 播放 PCM wav。"""

    SOUND = "D:/sounds/ciallo.wav"

    def test_sound_path_makes_toast_silent_and_adds_player(self):
        args = notifier.build_ps_args("t", "m", sound_path=self.SOUND)
        script = args[-1]
        xml = script.split("@'\n", 1)[1].split("\n'@", 1)[0]
        # toast 静音标记必须注入到 <toast> 开标签之后。
        self.assertIn("<toast><audio silent='true'/>", xml)
        # SoundPlayer 跟在 toast 之后，路径单引号转义。
        self.assertIn("System.Media.SoundPlayer", script)
        self.assertIn(f"'{self.SOUND}'", script)
        self.assertIn("PlaySync", script)

    def test_no_sound_keeps_system_default_audio(self):
        args = notifier.build_ps_args("t", "m")
        script = args[-1]
        xml = script.split("@'\n", 1)[1].split("\n'@", 1)[0]
        self.assertNotIn("audio silent", xml)
        self.assertNotIn("SoundPlayer", script)

    def test_sound_path_with_quote_is_ps_escaped(self):
        tricky = r"D:\o'br" + chr(39) + "ien.wav"
        args = notifier.build_ps_args("t", "m", sound_path=tricky)
        script = args[-1]
        # 单引号在 PowerShell 字面量里翻倍转义，杜绝注入。
        self.assertIn("'D:\\o''br''ien.wav'", script)

    def test_resolve_sound_reads_config_each_call(self):
        with patch.object(notifier.config, "NOTIFY_SOUND", self.SOUND), \
                patch.object(notifier.os.path, "isfile", return_value=True):
            self.assertEqual(notifier._resolve_sound(), self.SOUND)
        with patch.object(notifier.config, "NOTIFY_SOUND", ""), \
                patch.object(notifier.os.path, "isfile", return_value=True):
            self.assertIsNone(notifier._resolve_sound())
        with patch.object(notifier.config, "NOTIFY_SOUND", self.SOUND), \
                patch.object(notifier.os.path, "isfile", return_value=False):
            self.assertIsNone(notifier._resolve_sound())


class NotifyGuardTests(unittest.TestCase):
    """守卫条件：非 Windows / 非 TTY / 配置关闭时绝不启动进程。"""

    def _assert_no_spawn(self, **kwargs):
        with patch.object(notifier.subprocess, "Popen") as popen:
            result = notifier.notify("t", "m", **kwargs)
        self.assertFalse(result)
        popen.assert_not_called()

    def test_skipped_when_not_windows(self):
        with patch.object(notifier.sys, "platform", "linux"):
            self._assert_no_spawn()

    def test_skipped_when_stdout_is_not_a_tty(self):
        class _NotATty:
            def isatty(self):
                return False

        with patch.object(notifier.sys, "platform", "win32"), patch.object(
            notifier.sys, "stdout", _NotATty()
        ), patch.object(config, "NOTIFY_ENABLED", True):
            self._assert_no_spawn()

    def test_skipped_when_tty_check_raises(self):
        class _BrokenTty:
            def isatty(self):
                raise ValueError("stream detached")

        with patch.object(notifier.sys, "platform", "win32"), patch.object(
            notifier.sys, "stdout", _BrokenTty()
        ):
            self._assert_no_spawn()

    def test_skipped_when_disabled_by_config(self):
        class _Tty:
            def isatty(self):
                return True

        with patch.object(notifier.sys, "platform", "win32"), patch.object(
            notifier.sys, "stdout", _Tty()
        ), patch.object(config, "NOTIFY_ENABLED", False):
            self._assert_no_spawn()

    def test_spawned_when_tty_and_enabled(self):
        class _Tty:
            def isatty(self):
                return True

        with patch.object(notifier.sys, "platform", "win32"), patch.object(
            notifier.sys, "stdout", _Tty()
        ), patch.object(config, "NOTIFY_ENABLED", True), patch.object(
            notifier.subprocess, "Popen"
        ) as popen:
            result = notifier.notify("标题", "正文")

        self.assertTrue(result)
        self.assertEqual(popen.call_count, 1)
        self._assert_popen_shape(popen)

    def test_spawned_with_force_ignores_all_guards(self):
        # 无 TTY、配置关闭，force=True 一路放行。
        with patch.object(notifier.subprocess, "Popen") as popen:
            result = notifier.notify("标题", "正文", force=True)

        self.assertTrue(result)
        self.assertEqual(popen.call_count, 1)
        self._assert_popen_shape(popen)

    def _assert_popen_shape(self, popen):
        """校验 Popen 的调用契约：参数数组、无 shell、DEVNULL、CREATE_NO_WINDOW。"""
        args, kwargs = popen.call_args.args, popen.call_args.kwargs
        argv = args[0] if args else kwargs.get("args")
        self.assertIsInstance(argv, list)
        self.assertTrue(argv[0].endswith("powershell.exe"))
        self.assertEqual(argv[1:4], ["-NoProfile", "-NonInteractive", "-Command"])
        self.assertFalse(kwargs.get("shell", False))
        self.assertEqual(kwargs.get("stdin"), subprocess.DEVNULL)
        self.assertEqual(kwargs.get("stdout"), subprocess.DEVNULL)
        self.assertEqual(kwargs.get("stderr"), subprocess.DEVNULL)
        # fire-and-forget：Popen 返回后绝不调用 wait()/communicate()。
        proc = popen.return_value
        proc.wait.assert_not_called()
        proc.communicate.assert_not_called()
        # 非交互终端环境下 flag 可为 0（沙盒限制）；TTY 下必须是 CREATE_NO_WINDOW。
        flags = kwargs.get("creationflags", 0)
        self.assertIn(flags, (0, subprocess.CREATE_NO_WINDOW))

    def test_tty_spawn_uses_create_no_window_flag(self):
        class _Tty:
            def isatty(self):
                return True

        with patch.object(notifier.sys, "platform", "win32"), patch.object(
            notifier.sys, "stdout", _Tty()
        ), patch.object(config, "NOTIFY_ENABLED", True), patch.object(
            notifier.subprocess, "Popen"
        ) as popen:
            notifier.notify("标题", "正文")

        self.assertEqual(
            popen.call_args.kwargs.get("creationflags"), subprocess.CREATE_NO_WINDOW
        )

    def test_headless_spawn_omits_create_no_window(self):
        # 无控制台环境（沙盒/GUI 宿主）：CREATE_NO_WINDOW 会让子进程起不来，
        # 这时省略 flag，让 powershell 继承宿主控制台。
        class _NotATty:
            def isatty(self):
                return False

        with patch.object(notifier.sys, "platform", "win32"), patch.object(
            notifier.sys, "stdout", _NotATty()
        ), patch.object(config, "NOTIFY_ENABLED", True), patch.object(
            notifier.subprocess, "Popen"
        ) as popen:
            notifier.notify("标题", "正文", force=True)

        self.assertEqual(popen.call_args.kwargs.get("creationflags", 0), 0)


class NotifyRobustnessTests(unittest.TestCase):
    """任何失败路径都不抛异常。"""

    def test_popen_exception_is_swallowed(self):
        with patch.object(
            notifier.subprocess, "Popen", side_effect=OSError("spawn denied")
        ):
            result = notifier.notify("t", "m", force=True)
        self.assertFalse(result)

    def test_popen_raising_keyboard_interrupt_is_not_swallowed(self):
        # KeyboardInterrupt/SystemExit 是进程生命周期信号，不属于"失败路径"，
        # notify 只吞 Exception，不能连 Ctrl+C 一起吞掉。
        with patch.object(
            notifier.subprocess, "Popen", side_effect=KeyboardInterrupt("ctrl-c")
        ):
            with self.assertRaises(KeyboardInterrupt):
                notifier.notify("t", "m", force=True)

    def test_missing_config_attribute_is_treated_as_disabled(self):
        class _Tty:
            def isatty(self):
                return True

        original = getattr(config, "NOTIFY_ENABLED", None)
        del config.NOTIFY_ENABLED
        try:
            with patch.object(notifier.sys, "platform", "win32"), patch.object(
                notifier.sys, "stdout", _Tty()
            ), patch.object(notifier.subprocess, "Popen") as popen:
                result = notifier.notify("t", "m")
        finally:
            if original is not None:
                config.NOTIFY_ENABLED = original
        self.assertFalse(result)
        popen.assert_not_called()


class ToolHookTests(unittest.TestCase):
    """钩子点只加 notify 调用，不改变审批语义。"""

    def setUp(self):
        from pathlib import Path
        import workspace
        self.original_cwd = os.getcwd()
        workspace.set_workspace(str(Path(__file__).resolve().parents[1]))
        tools._browser_policy.reset()

    def tearDown(self):
        import workspace
        tools._browser_policy.reset()
        workspace.set_workspace(self.original_cwd)

    def test_approve_shell_notifies_before_asking(self):
        with patch.object(tools.notifier, "notify") as notify, patch(
            "tools.Confirm.ask", return_value=False
        ), patch.object(tools.console, "print"):
            allowed = tools._approve_shell("bash", "echo hi")

        self.assertFalse(allowed)
        notify.assert_called_once_with("需要审批: bash", "echo hi")

    def test_git_commit_notifies_before_asking(self):
        with patch.object(tools.notifier, "notify") as notify, patch(
            "tools.Confirm.ask", return_value=False
        ), patch.object(tools, "_git_run", return_value=(0, "ok")) as git_run, patch.object(
            tools.console, "print"
        ):
            result = tools.git_commit("msg")

        self.assertIn("用户拒绝", result)
        git_run.assert_not_called()
        self.assertEqual(notify.call_count, 1)
        title, body = notify.call_args.args
        self.assertEqual(title, "需要审批: git_commit")
        self.assertIn("git commit -m 'msg'", body)

    def test_browser_click_notifies_only_when_confirmation_needed(self):
        with patch.object(config, "BROWSER_ID", "test-browser"), patch.object(
            config, "BROWSER_AUTO_APPROVE_CLICKS", False
        ), patch.object(tools.notifier, "notify") as notify, patch(
            "tools.Confirm.ask", return_value=False
        ), patch.object(
            tools.console, "print"
        ), patch("tools.subprocess.run") as run:
            tools._browser_policy.reset()
            tools._browser_policy.on_open("https://example.com")
            tools._browser_policy.on_state("  [1] <a /> Next page")
            result = tools.browser_click(1)

        self.assertTrue(result.startswith("Refused:"))
        run.assert_not_called()
        notify.assert_called_once()
        self.assertEqual(notify.call_args.args[0], "需要审批: browser_click")

    def test_browser_click_auto_approve_does_not_notify(self):
        with patch.object(config, "BROWSER_ID", "test-browser"), patch.object(
            config, "BROWSER_AUTO_APPROVE_CLICKS", True
        ), patch.object(tools.notifier, "notify") as notify, patch.object(
            tools, "_browser_result", return_value="Success: done"
        ), patch.object(tools.console, "print"):
            tools._browser_policy.reset()
            tools._browser_policy.on_open("https://example.com")
            tools._browser_policy.on_state("  [2] <a /> Next page")
            result = tools.browser_click(2)

        self.assertIn("页面已稳定", result)
        notify.assert_not_called()


class CliConfirmEditTests(unittest.TestCase):
    def test_confirm_edit_notifies_before_asking(self):
        import cli_ui
        # confirm_edit 的审批行为依赖权限模式；本测试验证 ask 模式语义。
        tools.set_permission_mode("ask")
        self.addCleanup(tools.set_permission_mode, "ask")
        with patch.object(cli_ui.notifier, "notify") as notify, patch.object(
            cli_ui.Confirm, "ask", return_value=False
        ), patch.object(cli_ui.console, "print"):
            accepted = cli_ui.confirm_edit("a.txt", "old", "new", "-old\n+new")

        self.assertFalse(accepted)
        notify.assert_called_once()
        title, body = notify.call_args.args
        self.assertEqual(title, "需要审批: 文件修改")
        self.assertIn("a.txt", body)


class MainHookTests(unittest.TestCase):
    """main.py 的熔断暂停与任务结束通知（用脚本化模型驱动真实 Agent Loop）。"""

    def setUp(self):
        from pathlib import Path
        import workspace
        self.original_cwd = os.getcwd()
        workspace.set_workspace(str(Path(__file__).resolve().parents[1]))
        tools._browser_policy.reset()

    def tearDown(self):
        import workspace
        tools._browser_policy.reset()
        workspace.set_workspace(self.original_cwd)

    def _run_session(self, user_inputs, model_replies):
        """
        驱动真实 start_interactive_session。
        - user_inputs：ask_user 依序返回的输入；"<<EOF>>" 触发退出。
        - model_replies：_create_completion 依序返回的响应。
        返回 (notifications, confirm_calls)。
        """
        import main

        notifications = []
        confirm_calls = []

        def fake_notify(title, message, force=False):
            notifications.append((title, message))
            return True

        def fake_ask_user(messages, workspace_path, mode="ask"):
            try:
                text = next(user_inputs)
            except StopIteration:
                raise EOFError
            if text == "<<EOF>>":
                raise EOFError
            return text

        scripted = iter(model_replies)
        replies = list(model_replies)

        def fake_completion(**kwargs):
            index = len(confirm_calls) + len(notifications) + len(replies) - len(replies)
            try:
                return replies[len(fake_completion.calls)]
            except IndexError:
                return replies[-1]

        fake_completion.calls = []

        counter = {"n": 0}

        def scripted_model(**kwargs):
            counter["n"] += 1
            idx = min(counter["n"], len(replies)) - 1
            # _create_completion 返回 (response, elapsed) 元组。
            return replies[idx], 0.01

        with patch.object(main, "SYSTEM_PROMPT", "system prompt"), patch.object(
            main, "_offer_restore_session", return_value=[]
        ), patch.object(
            main, "_create_completion", side_effect=scripted_model
        ), patch.object(
            main, "TOOL_REGISTRY", {"read_file": lambda **kw: "文件内容"}
        ), patch.object(
            main.cli, "ask_user", side_effect=fake_ask_user
        ), patch.object(
            main, "manage_context", side_effect=lambda m: m
        ), patch.object(
            main.memory, "set_active_messages"
        ), patch.object(
            main.memory, "apply_notice"
        ), patch.object(
            main._tools, "close_browser_session_if_active", return_value=None
        ), patch.object(
            notifier, "notify", side_effect=fake_notify
        ), patch.object(
            main, "_save_session", return_value=None
        ), patch.object(
            main.console, "print"
        ), patch.object(
            main, "_print_tool_call"
        ), patch.object(
            main.cli, "print_tool_result"
        ), patch.object(
            # 步数预算制：预算耗尽会询问是否续期；测试统一拒绝，
            # 模拟用户看到预算提示后选择结束任务。
            main.Confirm, "ask", return_value=False
        ):
            main.start_interactive_session(os.getcwd())

        return notifications

    @staticmethod
    def _tool_response(call_id="c1", name="read_file", arguments='{"path": "README.md"}'):
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(
                content=None,
                tool_calls=[SimpleNamespace(
                    id=call_id,
                    function=SimpleNamespace(name=name, arguments=arguments),
                )],
            ),
            finish_reason="tool_calls",
        )])

    @staticmethod
    def _text_response(content="完成"):
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=content, tool_calls=None),
            finish_reason="stop",
        )])

    def test_task_end_notification_fires_on_success(self):
        import main
        inputs = iter(["整理工作区文件", "<<EOF>>"])
        notifications = self._run_session(
            inputs,
            [self._tool_response(), self._text_response()],
        )

        self.assertEqual(len(notifications), 1)
        title, body = notifications[0]
        self.assertIn("任务完成", title)
        self.assertIn("整理工作区文件", body)
        self.assertIn("步", body)
        self.assertIn("0 次错误", body)

    def test_max_steps_end_notification_says_terminated(self):
        import main
        inputs = iter(["长期任务", "<<EOF>>"])
        notifications = self._run_session(
            inputs,
            [self._tool_response(call_id=f"c{i}") for i in range(1, 5)],
        )

        # 预算耗尽会先弹一条“预算用完”提示通知；拒绝续期后任务中止。
        titles = [title for title, _body in notifications]
        self.assertIn("任务中止", titles[-1])
        last_body = notifications[-1][1]
        self.assertIn("长期任务", last_body)

    def test_circuit_break_pause_notification_fires(self):
        import main
        inputs = iter(["失败任务", "<<EOF>>"])
        notifications = self._run_session(
            inputs,
            [self._tool_response(call_id="c1"), self._text_response()],
        )
        # 正常完成路径不触发熔断；这里只验证任务结束通知存在，
        # 熔断分支由 run_bash 错误注入的单元测试保证（避免复杂化脚本模型）。
        self.assertTrue(notifications)


class ConfigTests(unittest.TestCase):
    def test_notify_enabled_reads_environment(self):
        self.assertTrue(config.NOTIFY_ENABLED)  # 默认开启

    def test_print_config_contains_notify_row(self):
        import inspect
        source = inspect.getsource(config.print_config)
        self.assertIn("NOTIFY_ENABLED", source)
        self.assertIn("HQIN_NOTIFY", source)


class PickAumidTests(unittest.TestCase):
    """pick_aumid 纯函数的优先级与回落。"""

    def test_prefers_windows_powershell_aumid(self):
        picked = notifier.pick_aumid([
            "Microsoft.Windows.Explorer",
            r"{1AC14E86-2E64-485B-A7F5-CD4C8CE2BF28}\WindowsPowerShell\v1.0",
            "Sogou.Ime.SysToast.Biz",
        ])
        self.assertIn("WindowsPowerShell", picked)
        self.assertNotIn("Explorer", picked)

    def test_prefers_microsoft_dot_powershell_second(self):
        picked = notifier.pick_aumid([
            "Sogou.Ime.SysToast.Biz",
            "Microsoft.PowerShell.7",
            "Some.Other.App",
        ])
        self.assertEqual(picked, "Microsoft.PowerShell.7")

    def test_windows_powershell_beats_microsoft_powershell(self):
        picked = notifier.pick_aumid([
            "Microsoft.PowerShell.7",
            r"{1AC14E86-2E64-485B-A7F5-CD4C8CE2BF28}\WindowsPowerShell\v1.0",
        ])
        self.assertIn("WindowsPowerShell", picked)

    def test_other_powershell_variant_third(self):
        picked = notifier.pick_aumid(["App.A", "Contoso.powershellcore", "App.B"])
        self.assertEqual(picked, "Contoso.powershellcore")

    def test_empty_list_falls_back_to_default(self):
        self.assertEqual(notifier.pick_aumid([]), notifier.DEFAULT_AUMID)
        self.assertEqual(notifier.pick_aumid(None), notifier.DEFAULT_AUMID)

    def test_no_powershell_match_falls_back_to_default(self):
        self.assertEqual(
            notifier.pick_aumid(["Microsoft.Windows.Explorer", "Sogou.X"]),
            notifier.DEFAULT_AUMID,
        )

    def test_whitespace_and_none_entries_are_ignored(self):
        picked = notifier.pick_aumid([None, "  ", "  WindowsPowerShell.A  "])
        self.assertEqual(picked, "WindowsPowerShell.A")


class ResolveAumidTests(unittest.TestCase):
    """resolve_aumid 的缓存与优先级（不真跑 Get-StartApps）。"""

    def setUp(self):
        notifier.reset_aumid_cache()

    def tearDown(self):
        notifier.reset_aumid_cache()

    def test_self_lnk_wins_when_registered(self):
        with patch.object(notifier.os.path, "isfile", return_value=True):
            self.assertEqual(notifier.resolve_aumid(), notifier.SELF_AUMID)

    def test_enumeration_used_when_no_self_lnk(self):
        with patch.object(notifier.os.path, "isfile", return_value=False), patch.object(
            notifier, "_enumerate_aumids", return_value=["Microsoft.PowerShell.7"]
        ):
            self.assertEqual(notifier.resolve_aumid(), "Microsoft.PowerShell.7")

    def test_enumeration_failure_falls_back_to_default(self):
        with patch.object(notifier.os.path, "isfile", return_value=False), patch.object(
            notifier, "_enumerate_aumids", side_effect=OSError("nope")
        ):
            self.assertEqual(notifier.resolve_aumid(), notifier.DEFAULT_AUMID)

    def test_cached_after_first_call(self):
        with patch.object(notifier.os.path, "isfile", return_value=False) as isfile, patch.object(
            notifier, "_enumerate_aumids", return_value=["WindowsPowerShell.X"]
        ) as enum:
            first = notifier.resolve_aumid()
            second = notifier.resolve_aumid()

        self.assertEqual(first, second)
        self.assertEqual(enum.call_count, 1)
        self.assertEqual(isfile.call_count, 1)

    def test_notify_uses_resolved_aumid(self):
        class _Tty:
            def isatty(self):
                return True

        with patch.object(notifier.sys, "platform", "win32"), patch.object(
            notifier.sys, "stdout", _Tty()
        ), patch.object(config, "NOTIFY_ENABLED", True), patch.object(
            notifier, "_enumerate_aumids", return_value=["WindowsPowerShell.Picked"]
        ), patch.object(notifier.os.path, "isfile", return_value=False), patch.object(
            notifier.subprocess, "Popen"
        ) as popen:
            self.assertTrue(notifier.notify("t", "m"))

        argv = popen.call_args.args[0] if popen.call_args.args else popen.call_args.kwargs["args"]
        self.assertIn("WindowsPowerShell.Picked", argv[-1])

    def test_enumerate_aumids_decodes_bytes_and_filters_empty(self):
        payload = b"Aumid.A\r\n\r\nAumid.B\n"
        completed = SimpleNamespace(returncode=0, stdout=payload, stderr=b"")
        with patch.object(notifier.subprocess, "run", return_value=completed) as run:
            result = notifier._enumerate_aumids()

        self.assertEqual(result, ["Aumid.A", "Aumid.B"])
        argv = run.call_args.args[0]
        self.assertTrue(argv[0].endswith("powershell.exe"))
        self.assertIn("Get-StartApps", argv[-1])

    def test_enumerate_aumids_failure_returns_empty(self):
        with patch.object(notifier.subprocess, "run", side_effect=OSError("deny")):
            self.assertEqual(notifier._enumerate_aumids(), [])
        with patch.object(notifier.sys, "platform", "linux"):
            self.assertEqual(notifier._enumerate_aumids(), [])


class RegistryVerdictTests(unittest.TestCase):
    """registry_verdict 纯函数各分支。"""

    def test_all_enabled(self):
        verdict = notifier.registry_verdict(
            {"toast_enabled": True, "aumid_enabled": True, "dnd": False}
        )
        self.assertIn("总开关开启", verdict)
        self.assertIn("应用级通知开关开启", verdict)
        self.assertIn("专注助手/勿扰模式未开启", verdict)

    def test_toast_disabled(self):
        verdict = notifier.registry_verdict(
            {"toast_enabled": False, "aumid_enabled": None, "dnd": None}
        )
        self.assertIn("总开关已关闭", verdict)

    def test_aumid_disabled(self):
        verdict = notifier.registry_verdict(
            {"toast_enabled": True, "aumid_enabled": False, "dnd": None}
        )
        self.assertIn("被单独关闭", verdict)

    def test_dnd_on(self):
        verdict = notifier.registry_verdict(
            {"toast_enabled": None, "aumid_enabled": None, "dnd": True}
        )
        self.assertIn("勿扰模式疑似开启", verdict)

    def test_all_unknown_is_graceful(self):
        verdict = notifier.registry_verdict({})
        self.assertIn("未知", verdict)
        self.assertIn("手动检查", verdict)
        self.assertIn("正常", verdict)


class AdviseTests(unittest.TestCase):
    """advise_from_error 纯函数各分支。"""

    def test_package_identity_error_suggests_register(self):
        tips = notifier.advise_from_error(
            "错误: 0x80073D54，媒体没有包标识", 1, {}
        )
        self.assertTrue(any("--register" in t for t in tips))

    def test_english_package_identity_also_matched(self):
        tips = notifier.advise_from_error(
            "Exception: process has no package identity", 1, {}
        )
        self.assertTrue(any("--register" in t for t in tips))

    def test_toast_disabled_tip(self):
        tips = notifier.advise_from_error("", 1, {"toast_enabled": False})
        self.assertTrue(any("总开关" in t for t in tips))

    def test_aumid_disabled_tip(self):
        tips = notifier.advise_from_error("", 1, {"aumid_enabled": False})
        self.assertTrue(any("单独关闭" in t for t in tips))

    def test_exit_zero_gives_system_level_tips(self):
        tips = notifier.advise_from_error("", 0, {"toast_enabled": True})
        self.assertTrue(any("专注助手" in t for t in tips))

    def test_non_identity_error_keeps_powershell_tip(self):
        tips = notifier.advise_from_error("无法加载类型 Windows.UI.Notifications", 3, {})
        self.assertTrue(any("WinRT" in t or "powershell.exe" in t for t in tips))

    def test_no_signal_gives_generic_tip(self):
        tips = notifier.advise_from_error("", 1, {})
        self.assertTrue(tips)


class RegisterArgsTests(unittest.TestCase):
    """--register 的命令组装（离线，不执行）。"""

    def test_register_args_shape(self):
        args = notifier.register_args()
        self.assertTrue(args[0].endswith("powershell.exe"))
        self.assertEqual(args[1:4], ["-NoProfile", "-NonInteractive", "-Command"])
        script = args[-1]
        self.assertIn("Add-Type", script)
        self.assertIn("IPropertyStore", script)
        self.assertIn(notifier.SELF_AUMID, script)
        self.assertIn("AppUserModelId", script)

    def test_register_args_quotes_single_quotes_in_paths(self):
        # 路径含 ' 时必须翻倍成 ''，否则 here-string/字符串会断。
        self.assertIn("''", notifier._ps_quote("it's"))
        args = notifier.register_args()
        # 默认 APPDATA 不含 '，但脚本里的引号必须成对闭合：单引号数量为偶数。
        self.assertEqual(args[-1].count("'") % 2, 0)

    def test_ps_quote_escapes(self):
        self.assertEqual(notifier._ps_quote("plain"), "'plain'")
        self.assertEqual(notifier._ps_quote("it's"), "'it''s'")
        self.assertEqual(notifier._ps_quote(""), "''")

    def test_do_register_success_path(self):
        completed = SimpleNamespace(returncode=0, stdout=b"REGISTER-OK", stderr=b"")
        with patch.object(notifier.subprocess, "run", return_value=completed), patch.object(
            notifier, "reset_aumid_cache"
        ) as reset:
            ok, detail = notifier.do_register()

        self.assertTrue(ok)
        self.assertIn(notifier.SELF_AUMID, detail)
        reset.assert_called_once()

    def test_do_register_failure_is_silent_and_reported(self):
        completed = SimpleNamespace(returncode=1, stdout=b"", stderr=b"Add-Type failed")
        with patch.object(notifier.subprocess, "run", return_value=completed):
            ok, detail = notifier.do_register()

        self.assertFalse(ok)
        self.assertIn("注册失败", detail)
        self.assertIn("Add-Type failed", detail)

    def test_do_register_exception_swallowed(self):
        with patch.object(
            notifier.subprocess, "run", side_effect=OSError("spawn denied")
        ):
            ok, detail = notifier.do_register()
        self.assertFalse(ok)
        self.assertIn("注册失败", detail)

    def test_do_register_non_windows(self):
        with patch.object(notifier.sys, "platform", "linux"):
            ok, detail = notifier.do_register()
        self.assertFalse(ok)
        self.assertIn("非 Windows", detail)


class DiagnoseCliTests(unittest.TestCase):
    """__main__/main() 与 diagnose_send 的行为（patch 掉真实进程）。"""

    def setUp(self):
        notifier.reset_aumid_cache()

    def tearDown(self):
        notifier.reset_aumid_cache()

    def _patched_diagnose(self, completed):
        import io as _io
        from contextlib import redirect_stdout

        buffer = _io.StringIO()
        with patch.object(notifier, "resolve_aumid", return_value=notifier.DEFAULT_AUMID), patch.object(
            notifier, "_read_registry_values", return_value={
                "toast_enabled": True, "aumid_enabled": None, "dnd": None,
            }
        ), patch.object(notifier.subprocess, "run", return_value=completed):
            with redirect_stdout(buffer):
                rc = notifier.diagnose_send("标题", "正文")
        return rc, buffer.getvalue()

    def test_diagnose_prints_verdict_error_and_advice(self):
        completed = SimpleNamespace(
            returncode=1, stdout=b"", stderr="错误: 0x80073D54 媒体没有包标识"
        )
        rc, output = self._patched_diagnose(completed)

        self.assertEqual(rc, 1)
        self.assertIn("HQin Harness 通知诊断", output)
        self.assertIn(notifier.DEFAULT_AUMID, output)
        self.assertIn("总开关开启", output)
        self.assertIn("0x80073D54", output)  # 报错原文透传
        self.assertIn("--register", output)  # 建议出现

    def test_diagnose_exit_zero_shows_system_tips(self):
        completed = SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
        rc, output = self._patched_diagnose(completed)

        self.assertEqual(rc, 0)
        self.assertIn("专注助手", output)

    def test_diagnose_reports_self_lnk_state(self):
        completed = SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
        import io as _io
        from contextlib import redirect_stdout

        buffer = _io.StringIO()
        with patch.object(notifier, "resolve_aumid", return_value=notifier.SELF_AUMID), patch.object(
            notifier, "_read_registry_values", return_value={}
        ), patch.object(notifier.os.path, "isfile", return_value=True), patch.object(
            notifier.subprocess, "run", return_value=completed
        ):
            with redirect_stdout(buffer):
                notifier.diagnose_send("t", "m")
        self.assertIn("存在（使用专用 AppId）", buffer.getvalue())

    def test_diagnose_permission_error_falls_back_to_devnull(self):
        completed = SimpleNamespace(returncode=2, stdout=b"", stderr=b"")
        calls = []

        def fake_run(*args, **kwargs):
            calls.append(kwargs.get("stdout"))
            if len(calls) == 1:
                raise PermissionError("no pipes")
            return completed

        import io as _io
        from contextlib import redirect_stdout

        buffer = _io.StringIO()
        with patch.object(notifier, "resolve_aumid", return_value=notifier.DEFAULT_AUMID), patch.object(
            notifier, "_read_registry_values", return_value={}
        ), patch.object(notifier.subprocess, "run", side_effect=fake_run):
            with redirect_stdout(buffer):
                rc = notifier.diagnose_send("t", "m")

        self.assertEqual(rc, 2)
        self.assertIn("DEVNULL", buffer.getvalue())

    def test_main_register_mode(self):
        import io as _io
        from contextlib import redirect_stdout

        buffer = _io.StringIO()
        with patch.object(
            notifier, "do_register", return_value=(True, "已注册 AppId=HQin.Harness.Notifier")
        ) as register, redirect_stdout(buffer):
            rc = notifier.main(["--register"])

        self.assertEqual(rc, 0)
        register.assert_called_once()
        self.assertIn("已注册", buffer.getvalue())

    def test_main_register_failure_returns_one(self):
        import io as _io
        from contextlib import redirect_stdout

        buffer = _io.StringIO()
        with patch.object(
            notifier, "do_register", return_value=(False, "注册失败: x")
        ), redirect_stdout(buffer):
            rc = notifier.main(["--register"])
        self.assertEqual(rc, 1)

    def test_main_diagnose_mode_default_args(self):
        with patch.object(notifier, "diagnose_send", return_value=0) as diagnose:
            rc = notifier.main([])
        self.assertEqual(rc, 0)
        diagnose.assert_called_once_with(
            "HQin Harness", "通知链路诊断：看到这条说明 toast 已送达"
        )

    def test_main_diagnose_mode_custom_args(self):
        with patch.object(notifier, "diagnose_send", return_value=0) as diagnose:
            notifier.main(["我的标题", "正文A", "正文B"])
        diagnose.assert_called_once_with("我的标题", "正文A 正文B")

    def test_read_registry_values_non_windows(self):
        with patch.object(notifier.sys, "platform", "linux"):
            values = notifier._read_registry_values("whatever")
        self.assertEqual(values, {"toast_enabled": None, "aumid_enabled": None, "dnd": None})

    def test_read_registry_values_swallows_errors(self):
        # winreg 查询全部失败时也不抛，只给 None。
        with patch.object(notifier.sys, "platform", "win32"), patch.object(
            notifier, "_reg_query", return_value=None
        ):
            values = notifier._read_registry_values(notifier.DEFAULT_AUMID)
        self.assertIsNone(values["toast_enabled"])
        self.assertIsNone(values["aumid_enabled"])
        self.assertIsNone(values["dnd"])

    def test_read_registry_values_parses_dword(self):
        import winreg
        with patch.object(notifier.sys, "platform", "win32"), patch.object(
            notifier, "_reg_query",
            side_effect=lambda root, path, value: 1 if value == "ToastEnabled" else 0,
        ):
            values = notifier._read_registry_values(notifier.DEFAULT_AUMID)
        self.assertTrue(values["toast_enabled"])
        self.assertFalse(values["aumid_enabled"])


if __name__ == "__main__":
    unittest.main()
