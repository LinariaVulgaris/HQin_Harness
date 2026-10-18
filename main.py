# main.py  —  Route B: 原生 OpenAI Tool Calling
import os
import sys
import json
import time
import platform
import traceback
import argparse
from pathlib import Path
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from openai import OpenAI
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.prompt import Prompt, Confirm

import config
import workspace as _ws
from prompts import SYSTEM_PROMPT
from tool_schemas import TOOL_SCHEMAS
from tools import TOOL_REGISTRY

console = Console()

client = OpenAI(
    base_url=config.BASE_URL,
    api_key=config.API_KEY,
    timeout=config.MODEL_TIMEOUT_SECONDS,
    max_retries=0,  # 由 _create_completion 统一记录并控制重试
)

# 只有没有副作用的工具可并行；写文件、运行命令和提交必须保持调用顺序。
_PARALLEL_SAFE_TOOLS = {
    "list_dir", "read_file", "search_in_files", "git_status", "git_diff", "web_search",
}


def _create_completion(**kwargs):
    """调用本地模型并对短暂服务错误做有限重试，返回 (response, elapsed)。"""
    last_error = None
    started = time.perf_counter()
    for attempt in range(config.MODEL_MAX_RETRIES + 1):
        try:
            return client.chat.completions.create(**kwargs), time.perf_counter() - started
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
        path = _ws.set_workspace(cli_path)
        console.print(f"[dim]工作区（命令行参数）: [cyan]{path}[/cyan][/dim]")
        return path

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
            f"共 {len(msgs)} 条消息，模型: {data.get('model', '?')}）"
        )
        if Confirm.ask("是否恢复上次会话？", default=True):
            console.print("[dim green]✓ 会话已恢复[/dim green]\n")
            return msgs
    except Exception:
        pass
    return []


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


def _trim_messages_hard(messages: list) -> list:
    """硬删除兜底：按完整用户轮次删除，绝不留下孤立 tool result。"""
    while _total_chars(messages) > config.MAX_CHARS:
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
                    "你是对话历史压缩助手。将以下 Agent 操作历史浓缩为简洁摘要（150字以内），"
                    "重点记录：完成了哪些文件操作、执行了哪些命令、关键发现及任务进展。"
                    "直接输出摘要，不要多余说明。"
                )},
                {"role": "user", "content": "\n".join(lines)}
            ],
            temperature=0.1,
            max_tokens=512,
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
    """统一上下文管理：超过 SUMMARY_THRESH → 摘要，仍超 MAX_CHARS → 硬删除。"""
    total = _total_chars(messages)
    if total <= config.SUMMARY_THRESH:
        return messages
    console.print(f"\n[dim yellow]⚠️  上下文 {total}/{config.MAX_CHARS} 字符，触发记忆管理...[/dim yellow]")
    messages = _compress_with_summary(messages)
    if _total_chars(messages) > config.MAX_CHARS:
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
        self.prompt_tokens += getattr(usage, "prompt_tokens", 0) or 0
        self.completion_tokens += getattr(usage, "completion_tokens", 0) or 0

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
        is_error = observation.startswith("Error:")
        stats.record(tc.function.name, elapsed, is_error)

        if is_error:
            consecutive_errors[0] += 1
            observation += f"\n\n[重试提示] 这是第 {consecutive_errors[0]} 次连续出错，请仔细检查参数后重试。"
        else:
            consecutive_errors[0] = 0

        console.print(
            f"[bold blue]⚙️  {tc.function.name}[/bold blue]"
            f"[dim] 耗时 {elapsed:.2f}s[/dim]\n{observation}\n"
        )
        console.print("─" * 50)

        tool_msgs.append({
            "role": "tool",
            "tool_call_id": tc.id,
            "content": observation,
        })
    return tool_msgs


def _print_tool_call(name: str, args: dict):
    console.print(
        f"\n[bold blue]🔧 调用工具:[/bold blue] [cyan]{name}[/cyan]  "
        f"参数: {json.dumps(args, ensure_ascii=False)}"
    )


# ══════════════════════════════════════════════
# 主交互会话
# ══════════════════════════════════════════════

def start_interactive_session(workspace_path: str):
    console.print("[bold cyan]==========================================[/bold cyan]")
    console.print("[bold cyan]               HQin Harness               [/bold cyan]")
    console.print("[bold cyan]==========================================[/bold cyan]")
    console.print(f"[dim]工作区: [cyan]{workspace_path}[/cyan]  |  模型: [cyan]{config.MODEL}[/cyan][/dim]")
    console.print("[dim]输入 'exit'/'quit' 退出，'config' 查看配置。[/dim]\n")

    system_prompt = SYSTEM_PROMPT.format(
        os_name=f"{platform.system()} {platform.version()}",
        workspace=workspace_path,
    )

    # 尝试恢复上次会话
    restored = _offer_restore_session(workspace_path)
    messages = [{"role": "system", "content": system_prompt}] + restored

    session_saved = False
    try:
        while True:
            user_input = input("\n[HQin] 请输入你的需求:\n> ").strip()
            if not user_input:
                continue
            if user_input.lower() in ("exit", "quit"):
                break
            if user_input.lower() == "config":
                config.print_config()
                continue

            messages.append({"role": "user", "content": user_input})
            console.print(f"\n[bold magenta]🚀 任务启动:[/bold magenta] {user_input}\n")

            stats = _TaskStats()
            consecutive_errors = [0]   # 用列表让内层函数可修改
            task_done   = False
            task_start  = time.perf_counter()

            for step in range(config.MAX_STEPS):
                stats.steps += 1
                console.print(f"[dim]>> Agent Step {step + 1}/{config.MAX_STEPS}...[/dim]")

                # 错误重试熔断
                if consecutive_errors[0] >= config.MAX_CONSECUTIVE_ERRORS:
                    console.print(
                        f"[bold red]⚠  连续 {consecutive_errors[0]} 次工具出错，任务暂停。[/bold red]"
                    )
                    if not Confirm.ask("是否继续尝试？", default=False):
                        break
                    consecutive_errors[0] = 0

                # 两级上下文管理
                messages = manage_context(messages)

                # 调用模型
                try:
                    response, model_elapsed = _create_completion(
                        model=config.MODEL,
                        messages=messages,
                        tools=TOOL_SCHEMAS,
                        tool_choice="auto",
                        temperature=0.1,
                        max_tokens=config.MAX_OUTPUT_TOKENS,
                    )
                    stats.record_model(response, model_elapsed)
                except RuntimeError as e:
                    console.print(f"[bold red]❌ {e}[/bold red]")
                    break
                msg           = response.choices[0].message
                finish_reason = response.choices[0].finish_reason

                # 模型直接回复（任务完成）
                if finish_reason == "stop" or not msg.tool_calls:
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

                # 执行工具（含并行 + 耗时 + 错误计数）
                tool_msgs = _run_tools(msg.tool_calls, stats, consecutive_errors)
                messages.extend(tool_msgs)

            task_elapsed = time.perf_counter() - task_start
            stats.print_summary(task_elapsed)

            if not task_done:
                console.print("[bold red]🛑 已达到最大步数或被熔断，请重新描述或拆分任务。[/bold red]")

    except KeyboardInterrupt:
        console.print("\n[bold green]👋 收到中断，正在退出...[/bold green]")
    except Exception as e:
        console.print(f"\n[bold red]❌ 意外错误: {e}[/bold red]")
        console.print(f"[dim]{traceback.format_exc()}[/dim]")
    finally:
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
