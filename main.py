# main.py  —  Route B: 原生 OpenAI Tool Calling
import os
import sys
import json
import time
import platform
import traceback
import argparse
from pathlib import Path
from types import SimpleNamespace
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from openai import OpenAI
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.prompt import Prompt, Confirm

import config
import console_setup
import workspace as _ws
import browser_policy
import memory
import cli_ui as cli
import notifier
import tools as _tools
from prompts import SYSTEM_PROMPT
from tool_schemas import TOOL_SCHEMAS
from tools import TOOL_REGISTRY

console_setup.configure_utf8_console()
console = Console()

client = OpenAI(
    base_url=config.BASE_URL,
    api_key=config.API_KEY,
    timeout=config.MODEL_TIMEOUT_SECONDS,
    max_retries=0,  # 由 _create_completion 统一记录并控制重试
)

# 只有没有副作用的工具可并行；写文件、运行命令和提交必须保持调用顺序。
_PARALLEL_SAFE_TOOLS = {
    "list_dir", "read_file", "search_in_files", "glob_files",
    "git_status", "git_diff", "web_search", "web_fetch_url", "finish_check",
    "run_tests",
}


class TaskInterrupted(Exception):
    """用户在模型生成或工具执行期间按 Ctrl+C；丢弃本轮，回到输入框。"""


def _collect_stream(stream, status) -> dict:
    """
    消费 OpenAI 兼容的流式响应，拼装成与非流式完全相同的响应结构。
    中断（KeyboardInterrupt）直接向上抛，由调用方丢弃半截内容。
    """
    text_parts: list[str] = []
    tool_calls: dict[int, dict] = {}   # index -> {id, name, arguments}
    finish_reason = None
    usage = None

    with status:
        for chunk in stream:
            if not chunk.choices:
                if getattr(chunk, "usage", None):
                    usage = chunk.usage
                continue
            choice = chunk.choices[0]
            delta = choice.delta
            if delta is None:
                continue
            if delta.content:
                text_parts.append(delta.content)
                status.update(f"生成中... {sum(len(p) for p in text_parts):,} 字符")
            for tc in delta.tool_calls or []:
                slot = tool_calls.setdefault(tc.index or 0, {
                    "id": "", "name": "", "arguments": "",
                })
                if tc.id:
                    slot["id"] = tc.id
                if tc.function and tc.function.name:
                    slot["name"] = slot["name"] + tc.function.name
                if tc.function and tc.function.arguments:
                    slot["arguments"] = slot["arguments"] + tc.function.arguments
            if choice.finish_reason:
                finish_reason = choice.finish_reason
            if getattr(chunk, "usage", None):
                usage = chunk.usage

    def _arg(name, default):
        return getattr(usage, name, default) if usage else default

    message = SimpleNamespace(
        content="".join(text_parts) or None,
        tool_calls=(
            [
                SimpleNamespace(
                    id=slot["id"] or f"call_{i}",
                    type="function",
                    function=SimpleNamespace(name=slot["name"], arguments=slot["arguments"]),
                )
                for i, slot in sorted(tool_calls.items())
            ] or None
        ),
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason=finish_reason or "stop")],
        usage=SimpleNamespace(
            prompt_tokens=_arg("prompt_tokens", 0),
            completion_tokens=_arg("completion_tokens", 0),
        ) if usage else None,
    )


def _create_completion(**kwargs):
    """
    调用本地模型并对短暂服务错误做有限重试，返回 (response, elapsed)。
    流式优先：逐块拼接，Rich status 显示进度；中断即抛 TaskInterrupted。
    流式不支持（旧网关）时自动回退非流式。
    """
    last_error = None
    started = time.perf_counter()
    want_stream = kwargs.pop("stream", False)
    for attempt in range(config.MODEL_MAX_RETRIES + 1):
        try:
            if want_stream:
                try:
                    stream = client.chat.completions.create(
                        **kwargs, stream=True,
                        stream_options={"include_usage": True},
                    )
                except TypeError:
                    # 个别 OpenAI 兼容服务不认 stream_options，去掉重试。
                    stream = client.chat.completions.create(**kwargs, stream=True)
                with console.status("[dim cyan]生成中...[/dim cyan]") as status:
                    return _collect_stream(stream, status), time.perf_counter() - started
            return client.chat.completions.create(**kwargs), time.perf_counter() - started
        except TaskInterrupted:
            raise
        except KeyboardInterrupt:
            raise TaskInterrupted()
        except Exception as exc:
            last_error = exc
            if attempt >= config.MODEL_MAX_RETRIES:
                break
            delay = config.MODEL_RETRY_BACKOFF * (2 ** attempt)
            console.print(
                f"[dim yellow]⚠ 模型请求失败，将在 {delay:.1f}s 后重试 "
                f"({attempt + 1}/{config.MODEL_MAX_RETRIES})：{exc}[/dim yellow]"
            )
            time.sleep(delay)
    raise RuntimeError(f"模型请求失败，已重试 {config.MODEL_MAX_RETRIES} 次：{last_error}") from last_error


# ══════════════════════════════════════════════
# 工作区启动向导
# ══════════════════════════════════════════════

def _select_workspace(cli_path: str | None) -> str:
    if cli_path:
        return _ws.set_workspace(cli_path)

    cwd = os.getcwd()
    console.print(Panel(
        f"[bold]请选择工作区（Agent 沙盒根目录）[/bold]\n\n"
        f"  当前目录: [cyan]{cwd}[/cyan]\n\n"
        f"  直接按 [bold]Enter[/bold] 使用当前目录\n"
        f"  输入路径指定其他目录（不存在会自动创建）",
        title="[bold cyan]🗂  工作区设置[/bold cyan]",
        border_style="cyan", width=70,
    ))
    user_input = Prompt.ask("[bold cyan]工作区路径[/bold cyan]", default=cwd).strip()
    path = _ws.set_workspace(user_input or cwd)
    console.print(f"[bold green]✓ 工作区已设置:[/bold green] [cyan]{path}[/cyan]\n")
    return path


# ══════════════════════════════════════════════
# 会话存档与恢复
# ══════════════════════════════════════════════

def _session_dir(workspace_path: str) -> Path:
    return Path(workspace_path) / config.SESSION_DIR


def _cleanup_old_sessions(workspace_path: str):
    """清理超过 SESSION_EXPIRE_DAYS 天的旧会话文件。"""
    d = _session_dir(workspace_path)
    if not d.exists():
        return
    now = time.time()
    for f in d.glob("session_*.json"):
        age = (now - f.stat().st_mtime) / 86400
        if age > config.SESSION_EXPIRE_DAYS:
            try:
                f.unlink()
            except OSError:
                pass


def _save_session(workspace_path: str, messages: list) -> str | None:
    """将当前 messages（不含 system）序列化保存，返回文件路径。"""
    d = _session_dir(workspace_path)
    d.mkdir(exist_ok=True)
    fname = f"session_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    fpath = d / fname
    # 过滤掉 system message，重启时重新生成
    payload = {
        "workspace": workspace_path,
        "model": config.MODEL,
        "saved_at": datetime.now().isoformat(),
        "messages": [m for m in messages if m.get("role") != "system"],
    }
    try:
        fpath.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return str(fpath)
    except Exception as e:
        console.print(f"[dim yellow]⚠  会话保存失败: {e}[/dim yellow]")
        return None


def _offer_restore_session(workspace_path: str) -> list:
    """
    查找最近的会话文件，询问是否恢复。
    返回恢复的 messages（不含 system），或空列表。
    """
    _cleanup_old_sessions(workspace_path)
    d = _session_dir(workspace_path)
    if not d.exists():
        return []

    sessions = sorted(d.glob("session_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not sessions:
        return []

    latest = sessions[0]
    age_sec = time.time() - latest.stat().st_mtime
    age_str = (f"{int(age_sec // 3600)}h {int((age_sec % 3600) // 60)}m 前"
               if age_sec > 3600 else f"{int(age_sec // 60)}m 前"
               if age_sec > 60 else "刚刚")

    try:
        data = json.loads(latest.read_text(encoding="utf-8"))
        if data.get("workspace") != workspace_path:
            return []
        msgs = data.get("messages", [])
        if not msgs:
            return []

        console.print(
            f"\n[bold cyan]💾 发现上次会话[/bold cyan]（{age_str}，"
            f"共 {len(msgs)} 条消息，模型: [bold cyan]{data.get('model', '?')}[/bold cyan]）"
        )
        if Confirm.ask("是否恢复上次会话？", default=True):
            console.print("[dim green]✓ 会话已恢复[/dim green]\n")
            return msgs
    except Exception:
        pass
    return []


def _session_choices(workspace_path: str) -> list[dict]:
    """列出可恢复会话，供 /resume 选择。"""
    d = _session_dir(workspace_path)
    if not d.exists():
        return []
    choices = []
    for index, path in enumerate(
        sorted(d.glob("session_*.json"), key=lambda p: p.stat().st_mtime, reverse=True),
        start=1,
    ):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if data.get("workspace") != workspace_path:
            continue
        choices.append({
            "index": index,
            "path": path,
            "saved_at": data.get("saved_at", "?"),
            "messages": len(data.get("messages", [])),
            "model": data.get("model", "?"),
            "name": path.name,
        })
    return choices


def _load_session_messages(choice: dict) -> list[dict]:
    data = json.loads(Path(choice["path"]).read_text(encoding="utf-8"))
    return [m for m in data.get("messages", []) if m.get("role") != "system"]


# ══════════════════════════════════════════════
# 上下文 / 记忆管理
# ══════════════════════════════════════════════

def _msg_chars(msg: dict) -> int:
    content = msg.get("content") or ""
    tc_str  = "".join(json.dumps(tc) for tc in (msg.get("tool_calls") or []))
    return len(content) + len(tc_str)


def _total_chars(messages: list) -> int:
    return sum(_msg_chars(m) for m in messages)


def _summary_end_at_turn_boundary(messages: list, desired_end: int) -> int:
    """将摘要边界推进到完整 tool-result 块之后，避免留下孤立 tool 消息。"""
    end = min(max(1, desired_end), len(messages))
    while end < len(messages) and messages[end].get("role") == "tool":
        end += 1
    return end


def _oldest_turn_end(messages: list) -> int | None:
    """返回最旧用户轮次的结束位置；不拆分 assistant/tool 消息关系。"""
    for index in range(2, len(messages)):
        if messages[index].get("role") == "user":
            return index
    return None


def _over_hard_budget(messages: list) -> bool:
    """token 预算或字符硬上限任一超限，都需要兜底。"""
    return memory.over_hard_budget(messages) or _total_chars(messages) > config.MAX_CHARS


def _trim_messages_hard(messages: list) -> list:
    """硬删除兜底：按完整用户轮次删除，绝不留下孤立 tool result。"""
    while _over_hard_budget(messages):
        end = _oldest_turn_end(messages)
        if end is None:
            # 当前只剩一个轮次时不能安全删除，保留协议完整性并交给模型服务最终判定。
            break
        del messages[1:end]
    return messages


def _compress_with_summary(messages: list) -> list:
    """调用 LLM 将最旧一批消息浓缩为摘要，替换为单条历史摘要消息。失败时原样返回。"""
    start_idx = 1
    end_idx = _summary_end_at_turn_boundary(
        messages, max(1, len(messages) - config.KEEP_RECENT)
    )
    # 至少保留一个最新轮次；若边界只能推进到末尾，交由硬删除按完整轮次处理。
    if end_idx >= len(messages):
        return messages
    if end_idx <= start_idx:
        return messages

    to_compress = messages[start_idx:end_idx]
    if not to_compress:
        return messages

    lines = []
    for m in to_compress:
        role, content = m.get("role", ""), (m.get("content") or "")[:300]
        tcs = m.get("tool_calls") or []
        if role == "user":
            lines.append(f"[用户] {content}")
        elif role == "assistant":
            if tcs:
                for tc in tcs:
                    fn = tc.get("function", {})
                    lines.append(f"[工具调用] {fn.get('name')}  {fn.get('arguments','')[:120]}")
            elif content:
                lines.append(f"[智能体] {content}")
        elif role == "tool":
            lines.append(f"[工具结果] {content}")

    old_chars = sum(_msg_chars(m) for m in to_compress)
    try:
        console.print("[dim cyan]🗜  上下文接近上限，正在 LLM 压缩历史记忆...[/dim cyan]")
        resp, _ = _create_completion(
            model=config.MODEL,
            messages=[
                {"role": "system", "content": (
                    "你是对话历史压缩助手。把以下 Agent 操作历史写成自包含摘要（400 字以内），"
                    "必须包含五节：## 任务目标、## 已完成、## 关键文件/命令、"
                    "## 未完成/下一步、## 约束。保留具体路径、命令、测试结果和未决问题，"
                    "不要只写“完成了任务”。直接输出摘要，不要多余说明。"
                )},
                {"role": "user", "content": "\n".join(lines)}
            ],
            temperature=0.1,
            max_tokens=800,
        )
        summary = (resp.choices[0].message.content or "").strip()
        if summary:
            messages[start_idx:end_idx] = [{"role": "user", "content": f"[历史操作摘要]\n{summary}"}]
            console.print(f"[dim cyan]✓ 记忆压缩：{old_chars} 字符 → {len(summary)} 字符"
                          f"  |  当前: {_total_chars(messages)}/{config.MAX_CHARS}[/dim cyan]")
            return messages
    except Exception as e:
        console.print(f"[dim yellow]⚠  摘要失败 ({e})，回退到硬删除。[/dim yellow]")
    return messages


def manage_context(messages: list) -> list:
    """
    自动兜底只在 85% token 预算或字符硬上限时触发。
    65%–85% 区间留给模型先用 context_status / compress_context 自主压缩；
    若模型不处理，下一次仍会由这里兜底。
    """
    if not _over_hard_budget(messages):
        return messages
    total = _total_chars(messages)
    console.print(
        f"\n[dim yellow]⚠️  上下文达到硬上限（{total} 字符，"
        f"约 {memory.calibrated_messages_tokens(messages):,} tokens），"
        "触发自动记忆管理...[/dim yellow]"
    )
    messages = _compress_with_summary(messages)
    if _over_hard_budget(messages):
        console.print("[dim yellow]⚠️  摘要后仍超限，执行硬删除...[/dim yellow]")
        messages = _trim_messages_hard(messages)
    return messages


# ══════════════════════════════════════════════
# 工具执行（含耗时统计 + 并行）
# ══════════════════════════════════════════════

def _execute_tool(name: str, args: dict) -> tuple[str, float]:
    """路由并执行工具，返回 (result_str, elapsed_seconds)。"""
    fn = TOOL_REGISTRY.get(name)
    if fn is None:
        return f"Error: 未知工具 '{name}'，请检查工具名拼写。", 0.0
    t0 = time.perf_counter()
    try:
        result = fn(**args)
    except TypeError as e:
        result = f"Error: 工具 '{name}' 参数错误 - {e}"
    except Exception as e:
        result = f"Error: 工具 '{name}' 执行崩溃 - {e}"
    return result, time.perf_counter() - t0


class _TaskStats:
    """记录单次任务的工具调用统计。"""
    def __init__(self):
        self.steps       = 0
        self.error_count = 0
        self.calls: dict[str, list[float]] = {}  # name -> [elapsed, ...]
        self.model_times: list[float] = []
        self.prompt_tokens = 0
        self.completion_tokens = 0

    def record_model(self, response, elapsed: float):
        self.model_times.append(elapsed)
        usage = getattr(response, "usage", None)
        prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
        completion_tokens = getattr(usage, "completion_tokens", 0) or 0
        self.prompt_tokens += prompt_tokens
        self.completion_tokens += completion_tokens
        # 同步刷新显示层的“最近实测”读数；零值/缺失不覆盖上次有效测量。
        memory.record_real_usage(prompt_tokens, completion_tokens)

    def record(self, name: str, elapsed: float, is_error: bool):
        self.calls.setdefault(name, []).append(elapsed)
        if is_error:
            self.error_count += 1

    def print_summary(self, task_elapsed: float):
        total_calls = sum(len(v) for v in self.calls.values())
        if total_calls == 0 and not self.model_times:
            return
        console.print()
        t = Table(title="📊 任务统计", show_header=True, header_style="bold cyan",
                  border_style="dim", show_footer=False)
        t.add_column("工具",     style="cyan",  min_width=18)
        t.add_column("调用次数", style="green", justify="right")
        t.add_column("平均耗时", style="yellow",justify="right")
        t.add_column("总耗时",   style="dim",   justify="right")
        if total_calls:
            for name, times in sorted(self.calls.items()):
                t.add_row(name, str(len(times)),
                          f"{sum(times)/len(times):.2f}s",
                          f"{sum(times):.2f}s")
            console.print(t)
        if self.model_times:
            token_note = ""
            if self.prompt_tokens or self.completion_tokens:
                token_note = (f"  |  prompt/completion tokens: "
                              f"{self.prompt_tokens}/{self.completion_tokens}")
            console.print(
                f"[dim]模型调用: {len(self.model_times)}  |  平均耗时: "
                f"{sum(self.model_times) / len(self.model_times):.2f}s  |  "
                f"总耗时: {sum(self.model_times):.2f}s{token_note}[/dim]"
            )
        console.print(
            f"[dim]步骤: {self.steps}  |  工具调用: {total_calls}  |  "
            f"错误次数: {self.error_count}  |  总耗时: {task_elapsed:.1f}s[/dim]\n"
        )


def _run_tools(tool_calls, stats: _TaskStats, consecutive_errors: list) -> list[dict]:
    """
    执行一轮中的所有工具调用，返回 tool result 消息列表。
    - 仅全部为无副作用工具时并行，且并发数有上限。
    - 任意写入、命令或未知工具会让整轮串行，保证执行顺序。
    """
    parsed_calls = []
    for tc in tool_calls:
        name = tc.function.name
        try:
            args = json.loads(tc.function.arguments)
            if not isinstance(args, dict):
                raise ValueError("参数根节点必须是对象")
            parse_error = None
        except (json.JSONDecodeError, ValueError) as exc:
            args = {}
            parse_error = f"Error: 工具 '{name}' 的参数不是合法 JSON 对象 - {exc}"
        parsed_calls.append((tc, name, args, parse_error))

    needs_sequential = any(name not in _PARALLEL_SAFE_TOOLS for _, name, _, _ in parsed_calls)

    results_ordered: dict[str, tuple[str, float]] = {}

    if needs_sequential or len(tool_calls) == 1:
        # 串行执行
        for tc, name, args, parse_error in parsed_calls:
            _print_tool_call(name, args)
            observation, elapsed = (parse_error, 0.0) if parse_error else _execute_tool(name, args)
            results_ordered[tc.id] = (observation, elapsed)
    else:
        workers = min(len(parsed_calls), max(1, config.MAX_PARALLEL_READ_TOOLS))
        console.print(f"[dim cyan]⚡ 并行执行 {len(tool_calls)} 个只读工具（最多 {workers} 个）...[/dim cyan]")
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {}
            for tc, name, args, parse_error in parsed_calls:
                _print_tool_call(name, args)
                if parse_error:
                    results_ordered[tc.id] = (parse_error, 0.0)
                else:
                    futures[executor.submit(_execute_tool, name, args)] = tc
            for future in as_completed(futures):
                tc = futures[future]
                observation, elapsed = future.result()
                results_ordered[tc.id] = (observation, elapsed)

    # 按原顺序生成 tool result 消息
    tool_msgs = []
    for tc in tool_calls:
        observation, elapsed = results_ordered[tc.id]
        # Error: 计入连续错误并追加重试提示；
        # VerificationFailed: 也计入错误，但提示是“修复失败项”，不是“检查参数”；
        # ConfigurationRequired: / Refused: 是确定性结果，重试不会改变结果。
        is_error = observation.startswith(("Error:", "VerificationFailed:"))
        stats.record(tc.function.name, elapsed, is_error)

        if is_error:
            consecutive_errors[0] += 1
            if observation.startswith("VerificationFailed:"):
                observation += (
                    "\n\n[验证提示] 请修复上面的失败项，然后重新调用 finish_check。"
                )
            else:
                observation += (
                    f"\n\n[重试提示] 这是第 {consecutive_errors[0]} 次连续出错，"
                    "请仔细检查参数后重试。"
                )
        else:
            consecutive_errors[0] = 0

        cli.print_tool_result(
            tc.function.name, observation, elapsed, is_error, console_obj=console
        )

        tool_msgs.append({
            "role": "tool",
            "tool_call_id": tc.id,
            "content": observation,
        })
    return tool_msgs


def _print_tool_call(name: str, args: dict):
    cli.print_tool_call(name, args, console_obj=console)


# ══════════════════════════════════════════════
# 主交互会话
# ══════════════════════════════════════════════

def _handle_slash_command(
    name: str,
    _arg: str,
    messages: list[dict],
    base_system_prompt: str,
    workspace_path: str,
    mode_box: list[str] | None = None,
) -> tuple[list[dict], bool]:
    """处理本地命令，返回 (messages, should_exit)。命令不会发给模型。
    mode_box 为单元素列表 [当前模式]；/mode 通过它切换，None 时保持不变。"""
    if name in ("exit", "quit"):
        return messages, True

    if name == "help":
        cli.print_help()
        return messages, False

    if name == "config":
        config.print_config()
        return messages, False

    if name == "mode":
        current = mode_box[0] if mode_box else "ask"
        target = cli.normalize_mode(_arg)
        if not _arg:
            cli.print_mode_help(current, console_obj=console)
            return messages, False
        if target is None:
            console.print(
                f"[yellow]未知模式: {_arg}[/yellow]  可用值: "
                "[cyan]ask[/cyan] | [cyan]workspace[/cyan] | [cyan]full[/cyan]"
            )
            return messages, False
        if target == current:
            console.print(f"[dim]当前已经是 {cli.mode_label(current)}。[/dim]")
            return messages, False
        if target == "full" and not Confirm.ask(
            "完全模式将跳过所有审批（含命令/提交/点击），确认切换？", default=False
        ):
            console.print("[dim]已保持原模式。[/dim]")
            return messages, False
        if mode_box:
            mode_box[0] = target
            _tools.set_permission_mode(target)
        console.print(
            f"[dim green]✓ 权限模式已切换: [bold {cli.mode_color(target)}]"
            f"{cli.mode_label(target)}[/bold {cli.mode_color(target)}][/dim green]\n"
        )
        return messages, False

    if name == "status":
        cli.print_status(messages, workspace_path, mode=(mode_box[0] if mode_box else "ask"))
        return messages, False

    if name == "context":
        cli.print_context_panel(messages, console_obj=console)
        return messages, False

    if name == "undo":
        code, status = _tools._git_run(["status", "--porcelain"])
        if code == 0 and status:
            preview_lines = status.splitlines()[:15]
            console.print("[bold yellow]以下改动将被丢弃（reset --hard）:[/bold yellow]")
            for line in preview_lines:
                console.print(f"  [dim]{line}[/dim]")
            if not Confirm.ask("确认回滚到最近一次任务前快照？", default=False):
                console.print("[dim]已取消回滚。[/dim]")
                return messages, False
        console.print(_tools.undo_last_snapshot())
        return messages, False

    if name == "clear":
        messages = [{"role": "system", "content": base_system_prompt}]
        memory.reset_real_usage()
        console.print("[dim green]✓ 已清空当前对话[/dim green]")
        return messages, False

    if name == "new":
        if any(m.get("role") != "system" for m in messages):
            _save_session(workspace_path, messages)
        messages = [{"role": "system", "content": base_system_prompt}]
        memory.reset_real_usage()
        console.print("[dim green]✓ 当前会话已保存，开始新会话[/dim green]")
        return messages, False

    if name == "resume":
        choices = _session_choices(workspace_path)
        if not choices:
            console.print("[dim yellow]没有可恢复的会话。[/dim yellow]")
            return messages, False
        cli.print_sessions(choices)
        raw = Prompt.ask("选择会话编号（回车取消）", default="").strip()
        if not raw:
            return messages, False
        try:
            index = int(raw)
        except ValueError:
            console.print("[yellow]请输入数字编号。[/yellow]")
            return messages, False
        if not 1 <= index <= len(choices):
            console.print("[yellow]编号超出范围。[/yellow]")
            return messages, False
        if any(m.get("role") != "system" for m in messages):
            _save_session(workspace_path, messages)
        restored = _load_session_messages(choices[index - 1])
        messages = [{"role": "system", "content": base_system_prompt}] + restored
        # 旧会话的 usage 与当前恢复后的上下文不对应，实测读数作废。
        memory.reset_real_usage()
        console.print(f"[dim green]✓ 已恢复 {len(restored)} 条消息[/dim green]")
        return messages, False

    if name == "compact":
        before = len(messages)
        messages = _compress_with_summary(messages)
        if len(messages) == before:
            console.print("[dim]当前没有可压缩的旧轮次。[/dim]")
        else:
            console.print("[dim green]✓ 已压缩最旧的历史[/dim green]")
        return messages, False

    cli.print_unknown_command(name)
    return messages, False


def start_interactive_session(workspace_path: str):
    # 权限模式：HQIN_MODE 设初始值；/mode 运行中随时切换。
    mode_box = [cli.normalize_mode(config.MODE) or "ask"]
    _tools.set_permission_mode(mode_box[0])
    cli.print_banner(workspace_path, console_obj=console, mode=mode_box[0])
    _tools.set_edit_preview_hook(cli.confirm_edit)

    base_system_prompt = SYSTEM_PROMPT.format(
        os_name=f"{platform.system()} {platform.version()}",
        workspace=workspace_path,
    ) + "\n\n" + browser_policy.prompt_block()

    # 尝试恢复上次会话
    restored = _offer_restore_session(workspace_path)
    messages = [{"role": "system", "content": base_system_prompt}] + restored
    memory.set_active_messages(messages)

    session_saved = False
    try:
        while True:
            try:
                user_input = cli.ask_user(messages, workspace_path, mode=mode_box[0])
            except EOFError:
                break
            if not user_input:
                continue

            command = cli.parse_command(user_input)
            if command is not None:
                messages, should_exit = _handle_slash_command(
                    command[0], command[1], messages, base_system_prompt,
                    workspace_path, mode_box,
                )
                memory.set_active_messages(messages)
                memory.apply_notice(messages, base_system_prompt)
                if should_exit:
                    break
                continue

            # 兼容旧的不带斜杠命令。
            if user_input.lower() in ("exit", "quit"):
                break
            if user_input.lower() == "config":
                config.print_config()
                continue

            messages.append({"role": "user", "content": user_input})
            console.print("\n[bold magenta]🚀 任务启动~[/bold magenta]\n")

            # 任务前自动 git 快照：改坏可 /undo 一键回滚。
            # 非仓库/干净/失败均静默跳过，绝不阻塞任务。
            _snapshot_note = _tools.task_snapshot()
            if _snapshot_note:
                console.print(f"[dim]快照: {_snapshot_note}[/dim]")

            stats = _TaskStats()
            consecutive_errors = [0]   # 用列表让内层函数可修改
            task_done   = False
            task_start  = time.perf_counter()
            step_budget = config.MAX_STEPS   # 预算制：耗尽时询问续期，而非硬终止

            while True:
                stats.steps += 1
                step_budget -= 1
                console.print(f"[dim]>> Agent Step {stats.steps}（剩余预算 {max(step_budget, 0)}）...[/dim]")
                # 执行期间提示框不可见，这里同步打印一行最新上下文读数；
                # 模式徽标显示在上下文左侧，随时可见当前审批档位。
                ctx_text, ctx_style = cli.context_token_text(messages)
                ctx_color = {
                    "ctx-ok": "cyan", "ctx-warn": "yellow", "ctx-danger": "red",
                }[ctx_style]
                console.print(
                    f"   [bold {cli.mode_color(mode_box[0])}]"
                    f"{cli.mode_label(mode_box[0])}[/bold {cli.mode_color(mode_box[0])}]"
                    f"  [{ctx_color}]{ctx_text}[/]"
                )

                # 错误重试熔断
                if consecutive_errors[0] >= config.MAX_CONSECUTIVE_ERRORS:
                    console.print(
                        f"[bold red]⚠  连续 {consecutive_errors[0]} 次工具出错，任务暂停。[/bold red]"
                    )
                    # 阻塞询问前先弹桌面通知，把"切窗回来确认"变成可选动作。
                    notifier.notify(
                        "任务已暂停",
                        f"连续 {consecutive_errors[0]} 次工具失败",
                    )
                    if not Confirm.ask("是否继续尝试？", default=False):
                        break
                    consecutive_errors[0] = 0

                # 两级上下文管理：模型主动压缩优先，85% 后由代码自动兜底。
                messages = manage_context(messages)
                memory.set_active_messages(messages)
                memory.apply_notice(messages, base_system_prompt)

                # 未配置 HQIN_BROWSER_ID 时，交互浏览工具不会出现在模型可见的工具列表中。
                tool_schemas = browser_policy.filter_tool_schemas(TOOL_SCHEMAS)

                # 预算耗尽：不再硬终止，向用户请求续期。模型完成任务自然
                # 停止（无工具调用）才是主退出路径；预算只是防失控护栏。
                if step_budget < 1:
                    notifier.notify(
                        "步数预算已用完",
                        f"{user_input[:100]} — 已执行 {stats.steps} 步，是否续期？",
                    )
                    if Confirm.ask(
                        f"步数预算已用完（已执行 {stats.steps} 步）。"
                        f"模型仍在工作中，是否追加 {config.STEP_RENEWAL} 步继续？",
                        default=True,
                    ):
                        step_budget += config.STEP_RENEWAL
                        console.print(f"[dim cyan]✓ 已追加 {config.STEP_RENEWAL} 步预算。[/dim cyan]")
                    else:
                        console.print("[dim]本任务到此为止。[/dim]")
                        break

                # 调用模型（流式：生成过程可见；Ctrl+C → TaskInterrupted 丢弃本轮）
                try:
                    response, model_elapsed = _create_completion(
                        model=config.MODEL,
                        messages=messages,
                        tools=tool_schemas,
                        tool_choice="auto",
                        temperature=0.1,
                        max_tokens=config.MAX_OUTPUT_TOKENS,
                        stream=True,
                    )
                    stats.record_model(response, model_elapsed)
                except TaskInterrupted:
                    console.print(
                        "\n[yellow]✋ 已中断当前任务（未执行任何半截内容），回到输入框。"
                        "按 Ctrl+C/Ctrl+D 于输入行可退出会话。[/yellow]\n"
                    )
                    break
                except RuntimeError as e:
                    console.print(f"[bold red]❌ {e}[/bold red]")
                    break
                msg           = response.choices[0].message
                finish_reason = response.choices[0].finish_reason

                # 输出被 max_tokens 截断时，工具参数往往是不完整 JSON。
                # 不要把它当工具调用执行；明确要求模型拆小，避免“半截文件”落盘。
                if finish_reason == "length":
                    consecutive_errors[0] += 1
                    console.print(
                        "[bold yellow]⚠ 模型输出达到 max_tokens，已被截断。"
                        "本轮不会执行不完整的工具参数。[/bold yellow]"
                    )
                    messages.append({
                        "role": "assistant",
                        "content": msg.content or "（模型输出因长度上限被截断）",
                    })
                    messages.append({
                        "role": "user",
                        "content": (
                            "[系统] 上一条输出因长度上限被截断。不要重复整段内容；"
                            "把任务拆成更小的步骤。写文件时先用 write_file 写较短骨架，"
                            "再用 append_file 或 edit_file 分块补全。"
                        ),
                    })
                    continue

                # 模型直接回复（任务完成）
                if not msg.tool_calls:
                    reply = msg.content or ""
                    console.print(f"\n[bold green]🤖 智能体:[/bold green]\n{reply}\n")
                    messages.append({"role": "assistant", "content": reply})
                    task_done = True
                    break

                # 模型请求工具调用
                messages.append({
                    "role": "assistant",
                    "content": msg.content,
                    "tool_calls": [
                        {"id": tc.id, "type": "function",
                         "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                        for tc in msg.tool_calls
                    ]
                })

                # 执行工具（含并行 + 耗时 + 错误计数）；Ctrl+C 同样只中断本任务
                try:
                    tool_msgs = _run_tools(msg.tool_calls, stats, consecutive_errors)
                except TaskInterrupted:
                    console.print("\n[yellow]✋ 工具执行被中断，回到输入框。[/yellow]\n")
                    break
                except KeyboardInterrupt:
                    console.print("\n[yellow]✋ 工具执行被中断，回到输入框。[/yellow]\n")
                    break
                messages.extend(tool_msgs)

            task_elapsed = time.perf_counter() - task_start
            stats.print_summary(task_elapsed)

            # 任务结局桌面通知：正常完成 / 用户拒绝续期 / 熔断终止。
            outcome = (
                "任务完成" if task_done
                else f"任务中止（已执行 {stats.steps} 步）"
            )
            notifier.notify(
                outcome,
                f"{user_input[:100]} — {stats.steps} 步，{stats.error_count} 次错误",
            )

            # 每个任务结束都回收后台浏览器，避免会话跨任务残留。
            closed = _tools.close_browser_session_if_active()
            if closed:
                console.print(f"[dim cyan]🌐 {closed}[/dim cyan]")

            if not task_done:
                console.print("[bold red]🛑 任务未完成（预算耗尽未续期或被熔断），请重新描述或拆分任务。[/bold red]")

    except KeyboardInterrupt:
        console.print("\n[bold green]👋 收到中断，正在退出...[/bold green]")
    except Exception as e:
        console.print(f"\n[bold red]❌ 意外错误: {e}[/bold red]")
        console.print(f"[dim]{traceback.format_exc()}[/dim]")
    finally:
        _tools.set_edit_preview_hook(None)
        # 异常或退出时兜底关闭后台浏览器会话。
        closed = _tools.close_browser_session_if_active()
        if closed:
            console.print(f"[dim cyan]🌐 {closed}[/dim cyan]")

        # 退出时自动保存会话
        non_system = [m for m in messages if m.get("role") != "system"]
        if non_system:
            path = _save_session(workspace_path, messages)
            if path:
                rel = os.path.relpath(path, workspace_path)
                console.print(f"[dim green]💾 会话已保存: {rel}[/dim green]")


# ══════════════════════════════════════════════
# 入口
# ══════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="HQin Harness — 本地编程智能体",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  python main.py                          # 交互式选择工作区\n"
            "  python main.py --workspace D:/my_proj  # 直接指定工作区\n"
            "  python main.py -w .                     # 使用当前目录\n"
        )
    )
    parser.add_argument("-w", "--workspace", metavar="PATH",
                        help="工作区（沙盒根目录）路径，省略则交互式选择",
                        default=None)
    args = parser.parse_args()
    workspace_path = _select_workspace(args.workspace)
    start_interactive_session(workspace_path)


if __name__ == "__main__":
    main()
