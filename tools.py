# tools.py
import os
import subprocess
import ast
import urllib.request
import urllib.parse
import html as _html
import re
from rich.console import Console
from rich.prompt import Confirm

import config
import workspace as _ws

console = Console()

# 扫描时忽略的目录
_EXCLUDE_DIRS = {'.git', '.venv', 'venv', '__pycache__', '.idea', 'node_modules', '.mypy_cache'}
# 扫描时忽略的二进制文件后缀
_EXCLUDE_EXTS = {'.pyc', '.png', '.jpg', '.jpeg', '.gif', '.exe', '.dll', '.zip', '.whl', '.so'}


# ──────────────────────────────────────────────
# 内部辅助
# ──────────────────────────────────────────────

def _resolve(path: str) -> tuple[str, str]:
    """
    解析并校验路径是否在工作区内。
    返回 (resolved_path, error_msg)；error_msg 为空表示成功。
    """
    try:
        return _ws.resolve_path(path), ""
    except ValueError as e:
        return "", f"Error: {e}"
    except RuntimeError as e:
        return "", f"Error: 工作区未初始化 - {e}"


def _check_python_syntax(file_path: str, code_content: str) -> str:
    """检查 Python 代码语法。返回空字符串表示无错误，否则返回错误描述。"""
    if not file_path.endswith('.py'):
        return ""
    try:
        ast.parse(code_content)
        return ""
    except SyntaxError as e:
        msg = f"SyntaxError 第 {e.lineno} 行: {e.msg}"
        if e.text:
            msg += f"\n  错误代码: {e.text.strip()}"
        return msg


# ──────────────────────────────────────────────
# 工具实现
# ──────────────────────────────────────────────

def list_dir(path: str = ".") -> str:
    """列出工作区内指定目录的树形结构（自动过滤无关目录）。"""
    resolved, err = _resolve(path or ".")
    if err:
        return err
    if not os.path.isdir(resolved):
        return f"Error: 路径 '{path}' 不是一个有效目录。"

    ws_root = _ws.get_workspace_str()
    display_root = path if path != "." else "(工作区根目录)"
    lines = [f"目录树: {resolved}  [工作区: {ws_root}]"]

    def _walk(current: str, prefix: str, depth: int):
        if depth > 4:
            lines.append(f"{prefix}... (层级过深，已截断)")
            return
        try:
            entries = sorted(os.listdir(current))
        except PermissionError:
            return
        entries = [e for e in entries if e not in _EXCLUDE_DIRS and not e.startswith('.')]
        dirs  = [e for e in entries if os.path.isdir(os.path.join(current, e))]
        files = [e for e in entries if os.path.isfile(os.path.join(current, e))]

        for i, d in enumerate(dirs):
            connector = "└── " if (i == len(dirs) - 1 and not files) else "├── "
            lines.append(f"{prefix}{connector}{d}/")
            extension = "    " if (i == len(dirs) - 1 and not files) else "│   "
            _walk(os.path.join(current, d), prefix + extension, depth + 1)
        for i, f in enumerate(files):
            connector = "└── " if i == len(files) - 1 else "├── "
            lines.append(f"{prefix}{connector}{f}")

    _walk(resolved, "", 0)
    return "\n".join(lines)


def read_file(path: str, start: int = None, end: int = None) -> str:
    """读取工作区内文件内容（带行号），支持 start/end 行范围。大文件自动分页。"""
    resolved, err = _resolve(path)
    if err:
        return err
    if not os.path.exists(resolved):
        return (f"Error: 文件 '{path}' 不存在。"
                "请先用 list_dir 查看工作区目录结构。")
    try:
        with open(resolved, 'r', encoding='utf-8') as f:
            lines = f.readlines()

        total = len(lines)
        # 大文件自动分页：未指定范围时只返回第一页
        auto_paged = False
        if start is None and end is None and total > config.READ_FILE_PAGE_SIZE:
            end = config.READ_FILE_PAGE_SIZE
            auto_paged = True

        s = max(1, start) if start else 1
        e = min(total, end) if end else total

        if s > total:
            return f"Error: start={s} 超过文件总行数 {total}。"

        selected = lines[s - 1:e]
        content = "".join(f"{i + s:04d} | {line}" for i, line in enumerate(selected))
        rel_path = os.path.relpath(resolved, _ws.get_workspace_str())

        header = f"文件: {rel_path}  (第 {s}–{e} 行 / 共 {total} 行)\n"
        footer = ""
        if auto_paged:
            footer = (
                f"\n⚠️  [分页提示] 文件共 {total} 行，当前仅显示第 1~{e} 行。"
                f"如需继续阅读，请用 start/end 参数指定范围，"
                f"例如 read_file(path=\"{path}\", start={e+1}, end={min(total, e + config.READ_FILE_PAGE_SIZE)})。"
            )
        return header + content + footer
    except UnicodeDecodeError:
        return f"Error: 文件 '{path}' 包含非 UTF-8 内容，无法以文本方式读取。"
    except Exception as ex:
        return f"Error: 读取文件异常 - {ex}"


def write_file(path: str, content: str) -> str:
    """在工作区内创建或覆盖文件（对 .py 文件进行语法检查）。"""
    resolved, err = _resolve(path)
    if err:
        return err

    syntax_error = _check_python_syntax(resolved, content)
    if syntax_error:
        return (f"Error: 写入被拦截！代码存在语法错误：\n{syntax_error}\n"
                "请检查缩进和语法后重试。")
    try:
        os.makedirs(os.path.dirname(resolved), exist_ok=True)
        with open(resolved, 'w', encoding='utf-8') as f:
            f.write(content)
        rel_path = os.path.relpath(resolved, _ws.get_workspace_str())
        return f"Success: 文件 '{rel_path}' 已成功写入（{len(content.splitlines())} 行）。"
    except Exception as ex:
        return f"Error: 写入文件异常 - {ex}"


def edit_file(path: str, search: str, replace: str) -> str:
    """在工作区内文件中执行精确 Search & Replace（对 .py 文件检查修改后的语法）。"""
    resolved, err = _resolve(path)
    if err:
        return err
    if not os.path.exists(resolved):
        return f"Error: 文件 '{path}' 不存在。"
    try:
        with open(resolved, 'r', encoding='utf-8') as f:
            original = f.read()

        if search not in original:
            preview = search[:120].replace('\n', '↵') + ('...' if len(search) > 120 else '')
            return (f"Error: 匹配失败！找不到以下文本（前120字符预览）：\n  {preview}\n"
                    "请确保：1) 不含行号前缀  2) 缩进与空格与原文件完全一致  "
                    "3) 用 read_file 重新确认原文后再试。")

        new_content = original.replace(search, replace, 1)
        syntax_error = _check_python_syntax(resolved, new_content)
        if syntax_error:
            return (f"Error: 替换被拦截！修改后代码存在语法错误：\n{syntax_error}\n"
                    "修改已撤销，请重新检查缩进和语法。")

        with open(resolved, 'w', encoding='utf-8') as f:
            f.write(new_content)
        rel_path = os.path.relpath(resolved, _ws.get_workspace_str())
        return f"Success: 文件 '{rel_path}' 已成功更新。"
    except Exception as ex:
        return f"Error: 编辑文件异常 - {ex}"


def run_bash(command: str) -> str:
    """在工作区目录中执行 Shell 命令，执行前需人工审批。"""
    ws_cwd = _ws.get_workspace_str()
    console.print(
        f"\n[bold yellow]⚠  智能体请求执行命令:[/bold yellow] "
        f"[bold cyan]{command}[/bold cyan]\n"
        f"[dim]  执行目录: {ws_cwd}[/dim]"
    )
    if not Confirm.ask("允许执行吗？"):
        return "Error: 用户拒绝了该命令的执行。"

    try:
        result = subprocess.run(
            command, shell=True, capture_output=True,
            timeout=config.BASH_TIMEOUT_SECONDS, stdin=subprocess.DEVNULL,
            cwd=ws_cwd  # 始终在工作区目录中执行
        )

        def _decode(b: bytes) -> str:
            if not b:
                return ""
            try:
                return b.decode('utf-8')
            except UnicodeDecodeError:
                return b.decode('gbk', errors='replace')

        output = _decode(result.stdout) + _decode(result.stderr)
        if result.returncode != 0:
            detail = output.strip() or "无终端输出"
            return f"Error: 命令执行失败（exit code={result.returncode}）：\n{detail}"
        if not output.strip():
            return "Success: 命令执行完毕，无终端输出。"
        if len(output) > config.BASH_OUTPUT_LIMIT:
            output = output[:config.BASH_OUTPUT_LIMIT] + f"\n...(输出过长，已截断，共 {len(output)} 字符)"
        return f"执行输出（exit code={result.returncode}）:\n{output}"
    except subprocess.TimeoutExpired:
        return f"Error: 命令执行超时（{config.BASH_TIMEOUT_SECONDS}秒）。"
    except Exception as ex:
        return f"Error: 命令执行崩溃 - {ex}"


def search_in_files(keyword: str, directory: str = ".") -> str:
    """在工作区内搜索包含关键字的代码行，返回文件路径和行号。"""
    resolved_dir, err = _resolve(directory or ".")
    if err:
        return err
    if not os.path.isdir(resolved_dir):
        return f"Error: 目录 '{directory}' 不存在。"

    ws_root = _ws.get_workspace_str()
    results = []
    try:
        for root, dirs, files in os.walk(resolved_dir):
            dirs[:] = [d for d in dirs if d not in _EXCLUDE_DIRS and not d.startswith('.')]
            for file in files:
                if any(file.endswith(ext) for ext in _EXCLUDE_EXTS):
                    continue
                file_path = os.path.join(root, file)
                try:
                    with open(file_path, 'r', encoding='utf-8') as f:
                        for line_idx, line in enumerate(f):
                            if keyword in line:
                                display = line.strip()
                                if len(display) > 120:
                                    display = display[:120] + "..."
                                rel = os.path.relpath(file_path, ws_root)
                                results.append(f"{rel}:{line_idx + 1} | {display}")
                except (UnicodeDecodeError, PermissionError):
                    pass

        if not results:
            return f"未找到包含关键字 '{keyword}' 的代码（搜索目录: {directory}）。"

        output = "\n".join(results[:config.SEARCH_MAX_RESULTS])
        if len(results) > config.SEARCH_MAX_RESULTS:
            output += f"\n\n...(省略 {len(results) - config.SEARCH_MAX_RESULTS} 条结果，请使用更精确的关键字)"
        return f"搜索完成，共 {len(results)} 条结果：\n{output}"
    except Exception as ex:
        return f"Error: 搜索异常 - {ex}"


# ──────────────────────────────────────────────
# Git 工具（以工作区为 cwd 执行）
# ──────────────────────────────────────────────

def _git_run(args: list[str], timeout: int = 15) -> tuple[int, str]:
    """在工作区目录中执行 git 子命令，避免经由 shell 解释参数。"""
    ws_cwd = _ws.get_workspace_str()
    result = subprocess.run(
        ["git", *args], shell=False, capture_output=True,
        timeout=timeout, stdin=subprocess.DEVNULL,
        cwd=ws_cwd
    )
    output = (result.stdout.decode('utf-8', errors='replace')
              + result.stderr.decode('utf-8', errors='replace'))
    return result.returncode, output.strip()


def git_status() -> str:
    """查看 Git 仓库当前状态（已修改、暂存、未跟踪的文件）。"""
    try:
        code, output = _git_run(["status", "--short", "--branch"])
        if code != 0 and "not a git repository" in output.lower():
            return "Error: 当前工作区不是 Git 仓库。请先执行 'git init' 或切换工作区。"
        return output or "Git 仓库状态干净，无任何变更。"
    except Exception as ex:
        return f"Error: git status 执行失败 - {ex}"


def git_diff(path: str = None) -> str:
    """查看 Git diff（未暂存的变更）。path 为空时查看所有变更。"""
    # 如果指定了 path，先校验在工作区内
    args = ["diff"]
    if path:
        resolved, err = _resolve(path)
        if err:
            return err
        relative_path = os.path.relpath(resolved, _ws.get_workspace_str())
        args.extend(["--", relative_path])
    try:
        code, output = _git_run(args)
        if code != 0:
            return f"Error: git diff 失败（exit code={code}）。\n{output}"
        if not output:
            return "没有未暂存的变更。如需查看已暂存内容，请用 run_bash 执行 'git diff --staged'。"
        if len(output) > config.DIFF_OUTPUT_LIMIT:
            output = output[:config.DIFF_OUTPUT_LIMIT] + f"\n...(diff 过长，已截断，共 {len(output)} 字符)"
        return output
    except Exception as ex:
        return f"Error: git diff 执行失败 - {ex}"


def git_commit(message: str, add_all: bool = True) -> str:
    """提交变更（带人工审批），默认先执行 'git add -A'。"""
    ws_cwd = _ws.get_workspace_str()
    preview = (f"git add -A；git commit -m {message!r}"
               if add_all else f"git commit -m {message!r}")
    console.print(
        f"\n[bold yellow]⚠  智能体请求提交 Git:[/bold yellow] "
        f"[bold cyan]{preview}[/bold cyan]\n"
        f"[dim]  工作区: {ws_cwd}[/dim]"
    )
    if not Confirm.ask("允许提交吗？"):
        return "Error: 用户拒绝了该 Git 提交操作。"

    try:
        if add_all:
            add_code, add_output = _git_run(["add", "-A"], timeout=10)
            if add_code != 0:
                return f"Error: git add 失败（exit code={add_code}）。\n{add_output}"
        code, output = _git_run(["commit", "-m", message])
        if code == 0:
            return f"Success: Git 提交成功。\n{output}"
        return f"Error: Git 提交失败（exit code={code}）。\n{output}"
    except Exception as ex:
        return f"Error: git commit 执行失败 - {ex}"


def web_search(query: str) -> str:
    """
    使用 DuckDuckGo 搜索网络内容（无需 API Key）。
    适用于查询库文档、错误信息、API 用法等。
    """
    max_results = config.WEB_SEARCH_MAX

    # ── 方式一：DuckDuckGo Instant Answer API ──
    try:
        params = urllib.parse.urlencode({
            "q": query, "format": "json",
            "no_html": "1", "skip_disambig": "1",
        })
        req = urllib.request.Request(
            f"https://api.duckduckgo.com/?{params}",
            headers={"User-Agent": "HQin-Harness/1.0 (coding agent)"}
        )
        import json as _json
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = _json.loads(resp.read().decode("utf-8"))

        results = []
        if data.get("Abstract"):
            src = data.get("AbstractURL", "")
            results.append(f"【摘要】{data['Abstract']}")
            if src:
                results.append(f"  来源: {src}")

        for topic in data.get("RelatedTopics", [])[:max_results]:
            if isinstance(topic, dict) and topic.get("Text"):
                text = topic["Text"]
                url  = topic.get("FirstURL", "")
                results.append(f"- {text[:200]}")
                if url:
                    results.append(f"  {url}")

        if results:
            return f"🔍 DuckDuckGo 搜索: {query}\n\n" + "\n".join(results)

    except Exception:
        pass  # API 失败，降级到 Lite HTML 解析

    # ── 方式二：DuckDuckGo Lite HTML 解析 ──
    try:
        params = urllib.parse.urlencode({"q": query, "kl": "cn-zh"})
        req = urllib.request.Request(
            f"https://lite.duckduckgo.com/lite/?{params}",
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode("utf-8", errors="replace")

        # 提取标题 + 链接
        links    = re.findall(r'class="result-link"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', body)
        snippets = re.findall(r'class="result-snippet"[^>]*>(.*?)</td>', body, re.DOTALL)

        items = []
        for i, (url, title) in enumerate(links[:max_results]):
            clean_title   = _html.unescape(re.sub(r"<[^>]+>", "", title)).strip()
            clean_snippet = ""
            if i < len(snippets):
                clean_snippet = _html.unescape(re.sub(r"<[^>]+>", "", snippets[i])).strip()[:200]
            item = f"{i+1}. {clean_title}\n   {url}"
            if clean_snippet:
                item += f"\n   {clean_snippet}"
            items.append(item)

        if items:
            return f"🔍 搜索: {query}\n\n" + "\n\n".join(items)

    except Exception as ex:
        return (
            f"Error: 网络搜索失败 - {ex}\n"
            "提示: 检查网络连接，或改用 run_bash 执行 curl/wget 手动搜索。"
        )

    return (
        f"未找到关于 '{query}' 的直接搜索结果。\n"
        "建议: 使用更具体的关键词，或改用 run_bash 执行 curl 手动搜索。"
    )


# ──────────────────────────────────────────────
# 工具注册表：名称 → 函数（供 main.py 路由使用）
# ──────────────────────────────────────────────

TOOL_REGISTRY = {
    "run_bash":        run_bash,
    "read_file":       read_file,
    "write_file":      write_file,
    "edit_file":       edit_file,
    "search_in_files": search_in_files,
    "list_dir":        list_dir,
    "git_status":      git_status,
    "git_diff":        git_diff,
    "git_commit":      git_commit,
    "web_search":      web_search,
}
