# cli_ui.py — Claude Code 风格的终端交互层
"""
职责边界：
- 只负责输入、补全、状态行、工具时间线和本地命令的展示；
- 不调用模型、不修改 Agent Loop、不决定任务是否完成；
- 没有 TTY 或 prompt_toolkit 不可用时回退到普通 input()。
"""
from __future__ import annotations

import builtins
import json
import os
import sys
from pathlib import Path
from typing import Iterable

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

import config
import console_setup
import memory
import notifier

try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.completion import Completer, Completion
    from prompt_toolkit.history import FileHistory, InMemoryHistory
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.styles import Style

    # bottom-toolbar 默认带 reverse（反显），会把前景色翻成背景色，
    # 出现“左绿右白”的割裂效果；这里显式覆盖成统一浅蓝底 + 深灰字。
    _TOOLBAR_STYLE = Style.from_dict({
        "bottom-toolbar": "bg:#e3f2fd #37474f noreverse",
        "ctx-ok": "#1b5e20",
        "ctx-warn": "#e65100",
        "ctx-danger": "#b71c1c",
    })
    _HAS_PROMPT_TOOLKIT = True
except Exception:  # pragma: no cover - 只在依赖缺失时走回退路径
    PromptSession = None  # type: ignore[assignment]
    Completer = object  # type: ignore[assignment,misc]
    Completion = None  # type: ignore[assignment]
    FileHistory = None  # type: ignore[assignment]
    InMemoryHistory = None  # type: ignore[assignment]
    KeyBindings = None  # type: ignore[assignment]
    Style = None  # type: ignore[assignment]
    _TOOLBAR_STYLE = None
    _HAS_PROMPT_TOOLKIT = False

console_setup.configure_utf8_console()
console = Console()
_ORIGINAL_INPUT = builtins.input

SLASH_COMMANDS: dict[str, str] = {
    "/help": "显示命令与快捷键",
    "/status": "查看会话、上下文和归档状态",
    "/context": "上下文占用明细：估算、实测、角色分解",
    "/mode": "查看或切换权限模式（ask / workspace / full）",
    "/undo": "回滚到最近一次任务前快照（需工作区是 git 仓库）",
    "/clear": "清空当前对话，保留 system prompt",
    "/new": "保存当前会话并开始空白对话",
    "/resume": "列出并恢复历史会话",
    "/compact": "压缩最旧的对话历史",
    "/config": "查看当前配置",
    "/exit": "保存并退出",
}

MODES = ("ask", "workspace", "full")
MODE_DESCRIPTIONS = {
    "ask": "每次写入/命令/提交/点击都需人工确认（默认，最安全）",
    "workspace": "工作区内文件写入免审批；命令、提交、点击仍需确认",
    "full": "全部动作免审批（含 shell/git/点击）；沙盒路径校验仍生效",
}
_MODE_COLORS = {"ask": "yellow", "workspace": "cyan", "full": "red"}


def normalize_mode(mode: str) -> str | None:
    """把用户输入规范化为合法模式名；非法返回 None。"""
    normalized = (mode or "").strip().lower()
    return normalized if normalized in MODES else None


def mode_label(mode: str) -> str:
    return f"mode:{mode}"


def mode_color(mode: str) -> str:
    return _MODE_COLORS.get((mode or "").strip().lower(), "yellow")


def print_mode_help(current: str, console_obj: Console | None = None) -> None:
    """打印当前模式与三档语义表。"""
    target = console_obj or console
    table = Table(title=f"权限模式（当前: {current}）", border_style="dim", show_header=True)
    table.add_column("模式", style="cyan", min_width=12)
    table.add_column("语义", style="white")
    for name in MODES:
        mark = " ← 当前" if name == current else ""
        table.add_row(name + mark, MODE_DESCRIPTIONS[name])
    target.print(table)
    target.print("[dim]切换: /mode ask | /mode workspace | /mode full[/dim]\n")


def parse_command(text: str) -> tuple[str, str] | None:
    """把 '/name args' 解析为 (name, args)；普通输入返回 None。"""
    text = (text or "").strip()
    if not text.startswith("/"):
        return None
    name, _, args = text[1:].partition(" ")
    return name.lower(), args.strip()


def read_git_branch(workspace_path: str) -> str:
    """不启动子进程，直接读 .git/HEAD；失败时返回空字符串。"""
    root = Path(workspace_path)
    git_path = root / ".git"
    try:
        if git_path.is_file():
            text = git_path.read_text(encoding="utf-8", errors="replace").strip()
            if text.lower().startswith("gitdir:"):
                target = Path(text.split(":", 1)[1].strip())
                git_path = target if target.is_absolute() else root / target
        if not git_path.is_dir():
            return ""
        head = (git_path / "HEAD").read_text(encoding="utf-8", errors="replace").strip()
        if head.startswith("ref:"):
            ref = head.split(":", 1)[1].strip()
            return ref.removeprefix("refs/heads/")
        return head[:8]
    except OSError:
        return ""


def usage_bar(ratio: float, width: int = 10) -> str:
    """把百分比渲染成进度条；超界钳制，越过软/硬阈值追加标记。"""
    ratio = min(max(ratio, 0.0), 100.0)
    filled = round(ratio / 100 * width)
    bar = "▓" * filled + "░" * (width - filled)
    if ratio >= config.CONTEXT_HARD_RATIO * 100:
        return f"{bar}‼"
    if ratio >= config.CONTEXT_SOFT_RATIO * 100:
        return f"{bar}⚠"
    return bar


def context_token_text(messages: list[dict]) -> tuple[str, str]:
    """
    状态行和工具栏共用的上下文片段，保证两处口径一致。
    返回 (文本, 颜色类名)；类名仅在 prompt_toolkit 工具栏生效，
    纯文本路径忽略它。`~` 标校准估算值，`⚡` 标最近一次主请求的实测值。
    """
    tokens = memory.calibrated_messages_tokens(messages)
    budget = max(1, config.MODEL_CONTEXT_TOKENS)
    ratio = tokens / budget * 100
    text = f"ctx:{ratio:.0f}% {usage_bar(ratio)} ~{tokens:,} tok"
    real = memory.get_real_usage()
    if real:
        text += f" · ⚡{real['prompt_tokens']:,} 实测"
    if ratio >= config.CONTEXT_HARD_RATIO * 100:
        return text, "ctx-danger"
    if ratio >= config.CONTEXT_SOFT_RATIO * 100:
        return text, "ctx-warn"
    return text, "ctx-ok"


def build_status_line(messages: list[dict], workspace_path: str, mode: str = "ask") -> str:
    """底部状态行；token 用 memory 的同一套估算，避免两处口径不一致。"""
    workspace = Path(workspace_path).name or workspace_path
    branch = read_git_branch(workspace_path) or "-"
    ctx_text, _style = context_token_text(messages)
    return (
        f" {config.MODEL}  |  {workspace}  |  git:{branch}  |  "
        f"{mode_label(mode)}  |  {ctx_text} "
    )


class SlashCompleter(Completer):  # type: ignore[misc]
    """只在输入以 '/' 开头且还没有参数时补全命令。"""

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if not text.startswith("/") or " " in text:
            return
        for name, description in SLASH_COMMANDS.items():
            if name.startswith(text):
                yield Completion(
                    name,
                    start_position=-len(text),
                    display=name,
                    display_meta=description,
                )


class HqinPrompt:
    """复用同一个 PromptSession，避免每次输入都重建历史/补全器。"""

    def __init__(self, workspace_path: str):
        self.workspace_path = workspace_path
        self.messages: list[dict] = []
        self.mode = "ask"
        self._branch = read_git_branch(workspace_path)

        history_path = Path(workspace_path) / config.CLI_HISTORY_FILE
        try:
            history = FileHistory(str(history_path))
        except Exception:
            history = InMemoryHistory()

        key_bindings = KeyBindings()

        @key_bindings.add("enter")
        def _submit(event):
            event.current_buffer.validate_and_handle()

        @key_bindings.add("escape", "enter")
        def _newline(event):
            event.current_buffer.insert_text("\n")

        self._session = PromptSession(
            history=history,
            completer=SlashCompleter(),
            complete_while_typing=True,
            bottom_toolbar=self._toolbar,
            key_bindings=key_bindings,
            style=_TOOLBAR_STYLE,
            multiline=True,
            prompt_continuation=lambda width, line_number, is_soft_wrap: ". ",
        )

    def _toolbar(self):
        ctx_text, ctx_style = context_token_text(self.messages)
        workspace = Path(self.workspace_path).name or self.workspace_path
        mode_cls = {"ask": "ctx-ok", "workspace": "ctx-warn", "full": "ctx-danger"}.get(
            self.mode, "ctx-ok"
        )
        return [
            ("class:ctx-ok", f" {config.MODEL}  |  {workspace}  |  git:{self._branch or '-'}  |  "),
            (f"class:{mode_cls}", f"{mode_label(self.mode)}"),
            ("class:ctx-ok", "  |  "),
            (f"class:{ctx_style}", f"{ctx_text}"),
        ]

    def update(self, messages: list[dict], mode: str = "ask") -> None:
        self.messages = messages
        self.mode = mode
        self._branch = read_git_branch(self.workspace_path)

    def ask(self) -> str:
        try:
            return self._session.prompt("\n[HQin] > ").strip()
        except KeyboardInterrupt:
            console.print("[dim]已取消当前输入；Ctrl+D 退出。[/dim]")
            return ""


_PROMPT: HqinPrompt | None = None


def ask_user(messages: list[dict], workspace_path: str, mode: str = "ask") -> str:
    """交互式读取输入；非 TTY 环境直接回退 input()。"""
    global _PROMPT
    try:
        tty = sys.stdin.isatty() and sys.stdout.isatty()
    except Exception:
        tty = False
    # 测试或宿主用 patch 替换了内置 input 时，尊重替换，不启动交互式终端。
    if builtins.input is not _ORIGINAL_INPUT:
        return builtins.input("\n[HQin] > ").strip()
    if not _HAS_PROMPT_TOOLKIT or not tty:
        return builtins.input("\n[HQin] > ").strip()

    if _PROMPT is None or _PROMPT.workspace_path != workspace_path:
        _PROMPT = HqinPrompt(workspace_path)
    _PROMPT.update(messages, mode)
    return _PROMPT.ask()


def print_banner(workspace_path: str, console_obj: Console | None = None,
                 mode: str = "ask") -> None:
    target = console_obj or console
    target.print(
        Panel.fit(
            f"[bold cyan]HQin Harness[/bold cyan]\n"
            f"工作区: [cyan]{workspace_path}[/cyan]\n"
            f"模型: [cyan]{config.MODEL}[/cyan]  |  权限: [{mode_color(mode)}]{mode_label(mode)}[/{mode_color(mode)}]",
            border_style="cyan",
        )
    )
    target.print(
        "[dim]Enter 发送 · Esc+Enter 换行 · / 查看命令（含 /mode） · Ctrl+D 退出[/dim]\n"
    )


def print_help() -> None:
    table = Table(title="HQin 命令", border_style="dim", show_header=True)
    table.add_column("命令", style="cyan", min_width=12)
    table.add_column("说明", style="white")
    for name, description in SLASH_COMMANDS.items():
        table.add_row(name, description)
    console.print(table)
    console.print(
        "[dim]快捷键: Enter 发送  |  Esc+Enter 换行  |  ↑/↓ 历史  |  Ctrl+D 退出[/dim]\n"
    )


def print_unknown_command(name: str) -> None:
    console.print(
        f"[yellow]未知命令: /{name}[/yellow]  "
        "输入 [cyan]/help[/cyan] 查看可用命令；该内容不会发送给模型。\n"
    )


def print_status(messages: list[dict], workspace_path: str, mode: str = "ask") -> None:
    table = Table(title="HQin 状态", border_style="dim", show_header=False)
    table.add_column("项目", style="cyan", min_width=14)
    table.add_column("值", style="white")
    table.add_row("模型", config.MODEL)
    table.add_row("工作区", workspace_path)
    table.add_row("git 分支", read_git_branch(workspace_path) or "-")
    table.add_row("权限模式", mode)
    table.add_row("消息数", str(len(messages)))
    table.add_row("上下文", build_status_line(messages, workspace_path, mode).strip())
    console.print(table)
    console.print(memory.context_status(messages))


def _display_width(text: str) -> int:
    """中文等宽字符按 2 列计，用于面板内文本对齐。"""
    return sum(2 if ord(ch) > 0x2E80 else 1 for ch in text)


def _pad(text: str, width: int) -> str:
    return text + " " * max(1, width - _display_width(text))


def render_context_report(messages: list[dict]) -> str:
    """生成 /context 的完整文本报告，供面板渲染和测试复用。"""
    tokens = memory.calibrated_messages_tokens(messages)
    budget = max(1, config.MODEL_CONTEXT_TOKENS)
    ratio = tokens / budget * 100
    soft = int(budget * config.CONTEXT_SOFT_RATIO)
    hard = int(budget * config.CONTEXT_HARD_RATIO)

    lines = [
        f"上下文: {tokens:,} / {budget:,} tokens ({ratio:.0f}%)  {usage_bar(ratio, 14)}",
        f"软阈值 {config.CONTEXT_SOFT_RATIO:.0%} ≈ {soft:,} tokens（提示模型压缩）",
        f"硬阈值 {config.CONTEXT_HARD_RATIO:.0%} ≈ {hard:,} tokens（自动摘要/硬删除）",
    ]
    real = memory.get_real_usage()
    if real:
        prompt = real["prompt_tokens"]
        raw = memory.estimate_messages_tokens(messages)
        drift = (raw - prompt) / prompt * 100
        lines.append(
            f"最近实测: {prompt:,} prompt tokens"
            f"（已按其校准；未校准估算 {raw:,}，"
            f"原始估算系统偏差 {drift:+.1f}%）"
            f" | completion {real['completion_tokens']:,}"
        )
    else:
        lines.append("最近实测: 暂无（等待下一次模型请求返回 usage）")

    breakdown = memory.context_usage_breakdown(messages)
    lines.append("")
    lines.append("按角色分解（估算，含每条消息 +4 协议开销）:")
    lines.append(f"  {_pad('角色', 12)}{_pad('消息', 8)}{_pad('tokens', 10)}占比")
    for role, entry in breakdown["roles"].items():
        share = entry["tokens"] / tokens * 100 if tokens else 0
        lines.append(
            f"  {_pad(role, 12)}{_pad(str(entry['messages']), 8)}"
            f"{_pad(f'{entry['tokens']:,}', 10)}{share:.1f}%"
        )
    lines.append(
        f"  {_pad('合计', 12)}{_pad(str(breakdown['messages']), 8)}"
        f"{_pad(f'{breakdown['total']:,}', 10)}100.0%"
    )
    fixed = tokens - breakdown["total"]
    if real and fixed:
        # 校准差值 = 实测 - 原始估算，来自工具 Schema、chat template 等
        # 每次请求固定存在的开销；列出它保证合计严格等于校准总量。
        lines.append(
            f"  {_pad('固定开销', 12)}{_pad('-', 8)}"
            f"{_pad(f'{fixed:,}', 10)}{fixed / tokens * 100:.1f}%  "
            f"(工具 Schema 等实测校准差值)"
        )

    ranges = memory.turn_ranges(messages)
    keep = min(max(0, config.MEMORY_KEEP_RECENT_TURNS), len(ranges))
    if ranges:
        lines.append("")
        lines.append(f"占用最大的轮次（共 {len(ranges)} 轮，受保护最近 {keep} 轮）:")
        sized = sorted(
            (
                (memory.calibrated_messages_tokens(messages[r["start"]:r["end"]]), r)
                for r in ranges
            ),
            key=lambda item: -item[0],
        )[:3]
        for size, r in sized:
            mark = "受保护" if r["local_turn"] > (len(ranges) - keep) else "可压缩"
            preview = memory._preview(messages[r["start"]].get("content") or "", 40)
            lines.append(
                f"  turn {r['turn']} | {size:,} tokens | {mark} | {preview}"
            )
    blocks = memory.get_store().list()
    if blocks:
        archived_tokens = sum(entry.get("tokens", 0) for entry in blocks)
        lines.append("")
        lines.append(
            f"磁盘归档块: {len(blocks)} 个，共 {archived_tokens:,} tokens"
            f"（已移出上下文，可 search_context / decompress_context）"
        )
    return "\n".join(lines)


def print_context_panel(messages: list[dict], console_obj: Console | None = None) -> None:
    """/context 命令的入口：渲染上下文占用明细面板。"""
    target = console_obj or console
    body = render_context_report(messages)
    accent = "yellow" if memory.over_soft_budget(messages) else "cyan"
    target.print(Panel(body, title="[bold]📊 上下文占用[/bold]", border_style=accent))


def print_sessions(sessions: Iterable[dict]) -> None:
    table = Table(title="历史会话", border_style="dim", show_header=True)
    table.add_column("#", style="cyan", justify="right", width=3)
    table.add_column("保存时间", style="white", min_width=18)
    table.add_column("消息", style="green", justify="right")
    table.add_column("模型", style="dim")
    table.add_column("文件", style="dim")
    for item in sessions:
        table.add_row(
            str(item["index"]),
            str(item.get("saved_at", "?")),
            str(item.get("messages", 0)),
            str(item.get("model", "?")),
            str(item.get("name", "?")),
        )
    console.print(table)


def _format_args(args: dict) -> str:
    preferred = (
        "path", "command", "query", "pattern", "directory",
        "keyword", "title", "url", "block_id", "start_turn", "end_turn",
    )
    parts = []
    for key in preferred:
        if key in args and args[key] not in (None, ""):
            value = str(args[key]).replace("\n", " ")
            if len(value) > 120:
                value = value[:117] + "..."
            parts.append(f"{key}={value}")
    if not parts:
        try:
            rendered = json.dumps(args, ensure_ascii=False)
        except Exception:
            rendered = str(args)
        if len(rendered) > 160:
            rendered = rendered[:157] + "..."
        return rendered
    return "  ".join(parts)


def print_tool_call(name: str, args: dict, console_obj: Console | None = None) -> None:
    target = console_obj or console
    target.print()
    target.print(Text(f"⚙  {name}", style="bold blue"), Text(f"  {_format_args(args)}", style="dim"))


def print_tool_result(
    name: str,
    observation: str,
    elapsed: float,
    is_error: bool,
    console_obj: Console | None = None,
) -> None:
    if observation.startswith("Error:"):
        style = "red"
    elif observation.startswith(("Refused:", "ConfigurationRequired:")):
        style = "yellow"
    else:
        style = "white"

    text = observation or "(无输出)"
    limit = max(200, config.CLI_TOOL_OUTPUT_CHARS)
    if len(text) > limit:
        text = text[:limit] + f"\n... [输出已截断，原长 {len(observation)} 字符]"

    target = console_obj or console
    target.print(Text(f"   {name} · {elapsed:.2f}s", style="dim"))
    target.print(Text(text, style=style))
    target.print()


def confirm_edit(rel_path: str, old_text: str, new_text: str, diff_text: str) -> bool:
    """CLI 安装到 tools 的写文件预览钩子；workspace/full 模式免审批直接放行。"""
    import tools as _tools
    mode = _tools.get_permission_mode()
    if mode in ("workspace", "full"):
        console.print(
            f"[dim]📝 mode={mode} 已自动批准写入 {rel_path}"
            f"（{'新建' if not (old_text or '').strip() else '修改'}）。[/dim]"
        )
        return True
    # 阻塞等用户确认前先弹桌面通知（best-effort，失败静默）。
    notifier.notify(
        "需要审批: 文件修改",
        f"写入 {rel_path}（{'新建' if not (old_text or '').strip() else '修改'}）",
    )
    console.print(f"\n[bold yellow]📝 待写入:[/bold yellow] [cyan]{rel_path}[/cyan]")
    if not diff_text.strip():
        console.print("[dim]内容没有变化。[/dim]")
    else:
        lines = diff_text.splitlines()
        limit = max(20, config.CLI_DIFF_MAX_LINES)
        if len(lines) > limit:
            diff_text = "\n".join(lines[:limit]) + (
                f"\n... [diff 已截断，共 {len(lines)} 行]"
            )
        try:
            console.print(
                Syntax(diff_text, "diff", theme="ansi_dark",
                       line_numbers=False, word_wrap=False)
            )
        except Exception:
            console.print(Text(diff_text, style="dim"))
    return Confirm.ask("应用这次修改？", default=True)
