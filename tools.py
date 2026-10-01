# tools.py
import os
import subprocess
import ast  # 用于解析 Python 语法树
from rich.console import Console
from rich.prompt import Confirm

console = Console()

def check_python_syntax(file_path: str, code_content: str) -> str:
    """检查 Python 代码语法是否正确 (仅针对 .py 文件)"""
    if not file_path.endswith('.py'):
        return "" # 非 Python 文件不检查
    try:
        ast.parse(code_content)
        return ""
    except SyntaxError as e:
        # 捕获语法错误并格式化
        error_msg = f"SyntaxError: 第 {e.lineno} 行存在语法错误: {e.msg}"
        if e.text:
            error_msg += f"\n错误代码: {e.text.strip()}"
        return error_msg

def read_file(path: str) -> str:
    """读取文件内容，并在每一行前面加上行号，方便模型精确定位"""
    if not os.path.exists(path):
        return f"Error: 文件 {path} 不存在。请使用 run_bash 执行 ls 查看当前目录文件。"
    try:
        with open(path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        # 补齐 4 位行号，例如 0012 | def main():
        content_with_lines = "".join([f"{i + 1:04d} | {line}" for i, line in enumerate(lines)])
        return f"文件 {path} 读取成功，内容如下:\n{content_with_lines}"
    except Exception as e:
        return f"Error: 读取文件异常 - {e}"


def edit_file(path: str, search_text: str, replace_text: str) -> str:
    """搜索并替换文件中的代码块"""
    if not os.path.exists(path):
        return f"Error: 文件 {path} 不存在。"
    try:
        with open(path, 'r', encoding='utf-8') as f:
            content = f.read()

        if search_text not in content:
            return "Error: 匹配失败！找不到完全对应的 SEARCH 文本。请确保不要包含行号，且缩进与空格与原文件完全一致。"

        # 执行替换 (仅替换第一次出现的地方)
        new_content = content.replace(search_text, replace_text, 1)

        syntax_error = check_python_syntax(path, new_content)
        if syntax_error:
            return f"Error: 替换失败！你修改后的代码存在语法错误，已被系统拦截并撤销。\n具体报错:\n{syntax_error}\n请重新思考并提供正确的缩进和语法。"

        with open(path, 'w', encoding='utf-8') as f:
            f.write(new_content)
        return f"Success: 文件 {path} 代码已成功更新。"
    except Exception as e:
        return f"Error: 编辑文件异常 - {e}"


def run_bash(command: str) -> str:
    """执行 Bash/CMD 命令，带有强人工审批拦截及 Windows 编码兼容"""
    console.print(f"\n[bold yellow]⚠ 智能体请求执行命令:[/bold yellow] [bold cyan]{command}[/bold cyan]")

    if not Confirm.ask("允许执行吗？"):
        return "Error: 用户拒绝了该命令的执行。"

    try:
        # 去掉 text=True，捕获原始 bytes 流，由我们手动安全解码
        result = subprocess.run(
            command, shell=True, capture_output=True, timeout=30, stdin=subprocess.DEVNULL
        )

        # 安全解码函数
        def safe_decode(b_str: bytes) -> str:
            if not b_str: return ""
            try:
                return b_str.decode('utf-8')
            except UnicodeDecodeError:
                # 兼容 Windows CMD 中文环境的 GBK 编码
                return b_str.decode('gbk', errors='replace')

        output = safe_decode(result.stdout) + safe_decode(result.stderr)

        if not output.strip():
            return "Success: 命令执行完毕，无终端输出。"
        return f"执行输出:\n{output}"

    except subprocess.TimeoutExpired:
        return "Error: 命令执行超时 (30秒)。"
    except Exception as e:
        return f"Error: 命令执行崩溃 - {e}"

def write_file(path: str, content: str) -> str:
    """创建新文件并写入内容，带有语法免疫防线"""
    syntax_error = check_python_syntax(path, content)
    if syntax_error:
        return f"Error: 写入失败！你提供的代码存在语法错误，已被系统拦截。\n具体报错:\n{syntax_error}\n请检查缩进和语法后重试。"

    try:
        with open(path, 'w', encoding='utf-8') as f:
            f.write(content)
        return f"Success: 文件 {path} 已成功创建并写入内容。"
    except Exception as e:
        return f"Error: 写入文件异常 - {e}"


def search_in_files(keyword: str, directory: str = ".") -> str:
    """在目录树中全局搜索关键字，返回包含关键字的文件路径和行号"""
    # 忽略常见的非代码目录
    exclude_dirs = {'.git', '.venv', 'venv', '__pycache__', '.idea', 'node_modules'}
    results = []

    try:
        for root, dirs, files in os.walk(directory):
            # 过滤不需要扫描的目录
            dirs[:] = [d for d in dirs if d not in exclude_dirs]

            for file in files:
                # 过滤掉常见的非文本文件后缀
                if file.endswith(('.pyc', '.png', '.jpg', '.exe', '.dll', '.zip')):
                    continue

                file_path = os.path.join(root, file)
                try:
                    with open(file_path, 'r', encoding='utf-8') as f:
                        for line_idx, line in enumerate(f):
                            if keyword in line:
                                # 截断过长的行，防止无用信息过多
                                display_line = line.strip()
                                if len(display_line) > 100:
                                    display_line = display_line[:100] + "..."
                                # 转换为相对路径，看起来更简洁
                                rel_path = os.path.relpath(file_path, directory)
                                results.append(f"{rel_path}:{line_idx + 1} | {display_line}")
                except (UnicodeDecodeError, PermissionError):
                    # 跳过无法以 UTF-8 文本读取的文件（如二进制文件）或无权限文件
                    pass

        if not results:
            return f"未在目录 '{directory}' 中搜索到包含关键字 '{keyword}' 的代码。"

        # 限制返回的最大条目数，防止返回结果过大撑爆上下文
        max_results = 30
        output = "\n".join(results[:max_results])
        if len(results) > max_results:
            output += f"\n\n...(省略了另外 {len(results) - max_results} 条结果，请尝试使用更长的关键字定位)"

        return f"搜索完成，结果如下:\n{output}"
    except Exception as e:
        return f"Error: 搜索异常 - {e}"