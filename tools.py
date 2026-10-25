# tools.py
import os
import sys
import shutil
import subprocess
import ast
import csv
import datetime
import difflib
import fnmatch
import glob as _glob
import io
import json
import urllib.request
import urllib.parse
import html as _html
import re
from pathlib import Path
from types import SimpleNamespace
from rich.console import Console
from rich.prompt import Confirm

import config
import console_setup
import workspace as _ws
import browser_policy
import memory
import notifier

console_setup.configure_utf8_console()
console = Console()

# 扫描时忽略的目录
_EXCLUDE_DIRS = {'.git', '.venv', 'venv', '__pycache__', '.idea', 'node_modules', '.mypy_cache'}
# 扫描时忽略的二进制文件后缀
_EXCLUDE_EXTS = {'.pyc', '.png', '.jpg', '.jpeg', '.gif', '.exe', '.dll', '.zip', '.whl', '.so'}

# BrowserAct 会话仅在当前 Harness 进程内有效，不将 Cookie 或会话状态写入 messages/session 存档。
# 状态机放在 browser_policy.py，保证“先 state 后 click”由代码而非模型自觉执行。
_browser_policy = browser_policy.BrowserSessionPolicy()

# CLI 在写文件前安装这个钩子来展示 diff；测试和脚本不安装时直接落盘。
_edit_preview_hook = None

# 本次进程成功写入的文件，供 finish_check 在模型不传 files 时做默认自检。
# 只记录真实落盘的文件，不记录被语法检查/用户拒绝的写入。
_session_written_files: set[str] = set()

# 当前权限模式（ask / workspace / full）。主循环通过 set_permission_mode
# 注入；默认 ask。模式只决定“是否需要人工确认”，不改变沙盒路径校验。
_permission_mode = "ask"


def set_permission_mode(mode: str) -> None:
    """主循环切换权限模式时调用；非法值静默保持 ask。"""
    global _permission_mode
    normalized = (mode or "").strip().lower()
    if normalized in ("ask", "workspace", "full"):
        _permission_mode = normalized


def get_permission_mode() -> str:
    return _permission_mode


def set_edit_preview_hook(hook):
    """hook(rel_path, old_text, new_text, diff_text) -> bool；None 表示不预览。"""
    global _edit_preview_hook
    _edit_preview_hook = hook


def _make_unified_diff(rel_path: str, old: str, new: str) -> str:
    return "\n".join(
        difflib.unified_diff(
            old.splitlines(),
            new.splitlines(),
            fromfile=f"a/{rel_path}",
            tofile=f"b/{rel_path}",
            lineterm="",
        )
    )


def _preview_edit(rel_path: str, old: str, new: str) -> bool:
    if _edit_preview_hook is None:
        return True
    return bool(_edit_preview_hook(rel_path, old, new, _make_unified_diff(rel_path, old, new)))


def _record_written_file(resolved: str) -> None:
    """记录成功落盘的文件，供 finish_check 默认检查本次会话的交付物。"""
    _session_written_files.add(os.path.normcase(os.path.abspath(resolved)))


def _write_size_error(content: str) -> str:
    if len(content) <= config.MAX_WRITE_CHARS:
        return ""
    return (
        f"Error: 内容过长（{len(content)} 字符，单次上限 {config.MAX_WRITE_CHARS}）。"
        "请拆成较小的块：第一块用 write_file，后续用 append_file 追加；"
        "Python 文件先写完整骨架，再用 edit_file 分段替换。"
    )


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
        chunks = []
        used = 0
        display_end = e
        char_truncated = False
        for i, line in enumerate(selected):
            rendered = f"{i + s:04d} | {line}"
            if used + len(rendered) > config.READ_OUTPUT_LIMIT:
                if not chunks:
                    chunks.append(rendered[:config.READ_OUTPUT_LIMIT] + "...\n")
                    display_end = s
                else:
                    display_end = s + len(chunks) - 1
                char_truncated = True
                break
            chunks.append(rendered)
            used += len(rendered)

        content = "".join(chunks)
        rel_path = os.path.relpath(resolved, _ws.get_workspace_str())

        header = f"文件: {rel_path}  (第 {s}–{display_end} 行 / 共 {total} 行)\n"
        footer = ""
        if char_truncated:
            resume = display_end + 1
            footer = (
                f"\n⚠️  [字符预算] 单次输出达到 {config.READ_OUTPUT_LIMIT} 字符上限，"
                f"已显示到第 {display_end} 行（超长行可能被截断）。"
                f"如需继续，请用 read_file(path=\"{path}\", start={resume}, "
                f"end={min(total, resume + config.READ_FILE_PAGE_SIZE)})。"
            )
        elif auto_paged:
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
    if path.lower().endswith((".docx", ".doc")):
        return (
            "Error: .docx/.doc 是二进制 Word 格式，不能按文本写入。"
            "请改用 create_docx(path, title, content) 生成真正的 Word 文档。"
        )
    resolved, err = _resolve(path)
    if err:
        return err
    too_long = _write_size_error(content)
    if too_long:
        return too_long

    syntax_error = _check_python_syntax(resolved, content)
    if syntax_error:
        return (f"Error: 写入被拦截！代码存在语法错误：\n{syntax_error}\n"
                "请检查缩进和语法后重试。")
    rel_path = os.path.relpath(resolved, _ws.get_workspace_str())
    old_content = ""
    if os.path.exists(resolved):
        try:
            with open(resolved, 'r', encoding='utf-8') as f:
                old_content = f.read()
        except (OSError, UnicodeDecodeError):
            old_content = ""
    try:
        if not _preview_edit(rel_path, old_content, content):
            return f"Refused: 用户拒绝了写入 '{rel_path}'，文件未修改。"
        os.makedirs(os.path.dirname(resolved), exist_ok=True)
        with open(resolved, 'w', encoding='utf-8') as f:
            f.write(content)
        _record_written_file(resolved)
        return (
            f"Success: 文件 '{rel_path}' 已成功写入"
            f"（{len(content.splitlines())} 行，{len(content)} 字符）。"
        )
    except Exception as ex:
        return f"Error: 写入文件异常 - {ex}"


def append_file(path: str, content: str) -> str:
    """向文件追加文本；用于大文件分块写入，不做 Python 语法检查。"""
    if path.lower().endswith((".docx", ".doc")):
        return (
            "Error: .docx/.doc 是二进制 Word 格式，不能追加文本。"
            "请改用 create_docx(path, title, content) 生成真正的 Word 文档。"
        )
    if not content:
        return "Error: append_file 的 content 不能为空。"
    resolved, err = _resolve(path)
    if err:
        return err
    too_long = _write_size_error(content)
    if too_long:
        return too_long

    rel_path = os.path.relpath(resolved, _ws.get_workspace_str())
    old_content = ""
    if os.path.exists(resolved):
        try:
            with open(resolved, 'r', encoding='utf-8') as f:
                old_content = f.read()
        except (OSError, UnicodeDecodeError):
            old_content = ""
    try:
        if not _preview_edit(rel_path, old_content, old_content + content):
            return f"Refused: 用户拒绝了向 '{rel_path}' 追加内容，文件未修改。"
        os.makedirs(os.path.dirname(resolved), exist_ok=True)
        with open(resolved, 'a', encoding='utf-8') as f:
            f.write(content)
        _record_written_file(resolved)
        return (
            f"Success: 已向 '{rel_path}' 追加 {len(content.splitlines())} 行"
            f"（{len(content)} 字符）。"
        )
    except Exception as ex:
        return f"Error: 追加文件异常 - {ex}"


def create_docx(path: str, title: str, content: str) -> str:
    """在工作区内生成真正的 Word .docx 文档（标题 + 正文，支持简单 Markdown 行）。"""
    if not path.lower().endswith(".docx"):
        path += ".docx"
    resolved, err = _resolve(path)
    if err:
        return err

    try:
        from docx import Document
        from docx.oxml.ns import qn
        from docx.shared import Pt
    except ImportError:
        return (
            "Error: 未安装 python-docx，无法生成 Word 文档。请运行：\n"
            '.venv\\Scripts\\python.exe -m pip install "python-docx>=1.1,<2"'
        )

    try:
        doc = Document()
        normal = doc.styles["Normal"]
        normal.font.name = "宋体"
        normal.font.size = Pt(10.5)
        normal._element.get_or_add_rPr().get_or_add_rFonts().set(
            qn("w:eastAsia"), "宋体"
        )

        if title.strip():
            doc.add_heading(title.strip(), level=0)

        def add_runs(paragraph, text: str):
            for part in re.split(r"(\*\*.+?\*\*)", text):
                if part.startswith("**") and part.endswith("**") and len(part) > 4:
                    run = paragraph.add_run(part[2:-2])
                    run.bold = True
                elif part:
                    paragraph.add_run(part)

        paragraphs = 0
        for raw in content.splitlines():
            stripped = raw.strip()
            if not stripped or stripped == "---":
                continue
            if stripped.startswith("### "):
                doc.add_heading(stripped[4:].strip(), level=3)
            elif stripped.startswith("## "):
                doc.add_heading(stripped[3:].strip(), level=2)
            elif stripped.startswith("# "):
                doc.add_heading(stripped[2:].strip(), level=1)
            elif stripped.startswith(("- ", "* ")):
                paragraph = doc.add_paragraph(style="List Bullet")
                add_runs(paragraph, stripped[2:].strip())
            elif re.match(r"^\d+\.\s+", stripped):
                paragraph = doc.add_paragraph(style="List Number")
                add_runs(paragraph, re.sub(r"^\d+\.\s+", "", stripped))
            else:
                paragraph = doc.add_paragraph()
                add_runs(paragraph, stripped)
            paragraphs += 1

        os.makedirs(os.path.dirname(resolved), exist_ok=True)
        doc.save(resolved)
        _record_written_file(resolved)
        rel_path = os.path.relpath(resolved, _ws.get_workspace_str())
        return (
            f"Success: 已生成真正的 Word 文档 '{rel_path}'"
            f"（{paragraphs} 段，{len(content)} 字符）。"
        )
    except Exception as ex:
        return f"Error: 生成 Word 文档异常 - {ex}"


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

        rel_path = os.path.relpath(resolved, _ws.get_workspace_str())
        if not _preview_edit(rel_path, original, new_content):
            return f"Refused: 用户拒绝了编辑 '{rel_path}'，文件未修改。"
        with open(resolved, 'w', encoding='utf-8') as f:
            f.write(new_content)
        _record_written_file(resolved)
        return f"Success: 文件 '{rel_path}' 已成功更新。"
    except Exception as ex:
        return f"Error: 编辑文件异常 - {ex}"


_PLACEHOLDER_RE = re.compile(
    r"\b(TODO|FIXME|TBD)\b|待补充|待完善|占位符|placeholder",
    re.IGNORECASE,
)
_TEXT_CHECK_EXTS = {
    ".py", ".json", ".csv", ".txt", ".md", ".html", ".htm", ".js", ".ts",
    ".css", ".yml", ".yaml", ".xml", ".sql", ".java", ".c", ".cpp", ".h",
}


def _placeholder_warnings(text: str) -> list[str]:
    warnings = []
    for line_no, line in enumerate(text.splitlines(), 1):
        match = _PLACEHOLDER_RE.search(line)
        if match:
            warnings.append(f"发现占位/未完成标记 `{match.group(0)}`（第 {line_no} 行）")
            if len(warnings) >= 3:
                warnings.append("占位标记较多，仅显示前 3 处")
                break
    return warnings


def _check_deliverable(resolved: str) -> tuple[bool, str, list[str]]:
    """检查单个文件/目录；返回 (是否通过, 说明, 警告)。"""
    if not os.path.exists(resolved):
        return False, "文件不存在", []
    if os.path.isdir(resolved):
        try:
            entries = [
                name for name in os.listdir(resolved)
                if name not in _EXCLUDE_DIRS and not name.startswith(".")
            ]
        except OSError as ex:
            return False, f"目录无法读取: {ex}", []
        if not entries:
            return False, "目录为空", []
        return True, f"目录，包含 {len(entries)} 项", []

    try:
        size = os.path.getsize(resolved)
    except OSError as ex:
        return False, f"无法读取文件大小: {ex}", []
    if size == 0:
        return False, "文件为空", []

    ext = os.path.splitext(resolved)[1].lower()
    if ext == ".doc":
        return False, "旧版 .doc 无法可靠验证，请改用 .docx", []
    if ext == ".docx":
        try:
            from docx import Document
            doc = Document(resolved)
        except ImportError:
            return False, "未安装 python-docx，无法验证 .docx", []
        except Exception as ex:
            return False, f"Word 文档无法打开: {ex}", []
        text = "\n".join(p.text for p in doc.paragraphs)
        warnings = _placeholder_warnings(text)
        detail = f"Word 文档可打开（{len(doc.paragraphs)} 段，{size} 字节）"
        return True, detail, warnings

    if ext not in _TEXT_CHECK_EXTS:
        return True, f"{size} 字节（二进制或未识别类型，仅检查存在/非空）", []

    try:
        with open(resolved, "r", encoding="utf-8") as f:
            text = f.read()
    except UnicodeDecodeError:
        return False, "扩展名是文本类型，但内容不是 UTF-8", []
    except OSError as ex:
        return False, f"文件无法读取: {ex}", []

    warnings = _placeholder_warnings(text)
    if ext == ".py":
        try:
            compile(text, resolved, "exec")
        except SyntaxError as ex:
            return False, f"Python 语法错误（第 {ex.lineno} 行）: {ex.msg}", warnings
        return True, f"Python 语法通过（{len(text.splitlines())} 行）", warnings

    if ext == ".json":
        try:
            value = json.loads(text)
        except (json.JSONDecodeError, ValueError) as ex:
            return False, f"JSON 解析失败: {ex}", warnings
        kind = "数组" if isinstance(value, list) else "对象" if isinstance(value, dict) else type(value).__name__
        return True, f"JSON 解析通过（{kind}，{size} 字节）", warnings

    if ext == ".csv":
        try:
            rows = list(csv.reader(io.StringIO(text, newline="")))
        except csv.Error as ex:
            return False, f"CSV 解析失败: {ex}", warnings
        if not rows:
            return False, "CSV 没有数据行", warnings
        widths = {len(row) for row in rows if row}
        if len(widths) > 1:
            return False, f"CSV 列数不一致: {sorted(widths)}", warnings
        width = widths.pop() if widths else 0
        return True, f"CSV 解析通过（{len(rows)} 行 × {width} 列）", warnings

    return True, f"文本文件（{len(text.splitlines())} 行，{size} 字节）", warnings


def finish_check(files: list[str] | None = None) -> str:
    """交付前检查本次写入的文件：存在、非空、可解析/可打开，并扫描占位符。"""
    if files is None:
        resolved_paths = sorted(_session_written_files)
        if not resolved_paths:
            return (
                "Error: 没有记录到本次会话写入的文件。"
                "请传入 files=[\"交付物路径\", ...] 指定要检查的文件。"
            )
        paths = resolved_paths
    else:
        if isinstance(files, str):
            files = [files]
        if not files:
            return "Error: files 不能为空；请指定至少一个要检查的交付物路径。"
        paths = []
        seen = set()
        for raw in files:
            resolved, err = _resolve(str(raw))
            if err:
                paths.append(("", str(raw), err))
                continue
            key = os.path.normcase(os.path.abspath(resolved))
            if key not in seen:
                seen.add(key)
                paths.append((resolved, str(raw), ""))

    if len(paths) > config.FINISH_CHECK_MAX_FILES:
        paths = paths[:config.FINISH_CHECK_MAX_FILES]

    lines = [f"交付物检查：{len(paths)} 个路径"]
    failures = 0
    warnings_count = 0
    for item in paths:
        if isinstance(item, tuple):
            resolved, raw, path_error = item
        else:
            resolved, raw, path_error = item, item, ""
        if path_error:
            failures += 1
            lines.append(f"❌ {raw} — {path_error}")
            continue
        ok, detail, warnings = _check_deliverable(resolved)
        rel = os.path.relpath(resolved, _ws.get_workspace_str())
        if ok:
            lines.append(f"✅ {rel} — {detail}")
        else:
            failures += 1
            lines.append(f"❌ {rel} — {detail}")
        for warning in warnings:
            warnings_count += 1
            lines.append(f"   ⚠️ {warning}")

    if failures:
        lines.append(
            f"\n结论: NOT READY（{failures} 个失败，{warnings_count} 个警告）。"
            "请修复失败项后重新调用 finish_check。"
        )
        return "VerificationFailed: " + "\n".join(lines)

    lines.append(f"\n结论: READY（{len(paths)} 个路径通过，{warnings_count} 个警告）。")
    return "Success: " + "\n".join(lines)


def _is_wsl_bash(path: str) -> bool:
    """Windows 的 System32\\bash.exe 是 WSL 启动器，不是可独立使用的 bash。"""
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    wsl = os.path.join(system_root, "System32", "bash.exe")
    return os.name == "nt" and os.path.normcase(os.path.abspath(path)) == os.path.normcase(wsl)


def _git_bash_candidates() -> list[str]:
    """从 PATH 上的 git.exe 反推 Git 安装根目录，兼容 D:\\git 这类自定义安装。"""
    if os.name != "nt":
        return []
    candidates = []
    seen = set()
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry:
            continue
        git_exe = os.path.join(entry, "git.exe")
        if not os.path.isfile(git_exe):
            continue
        # cmd\git.exe、bin\git.exe、mingw64\bin\git.exe 都能反推出 Git 根目录。
        root = os.path.dirname(os.path.dirname(git_exe))
        for base in (root, os.path.dirname(root)):
            for relative in (
                os.path.join("bin", "bash.exe"),
                os.path.join("usr", "bin", "bash.exe"),
            ):
                candidate = os.path.join(base, relative)
                key = os.path.normcase(os.path.abspath(candidate))
                if key not in seen:
                    seen.add(key)
                    candidates.append(candidate)
    return candidates


def _shell_candidates(kind: str) -> list[str]:
    """按“显式安装位置 → Git 安装 → PATH”排序候选，避免优先拿到 WSL 启动器。"""
    candidates = []
    home = os.path.expanduser("~")
    if kind == "bash":
        if os.name == "nt":
            program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
            program_files_x86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
            local_app_data = os.environ.get("LOCALAPPDATA", os.path.join(home, "AppData", "Local"))
            candidates += [
                os.path.join(program_files, "Git", "bin", "bash.exe"),
                os.path.join(program_files, "Git", "usr", "bin", "bash.exe"),
                os.path.join(program_files_x86, "Git", "bin", "bash.exe"),
                os.path.join(local_app_data, "Programs", "Git", "bin", "bash.exe"),
                r"C:\cygwin64\bin\bash.exe",
                r"C:\cygwin\bin\bash.exe",
            ]
            candidates += _git_bash_candidates()
            # DSH 自带的 portable Git 是可用回退；不依赖其是否存在。
            candidates += sorted(
                _glob.glob(os.path.join(home, ".dsh", "dsh-ops", "bash-ipc", "*", "usr", "bin", "bash.exe")),
                reverse=True,
            )
        candidates.append(shutil.which("bash") or "")
    else:
        if os.name == "nt":
            program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
            local_app_data = os.environ.get("LOCALAPPDATA", os.path.join(home, "AppData", "Local"))
            candidates += [
                os.path.join(program_files, "PowerShell", "7", "pwsh.exe"),
                os.path.join(local_app_data, "Microsoft", "PowerShell", "7", "pwsh.exe"),
            ]
        candidates.append(shutil.which("pwsh") or "")
    return [candidate for candidate in candidates if candidate]


def _shell_probe(kind: str, executable: str) -> bool:
    """启动一次空命令，排除 WSL 启动器、损坏的 MSYS/Cygwin 等不可用候选。"""
    invocation = (
        [executable, "-c", "exit 0"] if kind == "bash"
        else [executable, "-NoProfile", "-NonInteractive", "-Command", "exit 0"]
    )
    try:
        result = subprocess.run(
            invocation, shell=False,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=5, stdin=subprocess.DEVNULL,
        )
        return result.returncode == 0
    except Exception:
        return False


def _find_shell(kind: str) -> tuple[str, str]:
    """查找显式 shell 可执行文件；返回 (path, error)。"""
    if kind == "bash":
        configured = config.BASH_EXECUTABLE
        command_name = "bash"
        env_name = "HQIN_BASH"
    elif kind == "pwsh":
        configured = config.PWSH_EXECUTABLE
        command_name = "pwsh"
        env_name = "HQIN_PWSH"
    else:
        return "", f"Error: 未知 shell 类型 '{kind}'。"

    if configured:
        found = shutil.which(configured) or (configured if os.path.isfile(configured) else "")
        if not found:
            return "", (
                f"ConfigurationRequired: {env_name} 指向的可执行文件不存在：{configured}"
            )
        if not _shell_probe(kind, found):
            return "", (
                f"ConfigurationRequired: {env_name} 指向的 {found} 无法启动。"
                "请检查它是否需要额外运行时，或改用 Git for Windows 的 bash.exe。"
            )
        return found, ""

    rejected_wsl = ""
    rejected_broken = []
    for candidate in _shell_candidates(kind):
        if not os.path.isfile(candidate):
            continue
        if kind == "bash" and _is_wsl_bash(candidate):
            rejected_wsl = candidate
            continue
        if not _shell_probe(kind, candidate):
            rejected_broken.append(candidate)
            continue
        return candidate, ""

    if rejected_wsl:
        detail = (
            "ConfigurationRequired: 只找到 WSL 启动器 "
            f"{rejected_wsl}，它不是可直接运行的 bash。"
        )
        if rejected_broken:
            detail += f" 另外这些候选无法启动：{', '.join(rejected_broken)}。"
        return "", detail + " 请安装 Git for Windows，或用 HQIN_BASH 指定 bash.exe 的路径。"

    if rejected_broken:
        return "", (
            "ConfigurationRequired: 找到的 "
            f"{command_name} 都无法启动：{', '.join(rejected_broken)}。"
            f"请安装 Git for Windows，或用 {env_name} 指定可执行文件路径。"
        )
    return "", (
        f"ConfigurationRequired: 未找到 {command_name}。"
        f"请安装后加入 PATH，或用 {env_name} 指定可执行文件路径。"
    )


def _shell_invocation(kind: str, executable: str, command: str) -> list[str]:
    """把命令封装成参数数组，绝不交给 shell=True 二次解释。"""
    if kind == "bash":
        return [executable, "-c", command]
    return [executable, "-NoProfile", "-NonInteractive", "-Command", command]


def _execute_shell(kind: str, executable: str, command: str) -> str:
    timeout = (
        config.BASH_TIMEOUT_SECONDS if kind == "bash"
        else config.PWSH_TIMEOUT_SECONDS
    )
    label = "bash" if kind == "bash" else "PowerShell"
    try:
        result = subprocess.run(
            _shell_invocation(kind, executable, command),
            shell=False,
            capture_output=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            cwd=_ws.get_workspace_str(),
        )
    except FileNotFoundError:
        return f"ConfigurationRequired: 无法启动 {label}（{executable}）。"
    except subprocess.TimeoutExpired:
        return f"Error: {label} 命令执行超时（{timeout}秒）。"
    except Exception as ex:
        return f"Error: {label} 命令执行崩溃 - {ex}"

    output = _decode_process_output(result).strip()
    if result.returncode != 0:
        detail = output or "无终端输出"
        return f"Error: {label} 命令执行失败（exit code={result.returncode}）：\n{detail}"
    if not output:
        return "Success: 命令执行完毕，无终端输出。"
    if len(output) > config.SHELL_OUTPUT_LIMIT:
        output = (
            output[:config.SHELL_OUTPUT_LIMIT]
            + f"\n...(输出过长，已截断，共 {len(output)} 字符)"
        )
    return f"执行输出（{label}, exit code={result.returncode}）:\n{output}"


def _approve_shell(label: str, command: str) -> bool:
    # full 模式免审批：命令内容不可静态审计，但工作区 cwd 沙盒仍在。
    if _permission_mode == "full":
        console.print(f"[dim]mode=full 已自动批准 {label} 命令。[/dim]")
        return True
    # 通知是纯增强：在阻塞等待用户输入之前弹一条桌面通知，失败静默。
    notifier.notify(f"需要审批: {label}", command[:200])
    console.print(
        f"\n[bold yellow]⚠  智能体请求执行命令:[/bold yellow] "
        f"[bold cyan]{command}[/bold cyan]\n"
        f"[dim]  Shell: {label} | 执行目录: {_ws.get_workspace_str()}[/dim]"
    )
    return Confirm.ask("允许执行吗？")


# run_tests 白名单：extra_args 只允许这些 pytest/unittest 开关。
_TEST_FLAG_ALLOWLIST = ("-k", "-x", "-v", "-q", "-s", "--tb", "-m", "--maxfail",
                        "--junitxml", "--cov", "-vv", "-qvv")
_TEST_META_CHARS = set(";|&<>\r\n\"'`$(){}[]!\\")


def _validate_test_args(target: str, extra_args: str) -> str:
    """校验 run_tests 入参；返回错误消息或空串。"""
    target = (target or ".").strip() or "."
    if os.path.isabs(target) or ".." in target.replace("\\", "/").split("/"):
        return "Error: target 必须是工作区内的相对路径或测试 ID。"
    for ch in (extra_args or ""):
        if ch in _TEST_META_CHARS:
            return f"Error: extra_args 含不允许的字符 {ch!r}；只支持 -k/-x/-v/-q/--tb/-m/--junitxml/--cov 等测试开关。"
    for token in (extra_args or "").split():
        if token.startswith("-") and token not in _TEST_FLAG_ALLOWLIST:
            return f"Error: 不支持的开关 {token!r}；允许：{' '.join(_TEST_FLAG_ALLOWLIST)}"
    return ""


def run_tests(target: str = ".", extra_args: str = "") -> str:
    """
    运行项目测试（pytest 优先，缺失时回退 unittest）。
    ask 模式需确认；workspace/full 免审批——测试代码受写文件闸门保护，
    跑测试不授予超出写文件的能力；工作流纪律要求频繁自测，审批摩擦
    会抑制测试行为。输出末尾优先截断（保留失败摘要）。
    """
    target = (target or ".").strip() or "."
    error = _validate_test_args(target, extra_args)
    if error:
        return error

    if _permission_mode == "ask":
        preview = f"python -m pytest {target} {extra_args}".strip()
        notifier.notify("需要审批: run_tests", preview[:200])
        console.print(f"\n[bold yellow]⚠  智能体请求运行测试:[/bold yellow] [cyan]{preview}[/cyan]")
        if not Confirm.ask("允许运行吗？"):
            return "Refused: 用户拒绝了本次测试运行。"

    # 解释器：优先本会话 venv 的 python（tests 依赖装在那里）。
    venv_python = Path(sys.executable)
    cwd = _ws.get_workspace_str()
    timeout = config.TESTS_TIMEOUT_SECONDS

    def _spawn(argv):
        """运行测试进程；沙盒禁管道（WinError 5）时降级不捕获输出。"""
        try:
            return subprocess.run(
                argv, shell=False, capture_output=True, timeout=timeout,
                stdin=subprocess.DEVNULL, cwd=cwd,
            )
        except PermissionError:
            console.print("[dim]当前环境禁止捕获子进程输出，测试结果已直接打印到终端。[/dim]")
            proc = subprocess.run(
                argv, shell=False, timeout=timeout,
                stdin=subprocess.DEVNULL, cwd=cwd,
            )
            # 降级模式拿不到 stdout/stderr；返回退出码 + 指引。
            return SimpleNamespace(
                returncode=proc.returncode,
                stdout=None, stderr=None,
                passthrough=True,
            )

    # pytest 优先；模块不存在时回退 unittest。
    argv = [str(venv_python), "-m", "pytest", target] + (extra_args.split() if extra_args else [])
    try:
        result = _spawn(argv)
    except subprocess.TimeoutExpired:
        return f"Error: 测试执行超时（{timeout}秒）。可把大套件拆成子目录分批跑。"
    except Exception as ex:
        return f"Error: 测试进程启动失败 - {ex}"

    # pytest 不可用（exit 4 / 'No module named pytest'）→ 回退 unittest。
    if getattr(result, "passthrough", False):
        verdict = "通过" if result.returncode == 0 else "未通过"
        return (
            f"Info: 当前环境禁止捕获测试输出，完整结果已直接打印在终端"
            f"（exit code={result.returncode}，测试{verdict}）。"
            "请据此判断，无需重跑。"
        )
    out = _decode_process_output(result)
    if "No module named pytest" in out:
        argv = [str(venv_python), "-m", "unittest", target] + (extra_args.split() if extra_args else [])
        try:
            result = _spawn(argv)
            out = _decode_process_output(result)
        except subprocess.TimeoutExpired:
            return f"Error: 测试执行超时（{timeout}秒）。"
        except Exception as ex:
            return f"Error: 测试进程启动失败 - {ex}"

    body = out.strip() or "（无输出）"
    limit = config.TESTS_OUTPUT_LIMIT
    if len(body) > limit:
        body = body[:limit // 2] + "\n...(中段截断)...\n" + body[-limit // 2:]
    if result.returncode != 0:
        return f"Error: 测试未通过（exit code={result.returncode}）。\n{body}"
    return f"Success: 测试全部通过。\n{body}"


def run_bash(command: str) -> str:
    """在工作区中以显式 bash -c 执行命令，执行前需人工审批。"""
    executable, err = _find_shell("bash")
    if err:
        return err
    if not _approve_shell("bash", command):
        return "Error: 用户拒绝了该命令的执行。"
    return _execute_shell("bash", executable, command)


def run_pwsh(command: str) -> str:
    """在工作区中以 PowerShell 7 执行命令，执行前需人工审批。"""
    executable, err = _find_shell("pwsh")
    if err:
        return err
    if not _approve_shell("PowerShell 7", command):
        return "Error: 用户拒绝了该命令的执行。"
    return _execute_shell("pwsh", executable, command)


def _validate_glob_pattern(pattern: str) -> str | None:
    """校验工具传入的 glob，防止绝对路径或跳出工作区。"""
    if not pattern:
        return ""
    normalized = pattern.replace("\\", "/").strip()
    if not normalized:
        return ""
    if os.path.isabs(pattern) or normalized.startswith("/"):
        return None
    if any(part == ".." for part in normalized.split("/")):
        return None
    return normalized


def glob_files(path: str = ".", pattern: str = "**/*", limit: int = None) -> str:
    """按 glob 查找工作区内的文件，返回相对路径、大小和修改时间。"""
    resolved, err = _resolve(path or ".")
    if err:
        return err
    if not os.path.isdir(resolved):
        return f"Error: 目录 '{path}' 不存在。"
    normalized = _validate_glob_pattern(pattern or "**/*")
    if normalized is None:
        return "Error: glob 模式必须是工作区内的相对模式，不能包含绝对路径或 '..'。"

    if limit is None:
        max_results = config.GLOB_MAX_RESULTS
    else:
        try:
            max_results = max(1, min(int(limit), 1000))
        except (TypeError, ValueError):
            return f"Error: limit 必须是正整数，收到 {limit!r}。"

    try:
        matches = _glob.glob(
            os.path.join(resolved, normalized or "**/*"),
            recursive=True,
        )
        ws_root = _ws.get_workspace_str()
        root_real = os.path.realpath(resolved)
        rows = []
        for match in matches:
            if not os.path.isfile(match):
                continue
            rel = os.path.relpath(match, resolved)
            normalized_rel = rel.replace(os.sep, "/")
            if any(part in _EXCLUDE_DIRS or part.startswith(".") for part in normalized_rel.split("/")):
                continue
            if any(normalized_rel.endswith(ext) for ext in _EXCLUDE_EXTS):
                continue
            # 防止符号链接把真实路径带到工作区之外。
            if os.path.commonpath([os.path.realpath(match), root_real]) != root_real:
                continue
            try:
                stat = os.stat(match)
            except OSError:
                continue
            display_rel = os.path.relpath(match, ws_root)
            rows.append(
                f"{display_rel}  ({stat.st_size} B, "
                f"{datetime.datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M')})"
            )
    except Exception as ex:
        return f"Error: glob 查找异常 - {ex}"

    rows.sort()
    if not rows:
        return f"未找到匹配 '{pattern}' 的文件（目录: {path}）。"
    shown = rows[:max_results]
    output = "\n".join(shown)
    if len(rows) > max_results:
        output += f"\n\n...(共 {len(rows)} 个，已显示前 {max_results} 个；可缩小 pattern 或用 limit 调整)"
    return f"匹配 '{pattern}'：{len(rows)} 个文件\n{output}"


def _find_rg() -> str | None:
    """返回 ripgrep 可执行文件；测试可替换此函数。"""
    return shutil.which("rg")


def _glob_matches(rel_path: str, basename: str, pattern: str) -> bool:
    if not pattern:
        return True
    if "/" not in pattern:
        return fnmatch.fnmatch(basename, pattern)
    if fnmatch.fnmatch(rel_path, pattern):
        return True
    if pattern.startswith("**/"):
        return fnmatch.fnmatch(rel_path, pattern[3:])
    return False


def _search_with_rg(
    keyword: str,
    resolved_dir: str,
    glob_pattern: str,
    max_results: int,
) -> str | None:
    """优先用 ripgrep 搜索；rg 不存在/不可执行时返回 None 让调用方回退。"""
    rg = _find_rg()
    if not rg:
        return None
    args = [
        rg, "-n", "-F", "--no-heading", "--color", "never",
        "--max-columns", "240", "--max-columns-preview",
    ]
    if glob_pattern:
        args += ["--glob", glob_pattern]
    args += ["--", keyword, "."]
    try:
        result = subprocess.run(
            args, shell=False, capture_output=True,
            timeout=config.SEARCH_TIMEOUT_SECONDS,
            stdin=subprocess.DEVNULL, cwd=resolved_dir,
        )
    except FileNotFoundError:
        return None
    except subprocess.TimeoutExpired:
        return f"Error: 搜索超时（{config.SEARCH_TIMEOUT_SECONDS}秒），请缩小目录或 glob 范围。"
    except OSError:
        return None
    except Exception as ex:
        return f"Error: ripgrep 搜索异常 - {ex}"

    output = _decode_process_output(result).strip()
    if result.returncode == 1:
        return f"未找到包含关键字 '{keyword}' 的内容。"
    if result.returncode != 0:
        return f"Error: ripgrep 搜索失败（exit code={result.returncode}）：\n{output or '无输出'}"
    lines = output.splitlines()
    shown = lines[:max_results]
    text = "\n".join(shown)
    if len(lines) > max_results:
        text += f"\n\n...(共 {len(lines)} 条，已显示前 {max_results} 条；可用更精确的关键字)"
    return f"搜索完成（ripgrep），共 {len(lines)} 条结果：\n{text}"


def _search_python(
    keyword: str,
    resolved_dir: str,
    glob_pattern: str,
    max_results: int,
) -> str:
    """rg 不可用时的纯 Python 回退，保持相同的关键字/glob 语义。"""
    ws_root = _ws.get_workspace_str()
    results = []
    total = 0
    try:
        for root, dirs, files in os.walk(resolved_dir):
            dirs[:] = [d for d in dirs if d not in _EXCLUDE_DIRS and not d.startswith('.')]
            for file in files:
                if any(file.endswith(ext) for ext in _EXCLUDE_EXTS):
                    continue
                file_path = os.path.join(root, file)
                rel = os.path.relpath(file_path, ws_root)
                if not _glob_matches(rel.replace(os.sep, "/"), file, glob_pattern):
                    continue
                try:
                    with open(file_path, 'r', encoding='utf-8') as f:
                        for line_idx, line in enumerate(f):
                            if keyword in line:
                                total += 1
                                if len(results) >= max_results:
                                    continue
                                display = line.strip()
                                if len(display) > 120:
                                    display = display[:120] + "..."
                                results.append(f"{rel}:{line_idx + 1} | {display}")
                except (UnicodeDecodeError, PermissionError):
                    pass
    except Exception as ex:
        return f"Error: 搜索异常 - {ex}"

    if total == 0:
        return f"未找到包含关键字 '{keyword}' 的内容（搜索目录: {resolved_dir}）。"
    output = "\n".join(results)
    if total > max_results:
        output += f"\n\n...(共 {total} 条，已显示前 {max_results} 条；可用更精确的关键字)"
    return f"搜索完成（Python 回退），共 {total} 条结果：\n{output}"


def search_in_files(
    keyword: str,
    directory: str = ".",
    glob: str = None,
    max_results: int = None,
) -> str:
    """在工作区内搜索包含关键字的代码行，优先使用 ripgrep。"""
    if not keyword:
        return "Error: keyword 不能为空。"
    resolved_dir, err = _resolve(directory or ".")
    if err:
        return err
    if not os.path.isdir(resolved_dir):
        return f"Error: 目录 '{directory}' 不存在。"
    glob_pattern = _validate_glob_pattern(glob or "")
    if glob_pattern is None:
        return "Error: glob 模式必须是工作区内的相对模式，不能包含绝对路径或 '..'。"
    if max_results is None:
        limit = config.SEARCH_MAX_RESULTS
    else:
        try:
            limit = max(1, min(int(max_results), 500))
        except (TypeError, ValueError):
            return f"Error: max_results 必须是正整数，收到 {max_results!r}。"

    rg_result = _search_with_rg(keyword, resolved_dir, glob_pattern, limit)
    if rg_result is not None:
        return rg_result
    return _search_python(keyword, resolved_dir, glob_pattern, limit)


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


SNAPSHOT_MSG = "[hqin-snapshot] 任务前快照"


def task_snapshot() -> str:
    """
    任务开始前的自动 git 快照：仓库脏（有改动）时 add -A + commit。
    非仓库 / 仓库干净 / git 失败 → 静默跳过，返回提示字符串。
    成功返回空串。不打扰用户：快照失败不阻塞任务。
    """
    try:
        code, _ = _git_run(["rev-parse", "--is-inside-work-tree"])
        if code != 0:
            return "非 git 仓库，跳过快照"
        code, status = _git_run(["status", "--porcelain"])
        if code != 0 or not status:
            return "仓库干净，无需快照"
        _git_run(["add", "-A"], timeout=30)
        code, out = _git_run([
            "-c", "user.name=HQin Snapshot", "-c", "user.email=hqin@local",
            "commit", "-m", SNAPSHOT_MSG, "--allow-empty",
        ], timeout=60)
        if code != 0:
            return f"快照提交失败: {out[:200]}"
        return ""
    except Exception as ex:
        return f"快照异常（已跳过）: {ex}"


def undo_last_snapshot() -> str:
    """
    /undo：git reset --hard HEAD~1 回滚最近一次任务前快照之后的全部改动。
    校验 HEAD 确实是快照提交，避免误回滚用户自己的提交。
    """
    try:
        code, _ = _git_run(["rev-parse", "--is-inside-work-tree"])
        if code != 0:
            return "非 git 仓库，没有可回滚的快照。"
        code, subject = _git_run(["log", "-1", "--pretty=%s"])
        if code != 0 or subject.strip() != SNAPSHOT_MSG:
            return (
                "最近一次提交不是 HQin 快照（可能已被 git_commit 推进），"
                "无法安全回滚；请用 git 手动处理。"
            )
        code, changed = _git_run(["status", "--porcelain"])
        _git_run(["reset", "--hard", "HEAD~1"], timeout=60)
        summary = f"已回滚到快照之前；丢弃 {len(changed.splitlines())} 个文件的改动。"
        return f"Success: {summary}"
    except Exception as ex:
        return f"Error: 回滚失败 - {ex}"


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
    # full 模式免审批：git add -A 影响整个工作区，但提交仍只作用于仓库内。
    if _permission_mode == "full":
        console.print(f"[dim]mode=full 已自动批准 Git 提交。[/dim]")
    else:
        # 进入阻塞确认前先弹桌面通知，失败静默，不影响审批语义。
        notifier.notify("需要审批: git_commit", preview[:200])
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


_SEARCH_NOISE_WORDS = (
    "官方网站", "官方文档", "官网", "网站", "首页", "网址", "入口", "登录",
)


def _clean_search_query(query: str) -> str:
    """去掉“官网”等泛词，避免 Bing 把查询改写成无关结果。"""
    cleaned = query
    for word in _SEARCH_NOISE_WORDS:
        cleaned = cleaned.replace(word, " ")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or query.strip()


def _fetch_bing_html(query: str, timeout: float = 8.0) -> str:
    """抓取 Bing 国内站的结果页；国内网络通常比 DuckDuckGo 稳定。"""
    params = urllib.parse.urlencode({
        "q": query,
        "setlang": "zh-CN",
        "cc": "CN",
        "count": "20",
    })
    req = urllib.request.Request(
        f"https://cn.bing.com/search?{params}",
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0 Safari/537.36"
            ),
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _parse_bing_results(body: str) -> list[dict]:
    """从 Bing 结果页提取标题、链接和摘要。"""
    results = []
    pattern = re.compile(
        r'<h2[^>]*>\s*<a[^>]+href="(https?://[^"]+)"[^>]*>(.*?)</a>',
        re.S | re.I,
    )
    for match in pattern.finditer(body):
        url = _html.unescape(match.group(1)).strip()
        if not url or "bing.com" in url:
            continue
        title = _html.unescape(re.sub(r"<[^>]+>", "", match.group(2))).strip()
        title = re.sub(r"\s+", " ", title)
        if not title:
            continue

        snippet = ""
        tail = body[match.end(): match.end() + 2500]
        snippet_match = re.search(
            r'<p class="b_lineclamp[^"]*"[^>]*>(.*?)</p>',
            tail,
            re.S | re.I,
        )
        if snippet_match:
            snippet = _html.unescape(
                re.sub(r"<[^>]+>", "", snippet_match.group(1))
            ).strip()
            snippet = re.sub(r"\s+", " ", snippet)

        results.append({"title": title, "url": url, "snippet": snippet})
    return results


def _rank_search_results(results: list[dict], query: str) -> list[dict]:
    """去重，并优先展示学校/政府等可信域名和命中关键词的结果。"""
    seen = set()
    unique = []
    for item in results:
        key = item["url"].rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)

    tokens = [t for t in re.split(r"\s+", query) if len(t) >= 2]

    def score(item: dict) -> int:
        url = item["url"].lower()
        text = item["title"] + " " + item.get("snippet", "")
        value = 0
        if ".edu.cn" in url:
            value += 100
        if "nju.edu.cn" in url:
            value += 50
        if ".gov.cn" in url:
            value += 10
        if "baike.baidu.com" in url:
            value -= 15
        if "zhihu.com" in url:
            value -= 10
        for token in tokens:
            if token in text or token in url:
                value += 20
        return value

    unique.sort(key=lambda item: -score(item))
    return unique


def web_search(query: str) -> str:
    """
    使用 Bing 国内站搜索网络内容（无需 API Key）。
    适用于查询官网、库文档、错误信息、API 用法等。
    """
    cleaned = _clean_search_query(query)
    queries = [query] if cleaned == query else [query, cleaned]

    results = []
    errors = []
    for q in queries:
        try:
            results.extend(_parse_bing_results(_fetch_bing_html(q)))
        except Exception as ex:
            errors.append(f"Bing({q}): {ex}")

    results = _rank_search_results(results, cleaned)
    if results:
        lines = [f"🔍 Bing 搜索: {query}"]
        if cleaned != query:
            lines.append(f"（同时检索了更宽泛的关键词: {cleaned}）")
        for i, item in enumerate(results[:config.WEB_SEARCH_MAX], 1):
            lines.append(f"{i}. {item['title']}\n   {item['url']}")
            if item["snippet"]:
                lines.append(f"   {item['snippet'][:200]}")
        return "\n\n".join(lines)

    # Bing 不可用时保留 DuckDuckGo 兜底；国内网络下通常用不到。
    fallback = _web_search_duckduckgo(query)
    if errors and fallback.startswith("Error:"):
        return fallback + "\nBing 失败原因: " + "; ".join(errors)
    return fallback


def _web_search_duckduckgo(query: str) -> str:
    """
    DuckDuckGo 兜底搜索（无需 API Key）。
    适用于 Bing 不可用或没有返回结果的网络环境。
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
# 后台网页浏览（BrowserAct）
# ──────────────────────────────────────────────

def _validate_web_url(url: str) -> str:
    """只允许普通 HTTP(S) URL，避免把本地文件或其他协议交给浏览器。"""
    try:
        parsed = urllib.parse.urlparse(url)
    except (TypeError, ValueError):
        return "Error: URL 格式无效。"
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return "Error: 仅支持完整的 http:// 或 https:// URL。"
    return ""


def _decode_process_output(result: subprocess.CompletedProcess) -> str:
    """兼容 BrowserAct 与 Windows 命令行的 UTF-8/GBK 输出。"""
    output = result.stdout + result.stderr
    if isinstance(output, str):
        return output
    try:
        return output.decode("utf-8")
    except UnicodeDecodeError:
        return output.decode("gbk", errors="replace")


def _browser_run(args: list[str], timeout: int | None = None) -> tuple[int, str]:
    """以参数数组运行 BrowserAct，永不通过 shell 解释 URL 或页面参数。"""
    try:
        result = subprocess.run(
            [config.BROWSER_ACT_COMMAND, *args],
            shell=False,
            capture_output=True,
            timeout=timeout or config.BROWSER_TIMEOUT_SECONDS,
            stdin=subprocess.DEVNULL,
        )
        return result.returncode, _decode_process_output(result).strip()
    except FileNotFoundError:
        return 127, (
            "未找到 browser-act。请先安装 BrowserAct CLI，或设置 "
            "HQIN_BROWSER_ACT_COMMAND 指向可执行文件。"
        )
    except subprocess.TimeoutExpired:
        return 124, f"BrowserAct 操作超时（{timeout or config.BROWSER_TIMEOUT_SECONDS}秒）。"
    except Exception as ex:
        return 1, f"BrowserAct 执行异常：{ex}"


def _browser_result(action: str, args: list[str], limit: int = None) -> str:
    code, output = _browser_run(args)
    if code != 0:
        return f"Error: BrowserAct {action} 失败（exit code={code}）。\n{output}"
    if limit and len(output) > limit:
        output = output[:limit] + f"\n...(输出过长，已截断，共 {len(output)} 字符)"
    return output or f"Success: BrowserAct {action} 完成。"


def _require_browser_session() -> str:
    if not config.BROWSER_ID:
        return (
            f"{browser_policy.CONFIG_REQUIRED_PREFIX} 未配置 HQIN_BROWSER_ID，"
            "交互浏览不可用。请先选择一个已有的 BrowserAct 浏览器 ID，"
            "设置环境变量后重启 Harness；web_fetch_url 仍可用于只读浏览。"
        )
    if not _browser_policy.active:
        return (
            "Error: 当前没有活动的后台浏览器会话。请先调用 browser_open(url)。"
        )
    return ""


def web_fetch_url(url: str) -> str:
    """后台提取公开网页的 JavaScript 渲染 Markdown；不创建浏览器会话或保留 Cookie。"""
    error = _validate_web_url(url)
    if error:
        return error
    return _browser_result(
        "只读网页提取",
        ["stealth-extract", url, "--content-type", "markdown"],
        config.WEB_FETCH_OUTPUT_LIMIT,
    )


def browser_open(url: str) -> str:
    """在后台打开网页并创建本次 Harness 专属 BrowserAct 会话。"""
    if not config.BROWSER_ID:
        return (
            f"{browser_policy.CONFIG_REQUIRED_PREFIX} 未配置 HQIN_BROWSER_ID，"
            "不能使用交互浏览。请先选择一个已有的 BrowserAct 浏览器 ID，"
            "设置环境变量后重启 Harness；web_fetch_url 仍可用于只读浏览。"
        )
    error = _validate_web_url(url)
    if error:
        return error
    if _browser_policy.active:
        return (
            "Error: 已有活动的后台浏览器会话。请先调用 browser_close，"
            "或使用 browser_navigate 在当前会话内跳转。"
        )
    result = _browser_result(
        "打开后台浏览器",
        ["--session", config.BROWSER_SESSION, "browser", "open", config.BROWSER_ID, url],
    )
    if not result.startswith(browser_policy.ERROR_PREFIX):
        _browser_policy.on_open(url)
    return result


def browser_state() -> str:
    """列出当前页面可交互元素及编号；页面变化后必须重新调用，不能复用旧编号。"""
    error = _require_browser_session()
    if error:
        return error
    result = _browser_result(
        "读取页面元素",
        ["--session", config.BROWSER_SESSION, "state"],
        config.WEB_FETCH_OUTPUT_LIMIT,
    )
    if not result.startswith(browser_policy.ERROR_PREFIX):
        _browser_policy.on_state(result)
    return result


def browser_click(index: int) -> str:
    """经人工确认后点击当前 state 中的元素，并等待页面稳定。仅用于导航或分页。"""
    error = _require_browser_session()
    if error:
        return error
    if not isinstance(index, int) or index < 1:
        return "Error: index 必须是 browser_state 返回的正整数元素编号。"
    blocked = _browser_policy.click_block_reason(index)
    if blocked:
        return blocked
    console.print(
        f"\n[bold yellow]⚠ 智能体请求点击后台网页元素:[/bold yellow] "
        f"[cyan]#{index}[/cyan]（会话: {config.BROWSER_SESSION}）"
    )
    auto = config.BROWSER_AUTO_APPROVE_CLICKS or _permission_mode == "full"
    if not auto:
        # 只在真正要阻塞等用户确认时通知；自动导航/full 模式不需要打扰。
        notifier.notify(
            "需要审批: browser_click",
            f"点击元素 #{index}（会话: {config.BROWSER_SESSION}）",
        )
    if auto:
        console.print("[dim]已自动批准网页点击（自动导航或 full 模式）。[/dim]")
    else:
        if not Confirm.ask("允许点击吗？"):
            return f"{browser_policy.REFUSED_PREFIX} 用户拒绝了该网页点击操作。"
    # 一旦真正派发点击，无论成功与否都作废旧编号，要求重新读取 state。
    _browser_policy.on_click_dispatched()
    clicked = _browser_result(
        "点击页面元素",
        ["--session", config.BROWSER_SESSION, "click", str(index)],
    )
    if clicked.startswith(browser_policy.ERROR_PREFIX):
        return clicked
    stabilized = _browser_result(
        "等待页面稳定",
        ["--session", config.BROWSER_SESSION, "wait", "stable"],
    )
    if stabilized.startswith(browser_policy.ERROR_PREFIX):
        return f"{clicked}\n{stabilized}"
    return f"{clicked}\n页面已稳定；请重新调用 browser_state 获取新的元素编号。"


def browser_get_markdown() -> str:
    """提取当前后台页面为 Markdown。"""
    error = _require_browser_session()
    if error:
        return error
    return _browser_result(
        "提取页面 Markdown",
        ["--session", config.BROWSER_SESSION, "get", "markdown"],
        config.WEB_FETCH_OUTPUT_LIMIT,
    )


def browser_navigate(url: str) -> str:
    """在当前后台会话中访问另一个 URL；导航后需重新读取页面 state。"""
    error = _require_browser_session() or _validate_web_url(url)
    if error:
        return error
    result = _browser_result(
        "页面导航",
        ["--session", config.BROWSER_SESSION, "navigate", url],
    )
    # 导航失败也可能留下部分页面变化，保守地一律作废旧编号。
    _browser_policy.on_navigate(url)
    return result


def browser_close() -> str:
    """关闭本次 Harness 创建的后台浏览器会话并释放资源。"""
    if not config.BROWSER_ID:
        return (
            f"{browser_policy.CONFIG_REQUIRED_PREFIX} 未配置 HQIN_BROWSER_ID，"
            "没有可关闭的交互会话。"
        )
    if not _browser_policy.active:
        return "Info: 当前没有活动的后台浏览器会话，无需关闭。"
    result = _browser_result("关闭后台浏览器", ["session", "close", config.BROWSER_SESSION])
    _browser_policy.reset()
    return result


def close_browser_session_if_active() -> str | None:
    """任务结束/进程退出时兜底关闭会话，避免后台浏览器残留。"""
    if not _browser_policy.active:
        return None
    code, output = _browser_run(
        ["session", "close", config.BROWSER_SESSION],
        timeout=15,
    )
    _browser_policy.reset()
    if code != 0:
        return f"Warning: 自动关闭后台浏览器会话失败（exit code={code}）。\n{output}"
    return output or "后台浏览器会话已自动关闭。"


# ──────────────────────────────────────────────
# 工具注册表：名称 → 函数（供 main.py 路由使用）
# ──────────────────────────────────────────────

TOOL_REGISTRY = {
    "run_bash":        run_bash,
    "run_pwsh":        run_pwsh,
    "run_tests":       run_tests,
    "read_file":       read_file,
    "write_file":      write_file,
    "append_file":     append_file,
    "finish_check":    finish_check,
    "create_docx":     create_docx,
    "edit_file":       edit_file,
    "search_in_files": search_in_files,
    "glob_files":      glob_files,
    "list_dir":        list_dir,
    "git_status":      git_status,
    "git_diff":        git_diff,
    "git_commit":      git_commit,
    "web_search":      web_search,
    "web_fetch_url":   web_fetch_url,
    "browser_open":    browser_open,
    "browser_state":   browser_state,
    "browser_click":   browser_click,
    "browser_get_markdown": browser_get_markdown,
    "browser_navigate": browser_navigate,
    "browser_close":   browser_close,
    "context_status":  memory.context_status,
    "compress_context": memory.compress_context,
    "decompress_context": memory.decompress_context,
    "search_context":  memory.search_context,
}
