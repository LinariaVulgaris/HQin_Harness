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
# 本地 4B 模型生成 4096 token 可能超过 4 分钟；默认留足时间，避免长工具调用被截断。
MODEL_TIMEOUT_SECONDS = float(os.environ.get("HQIN_MODEL_TIMEOUT", "600"))
MODEL_MAX_RETRIES     = int(os.environ.get("HQIN_MODEL_RETRIES", "2"))
MODEL_RETRY_BACKOFF   = float(os.environ.get("HQIN_MODEL_BACKOFF", "1.5"))
MAX_OUTPUT_TOKENS     = int(os.environ.get("HQIN_MAX_OUTPUT_TOKENS", "4096"))

# ── Agent 行为 ─────────────────────────────────
# 步数预算制：模型完成任务自然停止（无工具调用）是主退出路径；
# MAX_STEPS 是防失控护栏，耗尽时询问用户是否追加 STEP_RENEWAL 步。
MAX_STEPS      = int(os.environ.get("HQIN_MAX_STEPS",      "15"))
STEP_RENEWAL   = int(os.environ.get("HQIN_STEP_RENEWAL",   "10"))

# 权限模式：ask（每次审批）/ workspace（文件写免审，默认）/ full（全免审）。
# 会话内 /mode 可随时切换；非法值回落 ask。
MODE = os.environ.get("HQIN_MODE", "workspace").strip().lower() or "workspace"
if MODE not in ("ask", "workspace", "full"):
    MODE = "ask"

# 上下文管理阈值
# 主路径：memory.py 按 token 估算，65% 提示模型压缩，85% 自动兜底。
# MAX_CHARS / SUMMARY_THRESH 保留为字符数硬兜底，默认值已放宽，避免在 token
# 预算尚未紧张时提前触发旧的自动摘要。
MODEL_CONTEXT_TOKENS = int(os.environ.get("HQIN_CONTEXT_TOKENS", "32768"))
CONTEXT_SOFT_RATIO   = float(os.environ.get("HQIN_CONTEXT_SOFT", "0.65"))
CONTEXT_HARD_RATIO   = float(os.environ.get("HQIN_CONTEXT_HARD", "0.85"))
MAX_CHARS      = int(os.environ.get("HQIN_MAX_CHARS",      "64000"))
SUMMARY_THRESH = int(os.environ.get("HQIN_SUMMARY_THRESH", "16000"))
KEEP_RECENT    = int(os.environ.get("HQIN_KEEP_RECENT",    "6"))

# ── 可逆记忆（memory.py） ──────────────────────
MEMORY_DIR               = os.environ.get("HQIN_MEMORY_DIR", ".hqin_memory")
MEMORY_KEEP_RECENT_TURNS = int(os.environ.get("HQIN_MEMORY_KEEP_TURNS", "2"))
MEMORY_SUMMARY_MAX_CHARS = int(os.environ.get("HQIN_MEMORY_SUMMARY_CHARS", "3000"))
MEMORY_SEARCH_LIMIT      = int(os.environ.get("HQIN_MEMORY_SEARCH_LIMIT", "3"))
MEMORY_DECOMPRESS_LIMIT  = int(os.environ.get("HQIN_MEMORY_DECOMPRESS_LIMIT", "12000"))

# ── 错误重试 ───────────────────────────────────
MAX_CONSECUTIVE_ERRORS = int(os.environ.get("HQIN_MAX_ERRORS", "3"))

# ── 工具行为 ───────────────────────────────────
READ_FILE_PAGE_SIZE = int(os.environ.get("HQIN_PAGE_SIZE",       "150"))  # 超过此行数触发分页提示
READ_OUTPUT_LIMIT   = int(os.environ.get("HQIN_READ_LIMIT",    "12000"))  # 单次 read_file 的字符预算
BASH_EXECUTABLE     = os.environ.get("HQIN_BASH", "").strip()
PWSH_EXECUTABLE     = os.environ.get("HQIN_PWSH", "").strip()
BASH_OUTPUT_LIMIT   = int(os.environ.get("HQIN_BASH_LIMIT",     "4000"))
SHELL_OUTPUT_LIMIT  = int(os.environ.get("HQIN_SHELL_LIMIT", str(BASH_OUTPUT_LIMIT)))
BASH_TIMEOUT_SECONDS = int(os.environ.get("HQIN_BASH_TIMEOUT",  "60"))
PWSH_TIMEOUT_SECONDS = int(os.environ.get("HQIN_PWSH_TIMEOUT",  "60"))
# run_tests 独立超时：测试套件天然比普通命令长。
TESTS_TIMEOUT_SECONDS = int(os.environ.get("HQIN_TESTS_TIMEOUT", "300"))
TESTS_OUTPUT_LIMIT   = int(os.environ.get("HQIN_TESTS_LIMIT",   "6000"))
SEARCH_TIMEOUT_SECONDS = int(os.environ.get("HQIN_SEARCH_TIMEOUT", "20"))
DIFF_OUTPUT_LIMIT   = int(os.environ.get("HQIN_DIFF_LIMIT",     "5000"))
SEARCH_MAX_RESULTS  = int(os.environ.get("HQIN_SEARCH_MAX",       "40"))
GLOB_MAX_RESULTS    = int(os.environ.get("HQIN_GLOB_MAX",        "200"))
WEB_SEARCH_MAX      = int(os.environ.get("HQIN_WEB_MAX",           "5"))
CLI_TOOL_OUTPUT_CHARS = int(os.environ.get("HQIN_CLI_TOOL_CHARS", "2000"))
CLI_DIFF_MAX_LINES    = int(os.environ.get("HQIN_CLI_DIFF_LINES",  "200"))
CLI_HISTORY_FILE      = os.environ.get("HQIN_CLI_HISTORY", ".hqin_history")
MAX_PARALLEL_READ_TOOLS = int(os.environ.get("HQIN_MAX_PARALLEL_READ_TOOLS", "4"))
# 单次写入/追加的字符上限。4B 生成 1500+ token 的大文件容易触碰模型超时，
# 超过上限时要求模型拆块，而不是把半截文件写进磁盘。
MAX_WRITE_CHARS       = int(os.environ.get("HQIN_MAX_WRITE_CHARS", "6000"))
FINISH_CHECK_MAX_FILES = int(os.environ.get("HQIN_FINISH_CHECK_MAX", "20"))

# ── 后台网页浏览（BrowserAct） ───────────────────
# 只读提取无需浏览器 ID；交互模式必须由操作者明确指定已有的 BrowserAct 浏览器。
BROWSER_ACT_COMMAND = os.environ.get("HQIN_BROWSER_ACT_COMMAND", "browser-act")
BROWSER_ID          = os.environ.get("HQIN_BROWSER_ID", "").strip()
BROWSER_SESSION     = os.environ.get("HQIN_BROWSER_SESSION", f"hqin-{os.getpid()}")
BROWSER_TIMEOUT_SECONDS = int(os.environ.get("HQIN_BROWSER_TIMEOUT", "45"))
WEB_FETCH_OUTPUT_LIMIT  = int(os.environ.get("HQIN_WEB_FETCH_LIMIT", "6000"))
BROWSER_AUTO_APPROVE_CLICKS = os.environ.get("HQIN_BROWSER_AUTO_APPROVE_CLICKS", "false").lower() in {
    "1", "true", "yes",
}

# ── 会话存档 ───────────────────────────────────
SESSION_DIR         = os.environ.get("HQIN_SESSION_DIR",   ".hqin_sessions")
SESSION_EXPIRE_DAYS = int(os.environ.get("HQIN_SESSION_TTL", "7"))

# ── Windows 通知（notifier.py） ────────────────────
# 任务结束/需要审批时弹 Windows toast；非 TTY 环境自动 no-op，测试不受影响。
NOTIFY_ENABLED = os.environ.get("HQIN_NOTIFY", "true").lower() in {
    "1", "true", "yes",
}
# 自定义提示音：PCM 编码 .wav 的绝对路径（SoundPlayer 只认 PCM）。
# 留空 = toast 走系统默认声音；设置后 toast 静音、wav 在同一 PowerShell
# 进程内播放。例：set HQIN_NOTIFY_SOUND=D:\sounds\ding.wav
NOTIFY_SOUND = os.environ.get("HQIN_NOTIFY_SOUND", "").strip()


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
        ("STEP_RENEWAL",       str(STEP_RENEWAL),  "HQIN_STEP_RENEWAL"),
        ("MODE",               MODE,               "HQIN_MODE"),
        ("MODEL_CONTEXT_TOKENS", str(MODEL_CONTEXT_TOKENS), "HQIN_CONTEXT_TOKENS"),
        ("CONTEXT_SOFT_RATIO", str(CONTEXT_SOFT_RATIO), "HQIN_CONTEXT_SOFT"),
        ("CONTEXT_HARD_RATIO", str(CONTEXT_HARD_RATIO), "HQIN_CONTEXT_HARD"),
        ("MAX_CHARS",          str(MAX_CHARS),     "HQIN_MAX_CHARS"),
        ("SUMMARY_THRESH",     str(SUMMARY_THRESH),"HQIN_SUMMARY_THRESH"),
        ("KEEP_RECENT",        str(KEEP_RECENT),   "HQIN_KEEP_RECENT"),
        ("MEMORY_KEEP_RECENT_TURNS", str(MEMORY_KEEP_RECENT_TURNS), "HQIN_MEMORY_KEEP_TURNS"),
        ("MEMORY_SUMMARY_MAX_CHARS", str(MEMORY_SUMMARY_MAX_CHARS), "HQIN_MEMORY_SUMMARY_CHARS"),
        ("MAX_CONSECUTIVE_ERRORS", str(MAX_CONSECUTIVE_ERRORS), "HQIN_MAX_ERRORS"),
        ("READ_FILE_PAGE_SIZE",str(READ_FILE_PAGE_SIZE), "HQIN_PAGE_SIZE"),
        ("READ_OUTPUT_LIMIT", str(READ_OUTPUT_LIMIT), "HQIN_READ_LIMIT"),
        ("MAX_WRITE_CHARS", str(MAX_WRITE_CHARS), "HQIN_MAX_WRITE_CHARS"),
        ("FINISH_CHECK_MAX_FILES", str(FINISH_CHECK_MAX_FILES), "HQIN_FINISH_CHECK_MAX"),
        ("BASH_EXECUTABLE", BASH_EXECUTABLE or "(查找 bash)", "HQIN_BASH"),
        ("PWSH_EXECUTABLE", PWSH_EXECUTABLE or "(查找 pwsh)", "HQIN_PWSH"),
        ("BASH_TIMEOUT_SECONDS", str(BASH_TIMEOUT_SECONDS), "HQIN_BASH_TIMEOUT"),
        ("PWSH_TIMEOUT_SECONDS", str(PWSH_TIMEOUT_SECONDS), "HQIN_PWSH_TIMEOUT"),
        ("TESTS_TIMEOUT_SECONDS", str(TESTS_TIMEOUT_SECONDS), "HQIN_TESTS_TIMEOUT"),
        ("TESTS_OUTPUT_LIMIT", str(TESTS_OUTPUT_LIMIT), "HQIN_TESTS_LIMIT"),
        ("SHELL_OUTPUT_LIMIT", str(SHELL_OUTPUT_LIMIT), "HQIN_SHELL_LIMIT"),
        ("GLOB_MAX_RESULTS", str(GLOB_MAX_RESULTS), "HQIN_GLOB_MAX"),
        ("CLI_TOOL_OUTPUT_CHARS", str(CLI_TOOL_OUTPUT_CHARS), "HQIN_CLI_TOOL_CHARS"),
        ("CLI_DIFF_MAX_LINES", str(CLI_DIFF_MAX_LINES), "HQIN_CLI_DIFF_LINES"),
        ("CLI_HISTORY_FILE", CLI_HISTORY_FILE, "HQIN_CLI_HISTORY"),
        ("MAX_PARALLEL_READ_TOOLS", str(MAX_PARALLEL_READ_TOOLS), "HQIN_MAX_PARALLEL_READ_TOOLS"),
        ("BROWSER_ID", BROWSER_ID or "(未配置)", "HQIN_BROWSER_ID"),
        ("BROWSER_SESSION", BROWSER_SESSION, "HQIN_BROWSER_SESSION"),
        ("BROWSER_AUTO_APPROVE_CLICKS", str(BROWSER_AUTO_APPROVE_CLICKS), "HQIN_BROWSER_AUTO_APPROVE_CLICKS"),
        ("SESSION_EXPIRE_DAYS",str(SESSION_EXPIRE_DAYS), "HQIN_SESSION_TTL"),
        ("NOTIFY_ENABLED", str(NOTIFY_ENABLED), "HQIN_NOTIFY"),
        ("NOTIFY_SOUND", NOTIFY_SOUND or "(系统默认音)", "HQIN_NOTIFY_SOUND"),
    ]
    for name, val, env in rows:
        t.add_row(name, val, env)
    c.print(t)
