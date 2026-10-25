# browser_policy.py
"""浏览器操作规范：提示词、工具过滤与会话状态机的唯一来源。

设计原则：
- 提示词与代码约束来自同一份定义，避免两处规则随迭代漂移。
- 未配置 HQIN_BROWSER_ID 时，交互工具直接从模型工具列表中移除；运行时仍保留配置提示兜底。
- 点击顺序由代码状态机保证，不依赖模型自觉遵守。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

import config

CONFIG_REQUIRED_PREFIX = "ConfigurationRequired:"
REFUSED_PREFIX = "Refused:"
ERROR_PREFIX = "Error:"

INTERACTIVE_TOOLS = frozenset({
    "browser_open",
    "browser_state",
    "browser_click",
    "browser_get_markdown",
    "browser_navigate",
    "browser_close",
})

# 英文敏感动作使用词边界，避免把 display 之类的普通单词误判。
_SENSITIVE_EN = re.compile(
    r"\b(?:log\s*in|sign\s*in|sign\s*up|signup|password|passwd|checkout|"
    r"payment|purchase|buy\s+now|upload|download|delete|remove\s+account|"
    r"submit|confirm\s+order|pay)\b",
    re.IGNORECASE,
)
_SENSITIVE_ZH = (
    "登录", "登陆", "注册", "支付", "付款", "购买", "下单", "结算",
    "提交", "上传", "下载", "删除", "注销", "退出登录", "确认订单",
)

_ELEMENT_RE = re.compile(r"\[(\d+)\]\s*(.*?)(?=\s*\[\d+\]|\n|\Z)", re.S)
_TRUNCATION_MARKER = "输出过长，已截断"


def interactive_enabled() -> bool:
    return bool(config.BROWSER_ID)


def filter_tool_schemas(schemas, enabled: bool | None = None):
    """未启用交互浏览时，从模型可见的工具列表中移除全部 browser_* 工具。"""
    if enabled is None:
        enabled = interactive_enabled()
    if enabled:
        return schemas
    return [
        schema for schema in schemas
        if schema.get("function", {}).get("name") not in INTERACTIVE_TOOLS
    ]


def prompt_block() -> str:
    if interactive_enabled():
        status = "已启用（操作者已配置 HQIN_BROWSER_ID）"
    else:
        status = "未启用（未配置 HQIN_BROWSER_ID）；不要调用任何 browser_* 工具"
    return f"""【网页浏览规范】
- 交互浏览状态：{status}
- 只读阅读优先使用 web_fetch_url；只有需要点击、翻页或展开内容时才创建交互会话。
- 交互流程固定为 browser_open → browser_state → browser_click → browser_state。browser_click 会自动等待页面稳定，但不会刷新元素编号。
- 页面发生变化（browser_open / browser_click / browser_navigate）后，旧编号立即失效，必须重新调用 browser_state，禁止猜测或复用。
- 只点击链接、分页、展开/收起等导航元素。禁止登录、注册、填写或提交表单、支付、购买、上传、下载、删除，禁止绕过验证码。
- 任务结束后调用 browser_close；Harness 也会在任务结束时自动关闭本次会话。"""


class BrowserPhase(str, Enum):
    CLOSED = "closed"
    OPENED = "opened"
    READY = "ready"
    STALE = "stale"


def extract_element_labels(state_text: str) -> dict[int, str]:
    """从 BrowserAct state 文本中提取编号与元素描述，用于敏感动作拦截。

    解析失败时返回空字典；状态机不会因此阻断点击，仍由人工确认兜底。
    """
    labels: dict[int, str] = {}
    for match in _ELEMENT_RE.finditer(state_text or ""):
        index = int(match.group(1))
        label = match.group(2).strip().rstrip(",").strip()
        labels[index] = label
    return labels


def is_sensitive_label(label: str) -> bool:
    if not label:
        return False
    if _SENSITIVE_EN.search(label):
        return True
    return any(word in label for word in _SENSITIVE_ZH)


@dataclass
class BrowserSessionPolicy:
    """代码层会话状态机：click 只能发生在最近一次 state 之后。"""

    phase: BrowserPhase = BrowserPhase.CLOSED
    current_url: str = ""
    last_state: str = ""
    state_reads: int = 0

    @property
    def active(self) -> bool:
        return self.phase is not BrowserPhase.CLOSED

    def reset(self) -> None:
        self.phase = BrowserPhase.CLOSED
        self.current_url = ""
        self.last_state = ""
        self.state_reads = 0

    def on_open(self, url: str) -> None:
        self.reset()
        self.phase = BrowserPhase.OPENED
        self.current_url = url

    def on_state(self, state_text: str) -> None:
        self.last_state = state_text
        self.state_reads += 1
        self.phase = BrowserPhase.READY

    def on_click_dispatched(self) -> None:
        # 点击可能跳转，也可能原地展开；两种情况都不能复用旧编号。
        self.phase = BrowserPhase.STALE
        self.last_state = ""

    def on_navigate(self, url: str) -> None:
        self.current_url = url
        self.phase = BrowserPhase.STALE
        self.last_state = ""

    def click_block_reason(self, index: int) -> str | None:
        if not self.active:
            return "Error: 当前没有活动的后台浏览器会话。请先调用 browser_open(url)。"
        if self.phase is not BrowserPhase.READY:
            return (
                "Error: 尚未读取当前页面 state，不能点击。"
                "请先调用 browser_state，并使用它返回的编号。"
            )
        labels = extract_element_labels(self.last_state)
        # state 被截断时，看不到的编号可能只是没显示出来，不能据此判定为旧编号。
        if (
            labels
            and index not in labels
            and _TRUNCATION_MARKER not in self.last_state
        ):
            return (
                f"Error: 编号 #{index} 不在最近一次 browser_state 输出中。"
                "请重新调用 browser_state，禁止复用旧编号。"
            )
        label = labels.get(index, "")
        if is_sensitive_label(label):
            return (
                f"{REFUSED_PREFIX} 元素 #{index}（{label}）看起来是登录、表单提交、"
                "支付、上传/下载或删除操作，已按浏览规范拦截。"
                "请改用 web_fetch_url 阅读，或让用户手动操作。"
            )
        return None
