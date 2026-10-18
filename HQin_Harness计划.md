# HQin Harness 计划

## 一、系统架构

系统主要分为四个模块，用 Python 编写：

1. **终端界面 (CLI UI)**：打印用户 Bug 和模型的思考过程，以及带人工审批权（Human-in-the-loop）的命令执行提示，使用 `rich` 库。
2. **LLM 网关 (LLM Gateway)**：使用标准的 `openai` Python 库接入 LM Studio 的本地 Server（`http://localhost:1234/v1`）。
3. **工具调用（Route B - 原生 Tool Calling）**：直接使用 OpenAI 兼容的 `tools=` 参数传入 JSON Schema，让框架负责格式，彻底消除 XML 解析这一整类错误。工具新增只需在 `tool_schemas.py` 追加一个 dict。
4. **沙盒执行 (Action Executor)**：带行号读取文件、Search & Replace 替换、Bash 执行（带安全拦截），以及 Python 语法自动检查。

## 二、协议约定

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

### 3. 两级上下文管理（防爆显存）

| 级别 | 触发条件 | 动作 |
|------|----------|------|
| **一级：摘要压缩** | 超过 `SUMMARY_THRESH = 16000` 字符 | 仅在完整 tool-call/tool-result 块之后切分；调用 LLM 将旧历史浓缩为 ≤150 字摘要 |
| **二级：硬删除兜底** | 压缩后仍超过 `MAX_CHARS = 24000` 字符 | 按完整用户轮次删除，绝不留下孤立的 tool result；单个超长最新轮次宁可保留，也不破坏协议 |

字符数只作为保守兜底，不能等价于 token 数；中文最坏情况接近 1 字符/token。LM Studio 建议将 Context Length 设为 32,768，并为 system prompt、工具 schema 和生成结果预留空间。

### 4. 行范围读取（防单文件撑爆上下文）

```python
read_file(path="main.py", start=10, end=50)  # 只读第 10~50 行
```

## 三、分步计划（MVP 阶段）

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
- 最大步数限制 `MAX_STEPS = 15`

## 四、优化与功能阶段

### Step 1：动态注入环境信息 ✅
- 启动时用 `platform.system()` + `os.getcwd()` 获取系统信息
- 动态格式化 `SYSTEM_PROMPT`，在开头告知模型当前 OS 和工作目录

### Step 2：持久化交互模式 ✅
- `main.py` 底层为 `while True` 循环，任务完成后不退出
- 保留 `messages` 数组，模型记住跨任务上下文

### Step 3：两级上下文管理 ✅
- `_msg_chars()` — 统计单条消息字符数（兼容 `tool_calls` 和普通 `content`）
- `_total_chars()` — 统计全部消息字符数
- `_trim_messages_hard()` — 按完整用户轮次硬删除，保持 tool-call/tool-result 配对
- `_compress_with_summary()` — 仅在完整工具结果块之后压缩，失败自动降级为硬删除
- `manage_context()` — 统一入口，先摘要后兜底，每轮调用模型前触发

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

## 五、Phase 3 功能（已全部完成）

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
- `web_search(query)` — 使用 DuckDuckGo，**无需 API Key**
- 两级降级策略：① Instant Answer JSON API（快，适合摘要）→ ② Lite HTML 解析（完整搜索结果）
- 结果数量由 `config.WEB_SEARCH_MAX` 控制（默认 5 条）

## 六、工作区（沙盒）✅

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

## 七、文件结构

```
HQin_Harness/
├── main.py           # Agent Loop + 工作区向导 + 两级上下文管理 + 会话存档
├── config.py         # 全局配置（支持环境变量覆盖）
├── workspace.py      # 工作区管理模块（沙盒核心）
├── tools.py          # 工具实现函数 + TOOL_REGISTRY 路由表
├── tool_schemas.py   # OpenAI Function Calling JSON Schema
├── prompts.py        # System Prompt（含工作区信息）
└── HQin_Harness计划.md
```

## 八、工具一览

| 工具 | 状态 | 审批 | 说明 |
|------|------|------|------|
| `list_dir` | ✅ | 无 | 树形目录（过滤噪音目录），沙盒校验 |
| `read_file` | ✅ | 无 | 带行号，支持行范围，超 150 行自动分页 |
| `write_file` | ✅ | 无 | 含语法拦截，沙盒校验 |
| `edit_file` | ✅ | 无 | SEARCH/REPLACE + 语法拦截，沙盒校验 |
| `run_bash` | ✅ | ⚠️ 人工 | 在工作区 cwd 执行；非操作系统级沙盒，非零退出为 Error，输出长度可配置 |
| `search_in_files` | ✅ | 无 | 最多 40 条结果，沙盒校验 |
| `git_status` | ✅ | 无 | 在工作区执行，查看仓库状态 |
| `git_diff` | ✅ | 无 | 参数数组执行，diff 截断长度可配置，路径沙盒校验 |
| `git_commit` | ✅ | ⚠️ 人工 | 默认 add -A，需用户确认 |
| `web_search` | ✅ | 无 | DuckDuckGo 双级降级，无需 API Key |

## 九、运行方式

```bash
# 基本启动（交互式选择工作区）
.venv\Scripts\python.exe main.py

# 直接指定工作区
.venv\Scripts\python.exe main.py --workspace D:/my_proj

# 使用环境变量切换模型
$env:HQIN_MODEL = "qwen2.5-7b-instruct"
.venv\Scripts\python.exe main.py -w .

# 会话内查看配置
> config
```

## 十、当前性能与可靠性配置

- **模型请求**：默认 120 秒超时，失败后最多重试 2 次，退避时间由 `HQIN_MODEL_BACKOFF` 控制；每个任务输出模型调用次数、耗时和服务端返回的 token usage（若 LM Studio 提供）。
- **输出限制**：默认 `HQIN_MAX_OUTPUT_TOKENS=4096`，避免模型回复挤占完整上下文。
- **LM Studio 建议**：Qwen3.5-4B 使用 32,768 Context Length、GPU Offload 26、Max Concurrent Predictions 1、Unified KV Cache 开启。当前 CLI 单会话不会受益于服务端 4 路预测并发。
- **批处理调参**：先保持 Evaluation Batch Size 2048 / Physical Batch Size 512；若显存紧张、首 token 很慢或长上下文卡顿，依次尝试 1024 / 256。

