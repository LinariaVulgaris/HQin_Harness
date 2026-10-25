# HQin Harness 计划

## 一、系统架构

系统主要分为四个模块，用 Python 编写：

1. **终端界面 (CLI UI)**：打印用户 Bug 和模型的思考过程，以及带人工审批权（Human-in-the-loop）的命令执行提示，使用 `rich` 库。
2. **LLM 网关 (LLM Gateway)**：使用标准的 `openai` Python 库接入 LM Studio 的本地 Server（`http://localhost:1234/v1`）。
3. **工具调用（Route B - 原生 Tool Calling）**：直接使用 OpenAI 兼容的 `tools=` 参数传入 JSON Schema，让框架负责格式，彻底消除 XML 解析这一整类错误。工具新增只需在 `tool_schemas.py` 追加一个 dict。
4. **沙盒执行 (Action Executor)**：带行号读取文件、Search & Replace 替换、Bash 执行（带安全拦截），以及 Python 语法自动检查。

## 二、文件结构

```
HQin_Harness/
├── main.py            # Agent Loop：流式输出、步数预算制、任务前 git 快照、会话存档
├── cli_ui.py          # Claude Code 风格 REPL：输入、补全、状态行/模式徽标、工具时间线、diff 审批
├── memory.py          # 可逆上下文记忆：token 估算与实测校准、轮次切分、归档、检索、解压
├── config.py          # 全局配置（支持环境变量覆盖）
├── workspace.py       # 工作区管理模块（沙盒核心）
├── console_setup.py   # Windows 控制台 UTF-8 兼容（避免非 GBK 页面文本导致崩溃）
├── notifier.py        # Windows 原生 Toast 通知：审批提醒、任务结束广播、诊断 CLI + AUMID 自愈
├── browser_policy.py  # 浏览规范：提示词片段 + 工具过滤 + 会话状态机
├── tools.py           # 工具实现函数 + TOOL_REGISTRY 路由表（含 run_tests、权限闸门、快照/undo）
├── tool_schemas.py    # OpenAI Function Calling JSON Schema
├── prompts.py         # System Prompt：标准工作流六步纪律 + 工具说明
├── bin/hqin.cmd       # 任意目录启动：当前文件夹作为工作区
├── install_launcher.ps1  # 把 bin 加入当前用户 PATH（只需运行一次）
├── tests/test_harness.py  # 不依赖模型的回归测试
├── tests/test_notifier.py # 通知模块与钩子点单测（不依赖模型的回归测试）
└── HQin_Harness计划.md
```

## 三、协议约定

### 1. 原生工具调用（Native Tool Calling）

通过 `client.chat.completions.create(tools=TOOL_SCHEMAS, tool_choice="auto")` 让框架保证格式。模型只需选择调哪个工具并填写参数，无需学习任何 XML 格式。

### 2. messages 格式

```python
# 用户输入
{"role": "user", "content": "..."}

# 模型发起工具调用
{"role": "assistant", "content": None, "tool_calls": [{
    "id": "call_xxx", "type": "function",
    "function": {"name": "read_file", "arguments": '{"path":"main.py"}'}
}]}

# 工具执行结果（必须对应 tool_call_id）
{"role": "tool", "tool_call_id": "call_xxx", "content": "文件内容..."}
```

### 3. 上下文管理（防爆显存）

| 级别 | 触发条件 | 动作 |
|------|----------|------|
| **主路径：可逆记忆** | 估算 token 超过 `MODEL_CONTEXT_TOKENS × 0.65` | system prompt 注入动态提示；模型按需调用 `context_status` → `compress_context`，把完整旧轮次归档到 `.hqin_memory/`，替换为结构化摘要 |
| **旧自动摘要** | 字符数超过 `SUMMARY_THRESH = 16000` | 仅作为兜底；在完整 tool-call/tool-result 块之后切分，调用模型生成 ≤150 字摘要 |
| **硬删除兜底** | 压缩后仍超过 `MODEL_CONTEXT_TOKENS × 0.85` 或 `MAX_CHARS = 64000` | 按完整用户轮次删除，绝不留下孤立的 tool result；单个超长最新轮次宁可保留，也不破坏协议 |

字符数只作为保守兜底，不能等价于 token 数；`memory.estimate_tokens()` 按“中文 1 字符≈1 token、其他约 3.5 字符/token”估算。LM Studio 的 Context Length 应设为 32,768，并为 system prompt、工具 schema 和生成结果预留空间。可逆记忆的完整设计见「可逆上下文记忆」节。

### 4. 行范围读取（防单文件撑爆上下文）

```python
read_file(path="main.py", start=10, end=50)  # 只读第 10~50 行
```

## 四、分步计划（MVP 阶段）

### Step 1：基础搭建 ✅
- 初始化 Python 项目，安装 `pip install openai rich`
- 接入 LM Studio（`http://localhost:1234/v1`）
- 实现 CLI 循环：用户输入 → 发给模型 → 打印回复

### Step 2：实现工具 Schema 与路由 ✅
- 新建 `tool_schemas.py`，定义所有工具的 OpenAI Function Calling JSON Schema
- `tools.py` 末尾维护 `TOOL_REGISTRY = {"tool_name": fn, ...}` 字典
- `main.py` 按 `finish_reason` 分发：`"tool_calls"` → 执行工具，`"stop"` → 任务完成

### Step 3：实现核心工具逻辑 ✅
- `list_dir(path=".")` — 树形展示目录（过滤 `.venv/.git`）
- `read_file(path, start?, end?)` — 带行号，支持行范围
- `write_file(path, content)` — 创建/覆盖，含语法拦截
- `edit_file(path, search, replace)` — SEARCH/REPLACE，含语法拦截；匹配失败时显示搜索内容预览
- `run_bash(command)` — 人工审批，输出超 4000 字符自动截断
- `search_in_files(keyword, directory?)` — 全局关键字搜索，最多返回 40 条

### Step 4：验证 MVP ✅
- 动态注入系统信息（OS + 工作目录）到 System Prompt
- 持久化交互会话（`while True`，任务完成后继续等待指令，`messages` 保留跨任务记忆）
- 步数预算制 `MAX_STEPS = 15`（见「工作流提示词与步数预算制」节：预算耗尽可续期，非硬终止）

## 五、优化与功能阶段

### Step 1：动态注入环境信息 ✅
- 启动时用 `platform.system()` + `os.getcwd()` 获取系统信息
- 动态格式化 `SYSTEM_PROMPT`，在开头告知模型当前 OS 和工作目录

### Step 2：持久化交互模式 ✅
- `main.py` 底层为 `while True` 循环，任务完成后不退出
- 保留 `messages` 数组，模型记住跨任务上下文

### Step 3：上下文管理（可逆记忆 + 兜底）✅
- `memory.estimate_messages_tokens()` — 估算 system prompt、摘要和消息的 token 用量
- `memory.turn_ranges()` — 以完整用户轮次为单位切分，自动跳过已压缩的摘要标记
- `memory.MemoryStore` — 把旧轮次原文归档到 `.hqin_memory/blocks/`，索引写入 `index.json`
- `memory.compress_context()` — 模型指定起止轮次和结构化摘要，归档后替换消息
- `memory.apply_notice()` — 仅在超过软预算时向 system prompt 注入动态压缩提示
- `manage_context()` — 旧自动摘要与硬删除仍保留，作为模型未响应提示时的兜底

### Step 4：全局搜索 ✅
- `search_in_files(keyword, directory=".")` 递归查找，返回文件路径和行号
- System Prompt 告知模型不确定位置时先搜索

### Step 5：Linter 语法检查 ✅
- `edit_file` 和 `write_file` 执行后，用 `ast.parse()` 检查 `.py` 文件语法
- 语法错误直接撤销并将报错返回给模型，强迫自我纠正

### Step 6：Git 集成 ✅
- `git_status()` — 查看仓库当前状态（已修改/已暂存/未跟踪）
- `git_diff(path?)` — 查看 diff，超 5000 字符自动截断
- `git_commit(message, add_all=True)` — 带人工审批，支持自动 `git add -A`

## 六、Phase 3 功能（已全部完成）

### 5.1 大文件分页提示 ✅
- `read_file` 不指定范围时，文件超过 `READ_FILE_PAGE_SIZE`（默认 150 行）自动截断
- 末尾附加提示，含下一页的 `start/end` 参数示例，引导模型分段阅读
- 阈值由 `config.py` 的 `READ_FILE_PAGE_SIZE` 控制，可通过 `HQIN_PAGE_SIZE` 环境变量覆盖

### 5.2 会话存档与恢复 ✅
- 退出（包括 Ctrl+C）时，自动将 `messages` 序列化为 `{workspace}/.hqin_sessions/session_YYYYMMDD_HHMMSS.json`
- 下次启动同一工作区时，检测到最近会话后询问是否恢复（显示会话时间、消息数、模型名）
- 自动清理超过 `SESSION_EXPIRE_DAYS`（默认 7 天）的旧存档
- System Prompt 不存储（每次重新生成），其余消息全部保留

### 5.3 配置文件支持 ✅
- 新增 `config.py`，所有常量集中管理，支持环境变量覆盖（`HQIN_MODEL`、`HQIN_BASE_URL` 等）
- 会话中输入 `config` 命令可实时打印当前生效的配置表
- `config.print_config()` 以 Rich 表格形式展示，含环境变量名提示

### 5.4 工具执行耗时统计 ✅
- `_execute_tool()` 返回 `(result, elapsed_seconds)` 元组
- `_TaskStats` 类按工具名归集调用次数和耗时列表
- 每个任务结束（无论成功/熔断）自动打印统计表：工具名、调用次数、平均耗时、总耗时
- 执行结果行内同步显示 `耗时 Xs`

### 5.5 多工具并行执行 ✅
- `_run_tools()` 仅在本轮全为只读工具（读取、搜索、Git 状态/diff、网络搜索）时并行；写入、编辑、命令、提交及未知工具一律串行
- 并发数由 `HQIN_MAX_PARALLEL_READ_TOOLS` 控制，默认 4，避免工具调用过多时创建无限线程
- 并行结果按原 `tool_calls` 顺序写回 `messages`，保证 API 一致性
- 并行时控制台提示 `⚡ 并行执行 N 个工具`

### 5.6 错误重试机制 ✅
- 工具返回 `Error:` 时，自动在 observation 末尾追加 `[重试提示]` 和当前连续错误次数
- 连续出错达到 `MAX_CONSECUTIVE_ERRORS`（默认 3）次时，暂停任务并询问用户是否继续
- 用户选择继续则重置计数；选择放弃则结束当前任务
- 非零 exit code 的 shell 命令统一返回 `Error:`，确保熔断统计不会漏记失败
- 工具调用成功时自动重置连续错误计数器

### 5.7 Web 搜索工具 ✅
- `web_search(query)` — 主用 Bing 国内站，**无需 API Key**
- 检索策略：① Bing HTML 解析（优先 `.edu.cn`/`.gov.cn`，命中关键词加权）→ ② DuckDuckGo Lite 兜底
- 含“官网/网站/首页”等泛词时，自动追加一次去掉泛词的查询，避免 Bing 改写为无关结果
- 结果数量由 `config.WEB_SEARCH_MAX` 控制（默认 5 条）

### 5.8 Word 文档生成 ✅
- `create_docx(path, title, content)` — 用 `python-docx` 生成真正的 `.docx`（ZIP/OOXML 容器），不是文本改后缀
- `title` 使用 Word 的 Title 样式；正文支持 `#/##/###` 标题、`-`/`*` 列表、`1.` 编号列表、`**粗体**`
- 路径限制在工作区内；省略 `.docx` 后缀时自动补全
- `write_file` 遇到 `.docx/.doc` 直接拒绝并提示改用 `create_docx`，防止再生成“文本改后缀”的假 Word 文件
- 依赖：`python-docx>=1.1,<2`（已写入 `requirements.txt`）

## 七、工作区（沙盒）✅

### 设计目标

允许用户指定某个文件夹为 Agent 的**沙盒根目录**，所有文件操作都限制在该目录内，防止 Agent 意外读写无关文件（如系统文件、其他项目）。

### 实现方式

**`workspace.py`（新模块）**：
- `set_workspace(path)` — 设置工作区，自动创建目录，同步 `os.chdir()` 保证 bash 相对路径一致
- `resolve_path(user_path)` — 路径解析 + 越界检测（Path Traversal Protection）
  - 相对路径 → 相对工作区根目录解析
  - 绝对路径 → 检查是否在工作区内，否则拒绝并报错
  - 使用 `Path.relative_to()` 防御 `../../` 等攻击

**工具层强制校验**：
- `read_file` / `write_file` / `edit_file` / `list_dir` / `search_in_files` — 所有路径参数先经 `_resolve()` 校验
- `run_bash` — 以 `cwd=workspace` 在工作区目录中执行，但这不是操作系统级沙盒；每次均需人工审阅并批准完整命令
- `git_*` — 以 `cwd=workspace` 执行，并使用 `shell=False` 的参数数组，避免模型参数触发 shell 注入

**启动向导（`main.py`）**：
- `python main.py --workspace D:/my_proj` — 命令行直接指定
- `python main.py -w .` — 使用当前目录
- `python main.py` — 交互式 Panel UI 选择（默认当前目录）

**System Prompt 感知**：模型被告知工作区路径，并要求只使用相对路径。

### 沙盒拦截效果示例

```
# 模型尝试读取工作区外的文件
read_file("../../etc/passwd")

→ Error: 沙盒越界！路径 '../../etc/passwd' 解析为 'C:\etc\passwd'，
          超出工作区 'D:\pycharm\my_proj' 范围。
          Agent 只能操作工作区内的文件，请使用相对路径。
```

## 八、工具一览

| 工具 | 状态 | 审批 | 说明 |
|------|------|------|------|
| `list_dir` | ✅ | 无 | 树形目录（过滤噪音目录），沙盒校验 |
| `read_file` | ✅ | 无 | 带行号，支持行范围，超 150 行或字符预算时截断并提示续读 |
| `write_file` | ✅ | 无 | 含语法拦截和 6000 字符上限，沙盒校验 |
| `append_file` | ✅ | 无 | 分块追加；不做 Python 语法检查，适合 JS/HTML/Markdown 等大文件 |
| `finish_check` | ✅ | 无 | 交付前自检：存在/非空/JSON/CSV/Python/docx/占位符；不传 files 时检查本次写入 |
| `create_docx` | ✅ | 无 | 生成真正的 Word `.docx`；支持简单 Markdown 结构，沙盒校验 |
| `edit_file` | ✅ | 无 | SEARCH/REPLACE + 语法拦截，沙盒校验 |
| `run_bash` | ✅ | ⚠️ 人工 | 显式调用 bash（`shell=False`），工作区 cwd，非零退出为 Error |
| `run_pwsh` | ✅ | ⚠️ 人工 | 显式调用 PowerShell 7（`pwsh`），用于 Windows 原生 cmdlet；缺失时返回 `ConfigurationRequired:` |
| `run_tests` | ✅ | 按模式 | pytest 优先/回退 unittest，白名单开关；workspace/full 免审批，ask 确认 |
| `search_in_files` | ✅ | 无 | 优先 ripgrep，缺失时回退 Python；支持 glob 过滤和结果上限 |
| `glob_files` | ✅ | 无 | 按 glob 查找文件路径，过滤噪音目录，结果有界 |
| `git_status` | ✅ | 无 | 在工作区执行，查看仓库状态 |
| `git_diff` | ✅ | 无 | 参数数组执行，diff 截断长度可配置，路径沙盒校验 |
| `git_commit` | ✅ | ⚠️ 人工 | 默认 add -A，需用户确认 |
| `context_status` | ✅ | 无 | 查看 token 用量、可压缩旧轮次、已归档块 |
| `compress_context` | ✅ | 无 | 归档完整旧轮次并用结构化摘要替换；当前及最近 2 轮受保护 |
| `decompress_context` | ✅ | 无 | 按块 ID 恢复摘要；支持 preview / full / 写入文件，只读不改消息 |
| `search_context` | ✅ | 无 | 检索归档块摘要和原文，返回块 ID、轮次和片段 |
| `web_search` | ✅ | 无 | Bing 国内站 + DuckDuckGo 兜底，无需 API Key |
| `web_fetch_url` | ✅ | 无 | BrowserAct `stealth-extract` 渲染后转 Markdown，不建会话 |
| `browser_open` | ✅ | 无 | 创建后台交互会话；未配置 `HQIN_BROWSER_ID` 时返回 `ConfigurationRequired:` 且不出现在工具列表 |
| `browser_state` | ✅ | 无 | 读取可交互元素编号；点击/导航后必须重新调用 |
| `browser_click` | ✅ | ⚠️ 人工 | 仅限导航元素；由代码状态机校验“先 state 后 click”，敏感标签直接拦截 |
| `browser_get_markdown` | ✅ | 无 | 提取当前页面 Markdown，不改变状态机 |
| `browser_navigate` | ✅ | 无 | 跳转 URL；成功后旧编号失效 |
| `browser_close` | ✅ | 无 | 关闭会话；无会话时幂等返回 `Info:` |

## 九、运行方式

```bash
# 首次安装依赖（含 Word 生成）
.venv\Scripts\python.exe -m pip install -r requirements.txt

# 基本启动（交互式选择工作区）
.venv\Scripts\python.exe main.py

# 直接指定工作区
.venv\Scripts\python.exe main.py --workspace D:/my_proj

# 使用环境变量切换模型
$env:HQIN_MODEL = "qwen2.5-7b-instruct"
.venv\Scripts\python.exe main.py -w .

# 会话内命令（不带斜杠的 exit/quit/config 继续兼容）
> /help
> /status
> /compact
> /resume
```

## 十、后台网页浏览与交互（BrowserAct）

### 目标

让 Agent 能够读取用户给出的 URL、处理 JavaScript 渲染内容，并在后台按页面的可交互元素继续导航；不依赖用户手动复制页面文本。

### 分层设计

1. **只读浏览**：`web_fetch_url(url)` 使用 `browser-act stealth-extract` 获取渲染后的 Markdown。它不创建浏览器会话、不保留 Cookie，适合阅读公开网页和用户提供的链接。
2. **交互浏览**：通过已配置的 BrowserAct 浏览器，在后台创建独立 session，按 `browser_state()` 返回的元素编号调用 `browser_click(index)`，之后等待页面稳定并重新获取状态。支持打开、导航、提取 Markdown 和关闭会话。
3. **状态机约束**：页面变化后旧元素编号失效；Agent 必须遵循“打开 → 读取 state → 点击 → 等待稳定 → 重新读取 state”的循环，不能猜测或复用旧编号。

### 操作规范（Policy）

规范集中在 `browser_policy.py`，作为唯一来源，同时供三处使用：

1. `prompt_block()` 生成 system prompt 中的动态能力说明；
2. `filter_tool_schemas()` 在未配置交互浏览器时隐藏全部 `browser_*` 工具；
3. `BrowserSessionPolicy` 在代码层校验 `open → state → click → state` 顺序。

具体规则：

- 普通阅读优先 `web_fetch_url`；只有需要点击、翻页或展开内容时才创建交互会话。
- `browser_click` 内部自动执行 `wait stable`，但不会刷新元素编号；点击后必须重新 `browser_state`。
- `browser_open` / `browser_click` / `browser_navigate` 都会让当前 state 失效；`browser_get_markdown` 不会。
- 仅允许链接、分页、展开/收起等导航动作；登录、注册、表单填写或提交、支付、购买、上传、下载、删除、验证码不自动化。
- 未配置 `HQIN_BROWSER_ID` 时，交互工具从模型工具列表中移除；即使模型从历史消息中复用了旧调用，运行时也返回 `ConfigurationRequired:`，不启动浏览器。
- 任务结束时 Harness 自动关闭本次进程创建的会话，避免后台残留；Agent 也应主动调用 `browser_close`。

### 结果状态语义

工具结果使用三种前缀，供 Agent Loop 区分处理：

| 前缀 | 含义 | 是否计入连续错误 | 是否追加重试提示 |
|------|------|------------------|------------------|
| `Error:` | 参数错误、执行失败、违反状态机 | 是 | 是 |
| `ConfigurationRequired:` | 缺少用户配置（如 `HQIN_BROWSER_ID`） | 否 | 否 |
| `Refused:` | 策略拒绝或用户拒绝 | 否 | 否 |

### 边界与已知限制

- 点击接口只接收元素编号，无法从协议层判断目标是否真的会触发下载或提交。当前采用“不暴露上传/下载工具 + 敏感标签拒绝 + 人工确认”的纵深防御；动态页面或图标按钮可能漏判，不能宣称绝对阻断。
- 敏感标签检查依赖 BrowserAct `state` 的文本格式。无法解析标签时不做文本拦截，仍由确认环节兜底。
- 状态机只保证“不会复用旧编号”，不保证点击一定产生页面变化；无变化时重新 `state` 即可。

### 安全与配置

- 交互浏览不会自动选择用户的 Chrome/登录态。必须由操作者显式设置 `HQIN_BROWSER_ID` 为一个已存在的 BrowserAct 浏览器 ID；未配置时交互工具不会出现在工具列表，只读浏览仍可用。
- `browser_click` 仅用于链接、分页和页面内导航；默认每次点击需要本地人工确认。操作者可显式设置 `HQIN_BROWSER_AUTO_APPROVE_CLICKS=true` 开启后台自动导航，但表单填写、登录、文件上传、下载和验证码始终不纳入本阶段。
- 默认后台运行，不弹出可见窗口；任务结束时 Harness 自动关闭本次创建的会话，Agent 也应主动调用 `browser_close()` 释放资源。
- 依赖 `browser-act` CLI。其浏览器档案、Cookie 和会话日志由该工具本地管理，不写入 Harness 会话存档。

### 实施验收

- [x] `web_fetch_url` 的命令组装、输出限制和 JavaScript 渲染路径已实现；实际提取依赖本机 BrowserAct/Camoufox 运行时可启动。
- [x] 未配置 `HQIN_BROWSER_ID` 时，交互工具返回 `ConfigurationRequired:`，不启动浏览器，且不出现在模型的工具列表。
- [x] 交互工具的命令使用参数数组、`shell=False`，并有单元测试覆盖命令组装、点击确认和会话状态。
- [x] `browser_policy.py` 统一提供提示词、工具过滤和状态机；未 `browser_state` 的点击、复用旧编号、点击敏感标签均被代码拦截。
- [x] `web_fetch_url` 在本机真实公开 URL 完成只读冒烟（example.com，958 字符）；此前的 Camoufox `Cache/fonts` 错误未再出现。
- [x] Windows 重定向输出自动切到 UTF-8，页面中的阿拉伯文等非 GBK 字符不再触发 `UnicodeEncodeError`。
- [x] 以真实浏览器会话完成 `open → state → click → state` 冒烟：`chrome_local_100115402056532060`（xyz-rewrite-remote）打开 example.com，点击 `Learn more` 后跳转到 iana.org，重新读取 state 成功，会话已关闭。
- [x] 用 `qwen3.5-4b` 真实 Agent Loop 复跑上述流程：6 步、5 次工具调用、0 错误，模型按 `open → state → click → state → close` 顺序执行。

## 十一、可逆上下文记忆（Memory）

### 背景

原来的两级上下文管理只有“摘要压缩 + 硬删除”：摘要由 Harness 固定生成，最多 150 字；硬删除直接丢弃旧消息，原文无法找回。模型既看不到“哪些旧轮次可压缩”，也无法在需要旧细节时检索。对于长时间任务，这会让模型在“忘记”和“爆上下文”之间二选一。

本阶段借鉴 ACP（Agent Context Protocol）的“块化 + 可检索 + 可解压”思路，但不把压缩时机完全交给模型：**代码负责预算、边界和兜底，模型负责判断哪些旧细节不再需要，并写自包含摘要。**

### 设计原则

1. **完整轮次**：以用户消息为边界切分，整轮归档，绝不拆散 `assistant tool_calls` 与 `tool` 结果。
2. **当前任务受保护**：当前轮次和最近 `MEMORY_KEEP_RECENT_TURNS = 2` 轮永远不压缩。
3. **可逆**：归档块保存原始消息、摘要、轮次范围、token 数和主题；模型可先 `search_context` 再 `decompress_context`。
4. **只读恢复**：`decompress_context` 只把内容返回给模型或写到 `restored/`，不会自动改回 `messages`，避免模型无意中把上下文重新撑爆。
5. **旧机制兜底**：模型没有响应提示时，仍由 `manage_context()` 的旧摘要和硬删除兜底。

### 四个工具

| 工具 | 参数 | 行为 |
|------|------|------|
| `context_status` | 无 | 显示估算 token、可压缩轮次、受保护轮次、上下文中摘要和磁盘归档块 |
| `compress_context` | `start_turn`、`end_turn`、`summary`、`topic?` | 校验范围后归档完整轮次，替换为一条 `[历史记忆 b0001 ...]` 摘要消息 |
| `decompress_context` | `block_id`、`mode=preview/full/file` | `preview` 返回摘要 + 原文前 1500 字；`full` 返回全文（上限 12000 字）；`file` 把完整 JSON 写入 `.hqin_memory/restored/` |
| `search_context` | `query`、`limit=3` | 在摘要和原文中做子串检索，按“摘要命中 5 分、用户消息 2 分、其他 1 分”排序，返回块 ID、轮次和片段 |

工具执行本身是串行的；`_PARALLEL_SAFE_TOOLS` 不包含这四个工具，避免并发修改 `messages`。

### 运行流程

1. 每次调用模型前，`main.py` 用 `memory.set_active_messages(messages)` 绑定当前消息列表。
2. `memory.apply_notice()` 估算 token；超过软预算（`MODEL_CONTEXT_TOKENS × 0.65`）且存在可压缩轮次时，把一段 `[HQIN_CONTEXT_NOTICE]` 附加到唯一的 system 消息末尾。
3. 模型先调用 `context_status` 查看“turn 1–N 可压缩”，再调用 `compress_context`，摘要建议包含：任务目标、已完成、关键文件/命令、未完成/下一步、约束。
4. 需要旧细节时，模型先 `search_context` 定位块，再 `decompress_context`；不要凭记忆猜。
5. 如果模型忽略提示且估算 token 超过硬预算（`× 0.85`），`manage_context()` 自动执行旧摘要/硬删除，保证请求不超限。

### 存储布局

```text
<workspace>/.hqin_memory/
├── index.json          # 块索引：id、轮次、主题、token、消息数、摘要
├── blocks/
│   └── b0001.json      # 原始消息、摘要、元数据
└── restored/
    └── b0001.json      # decompress_context(mode="file") 的副本
```

`.hqin_memory/` 已加入 `.gitignore`，不进入用户仓库；会话文件仍保存在 `.hqin_sessions/`，两者互不替代。

### 预算与保护

| 配置 | 默认值 | 用途 |
|------|--------|------|
| `MODEL_CONTEXT_TOKENS` | 32768 | 估算预算，不是 LM Studio 的硬限制 |
| `CONTEXT_SOFT_RATIO` | 0.65 | 注入动态提示的阈值 |
| `CONTEXT_HARD_RATIO` | 0.85 | 自动摘要/硬删除的阈值 |
| `MEMORY_KEEP_RECENT_TURNS` | 2 | 始终保护的最近轮次 |
| `MEMORY_SUMMARY_MAX_CHARS` | 3000 | 单块摘要上限 |
| `MEMORY_SEARCH_LIMIT` | 3 | 默认返回的检索结果数 |
| `MEMORY_DECOMPRESS_LIMIT` | 12000 | `full` 模式返回的字符上限 |

### 结果语义

- `Success:`：压缩/解压/写文件成功。
- `Error:`：参数错误、轮次越界、摘要为空或过长、归档读写失败；会计入连续错误。
- 普通说明（无前缀）：`context_status` 的状态表、`search_context` 的“没有找到”结果。

### 已知限制

- token 是估算值，不替代模型的真实 tokenizer；中文按 1 字符≈1 token 保守计算。
- 旧自动摘要路径产生的摘要不归档；只有模型主动 `compress_context` 的轮次可逆。
- `decompress_context` 不会自动把归档块放回 `messages`；需要模型自己把关键内容写进当前任务。
- 4B 模型可能忽略动态提示；目前用脚本化模型验证了完整链路，尚未用真实 `qwen3.5-4b` 跑“主动压缩”长任务。
- 记忆状态是进程内全局单例，适合当前单会话 CLI；多会话并发需要改为按会话注入。

### 实施验收

- [x] `memory.py` 提供 token 估算、完整轮次切分、归档存储、压缩、检索、解压和动态提示。
- [x] 四个工具已注册到 `TOOL_REGISTRY` 和 `tool_schemas.py`，并在 system prompt 中说明使用时机。
- [x] `compress_context` 保护当前轮次和最近 2 轮；越界、空摘要、超长摘要均返回 `Error:`。
- [x] 原文归档到 `.hqin_memory/blocks/`，`index.json` 持久化；`.gitignore` 已忽略。
- [x] `MemoryTests` 覆盖轮次边界、归档、保护、检索、解压、持久化、提示注入，以及多次压缩后绝对轮次编号不重置。
- [x] `MemoryAgentFlowTests` 用脚本化模型跑真实 Agent Loop：看到提示 → `context_status` → `compress_context` → `search_context` → `decompress_context`；当前全套 60 项测试通过。
- [ ] 真实 `qwen3.5-4b` 在长任务中主动压缩的冒烟测试（非阻塞，后续可选）。

## 十二、原生 Shell 与紧凑文件工具（借鉴 dsh-ops）

### 背景与边界

本阶段只借鉴其中的两部分：

1. **Shell 路由**：通用命令优先 bash；Windows 原生操作使用 PowerShell 7。
2. **紧凑文件工具**：有界读取、glob 查找、ripgrep 搜索，减少输出 token 和无效遍历。

不引入 Rust worker、DSH 插件运行时、PTC SDK、panel/TUI，也不宣称提供操作系统级沙箱；现有工作区路径校验和人工审批继续保留。

### Shell 设计

- `run_bash(command)`：优先使用 `HQIN_BASH`，否则依次检查常见 Git/Cygwin 路径、PATH 上的 `bash`，并从 PATH 上的 `git.exe` 反推自定义 Git 安装（兼容 `D:\git\Git`）；候选先执行一次 `bash -c "exit 0"` 探测，跳过 WSL 启动器和无法启动的 MSYS/Cygwin。以 `[bash, "-c", command]` 执行，`shell=False`。没有可用 bash 时返回 `ConfigurationRequired:`，不退回 `cmd.exe`，避免命令语义悄悄改变。
- `run_pwsh(command)`：优先使用 `HQIN_PWSH`，否则检查常见 PowerShell 7 路径和 PATH，并探测能否启动；以 `[pwsh, "-NoProfile", "-NonInteractive", "-Command", command]` 执行，`shell=False`。只接受 PowerShell 7；找不到时返回 `ConfigurationRequired:`，并提示安装或设置 `HQIN_PWSH`。
- 两者都在工作区目录中执行，执行前显示完整命令并请求人工确认；超时、非零退出、UTF-8/GBK 解码和输出截断行为一致。
- 命令本身仍可能产生任意副作用；人工确认是主要防线。工作区校验不等于 OS 级隔离。

### 文件工具设计

- `glob_files(path=".", pattern="**/*", limit=200)`：在沙盒目录内按 glob 查找普通文件；过滤 `.git`、`.venv`、`node_modules` 等噪音目录；返回相对路径、总数和截断提示。模式为空时使用 `**/*`；`pattern` 不接受绝对路径或 `..`。
- `search_in_files(keyword, directory=".", glob=None, max_results=40)`：优先调用 `rg -n --no-heading --color never`，用参数数组、`shell=False`；`rg` 不存在或不可执行时回退现有 Python 遍历。`glob` 仅作为文件过滤，不改变搜索根目录。
- `read_file(path, start=None, end=None)`：保留现有行范围和 150 行自动分页；新增字符预算 `READ_OUTPUT_LIMIT`。选中内容超过预算时只返回前缀，并明确提示下一次的 `start` 行号，避免单行超长文件撑爆上下文。
- 文件工具仍走 `workspace.resolve_path()`；符号链接、重解析点和越界路径按现有规则处理。

### 配置

| 配置 | 默认值 | 用途 |
|------|--------|------|
| `BASH_EXECUTABLE` | 空（查找 `bash`） | 显式指定 bash 路径，便于 Git Bash/PortableGit |
| `PWSH_EXECUTABLE` | 空（查找 `pwsh`） | 显式指定 PowerShell 7 路径 |
| `BASH_TIMEOUT_SECONDS` | 60 | bash 超时 |
| `PWSH_TIMEOUT_SECONDS` | 60 | PowerShell 超时 |
| `SHELL_OUTPUT_LIMIT` | 4000 | 两种 shell 的输出字符上限 |
| `READ_OUTPUT_LIMIT` | 12000 | 单次 `read_file` 返回的字符预算 |
| `GLOB_MAX_RESULTS` | 200 | `glob_files` 默认结果上限 |

### 工具契约

| 工具 | 参数 | 结果 |
|------|------|------|
| `run_bash` | `command` | 成功返回 `执行输出（exit code=0）`；失败返回 `Error:`；缺少 bash 返回 `ConfigurationRequired:` |
| `run_pwsh` | `command` | 同上；缺少 `pwsh` 返回 `ConfigurationRequired:` |
| `glob_files` | `path?`、`pattern?`、`limit?` | 路径列表、总数、截断提示；无匹配返回普通说明 |
| `search_in_files` | `keyword`、`directory?`、`glob?`、`max_results?` | 匹配行、总数、截断提示；ripgrep 不可用时自动回退 |

### 已知限制

- 本机已安装 `ripgrep 15.2.0`；从普通终端启动 HQin 时 `_find_rg` 会优先使用它。当前 DSH 进程的 PATH 没有包含用户的 `rg`，所以在 DSH 会话里仍会回退 Python；回退路径功能正确，但大仓库速度不如 ripgrep 这类编译型工具（“原生”指编译好的可执行程序，不是 Windows 自带 `findstr`）。
- 本机已通过 MSI 安装 PowerShell 7 到 `C:\Program Files\PowerShell\7\pwsh.exe`；`_find_shell` 会优先检查该固定路径，`run_pwsh` 真实冒烟已通过。未安装时 `run_pwsh` 返回 `ConfigurationRequired:`，不会自动退回 Windows PowerShell 5.1。
- `run_bash` 的 bash 可能来自 Cygwin、Git Bash 或 WSL，路径语义不同；需要稳定行为时用 `HQIN_BASH` 固定。本机 Git 安装在 `D:\git\Git`，`git.exe` 在 PATH 上但 `bash.exe` 不在；`_git_bash_candidates()` 会从 PATH 上的 `git.exe` 反推根目录，已自动找到 `D:\git\Git\bin\bash.exe`。`C:\Windows\System32\bash.exe` 是 WSL 启动器且没有可用发行版，已显式跳过；DSH 自带的 MSYS bash 在 DSH 进程内报 `signal pipe, Win32 error 5`。`_find_shell` 会先探测候选能否启动，避免把损坏的 bash 交给模型。
- 没有实现 DSH 的后台 job 管理；长命令仍受前台超时限制。

### 实施验收

- [x] `run_bash` 改为显式 bash + `shell=False`，并保留审批、超时、输出截断。
- [x] 新增 `run_pwsh`，缺失 `pwsh` 时返回 `ConfigurationRequired:`，不触发重试提示。
- [x] 新增 `glob_files`，完成沙盒校验、噪音过滤、结果上限和截断提示。
- [x] `search_in_files` 优先 ripgrep，缺失时回退 Python，并支持 glob/结果上限。
- [x] `read_file` 增加字符预算和续读提示。
- [x] 新工具进入 `TOOL_REGISTRY`、`tool_schemas.py` 和 system prompt；写操作/命令不进入并行白名单。
- [x] 单元测试覆盖命令组装、缺少可执行文件、非零退出、glob、ripgrep 回退、读取截断；全套 60 项测试通过。
- [x] 用 MSI 安装的 `C:\Program Files\PowerShell\7\pwsh.exe` 完成 `run_pwsh` 真实冒烟（`Write-Output msi-pwsh-ok`，exit code=0）。
- [x] 用 `D:\git\Git\bin\bash.exe` 完成 `run_bash` 真实冒烟（`echo bash-ok; pwd`，exit code=0，输出 `/d/pycharm/HQin_Harness`）；缺少 bash 时返回 `ConfigurationRequired:` 的行为已由单元测试覆盖。
- [x] 清理已克隆的 `dsh-ops` 目录，不把 DSH 插件运行时纳入项目。

## 十三、Claude Code 风格 CLI 交互层

### 目标

在不改 Agent Loop 和工具协议的前提下，把当前“裸 input + 逐行打印”升级为可持续使用的终端 REPL。交互层只负责输入、展示和本地命令，不替模型做决策。

第一批只做终端内体验；不引入 Web UI、Textual 全屏 TUI、权限模式或无人值守入口。

### 第一批范围

- `prompt_toolkit` 输入：历史持久化到工作区 `.hqin_history`，支持方向键、历史搜索、`/` 命令补全和多行输入（Enter 发送，Esc+Enter 换行）。
- Slash 命令：`/help`、`/status`、`/context`、`/clear`、`/new`、`/resume`、`/compact`、`/config`、`/exit`；未知命令只提示，不发给模型。
- 底部状态行：模型、工作区、git 分支、上下文百分比进度条与估算/实测 token；上下文数字使用 `memory.estimate_messages_tokens` 的估算值，软/硬阈值时分别转黄/红（详见「上下文占用显示」节）。
- 工具时间线：调用开始时显示工具名和关键参数，结束后显示状态、耗时和截断后的结果；`Error:` 红色，`Refused:`/`ConfigurationRequired:` 黄色。
- 编辑预览：`write_file` / `edit_file` 在真正落盘前显示 unified diff 并请求确认；拒绝时返回 `Refused:`，文件不改动。
- 没有 TTY 或 `prompt_toolkit` 不可用时自动回退到普通 `input()`，保证脚本和测试能运行。

### 命令契约

| 命令 | 行为 |
|------|------|
| `/help` | 列出命令和快捷键 |
| `/status` | 会话、模型、工作区、git、上下文与归档状态 |
| `/context` | 上下文占用明细：总量进度条、软/硬阈值、最近实测与偏差、角色分解、最大轮次、归档块 |
| `/clear` | 清空当前对话，保留 system prompt；实测读数一并重置 |
| `/new` | 保存当前会话后开始空白对话；实测读数一并重置 |
| `/resume` | 列出会话文件，按编号恢复；恢复前保存当前会话，恢复后实测读数作废 |
| `/compact` | 调用现有 `_compress_with_summary` 压缩最旧历史；没有可压缩内容时明确提示 |
| `/config` | 打印当前配置（兼容裸 `config`） |
| `/exit`、`/quit` | 保存并退出（兼容裸 `exit`/`quit`） |

### Diff 审批设计

- `tools.py` 增加 `set_edit_preview_hook(hook)`；`write_file` / `edit_file` 在语法检查和写入之间调用 hook。
- hook 接收 `(rel_path, old_text, new_text, diff_text)`，返回 `True` 继续、`False` 拒绝；未安装 hook 时直接执行，保持测试和非交互调用兼容。
- CLI 安装的 hook 用 Rich 显示 diff，`cli_ui.CLI_DIFF_MAX_LINES` 限制展示行数，再用 `Confirm.ask` 询问。
- 拒绝是确定性结果，返回 `Refused:`，不计入连续错误，也不触发重试提示。

### 配置与依赖

| 配置 | 默认值 | 用途 |
|------|--------|------|
| `CLI_TOOL_OUTPUT_CHARS` | 2000 | 工具时间线单次展示的字符上限 |
| `CLI_DIFF_MAX_LINES` | 200 | 落盘前 diff 的最大展示行数 |
| `CLI_HISTORY_FILE` | `.hqin_history` | 工作区内的输入历史文件 |

`requirements.txt` 增加 `prompt-toolkit>=3.0,<4`；`.gitignore` 忽略 `.hqin_history`。

### 从任意目录启动（Windows）

- `bin/hqin.cmd` 用 `%~dp0..` 定位项目，用 `%CD%` 作为工作区；在文件资源管理器地址栏输入 `cmd`，再输入 `hqin` 即可启动。
- `install_launcher.ps1` 只把 `D:\pycharm\HQin_Harness\bin` 追加到**当前用户** PATH，不修改系统 PATH；重复运行不会重复添加。
- 新开的 cmd/PowerShell 才会继承新的 PATH；已打开的窗口需要重启。
- 也支持显式指定工作区：`hqin -w D:\another\project`。

### 已知限制

- 第一批不做权限模式；写文件和命令仍沿用现有确认/无确认行为。
- Windows 控制台通常不把 Shift+Enter 与 Enter 区分开，`prompt_toolkit` 也无法绑定 `s-enter`；当前统一使用 Esc+Enter 换行。真正的 Shift+Enter 需要终端支持 CSI-u/modifyOtherKeys，或改用 Ink/原生输入层。
- 没有实现任务执行中的 Ctrl+C 取消模型请求；Ctrl+C 在输入行取消当前输入，在任务中仍按现有行为退出。
- `/compact` 使用旧的 LLM 摘要路径，不生成可检索的 `bNNNN` 归档块；需要可逆压缩时仍由模型调用 `compress_context`。
- 会话恢复仍是单文件 JSON；并发多会话和 `-p` 非交互入口留到后续批次。

### 实施验收

- [x] `cli_ui.py` 提供 prompt_toolkit 输入、slash 补全、状态行、帮助/状态/会话展示和 diff 确认。
- [x] `main.py` 接入 slash 命令；`/clear`、`/new`、`/resume`、`/compact` 不破坏 system prompt 和记忆绑定。
- [x] `tools.py` 的编辑预览 hook 能在写入前拒绝修改，并返回 `Refused:`。
- [x] 工具时间线替代逐行原始输出，保留错误/拒绝/配置缺失的视觉区分。
- [x] 无 TTY 时回退 `input()`；现有测试继续通过（当前 71 项）。
- [x] 新增 CLI 解析、状态行、git 分支读取、diff 拒绝和输入回退测试。
- [x] `bin/hqin.cmd` 在资源管理器打开的任意文件夹中启动，当前目录自动成为工作区；`install_launcher.ps1` 可幂等加入用户 PATH。
- [x] 输入层支持 Esc+Enter 换行，并在帮助和横幅中说明 Windows Shift+Enter 限制。

## 十四、小模型工具调用治理（P0）

### 目标

4B 模型的失败通常不是“不会想”，而是工具调用太长、写入不完整、完成后没有验证。本阶段不增加新的智能体范式，只加三道确定性护栏：写入分块、交付自检、截断不执行。

### 1. 写文件分块

- `write_file` 单次内容上限 `MAX_WRITE_CHARS`（默认 6000 字符）。超限直接返回 `Error:`，要求第一块用 `write_file`、后续用 `append_file`，不把半截内容落盘。
- 新增 `append_file(path, content)`：只追加文本，不做 Python 语法检查；文件不存在时创建。Python 文件仍要求先写完整骨架，再用 `edit_file` 分段替换，避免不完整语法被拒绝或写入坏文件。
- `append_file` 同样受 6000 字符上限约束，避免把“分块”变成一次更大的输出。
- 成功写入/追加/编辑/生成 docx 的路径会记录到进程内集合，供 `finish_check` 默认使用。

### 2. 交付自检 finish_check

`finish_check(files=None)` 是只读工具，不运行测试、不修改文件。不传 `files` 时检查本次会话成功写入的文件；没有记录时返回 `Error:` 并要求显式传入路径。

检查规则：

- 路径必须存在；空文件和空目录失败。
- `.json`：必须能被 `json.loads` 解析。
- `.csv`：必须能解析，且各行列数一致。
- `.py`：必须能 `compile()` 通过。
- `.docx`：必须能被 `python-docx` 重新打开。
- 文本文件：扫描 `TODO`、`FIXME`、`TBD`、`待补充`、`待完善`、`占位符`、`placeholder`，作为警告而不是失败（文档里合法提到这些词不应误判）。
- 二进制或未识别类型：只检查存在且非空。
- 最多检查 `FINISH_CHECK_MAX_FILES`（默认 20）个路径。

结果语义：

- 全部通过：`Success: 交付物检查...`，不计错误。
- 有失败：`VerificationFailed:`，计一次连续错误；主循环追加以“修复失败项后重新 finish_check”的验证提示，而不是“检查参数”的通用重试提示。
- 该工具不替代真实测试；测试仍由 `run_bash` / `run_pwsh` 执行。

### 3. finish_reason=length

模型返回 `finish_reason == "length"` 时，工具参数通常是残缺 JSON。主循环不执行这一轮 `tool_calls`，而是：

- 把已生成的文本作为 assistant 消息保留（空内容用占位文本）。
- 追加一条 user 系统提示，要求不要重复整段、把任务拆小、先写骨架再分块。
- 计入连续错误；达到上限后走现有熔断确认。

### 配置

| 配置 | 默认值 | 用途 |
|------|--------|------|
| `MAX_WRITE_CHARS` | 6000 | `write_file` / `append_file` 单次字符上限 |
| `FINISH_CHECK_MAX_FILES` | 20 | 一次 `finish_check` 最多检查的路径数 |

### 实施验收

- [x] `write_file` 超过 6000 字符时拒绝写入，并提示使用 `append_file`。
- [x] `append_file` 支持分块追加，成功路径被 `finish_check` 记录。
- [x] `finish_check` 覆盖 JSON/CSV/Python/docx/空文件/缺失文件/占位符。
- [x] `VerificationFailed:` 与 `Error:` 区分提示，但仍计入连续错误。
- [x] `finish_reason=length` 不执行残缺工具调用，要求模型拆小。
- [x] 新增写入上限、分块、交付自检和验证提示测试；现有测试继续通过。

## 十五、上下文占用显示

### 目标

让操作者和模型都能随时看清上下文的真实压力，而不是只在爆预算前的最后时刻才看到一条警告。显示是只读的：所有数字都来自现有估算器和 API usage，不新增任何决策逻辑，也不改变预算阈值的行为。

### 三层显示

1. **状态行（常驻）**：升级为 `ctx:42% ▓▓▓▓░░░░░░ ~12,345 tok · ⚡13,052 实测`。
   - `~` 是实时估算值，驱动所有预算判断；`⚡` 是最近一次主模型请求由 API 实测的 `prompt_tokens`，仅作校准参考。
   - 进度条越过软阈值（65%）追加 `⚠`，越过硬阈值（85%）追加 `‼`。
   - `prompt_toolkit` 工具栏按阈值自动染色（绿→黄→红），纯文本回退路径输出同样内容。
   - 状态行与工具栏共用 `context_token_text()`，两处口径完全一致。
   - **执行期可见性**：底部工具栏只在等待输入时渲染；任务执行期间主循环在每个 Agent Step 打印同一口径的上下文读数行，长任务中不会出现“盲区”。
   - **工具栏配色**：显式覆盖 prompt_toolkit 对 `bottom-toolbar` 默认的 `reverse`（反显）——否则前景色会被翻成背景色，出现“左绿右白”的割裂效果。现为统一浅蓝底（`#e3f2fd`）+ 深灰字（`#37474f`），上下文读数按阈值染深绿/橙/红前景。

2. **`/context` 明细面板**：一条命令看全貌，包含——
   - 总量、进度条、软/硬阈值各自换算成 token 数（如 32768 × 65% ≈ 21,299）。
   - 最近实测值与估算偏差百分比（能量化“中文 1 字符≈1 token”估算的真实误差）。
   - 按角色分解（system/user/assistant/tool 各自的消息数、tokens、占比、合计行）；分解与 `estimate_messages_tokens` 严格同口径（每条消息 +4 协议开销），各部分之和恒等于总量。
   - 占用最大的 3 个轮次（标注可压缩/受保护）。
   - 磁盘归档块数量与总 token（已移出上下文的部分）。

3. **实测校准（数据源 + 估算校准）**：`_TaskStats.record_model` 在每次主模型请求后把 `usage.prompt_tokens` 同步到 `memory.record_real_usage()`——这是唯一写入点。
   - `record_real_usage` 同时快照**当时的原始估算值**；差值（实测 − 原始估算）即工具 Schema、chat template 等每次请求固定存在的系统性开销。
   - `calibrated_messages_tokens()` = 原始估算 + 该差值：锚定最近一次实测，只对之后的增量做本地估算。**所有预算判断（软/硬阈值）和显示层统一使用校准口径**——百分比从此贴近真实占用，而不是偏低 30~40% 的粗估。
   - `/compact` 的摘要请求不经过 `record_model`，不会污染锚点。
   - 模型侧 `context_status` 工具输出也追加实测行，帮助模型自行校准。
   - 零值/缺失 usage 不覆盖上次有效测量（个别 OpenAI 兼容服务不上报）。
   - `/clear`、`/new`、`/resume` 及启动恢复会话时重置锚点——旧数字对新上下文无意义。
   - 无锚点时（首次请求前、刚清空后）自动退化为原始估算，行为与旧版一致。

### 已知限制

- 实测值是“最近一次主请求”的快照，工具结果追加后不实时更新，定位是校准参考而非实时读数。
- 校准假设“实测 − 原始估算”的差值在两次请求之间近似恒定（固定开销为主）；消息结构剧烈变化（如大量英文代码涌入，token 密度漂移）时校准会随之更新——每次请求都会重新计算差值，因此偏差不会累积。
- 估算口径不变（中文≈1 token、其他≈3.5 字符/token、每消息+4），作为校准的增量部分；无锚点时单独使用。
- 状态行在长用户消息追加后、下一次模型请求前，只有 `~` 估算会更新，`⚡` 实测保持上一次的值。

### 实施验收

- [x] `memory.py` 新增 `record_real_usage`（含原始估算快照）/ `get_real_usage` / `reset_real_usage` / `is_calibrated` / `calibrated_messages_tokens`；`reset_state` 一并清理校准状态。
- [x] 软/硬预算判断、`context_notice`、压缩前后对比、状态行、`/context` 面板全部切换为校准口径。
- [x] `context_status` 工具输出追加“最近实测”行；模型侧无需任何额外操作即可看到。
- [x] `cli_ui.py` 状态行升级为百分比+进度条+估算/实测双读数；工具栏彩色化，纯文本回退路径同步升级。
- [x] 新增 `/context` 命令，注册进 `SLASH_COMMANDS` 与帮助表；角色分解表含“固定开销”行，合计严格等于校准总量。
- [x] `main.py` 在 `record_model` 中接线实测记录；`/clear`、`/new`、`/resume` 重置锚点。
- [x] 新增 `ContextUsageDisplayTests`（19 项）：分解口径求和、校准锚定与增量、实测记录边界、record_model 接线、状态行/进度条、面板渲染、会话切换重置。
- [x] 全套 90 项测试通过。

## 十六、Windows 通知（Notifier）

### 目标

Agent 在终端里等用户确认时，用户往往在别的窗口工作。本阶段给 Harness 加 Windows 10/11 原生 toast 通知，在两类时刻把用户"叫回来"：

1. **需要人工审批**：`run_bash` / `run_pwsh` 执行确认、`git_commit` 提交确认、`browser_click` 点击确认、`write_file` / `edit_file` 的 diff 审批（`confirm_edit`）。
2. **任务结束**：正常完成、达到最大步数、连续工具失败熔断终止，以及熔断暂停的"是否继续尝试？"确认。

通知是**纯增强**：只加 `notify()` 调用，不改动任何审批语义；任何失败路径静默返回 False，绝不影响主流程。

### 设计

**`notifier.py`（新模块）**：

- `notify(title, message, force=False) -> bool`：best-effort 弹 toast。
  - 桥接方式：系统自带的 Windows PowerShell 5.1（`powershell.exe`）。**WinRT 投影 `[Windows.UI.Notifications.ToastNotificationManager]` 只在 5.1 可用，pwsh 7 不可用**，所以必须用 `powershell.exe` 而非 `pwsh`。显式指向 `%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe`，不受 PATH 干扰。
  - 参数数组 + `shell=False`；stdin/stdout/stderr 全部 DEVNULL；**不 wait**（fire-and-forget）——powershell 冷启动 1~2 秒，交互循环不能被它阻塞。
  - `creationflags`：stdout 是 TTY 时用 `CREATE_NO_WINDOW` 隐藏黑框；无控制台环境（沙盒 worker、GUI 宿主）省略该 flag（实测这种环境下带 flag 的子进程会以 0xC0000142 起不来，省略后正常）。
  - toast XML 用 ToastGeneric 模板（title + body 两行）；title/message **先截断（200/500 字符）再 XML 转义**（`& < > " '` 五字符转成实体），保证合法实体不被拦腰截断；换行压成空格，避免破坏 here-string。
  - AppId（AUMID）用 PowerShell 已注册的 `{1AC14E86-1E54-5FA7-B6C6-A3C4E4A5123B}\WindowsPowerShell\v1.0`（BurntToast / winotify 同款默认值），不额外写注册表。未注册 AppId 在 Win11 上 toast 可能不显示。
  - WinRT 细节：`ToastNotificationManager`、`ToastNotification`、`XmlDocument` 三个投影类型都要用 `Namespace, ContentType = WindowsRuntime` 语法显式加载；静态方法是 `CreateToastNotifier`（不存在 `CreateToast`）；XML 经单引号 here-string 传入，零插值零转义歧义。
  - **守卫条件**（`force=False` 时任一命中即 no-op 返回 False）：非 Windows 平台；`sys.stdout.isatty()` 为 False（**必须有 TTY 守卫**——测试跑在捕获输出环境，没有守卫的话跑测试会在用户桌面狂弹通知）；`config.NOTIFY_ENABLED` 为 False。
  - `force=True` 跳过全部守卫，供冒烟与测试用。
  - 失败处理：整体包在 `try/except Exception` 里，任何异常（含 powershell.exe 不存在）吞掉返回 False；但 `KeyboardInterrupt` / `SystemExit` 是进程生命周期信号，不属于失败路径，照常外抛。
- `build_ps_args(title, message) -> list[str]`：PowerShell 参数数组组装的纯函数，无进程即可单测。

**钩子点**（只加 notify 调用，不改审批流程）：

| 位置 | 时机 | 标题 | 正文 |
|------|------|------|------|
| `tools._approve_shell` | run_bash / run_pwsh 共享审批，`Confirm.ask` 前 | 需要审批: bash / PowerShell 7 | 命令前 200 字符 |
| `tools.git_commit` | `Confirm.ask` 前 | 需要审批: git_commit | add -A + commit 预览 |
| `tools.browser_click` | 仅需人工确认时（`BROWSER_AUTO_APPROVE_CLICKS=false`） | 需要审批: browser_click | 元素编号 + 会话 |
| `cli_ui.confirm_edit` | write_file / edit_file 的 diff 确认前 | 需要审批: 文件修改 | 文件名 + 新建/修改 |
| `main` 熔断暂停 | "是否继续尝试？"`Confirm.ask` 前 | 任务已暂停 | 连续 N 次工具失败 |
| `main` 任务结束 | `stats.print_summary` 后，浏览器回收前 | 任务完成 / 任务终止 | 用户请求前 100 字符 + 步数/错误统计 |

### 配置

| 配置 | 默认值 | 用途 |
|------|--------|------|
| `NOTIFY_ENABLED` | True | 总开关；`HQIN_NOTIFY=false/0/no` 关闭 |
| `NOTIFY_SOUND` | 空（系统默认音） | 自定义提示音：PCM 编码 `.wav` 的绝对路径，`HQIN_NOTIFY_SOUND` 指定 |

`print_config()` 已加入对应行（配置项 / 环境变量两列）。

**自定义提示音设计**：Windows toast 的 `<audio>` 只接受系统预置音效或打包应用资源，**不能指向任意文件**。因此自定义声音走"toast 静音 + 同一 PowerShell 进程内 `System.Media.SoundPlayer` 播放 wav"的组合：

- wav 必须是 **PCM 编码**（SoundPlayer 不认 mp3/ogg/aac；44.1kHz/16-bit 最稳，建议 2 秒内）。
- 路径校验在 Python 侧完成（`_resolve_sound`：配置空/文件不存在 → 退回系统默认音）；PowerShell 侧收到的是单引号转义字面量，杜绝注入。
- 配置每次 notify 调用时重新读取，运行中修改 `HQIN_NOTIFY_SOUND` 立即生效，无需重启。
- `PlaySync` 在 fire-and-forget 的后台进程里执行，不阻塞交互循环（wav 过长只会拖住后台进程）。

### 已知限制

- **AppId 注册差异（已自愈）**：硬编码的 PowerShell AUMID 依赖系统注册的快捷方式，精简版 Windows / 纯 pwsh 7 环境上可能未注册，toast 被无声拒收（0x80073D54"没有包标识"）。现由「AUMID 解析与自愈」解决：notify 自动枚举本机已注册 AUMID，诊断不通过时 `--register` 一条命令自注册。详见下文。
- **专注助手 / 勿扰模式会吞通知**：Windows 专注模式开启时 toast 进入通知中心但不弹横幅；这是系统级行为，应用层无法越过。
- **非 TTY 环境不弹**：设计如此——CI、管道、测试环境没有 TTY 守卫就会狂弹通知。非交互宿主需要通知时用 `force=True`。
- **必须用 powershell.exe 而非 pwsh**：WinRT 投影只在 Windows PowerShell 5.1 可用，pwsh 7 投影不出 `Windows.UI.Notifications`。
- fire-and-forget 意味着"返回 True"只代表 powershell 成功启动，不代表 toast 真的送达（焦点助手、AUMID 问题都发生在 powershell 内部，进程已脱离父进程视线）。这是避免阻塞交互循环的代价；诊断入口用同步路径补齐了"看不到失败"的盲区。
- DSH 沙盒会话内实测：toast 平台 RPC 被沙盒拦截（对任何 AUMID 都报 0x80073D54），因此沙盒内冒烟只能验证到"powershell 成功启动"；用户真实终端环境（有 TTY、无沙盒）才是通知的生效环境。
- 诊断注册表检查是**只读**的：`ToastEnabled` / 应用级 `Enabled` 键不存在时 Windows 语义是"默认开启"，诊断输出按"未知（通常等于开启）"表述，不做武断结论。专注助手实时状态存在 CloudStore 二进制里，注册表读不到可靠值，恒提示手动检查。

### 实施验收

- [x] `notifier.py` 提供 `notify` / `build_ps_args`；XML 转义、截断、换行压平、here-string 安全均有单测覆盖。
- [x] 守卫矩阵单测：非 Windows / 非 TTY / TTY 检测抛异常 / 配置关闭 → 不启动进程；TTY + 开启 → `CREATE_NO_WINDOW`；force=True 一路放行。
- [x] 失败路径单测：Popen 抛 `OSError` 被吞返回 False；`KeyboardInterrupt` 照常外抛；`config.NOTIFY_ENABLED` 缺失按关闭处理。
- [x] 钩子点单测：`_approve_shell`、`git_commit`、`browser_click`（含自动审批不通知）、`confirm_edit` 在 `Confirm.ask` 之前收到正确标题/正文。
- [x] Agent Loop 单测：脚本化模型驱动真实会话，任务完成→「任务完成」通知；达到最大步数→「任务终止」通知。
- [x] `config.py` 新增 `NOTIFY_ENABLED`（`HQIN_NOTIFY`）并进入 `print_config()`。
- [x] `NOTIFY_SOUND`（`HQIN_NOTIFY_SOUND`）自定义提示音：toast 静音 + SoundPlayer 同进程播 PCM wav；`_resolve_sound` 边界（空/不存在/正常）与脚本组装（静音标记、单引号转义、PlaySync）均有单测。
- [x] 新增 `tests/test_notifier.py`（32 项）全部通过；`tests.test_harness` 全套 90 项通过（合计 166 项，含自定义提示音 4 项）。
- [x] 真实冒烟：`notifier.notify(..., force=True)` 返回 True（powershell 成功启动；toast 送达受上述沙盒/勿扰限制）。

### 诊断入口（python -m notifier）

主路径保持 fire-and-forget 不变；CLI 入口改走**同步**路径补齐"失败不可见"的盲区：

```bash
# ① 诊断：同步发一条测试 toast，打印注册表读数 + PowerShell 报错原文 + 中文建议
.venv\Scripts\python.exe -m notifier "测试标题" "测试正文"

# ② 自愈：注册 HQin 专用 AppId（写开始菜单 .lnk + AppUserModelId 注册表）
.venv\Scripts\python.exe -m notifier --register

# ③ 再诊断确认：应显示「AppId: HQin.Harness.Notifier」+ 退出码 0
.venv\Scripts\python.exe -m notifier

# ④ 真实链路验证（走 notify 主路径，confirm_edit 钩子用的就是它）
.venv\Scripts\python.exe -c "import notifier; print('ok' if notifier.notify('HQin Harness','看到这条说明通知已生效', force=True) else 'failed')"
```

诊断输出的字段含义：

| 字段 | 含义 | 怎么读 |
|------|------|--------|
| `AppId(AUMID)` | 本次 toast 实际使用的应用身份 | `--register` 后应为 `HQin.Harness.Notifier` |
| `自注册快捷方式` | 开始菜单里 HQin 专用 .lnk 是否存在 | 不存在 → 可运行 `--register` |
| `系统通知总开关` | `ToastEnabled`（✗ = Windows 设置里通知被整体关闭） | ✗ 时去 设置 > 系统 > 通知 打开 |
| `AppId 级开关` | 当前 AUMID 的应用级通知开关 | ✗ 时在 设置 > 通知 里找到应用重新允许；"未知（键不存在）"通常是正常（未被单独关闭） |
| `专注助手` | 注册表读不到实时值（存 CloudStore 二进制），恒提示手动检查 | Win+A 看一眼 |
| `powershell 退出码` | 0 = 脚本层面成功；非 0 = 报错原文在 stderr 里 | 0 但没弹 → 勿扰/通知中心/横幅折叠 |
| `stderr 原文` | PowerShell 报错原文（0x80073D54 等） | 照着「排查建议」走 |

### AUMID 解析与自愈

notify 不再硬依赖 PowerShell 快捷方式 AUMID：

- `resolve_aumid()` 进程内缓存一次，之后零开销。优先级：
  1. 自注册 `.lnk` 存在（`%APPDATA%\...\Start Menu\Programs\HQin Harness Notifier.lnk`）→ `HQin.Harness.Notifier`；
  2. 否则同步跑一次 `Get-StartApps` 枚举本机已注册 AUMID，经 `pick_aumid()` 挑选；
  3. 枚举失败（老系统没有该 cmdlet、子进程被拦）回落硬编码默认值。
- `pick_aumid(registered) -> str` 纯函数，优先级：含 `WindowsPowerShell` → 含 `Microsoft.PowerShell` → 其他 PowerShell 变体 → 默认值。全部小写匹配、忽略空白项。
- 枚举只发生在首次 notify（或诊断），上限 30 秒，失败静默回落。

### 自注册（--register，best-effort）

若诊断发现本机没有理想 AUMID（0x80073D54 / toast 不弹），一条命令自愈：

- `do_register()` 同步执行：`Add-Type` 编译内嵌 C#（`IShellLinkW` + `IPersistFile` + `IPropertyStore`，完整 vtable 顺序声明），在**当前用户开始菜单**创建 `HQin Harness Notifier.lnk`，目标指向 powershell.exe，并写入 `PKEY_AppUserModel_ID = HQin.Harness.Notifier`；同时在 `HKCU\Software\Classes\AppUserModelId\` 下补 `DisplayName` 注册表项。
- 这是非打包桌面应用获得 toast 身份的标准做法（BurntToast、windows-toast 库同款原理）。
- 成功后清空 AUMID 缓存，notify 立即切换到专用 AppId。
- 任何失败静默返回 `(False, 原因)` 并在诊断输出里说明；不向上抛。
- `register_args()` 单独暴露参数数组组装，离线单测覆盖形状、AUMID 嵌入、引号转义。

### 排查建议逻辑（advise_from_error）

纯函数：输入 stderr 原文 + 退出码 + 注册表读数，输出中文建议列表——

- `0x80073D54` / "没有包标识" → 建议 `--register`（最常见原因）；
- `ToastEnabled=0` → 系统设置里开总开关；
- 应用级 `Enabled=0` → 设置里给该应用放行；
- 退出码 0 但没弹 → 勿扰（Win+A）/ 通知中心（Win+N）/ 多显示器全屏压制；
- 其他非零退出 + WinRT 加载错误 → 检查 System32 的 powershell.exe 是否被移除。

### 诊断与自愈的验收

- [x] `python -m notifier` 同步路径：wait + 捕获 stdout/stderr + 保留退出码，报错原文透传；沙盒禁管道时自动降级 DEVNULL 重试（单测覆盖 PermissionError 分支）。
- [x] 只读注册表检查：`ToastEnabled` / 应用级 `Enabled` / 专注助手提示；全部失败降级为「未知」，绝不抛错（单测覆盖 None/非 Windows/DWORD 解析）。
- [x] `pick_aumid` 优先级与空列表回落（7 项单测）；`resolve_aumid` 缓存语义：首次调用后枚举不再发生（call_count 断言）。
- [x] `do_register` 成功/失败/异常/非 Windows 四条路径全部静默返回（6 项单测）；`register_args` 形状、AUMID 嵌入、引号成对。
- [x] C# 互操作真实冒烟（沙盒内，工作区临时 .lnk）：Add-Type 编译通过、.lnk 创建成功、`PKEY_AppUserModel_ID` 写入后经 IPropertyStore 回读一致。
- [x] `tests.test_notifier` 扩至 72 项全绿；`tests.test_harness` 90 项全绿。

## 十七、权限模式（ask / workspace / full）

### 目标

从单一的 `ask`（每次都问）升级为三档权限模式，会话内用 `/mode` 即时切换。模式决定**哪些工具动作需要人工确认、哪些直接放行**，但**不改工作区路径校验**：`workspace.py` 的沙盒越界拦截在三种模式下全部保留，`full` 授予的是"动作免审批"，不是"文件系统越界"。

### 三档模式语义

| 模式 | 名称 | 文件读 | 文件写/编辑/docx | shell 命令 | git 提交 | 浏览器点击 | 沙盒越界拦截 |
|------|------|--------|------------------|-----------|----------|------------|--------------|
| `ask` | 询问模式 | 自由 | 每次弹 diff 确认 | 每次确认 | 每次确认 | 每次确认 | 保留 |
| `workspace` | 工作区信任（默认） | 自由 | **直接放行** | 每次确认 | 每次确认 | 每次确认 | 保留 |
| `full` | 完全信任 | 自由 | **直接放行** | **直接放行** | **直接放行** | **直接放行** | 保留（仍不越界） |

要点：

1. **只取消"确认等待"，不取消安全语义**：`workspace` 免去文件写入审批，因为路径校验已保证写不出去；shell/提交/点击仍需确认，因为命令内容不可静态审计。
2. **`full` 不等于 root**：`rm -rf` 在 full 模式下无需确认，但工作区校验仍然拦截工作区外的删除。想跨出工作区时依旧拒绝——这与 DSH 的 `danger-full-access` 不同，是有意的保守设计：CLI 无日志审计 UI，越界风险不对称。
3. **切换即时生效**：模式存于主循环变量，`/mode` 切换后下一次工具调用即按新模式执行；不打断进行中的任务。
4. **启动默认 `workspace`**：环境变量 `HQIN_MODE` 可设初始模式（值为 `ask/workspace/full` 之一，非法值回落 `ask` 并提示）——默认信任工作区内的读写改，命令仍需审批。
5. **状态行左侧常显当前模式**：`mode:workspace` 徽标，颜色区分（ask 黄、workspace 青、full 红），prompt_toolkit 工具栏与纯文本回退路径同步。

### /mode 命令契约

| 调用 | 行为 |
|------|------|
| `/mode` | 打印当前模式 + 三档语义表 |
| `/mode ask` / `/mode workspace` / `/mode full` | 切换；未知值打印可用值并保持原模式 |
| `/mode full` 时 | 额外一次确认（"完全模式将跳过所有审批，确认？"）——full 是危险档位，防误触 |

`/help`、`/status`、`print_banner` 同步显示模式信息；System Prompt 不注入模式（模型不需要知道审批机制的存在）。

### 实现设计

**模式存储**：`main.py` 主循环局部变量 + `cli_ui.py` 纯函数工具（`normalize_mode`、`mode_label`、`mode_color`）。不引入 config 常量以外的新全局；tools 层通过参数获取当前模式，不自己存状态。

**审批闸门改造**（唯一侵入点，4 处）：

| 位置 | 原逻辑 | 新逻辑 |
|------|--------|--------|
| `tools._approve_shell` | 无条件 `Confirm.ask` | `mode == "ask"` 时确认，否则放行并打印 `[dim]mode=full 已自动批准 shell 命令[/dim]` |
| `tools.git_commit` | 无条件 `Confirm.ask` | 同上 |
| `tools.browser_click` | `BROWSER_AUTO_APPROVE_CLICKS` 开关 | 叠加：`mode == "full"` 也直接放行（`workspace` 仍按原开关） |
| `cli_ui.confirm_edit`（diff 审批） | 无条件 `Confirm.ask` | `mode == "ask"` 时确认，否则跳过 diff 打印直接放行 |

**模式传递**：主循环把 `mode` 存进 `tools._permission_mode`（模块级变量 + setter），`cli_ui.confirm_edit` 由 `main` 安装 hook 时闭包捕获——**工具执行链不感知模式切换**，只有 4 个闸门读它。切换模式即改这一个变量，无传播逻辑。

**通知联动**：4 个闸门在"需要确认"时才弹通知；`workspace`/`full` 自动放行的动作**不弹通知**（没打扰就没有通知）。任务结束通知不变。

**执行期读数行**：`>> Agent Step` 下方的上下文行左侧加模式徽标：`mode:ask | ctx:12% ...`。

### 配置

| 配置 | 默认值 | 用途 |
|------|--------|------|
| `MODE` | `workspace` | 初始权限模式；`HQIN_MODE` 环境变量覆盖 |

### 已知限制

- `full` 下 `browser_click` 跳过敏感标签拦截**不**成立：`browser_policy.click_block_reason`（登录/支付等敏感标签拒绝）仍然生效——模式免审批，不豁免策略拒绝。
- `workspace`/`full` 下文件写入仍执行 Python 语法检查、大小上限、docx 拦截等**确定性校验**；免去的只是人工确认。
- 模式不进会话存档：恢复会话后回到启动时的初始模式（`HQIN_MODE`），不继承存档时刻的模式——模式是"操作者当下的意愿"，不是会话状态。
- 并行工具白名单不变：写操作在任何模式下都串行。

### 实施验收

- [x] `cli_ui.py` 新增 `normalize_mode` / `mode_label` / `mode_color` / `print_mode_help`；`SLASH_COMMANDS` 注册 `/mode`。
- [x] `main.py`：`/mode` 分发（含 full 二次确认）；启动读取 `HQIN_MODE`；模式徽标进入状态行与执行期读数行；`print_banner`/`print_status` 显示模式。
- [x] `tools.py`：`_permission_mode` + `set_permission_mode`；`_approve_shell`/`git_commit`/`browser_click` 三个闸门按模式短路；自动放行时打印提示行（不静默——可审计）。
- [x] `cli_ui.confirm_edit`：`workspace`/`full` 跳过 diff 确认直接放行（语法检查仍在 tools 层）。
- [x] `config.py` 新增 `MODE`；`print_config()` 加行。
- [x] 通知不弹：`workspace`/`full` 下自动放行的动作不触发 notify。
- [x] 测试：模式切换命令解析、三档闸门行为（mock Confirm 确认从未调用）、full 二次确认、浏览器敏感标签在 full 下仍拦截、状态行/执行期行含模式徽标、非法 HQIN_MODE 回落 ask（PermissionModeTests 17 项）。
- [x] 全套 183 项测试通过（90 harness + 72 notifier + 17 权限模式 + 4 声音）。

## 十八、工作流提示词与步数预算制

### 背景

两个问题一起暴露了同一个架构短板：
1. 模型面对任务没有基本工作纪律——直接扎进代码，不复述需求、不写计划、不测试。
2. 固定 `for step in range(15)` 循环把步数当“任务死刑”：复杂任务（探索+计划+实施+测试天然需要 20+ 步）到上限被硬切断，模型永远完不成大任务；而 Claude Code 类产品没有可见步数，因为它们把终止权交还给了模型——模型不再调用工具即任务结束，步数护栏只防失控、不判死刑。

### 工作流提示词（prompts.py）

新增【标准工作流】六步纪律，要求中等以上复杂度任务严格执行、简单任务可合并快走：

1. **理解需求**：开工前复述任务目标、交付物、验收标准；有歧义先问再猜。
2. **探索现状**：先 list_dir/read_file/search_in_files，禁止没读过文件就改它。
3. **写计划**：2 个以上文件/新功能/架构改动的任务必须先写简短计划文档，用户过目后再动手。
4. **小步实施**：一次一个子任务，对照计划推进，不做计划外改动。
5. **测试验证**：代码任务必须跑已有测试或写小验证脚本；测试不过不得宣称完成。
6. **交付汇报**：自然语言总结做了什么/关键文件/测试结果/遗留事项。

同时新增工作原则 10“未完成就明说”：预算（步数/上下文）即将耗尽时明确告知已完成与缺失部分，宁可请求续期不交半成品。

### 步数预算制（main.py）

| 项 | 旧 | 新 |
|----|----|----|
| 循环结构 | `for step in range(MAX_STEPS)` | `while True` + `step_budget` 计数 |
| 耗尽行为 | 直接 break，打印“达到最大步数” | 弹通知 + `Confirm.ask` 询问是否追加 `STEP_RENEWAL`（默认 10）步 |
| 终止权 | 代码说了算 | 模型自然停止（无工具调用）为主路径；预算是护栏；用户拒绝续期才中止 |
| 步数显示 | `Step 3/15` | `Step 3（剩余预算 12）` |
| 中止通知 | “任务终止” | “任务中止（已执行 N 步）” |

新配置：`STEP_RENEWAL`（`HQIN_STEP_RENEWAL`，默认 10）。

### 关于 LangChain 的结论

不需要。HQin 已具备消息循环 + 原生工具调用 + 上下文管理 + 沟通渠道的完整自研架构，每层可控可调试；LangChain 提供的现成拼装以引入多层抽象、行为黑盒、调试困难为代价，且给不了“模型工作纪律”这种提示词层面的东西。保持自研。

### 实施验收

- [x] `prompts.py` 新增【标准工作流】六步 + 原则 10“未完成就明说”。
- [x] `main.py` 步数循环改预算制：预算耗尽 → 通知 + 确认续期 → 拒绝则中止；自然完成仍是主退出路径。
- [x] `config.py` 新增 `STEP_RENEWAL`；`print_config()` 加行；步数显示改为“剩余预算”。
- [x] 通知测试适配新语义：预算耗尽先弹“步数预算已用完”，拒绝续期后“任务中止”。
- [x] 全套 183 项测试通过。

## 十九、流式输出、免审批测试与任务快照

### 背景

三项同属"任务执行体验与安全网"批次：
1. 模型生成 4096 token 需静默 2~4 分钟，全程黑盒；想放弃只能 Ctrl+C，而 Ctrl+C 会杀掉整个会话（只靠退出存档兜底）。
2. 上一批立法"代码任务必须测试"，但 run_bash 在 workspace 模式仍需审批——工作流纪律与权限模型打架，每个代码任务凭空多出 2~3 次审批。
3. workspace/full 模式放开写文件后没有后悔药：改坏没有一键回滚。

### 1. 流式输出 + 任务级中断（main.py）

- `_create_completion` 增加 `stream=True` 路径：逐块拼接 tool_calls 的参数分片与文本增量，Rich `status` 显示"生成中..."+已收字符数，收完一次性返回与原结构完全相同的响应对象。重试逻辑保留（流式失败回退非流式重试）。
- **Ctrl+C 语义改为任务级**：任务循环内捕获 KeyboardInterrupt → 打印"已中断当前任务"，丢弃本轮已生成内容，回到输入框等待下一条指令；只有**输入行**按 Ctrl+C / Ctrl+D 才退出会话。退出存档逻辑保留。
- 中断不产生半截 tool_calls：流式收集器在未收完前不执行任何工具；中断即丢弃，绝不执行残缺参数。

### 2. run_tests 免审批测试工具（tools.py + tool_schemas.py）

- 新工具 `run_tests(target=".", extra_args="")`：以参数数组执行 `.venv/Scripts/python.exe -m pytest target extra...`（回退 `python -m unittest` 形式），cwd=工作区，超时独立配置（默认 300s，测试天然比命令长）。
- **白名单约束**：只接受 pytest 可识别的目标路径/测试 ID；extra_args 只允许 `-k -x -v -q --tb -m --junit` 等测试开关，拒绝 `;` `&&` `|` 重定向与 shell 元字符（参数数组 + shell=False 本身已杜绝注入，白名单是纵深防御）。
- **审批语义**：ask 模式仍确认；workspace/full **免审批**——理由：测试代码由 Harness 写入流程保护（语法检查/审批在写文件时已发生），跑测试不授予超出写文件的能力，且工作流纪律要求模型频繁自测，审批摩擦会实质性地抑制测试行为。
- 输出截断：末尾优先（保留失败摘要），上限 `TESTS_OUTPUT_LIMIT`（默认 6000 字符）。
- System Prompt 工作原则 3 更新："优先用 run_tests 跑测试"；并行白名单加入 run_tests。

### 3. 任务前 git 快照 + /undo（main.py + tools.py）

- 任务开始时（用户输入进入循环前）自动快照：工作区是 git 仓库 → `git add -A && git commit --allow-empty -m "[hqin-snapshot] 任务前快照"`（仓库脏时）；非仓库 → 跳过（不自动 git init，避免污染用户目录）。快照失败静默（通知不弹）。
- `/undo`：`git reset --hard HEAD~1` 回滚最近一次快照之后的全部改动，二次确认 + 列出将丢弃的文件（`git status --porcelain`）；无快照时提示。
- 快照是普通 git 提交，用户可交互式 rebase 清理；提交者标记 `HQin Snapshot <hqin@local>`。
- 已知限制：只回滚最近一次任务前的快照；跨多次任务的累计回滚用 git 手动操作。

### 配置

| 配置 | 默认值 | 用途 |
|------|--------|------|
| `TESTS_TIMEOUT_SECONDS` | 300 | run_tests 超时 |
| `TESTS_OUTPUT_LIMIT` | 6000 | run_tests 输出上限（末尾优先） |

### 实施验收

- [x] 流式路径与原响应结构等价（脚本化流块驱动单测：文本拼接、tool_calls 参数分片重组、usage 提取、中断传播）；Ctrl+C → TaskInterrupted 丢弃本轮回到输入框，会话存活。
- [x] run_tests：白名单拒绝注入字符与未知开关、拒绝绝对/越界 target、ask 确认与 Refused 语义、workspace/full 免审批、非零退出为 Error；pytest 缺失回退 unittest；沙盒禁管道时降级透传并返回 Info 语义（真实终端不受影响）。
- [x] 快照：仓库脏时自动提交（标记 [hqin-snapshot]）、干净/非仓库跳过；/undo 校验 HEAD 确为快照后 reset --hard + 二次确认 + 文件清单预览；HEAD 被用户提交推进时拒绝误回滚。
- [x] System Prompt 更新（工具表 + 工作原则 3 + 工作流 5 优先 run_tests）；run_tests 进入并行白名单。
- [x] 全套 195 项测试通过（新增 12 项；3 项快照测试在禁管道沙盒内自动 skip，真实终端执行）。
- [x] 真机冒烟：run_tests 以真实 pytest 跑通 tests/test_notifier.py::PickAumidTests（7 passed）。
