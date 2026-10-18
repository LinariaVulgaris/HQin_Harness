# workspace.py  —  工作区（沙盒）管理
"""
统一管理 Agent 的工作区路径。
所有文件操作都必须通过 resolve_path() 校验，确保不越界。
"""
import os
from pathlib import Path

# 全局工作区根路径（启动时由 main.py 调用 set_workspace() 设置）
_workspace: Path | None = None


def set_workspace(path: str) -> str:
    """
    设置工作区根目录。
    - 如果目录不存在，自动创建。
    - 返回标准化的绝对路径字符串。
    """
    global _workspace
    p = Path(path).resolve()
    if not p.exists():
        p.mkdir(parents=True, exist_ok=True)
    if not p.is_dir():
        raise ValueError(f"'{path}' 不是一个有效目录。")
    _workspace = p
    # 同步修改进程工作目录，使 run_bash 的相对路径也生效
    os.chdir(_workspace)
    return str(_workspace)


def get_workspace() -> Path:
    """返回当前工作区 Path 对象。未设置时抛出 RuntimeError。"""
    if _workspace is None:
        raise RuntimeError("工作区尚未初始化，请先调用 set_workspace()。")
    return _workspace


def get_workspace_str() -> str:
    """返回当前工作区绝对路径字符串。"""
    return str(get_workspace())


def resolve_path(user_path: str) -> str:
    """
    将用户（或模型）提供的路径解析为工作区内的绝对路径。

    规则：
      - 相对路径 → 相对于工作区根目录解析
      - 绝对路径 → 检查是否位于工作区内，否则拒绝

    返回：解析后的绝对路径字符串
    抛出：ValueError（路径越界时）
    """
    ws = get_workspace()
    p = Path(user_path)

    if p.is_absolute():
        resolved = p.resolve()
    else:
        resolved = (ws / p).resolve()

    # 安全检查：resolved 必须是 workspace 的子路径
    try:
        resolved.relative_to(ws)
    except ValueError:
        raise ValueError(
            f"沙盒越界！路径 '{user_path}' 解析为 '{resolved}'，"
            f"超出工作区 '{ws}' 范围。\n"
            f"Agent 只能操作工作区内的文件，请使用相对路径。"
        )

    return str(resolved)


def is_within_workspace(path: str) -> bool:
    """检查路径是否在工作区内（不抛异常版本）。"""
    try:
        resolve_path(path)
        return True
    except (ValueError, RuntimeError):
        return False
