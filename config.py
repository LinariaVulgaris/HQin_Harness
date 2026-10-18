# config.py  —  全局配置（所有常量集中于此，支持环境变量覆盖）
"""
优先级：环境变量 > 此文件默认值
示例（PowerShell）:
  $env:HQIN_MODEL = "qwen2.5-7b-instruct"
  python main.py
"""
import os

# ── LLM 设置 ──────────────────────────────────
MODEL    = os.environ.get("HQIN_MODEL",    "qwen3.5-4b")
BASE_URL = os.environ.get("HQIN_BASE_URL", "http://127.0.0.1:1234/v1")
API_KEY  = os.environ.get("HQIN_API_KEY",  "lm-studio")
MODEL_TIMEOUT_SECONDS = float(os.environ.get("HQIN_MODEL_TIMEOUT", "120"))
MODEL_MAX_RETRIES     = int(os.environ.get("HQIN_MODEL_RETRIES", "2"))
MODEL_RETRY_BACKOFF   = float(os.environ.get("HQIN_MODEL_BACKOFF", "1.5"))
MAX_OUTPUT_TOKENS     = int(os.environ.get("HQIN_MAX_OUTPUT_TOKENS", "4096"))

# ── Agent 行为 ─────────────────────────────────
MAX_STEPS      = int(os.environ.get("HQIN_MAX_STEPS",      "15"))

# 上下文管理阈值（基于字符数统计，非 token 数）
# 字符数不能精确换算 token，尤其中文最坏情况接近 1 字符/token。
# 默认值为 LM Studio 设为 32k context 时的保守兜底；生产环境应改用模型 tokenizer 计数。
# 仍需为 system prompt、工具 schema 和模型输出预留空间。
MAX_CHARS      = int(os.environ.get("HQIN_MAX_CHARS",      "24000"))
SUMMARY_THRESH = int(os.environ.get("HQIN_SUMMARY_THRESH", "16000"))
KEEP_RECENT    = int(os.environ.get("HQIN_KEEP_RECENT",    "6"))

# ── 错误重试 ───────────────────────────────────
MAX_CONSECUTIVE_ERRORS = int(os.environ.get("HQIN_MAX_ERRORS", "3"))

# ── 工具行为 ───────────────────────────────────
READ_FILE_PAGE_SIZE = int(os.environ.get("HQIN_PAGE_SIZE",       "150"))  # 超过此行数触发分页提示
BASH_OUTPUT_LIMIT   = int(os.environ.get("HQIN_BASH_LIMIT",     "4000"))
BASH_TIMEOUT_SECONDS = int(os.environ.get("HQIN_BASH_TIMEOUT",  "60"))
DIFF_OUTPUT_LIMIT   = int(os.environ.get("HQIN_DIFF_LIMIT",     "5000"))
SEARCH_MAX_RESULTS  = int(os.environ.get("HQIN_SEARCH_MAX",       "40"))
WEB_SEARCH_MAX      = int(os.environ.get("HQIN_WEB_MAX",           "5"))
MAX_PARALLEL_READ_TOOLS = int(os.environ.get("HQIN_MAX_PARALLEL_READ_TOOLS", "4"))

# ── 会话存档 ───────────────────────────────────
SESSION_DIR         = os.environ.get("HQIN_SESSION_DIR",   ".hqin_sessions")
SESSION_EXPIRE_DAYS = int(os.environ.get("HQIN_SESSION_TTL", "7"))


def print_config():
    """打印当前生效的配置（调试用）。"""
    from rich.console import Console
    from rich.table import Table
    c = Console()
    t = Table(title="HQin Harness — 当前配置", show_header=True, header_style="bold cyan")
    t.add_column("配置项", style="cyan")
    t.add_column("当前值", style="green")
    t.add_column("环境变量", style="dim")
    rows = [
        ("MODEL",              MODEL,              "HQIN_MODEL"),
        ("BASE_URL",           BASE_URL,           "HQIN_BASE_URL"),
        ("MODEL_TIMEOUT_SECONDS", str(MODEL_TIMEOUT_SECONDS), "HQIN_MODEL_TIMEOUT"),
        ("MODEL_MAX_RETRIES",  str(MODEL_MAX_RETRIES), "HQIN_MODEL_RETRIES"),
        ("MAX_OUTPUT_TOKENS",  str(MAX_OUTPUT_TOKENS), "HQIN_MAX_OUTPUT_TOKENS"),
        ("MAX_STEPS",          str(MAX_STEPS),     "HQIN_MAX_STEPS"),
        ("MAX_CHARS",          str(MAX_CHARS),     "HQIN_MAX_CHARS"),
        ("SUMMARY_THRESH",     str(SUMMARY_THRESH),"HQIN_SUMMARY_THRESH"),
        ("KEEP_RECENT",        str(KEEP_RECENT),   "HQIN_KEEP_RECENT"),
        ("MAX_CONSECUTIVE_ERRORS", str(MAX_CONSECUTIVE_ERRORS), "HQIN_MAX_ERRORS"),
        ("READ_FILE_PAGE_SIZE",str(READ_FILE_PAGE_SIZE), "HQIN_PAGE_SIZE"),
        ("BASH_TIMEOUT_SECONDS", str(BASH_TIMEOUT_SECONDS), "HQIN_BASH_TIMEOUT"),
        ("MAX_PARALLEL_READ_TOOLS", str(MAX_PARALLEL_READ_TOOLS), "HQIN_MAX_PARALLEL_READ_TOOLS"),
        ("SESSION_EXPIRE_DAYS",str(SESSION_EXPIRE_DAYS), "HQIN_SESSION_TTL"),
    ]
    for name, val, env in rows:
        t.add_row(name, val, env)
    c.print(t)
