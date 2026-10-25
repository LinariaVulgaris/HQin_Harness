import io
import json
import os
import shutil
import subprocess
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import browser_policy
import cli_ui
import config
import main
import memory
import tools
import workspace


def _tool_call(call_id: str, name: str, arguments: str = "{}"):
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def _assistant_tool_call(call_id: str, name: str, args: dict):
    return SimpleNamespace(
        choices=[SimpleNamespace(
            message=SimpleNamespace(
                content=None,
                tool_calls=[_tool_call(call_id, name, json.dumps(args, ensure_ascii=False))],
            ),
            finish_reason="tool_calls",
        )]
    )


def _assistant_text(content: str):
    return SimpleNamespace(
        choices=[SimpleNamespace(
            message=SimpleNamespace(content=content, tool_calls=None),
            finish_reason="stop",
        )]
    )


class _FakeBrowserAct:
    """模拟 BrowserAct CLI 的参数协议，用于驱动完整的 Agent Loop。"""

    def __init__(self):
        self.calls: list[list[str]] = []
        self.state_text = (
            "[1] <a /> Next page\n"
            "[2] <a /> 登录\n"
            "[3] <a /> 文档"
        )

    def __call__(self, args: list[str], timeout: int | None = None):
        self.calls.append(list(args))
        if args[:2] == ["session", "close"]:
            return 0, "closed"
        if args[2:4] == ["browser", "open"]:
            return 0, "opened"
        if args[2:3] == ["state"]:
            return 0, self.state_text
        if args[2:3] == ["click"]:
            return 0, f"clicked {args[3]}"
        if args[2:4] == ["wait", "stable"]:
            return 0, "stable"
        return 0, "ok"

    def count(self, *prefix: str) -> int:
        return sum(1 for call in self.calls if call[:len(prefix)] == list(prefix))

    def count_click(self, index: int) -> int:
        return sum(1 for call in self.calls if call[2:4] == ["click", str(index)])


class WorkspaceTestCase(unittest.TestCase):
    def setUp(self):
        self.original_cwd = os.getcwd()
        # 测试不写文件，直接使用受控项目目录，避免受限环境拒绝切换到系统临时目录。
        workspace.set_workspace(str(Path(__file__).resolve().parents[1]))
        tools._browser_policy.reset()

    def tearDown(self):
        tools._browser_policy.reset()
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


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parents[1] / f".memory_test_{uuid.uuid4().hex}"
        self.root.mkdir()
        self.store = memory.MemoryStore(self.root)
        memory.set_store(self.store)
        memory.set_active_messages(None)

    def tearDown(self):
        memory.reset_state()
        shutil.rmtree(self.root, ignore_errors=True)

    def _messages(self):
        return [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "旧任务：实现 pathlib 扫描"},
            {"role": "assistant", "content": "我先看看目录"},
            {"role": "tool", "tool_call_id": "call_1", "content": "扫描结果：README.md"},
            {"role": "user", "content": "第二个任务：修复编码"},
            {"role": "assistant", "content": "已修复 GBK 输出"},
            {"role": "user", "content": "第三个任务：补测试"},
            {"role": "assistant", "content": "测试通过"},
            {"role": "user", "content": "当前任务：继续优化"},
        ]

    def test_turn_ranges_keep_tool_results_inside_turn(self):
        ranges = memory.turn_ranges(self._messages())
        self.assertEqual([item["turn"] for item in ranges], [1, 2, 3, 4])
        self.assertEqual((ranges[0]["start"], ranges[0]["end"]), (1, 4))
        self.assertEqual(
            [m["role"] for m in self._messages()[ranges[0]["start"]:ranges[0]["end"]]],
            ["user", "assistant", "tool"],
        )

    def test_compress_archives_complete_old_turns(self):
        messages = self._messages()
        memory.set_active_messages(messages)
        result = memory.compress_context(
            1, 1, "## 任务目标\n实现 pathlib 扫描\n## 已完成\n扫描 README\n## 未完成\n无",
            topic="初始化",
        )
        self.assertIn("Success", result)
        self.assertTrue(messages[1]["content"].startswith("[历史记忆 b0001"))
        block = self.store.get("b0001")
        self.assertEqual(
            [m["role"] for m in block["messages"]],
            ["user", "assistant", "tool"],
        )
        self.assertEqual(len(memory.turn_ranges(messages)), 3)

    def test_turn_numbers_stay_absolute_after_compression(self):
        messages = self._messages()
        memory.set_active_messages(messages)
        self.assertIn("Success", memory.compress_context(1, 1, "## 已完成\n旧任务"))
        ranges = memory.turn_ranges(messages)
        self.assertEqual([item["turn"] for item in ranges], [2, 3, 4])
        self.assertEqual([item["local_turn"] for item in ranges], [1, 2, 3])
        self.assertIn("turn 2–2", memory.context_status(messages))

        # 第二次压缩必须继续使用绝对轮次 2，而不是把下一轮误叫成 turn 1。
        result = memory.compress_context(2, 2, "## 已完成\n第二个任务")
        self.assertIn("Success: 已压缩 turn 2–2", result)
        self.assertEqual(
            [item["turn"] for item in memory.turn_ranges(messages)],
            [3, 4],
        )

    def test_compress_protects_current_and_recent_turns(self):
        messages = self._messages()
        memory.set_active_messages(messages)
        self.assertIn("Error:", memory.compress_context(3, 4, "不应压缩当前任务"))
        self.assertIn("Error:", memory.compress_context(1, 4, "不应压缩最近轮次"))
        self.assertIn("Success:", memory.compress_context(1, 2, "## 已完成\n旧任务"))

    def test_decompress_preview_full_and_file(self):
        messages = self._messages()
        memory.set_active_messages(messages)
        memory.compress_context(1, 1, "## 任务目标\npathlib 扫描")
        preview = memory.decompress_context("b0001")
        self.assertIn("pathlib 扫描", preview)
        full = memory.decompress_context("b0001", "full")
        self.assertIn("扫描结果", full)
        result = memory.decompress_context("b0001", "file")
        self.assertIn("Success", result)
        restored = self.root / "restored" / "b0001.json"
        self.assertTrue(restored.exists())
        self.assertIn("README.md", restored.read_text(encoding="utf-8"))

    def test_search_context_finds_summary_and_original_messages(self):
        messages = self._messages()
        memory.set_active_messages(messages)
        memory.compress_context(1, 1, "## 任务目标\npathlib 扫描与编码修复")
        by_summary = memory.search_context("编码修复")
        self.assertIn("b0001", by_summary)
        by_original = memory.search_context("README.md")
        self.assertIn("b0001", by_original)
        self.assertIn("tool", by_original)

    def test_store_persists_index_and_blocks(self):
        memory.set_active_messages(self._messages())
        memory.compress_context(1, 1, "## 已完成\n持久化测试")
        reloaded = memory.MemoryStore(self.root)
        self.assertEqual(reloaded.list()[0]["id"], "b0001")
        self.assertEqual(reloaded.get("b0001")["summary"], "## 已完成\n持久化测试")

    def test_status_shows_compressible_turns_and_blocks(self):
        messages = self._messages()
        memory.set_active_messages(messages)
        memory.compress_context(1, 1, "## 已完成\n旧任务")
        status = memory.context_status(messages)
        self.assertIn("可压缩", status)
        self.assertIn("受保护", status)
        self.assertIn("b0001", status)

    def test_notice_is_attached_to_single_system_message(self):
        messages = self._messages()
        for msg in messages:
            if msg.get("role") == "user":
                msg["content"] = msg["content"] * 20
        memory.set_active_messages(messages)
        with patch.object(config, "MODEL_CONTEXT_TOKENS", 500), \
                patch.object(config, "CONTEXT_SOFT_RATIO", 0.5), \
                patch.object(config, "CONTEXT_HARD_RATIO", 0.9):
            notice = memory.context_notice(messages)
            self.assertIn("可压缩旧轮次", notice)
            memory.apply_notice(messages, "BASE")
            self.assertIn("BASE", messages[0]["content"])
            self.assertIn(memory.NOTICE_MARKER, messages[0]["content"])

        with patch.object(config, "MODEL_CONTEXT_TOKENS", 10 ** 9):
            memory.apply_notice(messages, "BASE")
        self.assertEqual(messages[0]["content"], "BASE")

    def test_compress_is_routed_through_tool_registry(self):
        messages = self._messages()
        memory.set_active_messages(messages)
        result, _ = main._execute_tool(
            "compress_context",
            {"start_turn": 1, "end_turn": 1, "summary": "## 已完成\n注册表测试"},
        )
        self.assertIn("Success", result)
        self.assertTrue(messages[1]["content"].startswith("[历史记忆"))

    def test_memory_tools_are_in_schema_and_registry(self):
        names = {item["function"]["name"] for item in main.TOOL_SCHEMAS}
        self.assertTrue(
            {"context_status", "compress_context", "decompress_context", "search_context"}
            <= names
        )
        self.assertIn("context_status", tools.TOOL_REGISTRY)
        self.assertIn("compress_context", tools.TOOL_REGISTRY)


class PolicyUnitTests(unittest.TestCase):
    def test_element_labels_are_parsed_from_state_text(self):
        labels = browser_policy.extract_element_labels(
            '  [1] <a /> Learn more\n  [2] input "Search"'
        )
        self.assertEqual(labels[1], "<a /> Learn more")
        self.assertEqual(labels[2], 'input "Search"')

    def test_sensitive_labels_are_detected_without_false_positives(self):
        self.assertTrue(browser_policy.is_sensitive_label("登录"))
        self.assertTrue(browser_policy.is_sensitive_label("Download PDF"))
        self.assertFalse(browser_policy.is_sensitive_label("display results"))
        self.assertFalse(browser_policy.is_sensitive_label("下一页"))

    def test_truncated_state_does_not_block_unseen_indices(self):
        policy = browser_policy.BrowserSessionPolicy()
        policy.on_open("https://example.com")
        policy.on_state(
            "  [1] <a /> Next page\n...(输出过长，已截断，共 9000 字符)"
        )

        self.assertIsNone(policy.click_block_reason(9))


class _FakeHttpResponse:
    def __init__(self, body: str):
        self._body = body.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self._body


class WebSearchTests(unittest.TestCase):
    def test_clean_query_drops_generic_words(self):
        self.assertEqual(
            tools._clean_search_query("南京大学 智能科学与技术学院 官网"),
            "南京大学 智能科学与技术学院",
        )
        self.assertEqual(
            tools._clean_search_query("Python pathlib 官方文档"),
            "Python pathlib",
        )

    def test_bing_results_are_parsed_and_ranked(self):
        body = """
        <h2><a href="https://zhuanlan.zhihu.com/p/1">南京大学智能科学与技术学院怎么样</a></h2>
        <p class="b_lineclamp2">知乎讨论</p>
        <h2><a href="https://is.nju.edu.cn/main.htm">NJU - 南京大学智能科学与技术学院</a></h2>
        <p class="b_lineclamp2">学院官网</p>
        <h2><a href="https://www.bing.com/ck/a?x">Bing 跳转</a></h2>
        """
        parsed = tools._parse_bing_results(body)
        self.assertEqual(len(parsed), 2)
        ranked = tools._rank_search_results(parsed, "南京大学 智能科学与技术学院")
        self.assertEqual(ranked[0]["url"], "https://is.nju.edu.cn/main.htm")
        self.assertIn("学院官网", ranked[0]["snippet"])

    def test_web_search_uses_bing_and_returns_domain(self):
        body = """
        <h2><a href="https://zhuanlan.zhihu.com/p/1">南京大学智能科学与技术学院怎么样</a></h2>
        <h2><a href="https://is.nju.edu.cn/main.htm">NJU - 南京大学智能科学与技术学院</a></h2>
        """
        with patch(
            "tools.urllib.request.urlopen",
            return_value=_FakeHttpResponse(body),
        ) as mocked:
            output = tools.web_search("南京大学 智能科学与技术学院 官网")
        self.assertEqual(mocked.call_count, 2)
        self.assertIn("https://is.nju.edu.cn/main.htm", output)
        self.assertLess(output.index("is.nju.edu.cn"), output.index("zhihu.com"))


class DocxToolTests(unittest.TestCase):
    def setUp(self):
        self.project_root = Path(__file__).resolve().parents[1]
        self.original_cwd = os.getcwd()
        workspace.set_workspace(str(self.project_root))
        self.basename = f"_docx_tmp_{os.getpid()}"
        self.path = self.project_root / f"{self.basename}.docx"

    def tearDown(self):
        os.chdir(self.original_cwd)
        self.path.unlink(missing_ok=True)
        workspace.set_workspace(str(self.project_root))

    def test_create_docx_writes_real_word_document(self):
        result = tools.create_docx(
            self.basename,
            "张三",
            "# 基本信息\n- 职称：准聘副教授\n- 研究方向：三维视觉\n\n"
            "## 简介\n**重点**：三维计算机视觉与模式识别。",
        )
        self.assertIn("Success", result)
        path = self.path
        self.assertTrue(path.exists())
        self.assertEqual(path.read_bytes()[:2], b"PK")

        from docx import Document

        doc = Document(str(path))
        text = "\n".join(p.text for p in doc.paragraphs)
        self.assertIn("张三", text)
        self.assertIn("准聘副教授", text)
        self.assertIn("三维视觉", text)
        self.assertIn("重点", text)

    def test_create_docx_rejects_path_outside_workspace(self):
        result = tools.create_docx("../escape.docx", "x", "y")
        self.assertTrue(result.startswith("Error:"))

    def test_write_file_rejects_docx_extension(self):
        result = tools.write_file(f"{self.basename}.docx", "plain text")
        self.assertIn("create_docx", result)


class FinishCheckTests(WorkspaceTestCase):
    def setUp(self):
        super().setUp()
        tools._session_written_files.clear()
        self.root = Path(__file__).resolve().parents[1]
        self.created: list[Path] = []

    def tearDown(self):
        for path in self.created:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        tools._session_written_files.clear()
        super().tearDown()

    def _name(self, suffix: str) -> str:
        name = f"_finish_{uuid.uuid4().hex}{suffix}"
        self.created.append(self.root / name)
        return name

    def test_write_file_rejects_oversized_content(self):
        name = self._name(".txt")
        result = tools.write_file(name, "x" * (config.MAX_WRITE_CHARS + 1))
        self.assertTrue(result.startswith("Error:"))
        self.assertIn("append_file", result)
        self.assertFalse((self.root / name).exists())

    def test_append_file_builds_chunked_json_and_finish_check_passes(self):
        name = self._name(".json")
        first = '{\n  "a": 1'
        second = ',\n  "b": 2\n}\n'
        self.assertIn("Success", tools.write_file(name, first))
        self.assertIn("Success", tools.append_file(name, second))
        self.assertEqual((self.root / name).read_text(encoding="utf-8"), first + second)

        result = tools.finish_check([name])
        self.assertIn("Success", result)
        self.assertIn("JSON 解析通过", result)

    def test_finish_check_rejects_invalid_json_and_empty_file(self):
        bad_json = self._name(".json")
        empty = self._name(".txt")
        tools.write_file(bad_json, "{not json")
        tools.write_file(empty, "")

        result = tools.finish_check([bad_json, empty])
        self.assertTrue(result.startswith("VerificationFailed:"))
        self.assertIn("JSON 解析失败", result)
        self.assertIn("文件为空", result)

    def test_finish_check_accepts_python_and_docx(self):
        py_name = self._name(".py")
        docx_name = self._name("")
        self.created.append(self.root / f"{docx_name}.docx")
        self.assertIn("Success", tools.write_file(py_name, "def add(a, b):\n    return a + b\n"))
        self.assertIn("Success", tools.create_docx(docx_name, "测试", "正文内容"))

        result = tools.finish_check([py_name, f"{docx_name}.docx"])
        self.assertIn("Success", result)
        self.assertIn("Python 语法通过", result)
        self.assertIn("Word 文档可打开", result)

    def test_finish_check_defaults_to_session_writes(self):
        name = self._name(".txt")
        tools.write_file(name, "hello\n")
        result = tools.finish_check()
        self.assertIn("Success", result)
        self.assertIn(name, result)

    def test_finish_check_warns_about_placeholders_but_passes(self):
        name = self._name(".md")
        tools.write_file(name, "# 文档\nTODO: 补充内容\n")
        result = tools.finish_check([name])
        self.assertIn("Success", result)
        self.assertIn("TODO", result)
        self.assertIn("警告", result)

    def test_finish_check_reports_missing_file(self):
        name = f"_finish_missing_{uuid.uuid4().hex}.txt"
        result = tools.finish_check([name])
        self.assertTrue(result.startswith("VerificationFailed:"))
        self.assertIn("文件不存在", result)

    def test_finish_check_without_writes_requires_files(self):
        result = tools.finish_check()
        self.assertTrue(result.startswith("Error:"))
        self.assertIn("files", result)

    def test_finish_tools_are_registered_and_in_schemas(self):
        names = {schema["function"]["name"] for schema in main.TOOL_SCHEMAS}
        self.assertIn("append_file", names)
        self.assertIn("finish_check", names)
        self.assertIn("append_file", tools.TOOL_REGISTRY)
        self.assertIn("finish_check", tools.TOOL_REGISTRY)


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

    def test_verification_failure_uses_fix_hint_not_generic_retry_hint(self):
        stats = main._TaskStats()
        errors = [0]
        with patch.object(
            main, "_execute_tool", return_value=("VerificationFailed: 坏 JSON", 0.0)
        ), patch.object(main.console, "print"), patch.object(main, "_print_tool_call"), patch.object(
            main.cli, "print_tool_result"
        ):
            result = main._run_tools([_tool_call("1", "finish_check")], stats, errors)

        content = result[0]["content"]
        self.assertIn("[验证提示]", content)
        self.assertNotIn("[重试提示]", content)
        self.assertEqual(errors[0], 1)

    def test_git_runner_uses_argument_list_not_shell(self):
        completed = SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
        with patch("tools.subprocess.run", return_value=completed) as run:
            code, output = tools._git_run(["diff", "--", "a && not-a-command"])

        self.assertEqual((code, output), (0, ""))
        self.assertEqual(run.call_args.args[0], ["git", "diff", "--", "a && not-a-command"])
        self.assertFalse(run.call_args.kwargs["shell"])

    def test_failed_command_is_an_error(self):
        completed = SimpleNamespace(returncode=1, stdout=b"", stderr=b"failed")
        with patch.object(tools, "_find_shell", return_value=("/usr/bin/bash", "")), patch(
            "tools.Confirm.ask", return_value=True
        ), patch("tools.subprocess.run", return_value=completed), patch.object(tools.console, "print"):
            result = tools.run_bash("exit 1")

        self.assertTrue(result.startswith("Error:"))
        self.assertIn("bash", result)

    def test_run_bash_uses_argument_list_not_shell(self):
        completed = SimpleNamespace(returncode=0, stdout=b"ok", stderr=b"")
        with patch.object(tools, "_find_shell", return_value=("/usr/bin/bash", "")), patch(
            "tools.Confirm.ask", return_value=True
        ), patch("tools.subprocess.run", return_value=completed) as run, patch.object(
            tools.console, "print"
        ):
            result = tools.run_bash("echo hi | wc -l")

        self.assertIn("ok", result)
        self.assertEqual(run.call_args.args[0], ["/usr/bin/bash", "-c", "echo hi | wc -l"])
        self.assertFalse(run.call_args.kwargs["shell"])

    def test_run_pwsh_uses_argument_list_not_shell(self):
        completed = SimpleNamespace(returncode=0, stdout=b"ok", stderr=b"")
        with patch.object(tools, "_find_shell", return_value=("C:\\pwsh.exe", "")), patch(
            "tools.Confirm.ask", return_value=True
        ), patch("tools.subprocess.run", return_value=completed) as run, patch.object(
            tools.console, "print"
        ):
            result = tools.run_pwsh("Get-Location")

        self.assertIn("PowerShell", result)
        self.assertEqual(
            run.call_args.args[0],
            ["C:\\pwsh.exe", "-NoProfile", "-NonInteractive", "-Command", "Get-Location"],
        )
        self.assertFalse(run.call_args.kwargs["shell"])

    def test_missing_shell_is_configuration_required(self):
        with patch.object(
            tools, "_find_shell", return_value=("", "ConfigurationRequired: 未找到 bash。")
        ), patch.object(tools.console, "print"):
            result = tools.run_bash("echo hi")

        self.assertTrue(result.startswith("ConfigurationRequired:"))

    def test_shell_discovery_skips_wsl_launcher(self):
        with patch.object(config, "BASH_EXECUTABLE", ""), patch.object(
            tools, "_shell_candidates",
            return_value=[r"C:\Windows\System32\bash.exe", r"C:\Git\bin\bash.exe"],
        ), patch.object(
            tools, "_is_wsl_bash", side_effect=lambda path: "System32" in path
        ), patch.object(tools, "_shell_probe", return_value=True), patch.object(
            tools.os.path, "isfile", return_value=True
        ):
            path, err = tools._find_shell("bash")

        self.assertEqual(path, r"C:\Git\bin\bash.exe")
        self.assertEqual(err, "")

    def test_shell_discovery_finds_git_bash_on_custom_path(self):
        with patch.dict(
            os.environ, {"PATH": r"D:\git\Git\cmd;C:\Windows"}, clear=False
        ), patch.object(
            tools.os.path, "isfile",
            side_effect=lambda path: path.lower() == r"d:\git\git\cmd\git.exe",
        ):
            candidates = tools._git_bash_candidates()

        self.assertIn(r"D:\git\Git\bin\bash.exe", candidates)
        self.assertIn(r"D:\git\Git\usr\bin\bash.exe", candidates)

    def test_declined_shell_command_is_not_executed(self):
        with patch.object(tools, "_find_shell", return_value=("/usr/bin/bash", "")), patch(
            "tools.Confirm.ask", return_value=False
        ), patch("tools.subprocess.run") as run, patch.object(tools.console, "print"):
            result = tools.run_bash("echo hi")

        self.assertIn("用户拒绝", result)
        run.assert_not_called()

    def test_web_fetch_uses_background_browseract_without_shell(self):
        completed = SimpleNamespace(returncode=0, stdout=b"# Rendered page", stderr=b"")
        with patch("tools.subprocess.run", return_value=completed) as run:
            result = tools.web_fetch_url("https://example.com/docs")

        self.assertEqual(result, "# Rendered page")
        self.assertEqual(
            run.call_args.args[0],
            [config.BROWSER_ACT_COMMAND, "stealth-extract", "https://example.com/docs", "--content-type", "markdown"],
        )
        self.assertFalse(run.call_args.kwargs["shell"])

    def test_interactive_browser_requires_explicit_configuration(self):
        with patch.object(config, "BROWSER_ID", ""):
            result = tools.browser_open("https://example.com")

        self.assertTrue(result.startswith("ConfigurationRequired:"))
        self.assertIn("HQIN_BROWSER_ID", result)

    def test_unconfigured_browser_tools_are_hidden_and_skip_retry_hint(self):
        with patch.object(config, "BROWSER_ID", ""):
            names = {
                schema["function"]["name"]
                for schema in browser_policy.filter_tool_schemas(main.TOOL_SCHEMAS)
            }
            self.assertIn("web_fetch_url", names)
            self.assertNotIn("browser_open", names)
            self.assertIn("未启用", browser_policy.prompt_block())

            stats = main._TaskStats()
            errors = [0]
            with patch.object(main.console, "print"), patch.object(main, "_print_tool_call"):
                result = main._run_tools(
                    [_tool_call("1", "browser_open", '{"url": "https://example.com"}')],
                    stats,
                    errors,
                )

        self.assertTrue(result[0]["content"].startswith("ConfigurationRequired:"))
        self.assertNotIn("[重试提示]", result[0]["content"])
        self.assertEqual(errors[0], 0)
        self.assertEqual(stats.error_count, 0)

    def test_click_requires_state_before_first_click(self):
        with patch.object(config, "BROWSER_ID", "test-browser"):
            tools._browser_policy.on_open("https://example.com")
            result = tools.browser_click(1)

        self.assertTrue(result.startswith("Error: 尚未读取当前页面 state"))

    def test_click_invalidates_state_and_requires_new_state(self):
        with patch.object(config, "BROWSER_ID", "test-browser"), patch.object(
            config, "BROWSER_AUTO_APPROVE_CLICKS", True
        ), patch.object(tools, "_browser_result", return_value="Success: done"), patch.object(
            tools.console, "print"
        ):
            tools._browser_policy.on_open("https://example.com")
            tools.browser_state()
            first = tools.browser_click(1)
            second = tools.browser_click(1)

        self.assertIn("页面已稳定", first)
        self.assertTrue(second.startswith("Error: 尚未读取当前页面 state"))

    def test_click_rejects_index_missing_from_latest_state(self):
        with patch.object(config, "BROWSER_ID", "test-browser"), patch.object(
            config, "BROWSER_AUTO_APPROVE_CLICKS", True
        ):
            tools._browser_policy.on_open("https://example.com")
            tools._browser_policy.on_state("  [1] <a /> Next page")
            result = tools.browser_click(9)

        self.assertTrue(result.startswith("Error: 编号 #9"))

    def test_click_refuses_sensitive_label(self):
        with patch.object(config, "BROWSER_ID", "test-browser"), patch.object(
            config, "BROWSER_AUTO_APPROVE_CLICKS", True
        ), patch.object(tools, "_browser_result") as browser_result, patch.object(
            tools.console, "print"
        ):
            tools._browser_policy.on_open("https://example.com")
            tools._browser_policy.on_state("  [1] <a /> 登录\n  [2] <a /> 下一页")
            result = tools.browser_click(1)

        self.assertTrue(result.startswith("Refused:"))
        browser_result.assert_not_called()

    def test_navigate_invalidates_state(self):
        with patch.object(config, "BROWSER_ID", "test-browser"), patch.object(
            tools, "_browser_result", return_value="Success: navigated"
        ):
            tools._browser_policy.on_open("https://example.com")
            tools._browser_policy.on_state("  [1] <a /> Next page")
            tools.browser_navigate("https://example.com/next")
            result = tools.browser_click(1)

        self.assertTrue(result.startswith("Error: 尚未读取当前页面 state"))

    def test_browser_click_requires_confirmation(self):
        with patch.object(config, "BROWSER_ID", "test-browser"), patch.object(
            config, "BROWSER_AUTO_APPROVE_CLICKS", False
        ), patch("tools.Confirm.ask", return_value=False), patch.object(
            tools.console, "print"
        ), patch("tools.subprocess.run") as run:
            tools._browser_policy.on_open("https://example.com")
            tools._browser_policy.on_state("  [1] <a /> Next page")
            result = tools.browser_click(1)

        self.assertTrue(result.startswith("Refused: 用户拒绝"))
        run.assert_not_called()

    def test_browser_click_can_auto_approve_navigation_when_opted_in(self):
        with patch.object(config, "BROWSER_ID", "test-browser"), patch.object(
            config, "BROWSER_AUTO_APPROVE_CLICKS", True
        ), patch.object(tools, "_browser_result", return_value="Success: done") as browser_result, patch(
            "tools.Confirm.ask"
        ) as confirm, patch.object(tools.console, "print"):
            tools._browser_policy.on_open("https://example.com")
            tools._browser_policy.on_state("  [2] <a /> Next page")
            result = tools.browser_click(2)

        self.assertIn("页面已稳定", result)
        confirm.assert_not_called()
        self.assertEqual(browser_result.call_count, 2)

    def test_browser_close_is_idempotent(self):
        with patch.object(config, "BROWSER_ID", "test-browser"):
            result = tools.browser_close()

        self.assertTrue(result.startswith("Info:"))

    def test_close_browser_session_if_active_runs_once(self):
        with patch.object(config, "BROWSER_ID", "test-browser"), patch.object(
            tools, "_browser_run", return_value=(0, "closed")
        ) as browser_run:
            tools._browser_policy.on_open("https://example.com")
            first = tools.close_browser_session_if_active()
            second = tools.close_browser_session_if_active()

        self.assertEqual(first, "closed")
        self.assertIsNone(second)
        self.assertEqual(browser_run.call_count, 1)


class CompactToolTests(WorkspaceTestCase):
    """验证 bash/pwsh 路由、glob、ripgrep 回退和 read_file 字符预算。"""

    def setUp(self):
        super().setUp()
        self.root = Path(__file__).resolve().parents[1] / f".tool_test_{uuid.uuid4().hex}"
        self.root.mkdir()
        (self.root / "a.py").write_text("needle = 1\n", encoding="utf-8")
        (self.root / "b.txt").write_text("needle = 2\n", encoding="utf-8")
        (self.root / "sub").mkdir()
        (self.root / "sub" / "c.py").write_text("needle = 3\n", encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)
        super().tearDown()

    def test_glob_files_finds_recursive_matches_and_limits(self):
        result = tools.glob_files(self.root.name, "**/*.py", limit=1)

        self.assertIn("2 个文件", result)
        self.assertIn("a.py", result)
        self.assertIn("已显示前 1 个", result)
        self.assertNotIn("b.txt", result)

    def test_glob_files_rejects_escape_pattern(self):
        result = tools.glob_files(".", "../*.py")

        self.assertTrue(result.startswith("Error:"))

    def test_search_in_files_falls_back_without_rg_and_honors_glob(self):
        with patch.object(tools, "_find_rg", return_value=None):
            result = tools.search_in_files(
                "needle", self.root.name, glob="*.py"
            )

        self.assertIn("Python 回退", result)
        self.assertIn("a.py", result)
        self.assertIn("c.py", result)
        self.assertNotIn("b.txt", result)

    def test_search_in_files_uses_rg_when_available(self):
        completed = SimpleNamespace(
            returncode=0,
            stdout=b"./a.py:1:needle = 1\n",
            stderr=b"",
        )
        with patch.object(tools, "_find_rg", return_value="rg"), patch(
            "tools.subprocess.run", return_value=completed
        ) as run:
            result = tools.search_in_files(
                "needle", self.root.name, glob="*.py", max_results=5
            )

        self.assertIn("ripgrep", result)
        args = run.call_args.args[0]
        self.assertEqual(args[0], "rg")
        self.assertIn("--glob", args)
        self.assertEqual(args[-3:], ["--", "needle", "."])
        self.assertEqual(Path(run.call_args.kwargs["cwd"]).resolve(), self.root.resolve())
        self.assertFalse(run.call_args.kwargs["shell"])

    def test_read_file_truncates_by_character_budget(self):
        (self.root / "long.txt").write_text(
            "\n".join("x" * 100 for _ in range(3)) + "\n",
            encoding="utf-8",
        )
        with patch.object(config, "READ_OUTPUT_LIMIT", 80):
            result = tools.read_file(f"{self.root.name}/long.txt")

        self.assertIn("字符预算", result)
        self.assertIn("start=2", result)


class AgentBrowserFlowTests(WorkspaceTestCase):
    """用假模型和假 BrowserAct 驱动真实 Agent Loop，验证浏览器策略的端到端行为。"""

    def _run_agent(self, script, browser_id, fake_browser):
        responses = list(script)
        model_calls = []
        printed = []

        def fake_completion(**kwargs):
            model_calls.append({
                "system": kwargs["messages"][0]["content"],
                "tools": [schema["function"]["name"] for schema in kwargs["tools"]],
                "messages": [dict(message) for message in kwargs["messages"]],
            })
            item = responses.pop(0)
            if item[0] == "tool":
                _, call_id, name, args = item
                return _assistant_tool_call(call_id, name, args), 0.0
            if item[0] == "truncated":
                _, call_id, name, args, content = item
                return SimpleNamespace(choices=[SimpleNamespace(
                    message=SimpleNamespace(
                        content=content,
                        tool_calls=[_tool_call(
                            call_id, name, json.dumps(args, ensure_ascii=False)
                        )],
                    ),
                    finish_reason="length",
                )]), 0.0
            return _assistant_text(item[1]), 0.0

        def fake_print(*args, **kwargs):
            printed.append(" ".join(str(arg) for arg in args))

        with patch.object(config, "BROWSER_ID", browser_id), patch.object(
            config, "BROWSER_AUTO_APPROVE_CLICKS", True
        ), patch.object(main, "_offer_restore_session", return_value=[]), patch.object(
            main, "_save_session", return_value=None
        ), patch.object(main, "_create_completion", side_effect=fake_completion), patch.object(
            tools, "_browser_run", side_effect=fake_browser
        ), patch.object(main.console, "print", side_effect=fake_print), patch.object(
            tools.console, "print"
        ), patch("builtins.input", side_effect=["打开示例网站并翻页", "exit"]):
            main.start_interactive_session(str(Path(__file__).resolve().parents[1]))

        self.assertEqual(responses, [], "Agent Loop 未消费完所有脚本步骤")
        output = "\n".join(printed)
        self.assertNotIn("❌ 意外错误", output)
        return model_calls, output

    def test_agent_runs_open_state_click_state_and_auto_closes(self):
        fake_browser = _FakeBrowserAct()
        script = [
            ("tool", "c1", "browser_open", {"url": "https://example.com"}),
            ("tool", "c2", "browser_state", {}),
            ("tool", "c3", "browser_click", {"index": 1}),
            ("tool", "c4", "browser_click", {"index": 1}),
            ("tool", "c5", "browser_state", {}),
            ("tool", "c6", "browser_click", {"index": 2}),
            ("tool", "c7", "browser_click", {"index": 3}),
            ("text", "已完成：打开示例页面并翻页。"),
        ]

        model_calls, output = self._run_agent(script, "test-browser", fake_browser)

        tool_names = set(model_calls[0]["tools"])
        self.assertTrue(browser_policy.INTERACTIVE_TOOLS <= tool_names)
        self.assertIn("web_fetch_url", tool_names)
        self.assertIn("交互浏览状态：已启用", model_calls[0]["system"])

        self.assertIn("页面已稳定", output)
        self.assertIn("尚未读取当前页面 state", output)
        self.assertIn("Refused:", output)
        self.assertIn("已完成：打开示例页面并翻页。", output)
        self.assertEqual(output.count("[重试提示]"), 1)
        self.assertIn("错误次数: 1", output)
        self.assertIn("🌐 closed", output)

        session = config.BROWSER_SESSION
        self.assertEqual(fake_browser.count("--session", session, "browser", "open"), 1)
        self.assertEqual(fake_browser.count("--session", session, "state"), 2)
        self.assertEqual(fake_browser.count_click(1), 1)   # 第二次点击被状态机拦截
        self.assertEqual(fake_browser.count_click(2), 0)   # 登录按钮被敏感词拦截
        self.assertEqual(fake_browser.count_click(3), 1)   # 拒绝未使 state 失效
        self.assertEqual(fake_browser.count("--session", session, "wait", "stable"), 2)
        self.assertEqual(fake_browser.count("session", "close", session), 1)
        self.assertIs(tools._browser_policy.phase, browser_policy.BrowserPhase.CLOSED)

    def test_unconfigured_agent_does_not_see_or_run_browser_tools(self):
        fake_browser = _FakeBrowserAct()
        script = [
            # 模拟模型从历史消息里复用了一个旧的 browser_open 调用。
            ("tool", "u1", "browser_open", {"url": "https://example.com"}),
            ("text", "交互浏览未配置，已停止。"),
        ]

        model_calls, output = self._run_agent(script, "", fake_browser)

        tool_names = set(model_calls[0]["tools"])
        self.assertIn("web_fetch_url", tool_names)
        self.assertFalse(browser_policy.INTERACTIVE_TOOLS & tool_names)
        self.assertIn("交互浏览状态：未启用", model_calls[0]["system"])
        self.assertIn(
            "ConfigurationRequired:",
            model_calls[-1]["messages"][-1]["content"],
        )
        self.assertNotIn("[重试提示]", output)
        self.assertIn("错误次数: 0", output)
        self.assertEqual(fake_browser.calls, [])

    def test_truncated_tool_call_is_not_executed(self):
        name = f"_truncated_{uuid.uuid4().hex}.txt"
        path = Path(__file__).resolve().parents[1] / name
        self.addCleanup(path.unlink, missing_ok=True)
        script = [
            ("truncated", "t1", "write_file",
             {"path": name, "content": "half-written"},
             "正在写入文件"),
            ("text", "已改为分块写入。"),
        ]

        model_calls, output = self._run_agent(script, "", _FakeBrowserAct())

        self.assertIn("达到 max_tokens", output)
        self.assertFalse(path.exists())
        self.assertIn(
            "上一条输出因长度上限被截断",
            model_calls[1]["messages"][-1]["content"],
        )
        self.assertIn("已改为分块写入。", output)

    def test_context_line_printed_during_task_execution(self):
        # 执行期间 prompt_toolkit 底部栏不可见，主循环必须在每步打印读数。
        script = [
            ("tool", "c1", "list_dir", {"path": "."}),
            ("text", "完成。"),
        ]
        _model_calls, output = self._run_agent(script, "", _FakeBrowserAct())

        self.assertIn("ctx:", output)
        self.assertIn("~", output)


class MemoryAgentFlowTests(WorkspaceTestCase):
    """用脚本化模型驱动真实 Agent Loop：看到提示 → 压缩 → 检索 → 解压。"""

    def setUp(self):
        super().setUp()
        self.memory_root = Path(__file__).resolve().parents[1] / f".memory_flow_{uuid.uuid4().hex}"
        self.memory_root.mkdir()
        memory.set_store(memory.MemoryStore(self.memory_root))
        memory.set_active_messages(None)

    def tearDown(self):
        memory.reset_state()
        shutil.rmtree(self.memory_root, ignore_errors=True)
        super().tearDown()

    def test_agent_proactively_compresses_searches_and_decompresses(self):
        filler = "这里是很长的旧工具输出，包含目录扫描结果和测试日志。" * 20
        responses = [
            ("text", "任务一完成。" + filler),
            ("text", "任务二完成。" + filler),
            ("text", "任务三完成。" + filler),
            ("tool", "m1", "context_status", {}),
            ("tool", "m2", "compress_context", {
                "start_turn": 1,
                "end_turn": 1,
                "summary": (
                    "## 任务目标\n实现 pathlib 扫描\n"
                    "## 已完成\n扫描目录并记录 README.md\n"
                    "## 关键文件/命令\nREADME.md\n"
                    "## 未完成/下一步\n无\n## 约束\n保持工作区只读"
                ),
                "topic": "初始化",
            }),
            ("tool", "m3", "search_context", {"query": "pathlib"}),
            ("tool", "m4", "decompress_context", {"block_id": "b0001"}),
            ("text", "已完成压缩、检索和解压验证。"),
        ]
        model_calls = []
        printed = []

        def fake_completion(**kwargs):
            model_calls.append({
                "system": kwargs["messages"][0]["content"],
                "messages": [dict(message) for message in kwargs["messages"]],
            })
            item = responses.pop(0)
            if item[0] == "tool":
                _, call_id, name, args = item
                return _assistant_tool_call(call_id, name, args), 0.0
            return _assistant_text(item[1]), 0.0

        def fake_print(*args, **kwargs):
            printed.append(" ".join(str(arg) for arg in args))

        with patch.object(config, "MODEL_CONTEXT_TOKENS", 500), \
                patch.object(config, "CONTEXT_SOFT_RATIO", 0.5), \
                patch.object(config, "CONTEXT_HARD_RATIO", 100.0), \
                patch.object(main, "_offer_restore_session", return_value=[]), \
                patch.object(main, "_save_session", return_value=None), \
                patch.object(main, "_create_completion", side_effect=fake_completion), \
                patch.object(main.console, "print", side_effect=fake_print), \
                patch.object(tools.console, "print"), \
                patch("builtins.input", side_effect=[
                    "任务一", "任务二", "任务三", "任务四，请压缩旧历史", "exit",
                ]):
            main.start_interactive_session(str(Path(__file__).resolve().parents[1]))

        self.assertEqual(responses, [], "Agent Loop 未消费完所有脚本步骤")
        output = "\n".join(printed)
        self.assertNotIn("❌ 意外错误", output)
        self.assertTrue(
            any("[HQIN_CONTEXT_NOTICE]" in call["system"] for call in model_calls),
            "模型在长上下文中没有看到动态压缩提示",
        )
        self.assertIn("Success: 已压缩 turn 1–1", output)
        self.assertIn("找到 1 个相关归档块", output)
        self.assertIn("归档块 b0001", output)
        self.assertIn("已完成压缩、检索和解压验证。", output)
        self.assertEqual(len(memory.get_store().list()), 1)
        self.assertIn("pathlib", memory.get_store().get("b0001")["summary"])


class CliUiTests(WorkspaceTestCase):
    def test_parse_slash_command(self):
        self.assertEqual(cli_ui.parse_command("/status"), ("status", ""))
        self.assertEqual(cli_ui.parse_command("/help  context"), ("help", "context"))
        self.assertIsNone(cli_ui.parse_command("普通输入"))

    def test_slash_clear_preserves_system_prompt(self):
        messages = [
            {"role": "system", "content": "BASE"},
            {"role": "user", "content": "旧任务"},
        ]
        with patch.object(main.console, "print"):
            result, should_exit = main._handle_slash_command(
                "clear", "", messages, "BASE", str(Path(__file__).resolve().parents[1])
            )
        self.assertFalse(should_exit)
        self.assertEqual(result, [{"role": "system", "content": "BASE"}])

    def test_status_line_contains_model_and_context(self):
        line = cli_ui.build_status_line(
            [{"role": "user", "content": "一段用于估算上下文的中文"}],
            str(Path(__file__).resolve().parents[1]),
        )
        self.assertIn(config.MODEL, line)
        self.assertIn("ctx:", line)
        self.assertIn("tok", line)

    def test_read_git_branch_from_head(self):
        # 直接读取项目自己的 .git/HEAD，避免在受限环境创建临时目录。
        branch = cli_ui.read_git_branch(str(Path(__file__).resolve().parents[1]))
        self.assertIsInstance(branch, str)
        self.assertNotIn("\n", branch)
        self.assertTrue(branch)

    def test_ask_user_falls_back_without_tty(self):
        fake_stdin = SimpleNamespace(isatty=lambda: False)
        fake_stdout = SimpleNamespace(isatty=lambda: False)
        with patch.object(cli_ui.sys, "stdin", fake_stdin), patch.object(
            cli_ui.sys, "stdout", fake_stdout
        ), patch("builtins.input", return_value="hello"):
            self.assertEqual(cli_ui.ask_user([], str(Path(__file__).resolve().parents[1])), "hello")

    def test_write_file_preview_can_refuse(self):
        name = f"_cli_write_{uuid.uuid4().hex}.txt"
        path = Path(__file__).resolve().parents[1] / name
        seen = []

        def hook(rel_path, old_text, new_text, diff_text):
            seen.append((rel_path, old_text, new_text, diff_text))
            return False

        tools.set_edit_preview_hook(hook)
        try:
            result = tools.write_file(name, "new content")
            self.assertTrue(result.startswith("Refused:"))
            self.assertFalse(path.exists())
            self.assertIn("+new content", seen[0][3])
        finally:
            tools.set_edit_preview_hook(None)
            path.unlink(missing_ok=True)

    def test_edit_file_preview_can_refuse(self):
        name = f"_cli_edit_{uuid.uuid4().hex}.txt"
        path = Path(__file__).resolve().parents[1] / name
        tools.set_edit_preview_hook(None)
        tools.write_file(name, "old content")
        try:
            tools.set_edit_preview_hook(lambda *args: False)
            result = tools.edit_file(name, "old", "new")
            self.assertTrue(result.startswith("Refused:"))
            self.assertEqual(path.read_text(encoding="utf-8"), "old content")
        finally:
            tools.set_edit_preview_hook(None)
            path.unlink(missing_ok=True)

    def test_confirm_edit_returns_user_choice(self):
        # confirm_edit 的审批行为依赖当前权限模式；本测试验证 ask 模式语义。
        tools.set_permission_mode("ask")
        self.addCleanup(tools.set_permission_mode, "ask")
        with patch.object(cli_ui.Confirm, "ask", return_value=False), patch.object(
            cli_ui.console, "print"
        ):
            self.assertFalse(cli_ui.confirm_edit("a.txt", "old", "new", "-old\n+new"))
        with patch.object(cli_ui.Confirm, "ask", return_value=True), patch.object(
            cli_ui.console, "print"
        ):
            self.assertTrue(cli_ui.confirm_edit("a.txt", "old", "new", "-old\n+new"))


class ContextUsageDisplayTests(WorkspaceTestCase):
    """三层上下文占用显示：状态行升级、/context 面板、实测校准。"""

    def setUp(self):
        super().setUp()
        memory.reset_real_usage()

    def tearDown(self):
        memory.reset_real_usage()
        super().tearDown()

    def test_breakdown_sums_match_total_estimate(self):
        messages = [
            {"role": "system", "content": "系统提示"},
            {"role": "user", "content": "任务描述"},
            {"role": "assistant", "content": "回复", "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "read_file", "arguments": '{"path":"main.py"}'}}
            ]},
            {"role": "tool", "tool_call_id": "c1", "content": "文件内容"},
        ]
        breakdown = memory.context_usage_breakdown(messages)
        self.assertEqual(
            sum(entry["tokens"] for entry in breakdown["roles"].values()),
            breakdown["total"],
        )
        self.assertEqual(breakdown["total"], memory.estimate_messages_tokens(messages))
        self.assertEqual(breakdown["messages"], len(messages))
        self.assertIn("system", breakdown["roles"])
        self.assertIn("tool", breakdown["roles"])

    def test_record_real_usage_ignores_zero_and_invalid(self):
        memory.record_real_usage(0)
        self.assertIsNone(memory.get_real_usage())
        memory.record_real_usage(None)
        self.assertIsNone(memory.get_real_usage())
        memory.record_real_usage("not-a-number")
        self.assertIsNone(memory.get_real_usage())
        memory.record_real_usage(1234, 56)
        usage = memory.get_real_usage()
        self.assertEqual(usage["prompt_tokens"], 1234)
        self.assertEqual(usage["completion_tokens"], 56)
        self.assertEqual(usage["total_tokens"], 1234 + 56)
        # 零值代表服务端未上报 usage，不能覆盖上一次有效测量。
        memory.record_real_usage(0, 10)
        self.assertEqual(memory.get_real_usage()["prompt_tokens"], 1234)

    def test_reset_real_usage_and_reset_state(self):
        memory.record_real_usage(100)
        self.assertIsNotNone(memory.get_real_usage())
        memory.reset_real_usage()
        self.assertIsNone(memory.get_real_usage())
        memory.record_real_usage(100)
        memory.reset_state()
        self.assertIsNone(memory.get_real_usage())

    def test_record_model_updates_real_usage(self):
        stats = main._TaskStats()
        response = SimpleNamespace(
            usage=SimpleNamespace(prompt_tokens=9000, completion_tokens=120)
        )
        stats.record_model(response, 0.5)
        self.assertEqual(stats.prompt_tokens, 9000)
        self.assertEqual(stats.completion_tokens, 120)
        self.assertEqual(memory.get_real_usage()["prompt_tokens"], 9000)

    def test_calibration_anchors_estimates_to_real_usage(self):
        messages = [{"role": "user", "content": "一段上下文"}] * 3
        # 未校准：退化为原始估算。
        self.assertEqual(
            memory.calibrated_messages_tokens(messages),
            memory.estimate_messages_tokens(messages),
        )
        # 实测 9000；快照时的原始估算作为锚点。
        memory.set_active_messages(messages)
        memory.record_real_usage(9000, 100)
        self.assertTrue(memory.is_calibrated())
        offset = 9000 - memory.get_real_usage()["raw_estimate"]
        self.assertGreater(offset, 0)
        # 消息不变时：校准值 = 实测值本身。
        self.assertEqual(memory.calibrated_messages_tokens(messages), 9000)
        # 追加消息后：校准值 = 原始估算 + 差值，增量部分仍用本地估算。
        messages.append({"role": "user", "content": "新增消息"})
        raw_after = memory.estimate_messages_tokens(messages)
        self.assertEqual(
            memory.calibrated_messages_tokens(messages),
            raw_after + offset,
        )
        # 预算判断使用校准口径。
        with patch.object(config, "MODEL_CONTEXT_TOKENS", 9100), \
                patch.object(config, "CONTEXT_HARD_RATIO", 0.99):
            self.assertFalse(memory.over_hard_budget(messages))
        with patch.object(config, "MODEL_CONTEXT_TOKENS", 9000), \
                patch.object(config, "CONTEXT_HARD_RATIO", 0.99):
            self.assertTrue(memory.over_hard_budget(messages))

    def test_record_real_usage_snapshots_raw_estimate(self):
        messages = [{"role": "user", "content": "锚点快照"}]
        memory.set_active_messages(messages)
        memory.record_real_usage(5000, 50)
        usage = memory.get_real_usage()
        self.assertEqual(usage["raw_estimate"],
                         memory.estimate_messages_tokens(messages))

    def test_reset_state_clears_calibration(self):
        memory.set_active_messages([{"role": "user", "content": "hi"}])
        memory.record_real_usage(9000)
        self.assertTrue(memory.is_calibrated())
        memory.reset_state()
        self.assertFalse(memory.is_calibrated())

    def test_record_model_without_usage_keeps_previous(self):
        stats = main._TaskStats()
        memory.record_real_usage(1000)
        # 个别服务不上报 usage；不应覆盖上次有效测量。
        stats.record_model(SimpleNamespace(), 0.5)
        self.assertEqual(memory.get_real_usage()["prompt_tokens"], 1000)

    def test_status_line_contains_bar_and_real_usage(self):
        workspace = str(Path(__file__).resolve().parents[1])
        # 短消息：0% 进度条为空，但条形字符和估算标记必须存在。
        line_plain = cli_ui.build_status_line(
            [{"role": "user", "content": "估算上下文"}], workspace
        )
        self.assertIn("ctx:", line_plain)
        self.assertIn("░", line_plain)
        self.assertIn("~", line_plain)
        self.assertNotIn("实测", line_plain)
        # 长消息：进度条必须有填充块。
        big = [{"role": "user", "content": "上下文很长 " * 2000}]
        line_big = cli_ui.build_status_line(big, workspace)
        self.assertIn("▓", line_big)
        memory.record_real_usage(8888, 20)
        line_real = cli_ui.build_status_line(
            [{"role": "user", "content": "估算上下文"}], workspace
        )
        self.assertIn("⚡8,888 实测", line_real)

    def test_context_token_text_returns_style_class(self):
        _text, style = cli_ui.context_token_text([{"role": "user", "content": "hi"}])
        self.assertIn(style, ("ctx-ok", "ctx-warn", "ctx-danger"))

    def test_usage_bar_clamps_and_marks(self):
        bar_over = cli_ui.usage_bar(150.0)
        self.assertEqual(len([c for c in bar_over if c in "▓░"]), 10)
        self.assertIn("‼", bar_over)
        with patch.object(config, "CONTEXT_HARD_RATIO", 0.85), \
                patch.object(config, "CONTEXT_SOFT_RATIO", 0.65):
            self.assertIn("⚠", cli_ui.usage_bar(70.0))
            self.assertNotIn("⚠", cli_ui.usage_bar(30.0))
            self.assertNotIn("‼", cli_ui.usage_bar(30.0))

    def test_render_context_report_full(self):
        messages = [
            {"role": "system", "content": "系统提示"},
            {"role": "user", "content": "第一个任务"},
            {"role": "assistant", "content": "完成"},
            {"role": "user", "content": "第二个任务"},
            {"role": "assistant", "content": "完成"},
        ]
        memory.set_active_messages(messages)
        memory.record_real_usage(3000, 100)
        report = cli_ui.render_context_report(messages)
        self.assertIn("软阈值", report)
        self.assertIn("硬阈值", report)
        self.assertIn("system", report)
        self.assertIn("assistant", report)
        self.assertIn("合计", report)
        self.assertIn("最近实测: 3,000 prompt tokens", report)
        self.assertIn("已按其校准", report)
        self.assertIn("系统偏差", report)
        self.assertIn("固定开销", report)
        self.assertIn("占用最大的轮次", report)

    def test_render_context_report_empty_messages(self):
        report = cli_ui.render_context_report([])
        self.assertIn("上下文: 0 /", report)
        self.assertIn("暂无", report)
        self.assertNotIn("占用最大的轮次", report)

    def test_print_context_panel_renders(self):
        buffer = io.StringIO()
        target = cli_ui.Console(file=buffer, width=120)
        cli_ui.print_context_panel(
            [{"role": "user", "content": "hi"}], console_obj=target
        )
        text = buffer.getvalue()
        self.assertIn("上下文占用", text)
        self.assertIn("合计", text)

    def test_context_status_tool_includes_real_usage(self):
        memory.record_real_usage(4000)
        status = memory.context_status([{"role": "user", "content": "hi"}])
        self.assertIn("最近实测: 4,000", status)

    def test_slash_context_registered_and_leaves_messages_untouched(self):
        self.assertIn("/context", cli_ui.SLASH_COMMANDS)
        messages = [
            {"role": "system", "content": "BASE"},
            {"role": "user", "content": "hi"},
        ]
        with patch.object(main.console, "print"):
            result, should_exit = main._handle_slash_command(
                "context", "", messages, "BASE",
                str(Path(__file__).resolve().parents[1]),
            )
        self.assertFalse(should_exit)
        self.assertEqual(result, messages)
        self.assertEqual(messages[0]["content"], "BASE")

    def test_clear_resets_real_usage(self):
        memory.record_real_usage(500)
        messages = [
            {"role": "system", "content": "BASE"},
            {"role": "user", "content": "旧任务"},
        ]
        with patch.object(main.console, "print"):
            result, _should_exit = main._handle_slash_command(
                "clear", "", messages, "BASE",
                str(Path(__file__).resolve().parents[1]),
            )
        self.assertIsNone(memory.get_real_usage())
        self.assertEqual(result, [{"role": "system", "content": "BASE"}])

    def test_new_resets_real_usage(self):
        memory.record_real_usage(500)
        messages = [
            {"role": "system", "content": "BASE"},
            {"role": "user", "content": "旧任务"},
        ]
        with patch.object(main.console, "print"), patch.object(
            main, "_save_session", return_value=None
        ):
            result, _should_exit = main._handle_slash_command(
                "new", "", messages, "BASE",
                str(Path(__file__).resolve().parents[1]),
            )
        self.assertIsNone(memory.get_real_usage())
        self.assertEqual(result, [{"role": "system", "content": "BASE"}])


class PermissionModeTests(WorkspaceTestCase):
    """三档权限模式：/mode 切换、闸门行为、模式徽标。"""

    def setUp(self):
        super().setUp()
        tools.set_permission_mode("ask")
        self.addCleanup(tools.set_permission_mode, "ask")

    # ── 纯函数与注册 ──────────────────────────────

    def test_normalize_mode(self):
        self.assertEqual(cli_ui.normalize_mode("ASK"), "ask")
        self.assertEqual(cli_ui.normalize_mode(" Workspace "), "workspace")
        self.assertEqual(cli_ui.normalize_mode("full"), "full")
        self.assertIsNone(cli_ui.normalize_mode("danger"))
        self.assertIsNone(cli_ui.normalize_mode(""))
        self.assertIsNone(cli_ui.normalize_mode(None))

    def test_mode_label_and_color(self):
        self.assertEqual(cli_ui.mode_label("workspace"), "mode:workspace")
        self.assertEqual(cli_ui.mode_color("ask"), "yellow")
        self.assertEqual(cli_ui.mode_color("workspace"), "cyan")
        self.assertEqual(cli_ui.mode_color("full"), "red")
        self.assertEqual(cli_ui.mode_color("unknown"), "yellow")

    def test_mode_registered_in_slash_commands(self):
        self.assertIn("/mode", cli_ui.SLASH_COMMANDS)

    def test_status_line_contains_mode_badge(self):
        line = cli_ui.build_status_line(
            [{"role": "user", "content": "hi"}],
            str(Path(__file__).resolve().parents[1]),
            mode="workspace",
        )
        self.assertIn("mode:workspace", line)
        self.assertIn("ctx:", line)

    # ── /mode 命令 ────────────────────────────────

    def _mode_cmd(self, arg, mode_box, extra_patches=()):
        messages = [{"role": "system", "content": "BASE"}]
        with patch.object(main.console, "print"):
            for cm in extra_patches:
                cm.__enter__()
            try:
                result, should_exit = main._handle_slash_command(
                    "mode", arg, messages, "BASE",
                    str(Path(__file__).resolve().parents[1]), mode_box,
                )
            finally:
                for cm in reversed(extra_patches):
                    cm.__exit__(None, None, None)
        return result, should_exit

    def test_mode_without_arg_shows_help(self):
        mode_box = ["ask"]
        result, should_exit = self._mode_cmd("", mode_box)
        self.assertFalse(should_exit)
        self.assertEqual(mode_box[0], "ask")
        self.assertEqual(result, [{"role": "system", "content": "BASE"}])

    def test_mode_switches_to_workspace(self):
        mode_box = ["ask"]
        self._mode_cmd("workspace", mode_box)
        self.assertEqual(mode_box[0], "workspace")
        self.assertEqual(tools.get_permission_mode(), "workspace")

    def test_mode_rejects_unknown_value(self):
        mode_box = ["ask"]
        self._mode_cmd("yolo", mode_box)
        self.assertEqual(mode_box[0], "ask")

    def test_mode_full_requires_confirm(self):
        mode_box = ["ask"]
        # 拒绝二次确认 → 不切换。
        self._mode_cmd(
            "full", mode_box,
            extra_patches=[patch.object(main.Confirm, "ask", return_value=False)],
        )
        self.assertEqual(mode_box[0], "ask")
        # 接受二次确认 → 切换。
        self._mode_cmd(
            "full", mode_box,
            extra_patches=[patch.object(main.Confirm, "ask", return_value=True)],
        )
        self.assertEqual(mode_box[0], "full")
        self.assertEqual(tools.get_permission_mode(), "full")

    def test_mode_switching_to_same_mode_is_noop(self):
        mode_box = ["ask"]
        self._mode_cmd("ask", mode_box)
        self.assertEqual(mode_box[0], "ask")

    # ── 闸门行为 ──────────────────────────────────

    def test_shell_gate_skips_confirm_in_full_mode(self):
        tools.set_permission_mode("full")
        with patch.object(tools.Confirm, "ask") as confirm, \
                patch.object(tools.console, "print"):
            self.assertTrue(tools._approve_shell("bash", "echo hi"))
        confirm.assert_not_called()

    def test_shell_gate_asks_in_ask_mode(self):
        tools.set_permission_mode("ask")
        with patch.object(tools.Confirm, "ask", return_value=False) as confirm, \
                patch.object(tools.console, "print"):
            self.assertFalse(tools._approve_shell("bash", "echo hi"))
        confirm.assert_called_once()

    def test_git_commit_gate_skips_confirm_in_full_mode(self):
        tools.set_permission_mode("full")
        with patch.object(tools, "_git_run", return_value=(0, "ok")) as git_run, \
                patch.object(tools.Confirm, "ask") as confirm, \
                patch.object(tools.console, "print"):
            result = tools.git_commit("msg")
        confirm.assert_not_called()
        git_run.assert_called()
        self.assertIn("Success", result)

    def test_browser_click_full_mode_still_blocks_sensitive_labels(self):
        # full 免审批，但敏感标签拦截（登录/支付等）仍生效。
        tools.set_permission_mode("full")
        tools._browser_policy.reset()
        self.addCleanup(tools._browser_policy.reset)
        with patch.object(config, "BROWSER_ID", "test_browser"), \
                patch.object(tools, "_browser_run", return_value=(0, "opened")):
            opened = tools.browser_open("https://example.com")
        self.assertNotIn("ConfigurationRequired", opened)
        tools._browser_policy.on_state("[1] <a /> 登录\n")
        with patch.object(config, "BROWSER_ID", "test_browser"), \
                patch.object(tools, "_browser_run", return_value=(0, "state")):
            result = tools.browser_click(1)
        self.assertTrue(result.startswith(("Error:", "Refused:")))

    def test_confirm_edit_skips_confirm_in_workspace_mode(self):
        tools.set_permission_mode("workspace")
        with patch.object(cli_ui.Confirm, "ask") as confirm, \
                patch.object(cli_ui.console, "print"):
            self.assertTrue(cli_ui.confirm_edit("a.txt", "", "new", "+new"))
        confirm.assert_not_called()

    def test_confirm_edit_asks_in_ask_mode(self):
        tools.set_permission_mode("ask")
        with patch.object(cli_ui.Confirm, "ask", return_value=False) as confirm, \
                patch.object(cli_ui.console, "print"):
            self.assertFalse(cli_ui.confirm_edit("a.txt", "old", "new", "diff"))
        confirm.assert_called_once()

    def test_full_mode_write_file_no_confirm(self):
        # 端到端：full 模式下 write_file 不再触发 diff 确认，直接落盘。
        tools.set_permission_mode("full")
        tools.set_edit_preview_hook(cli_ui.confirm_edit)
        self.addCleanup(tools.set_edit_preview_hook, None)
        name = f"_mode_write_{uuid.uuid4().hex}.txt"
        path = Path(__file__).resolve().parents[1] / name
        self.addCleanup(path.unlink, missing_ok=True)
        with patch.object(cli_ui.Confirm, "ask") as confirm:
            result = tools.write_file(name, "内容")
        confirm.assert_not_called()
        self.assertTrue(result.startswith("Success"))
        self.assertEqual(path.read_text(encoding="utf-8"), "内容")

    def test_hqin_mode_env_default_and_fallback(self):
        # config.MODE 在导入时读取 HQIN_MODE；非法值回落 ask。
        self.assertIn(config.MODE, ("ask", "workspace", "full"))


class StreamAndInterruptTests(WorkspaceTestCase):
    """流式收集器：脚本化流块 → 与非流式等价的响应结构。"""

    @staticmethod
    def _chunk(delta=None, finish=None, usage=None):
        return SimpleNamespace(
            choices=[SimpleNamespace(delta=delta, finish_reason=finish)],
            usage=usage,
        )

    def test_collect_stream_text_and_tool_calls(self):
        def d(content=None, tool_calls=None):
            return SimpleNamespace(content=content, tool_calls=tool_calls)
        chunks = [
            self._chunk(d(content="你")),
            self._chunk(d(content="好")),
            self._chunk(d(tool_calls=[SimpleNamespace(
                index=0, id="call_1", type="function",
                function=SimpleNamespace(name="read_file", arguments='{"pa'),
            )])),
            self._chunk(d(tool_calls=[SimpleNamespace(
                index=0, id=None, type=None,
                function=SimpleNamespace(name=None, arguments='th": "a.py"}'),
            )]), finish="tool_calls"),
            SimpleNamespace(choices=[], usage=SimpleNamespace(
                prompt_tokens=100, completion_tokens=20)),
        ]

        class FakeStatus:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def update(self, text): pass

        resp = main._collect_stream(iter(chunks), FakeStatus())
        msg = resp.choices[0].message
        self.assertEqual(msg.content, "你好")
        self.assertEqual(resp.choices[0].finish_reason, "tool_calls")
        tc = msg.tool_calls[0]
        self.assertEqual(tc.id, "call_1")
        self.assertEqual(tc.function.name, "read_file")
        self.assertEqual(tc.function.arguments, '{"path": "a.py"}')
        self.assertEqual(resp.usage.prompt_tokens, 100)

    def test_collect_stream_interrupt_propagates(self):
        def gen():
            yield self._chunk(SimpleNamespace(content="x", tool_calls=None))
            raise KeyboardInterrupt
        class FakeStatus:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def update(self, text): pass
        with self.assertRaises(KeyboardInterrupt):
            main._collect_stream(gen(), FakeStatus())


class RunTestsToolTests(WorkspaceTestCase):
    """run_tests：白名单校验 + 审批语义（不真跑测试进程）。"""

    def test_validate_rejects_meta_chars(self):
        for bad in ("a; rm -rf", "a | b", "a && b", "a>b", "$(x)", "`x`"):
            self.assertNotEqual(
                tools._validate_test_args("tests", bad), "",
                f"应当拒绝 {bad!r}",
            )

    def test_validate_rejects_unknown_flags(self):
        self.assertNotEqual(tools._validate_test_args("tests", "--pdb"), "")

    def test_validate_accepts_normal_args(self):
        self.assertEqual(tools._validate_test_args("tests", "-x -q"), "")
        self.assertEqual(tools._validate_test_args(".", ""), "")
        self.assertEqual(
            tools._validate_test_args("tests/test_x.py::TestY", "-k name"), "")

    def test_validate_rejects_absolute_or_escape_target(self):
        self.assertNotEqual(tools._validate_test_args("D:/evil", ""), "")
        self.assertNotEqual(tools._validate_test_args("../outside", ""), "")

    def test_ask_mode_confirms_and_refuses(self):
        tools.set_permission_mode("ask")
        with patch.object(tools.Confirm, "ask", return_value=False) as confirm, \
                patch.object(tools.console, "print"):
            result = tools.run_tests("tests")
        confirm.assert_called_once()
        self.assertTrue(result.startswith("Refused:"))

    def test_workspace_mode_skips_confirm_and_runs(self):
        tools.set_permission_mode("workspace")
        fake = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout="3 passed".encode("utf-8"), stderr=b"",
        )
        with patch.object(tools.subprocess, "run", return_value=fake) as run, \
                patch.object(tools.Confirm, "ask") as confirm:
            result = tools.run_tests("tests")
        confirm.assert_not_called()
        run.assert_called_once()
        self.assertTrue(result.startswith("Success:"))

    def test_failure_exit_code_is_error(self):
        tools.set_permission_mode("full")
        fake = subprocess.CompletedProcess(
            args=[], returncode=1,
            stdout="1 failed".encode("utf-8"), stderr=b"",
        )
        with patch.object(tools.subprocess, "run", return_value=fake):
            result = tools.run_tests("tests")
        self.assertTrue(result.startswith("Error:"))


class SnapshotUndoTests(WorkspaceTestCase):
    """任务前 git 快照 + /undo：用真实临时 git 仓库验证。"""

    @staticmethod
    def _pipes_available() -> bool:
        """DSH 沙盒禁止 CreatePipe；探测子进程管道是否可用，不可用则跳过。"""
        try:
            subprocess.run(
                ["git", "--version"], capture_output=True, timeout=15,
                stdin=subprocess.DEVNULL,
            )
            return True
        except (PermissionError, OSError):
            return False

    def setUp(self):
        super().setUp()
        if not self._pipes_available():
            self.skipTest("当前环境禁止子进程管道（DSH 沙盒），快照测试在真实终端执行")
        import tempfile
        self.repo_dir = Path(tempfile.mkdtemp(prefix="hqin_snap_"))
        self.addCleanup(shutil.rmtree, self.repo_dir, ignore_errors=True)
        self._old_cwd = os.getcwd()
        self.repo_dir.mkdir(exist_ok=True)
        subprocess.run(["git", "init"], cwd=self.repo_dir,
                       capture_output=True, timeout=30)
        subprocess.run(
            ["git", "-c", "user.name=T", "-c", "user.email=t@t",
             "commit", "--allow-empty", "-m", "init"],
            cwd=self.repo_dir, capture_output=True, timeout=30)

    def _in_repo(self, fn):
        os.chdir(self.repo_dir)
        try:
            return fn()
        finally:
            os.chdir(self._old_cwd)

    def test_snapshot_skips_clean_repo(self):
        def check():
            note = tools.task_snapshot()
            self.assertEqual(note, "仓库干净，无需快照")
        self._in_repo(check)

    def test_snapshot_commits_dirty_repo_and_undo_reverts(self):
        def prepare():
            (self.repo_dir / "a.txt").write_text("v1", encoding="utf-8")
            self.assertEqual(tools.task_snapshot(), "")
            # 快照后 agent 改坏文件。
            (self.repo_dir / "a.txt").write_text("v2-broken", encoding="utf-8")
            (self.repo_dir / "b.txt").write_text("new", encoding="utf-8")
        self._in_repo(prepare)

        def undo():
            result = tools.undo_last_snapshot()
            self.assertTrue(result.startswith("Success:"))
            self.assertEqual(
                (self.repo_dir / "a.txt").read_text(encoding="utf-8"), "v1")
            self.assertFalse((self.repo_dir / "b.txt").exists())
        self._in_repo(undo)

    def test_undo_refuses_when_head_is_not_snapshot(self):
        def check():
            (self.repo_dir / "c.txt").write_text("x", encoding="utf-8")
            subprocess.run(
                ["git", "-c", "user.name=T", "-c", "user.email=t@t",
                 "add", "-A", "-c", "user.name=T"],
                cwd=self.repo_dir, capture_output=True)
            subprocess.run(
                ["git", "-c", "user.name=T", "-c", "user.email=t@t",
                 "commit", "-m", "user's own commit"],
                cwd=self.repo_dir, capture_output=True, timeout=30)
            result = tools.undo_last_snapshot()
            self.assertIn("不是 HQin 快照", result)
        self._in_repo(check)


if __name__ == "__main__":
    unittest.main()
