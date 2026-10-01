# HQin Harness 构建计划

## 一、 系统架构设计

系统主要分为四个核心模块，均采用 Python 编写：

1. **终端交互层 (CLI UI)：** 负责接收用户 Bug 描述，打印模型的思考过程，以及拦截并请求用户授权（Human-in-the-loop）危险操作（如执行 Bash 脚本、修改文件）。推荐使用 `rich` 和 `prompt_toolkit` 库。
2. **大模型网关 (LLM Gateway)：** 使用标准的 `openai` Python 库对接 LM Studio 的本地 Server (`http://localhost:1234/v1`)。
3. **工具解析与路由层 (Tool Parser & Router)：** **（本项目最核心部分）** 负责解析模型输出的纯文本，通过正则表达式或 XML 解析提取出模型想要执行的动作（Action），并路由给具体的执行器。
4. **沙盒与环境执行器 (Action Executor)：** 包含读取文件（带行号）、搜索替换（Search & Replace）、本地 Bash 执行（带有安全拦截）以及 Git 操作模块。

## 二、 核心机制约定 (针对 4B 模型的妥协与优化)

由于 Qwen3.5-4B-GGUF 模型能力有限（无原生 Tool Calling，8k 上下文），我们需要建立非常明确的交互协议：

### 1. 伪装工具调用 (Simulated Tool Calling)
强制模型在回答时使用 XML 标签来调用工具。例如：
```xml
<thought>我需要查看 main.py 的内容来定位 bug。</thought>
<action name="read_file">
  <path>main.py</path>
</action>
```

Python 端拦截到 `<action>` 后，执行完毕，将结果作为 `Observation`
返回给模型。

### 2. Search & Replace 格式

采用最严格的 SEARCH/REPLACE 块格式，要求 SEARCH
部分必须与原文件完全一致。

``` text
<action name="edit_file">
    <path>main.py</path>
    <<<< SEARCH
def add(a, b):
    return a - b
    ====
def add(a, b):
    return a + b
    >>>> REPLACE
</action>
```

### 3. 上下文管理（8k 限制）

读取文件时必须支持分页或仅读取特定行数，防止一次性读取大文件撑爆 8k
上下文：

``` xml
<action name="read_file">
    <path>main.py</path>
    <start>10</start>
    <end>50</end>
</action>
```

## 三、分步构建计划（MVP 阶段）

### Step 1: 基础设施搭建

-   **目标**：跑通终端与 LM Studio 的基础对话。
-   **任务**：
    1.  初始化 Python 项目（建议使用 `poetry` 或 `uv`）。
    2.  安装依赖：`pip install openai rich prompt_toolkit`。
    3.  编写 `llm_client.py`，封装 OpenAI 客户端连接到
        `http://localhost:1234/v1`。
    4.  编写一个极简的 CLI 循环：读取用户输入 -\> 发送给模型 -\>
        打印流式输出。

### Step 2: 建立"伪工具调用"解析器

-   **目标**：让 Python 能够听懂 4B 模型的命令。
-   **任务**：
    1.  编写精心设计的
        `System Prompt`，明确告诉模型它有且仅有几个工具（`read_file`、`edit_file`、`run_bash`），并给出正例和反例。

    2.  编写 `parser.py`，使用正则表达式提取模型回复中的
        `<action name="...">...</action>` 内容。

    3.  实现基础的 Agent Loop：

        `User 提问 -> LLM 思考并输出 Action -> 解析 Action -> 执行 Action -> 将结果包装为 System Message 给 LLM -> 循环`

### Step 3: 实现核心工具集

-   **目标**：实现文件操作与人工审批机制。
-   **任务**：
    1.  实现 `read_file`：给文件内容加上行号返回，方便模型定位。

    2.  实现 `edit_file`：编写逻辑，严格匹配 `<<<< SEARCH` 和 `====`
        之间的文本，并替换为 `====` 到 `>>>> REPLACE`
        之间的文本。如果匹配失败，必须给模型返回明确的报错信息（如"匹配失败，请检查空格和缩进"）。

    3.  实现 `run_bash`：使用 `subprocess`
        执行命令，但在执行前，在终端弹出提示框：

        `"模型想要执行指令：pytest tests/ 允许吗？[Y/n]"`

### Step 4: 串联 MVP 任务流

-   **目标**：完整跑通"修 Bug -\> 测试 -\> 提交"的闭环。
-   **任务**：
    1.  提供一个简单的测试项目（例如一个有简单运算错误的 Python
        脚本和对应的测试用例）。
    2.  在终端启动 HQin Harness，输入："帮我修复 `math_ops.py` 中的减法
        bug，跑通测试后提交 commit。"
    3.  调试 Agent Loop，观察 4B 模型是否会卡死或幻觉。如果是，调整
        System Prompt，甚至可以把当前目录树（通过 `tree`
        命令获取的小范围结构）作为初始上下文注入。

## 四、优化与功能扩建阶段

MVP 已经证明了可行性，本阶段的核心目标是**提升模型执行效率、增强交互体验，以及防止内存溢出**。

### Step 1: 动态注入环境上下文（减少试错成本）

- **痛点**：目前模型需要通过试错（执行 `ls` 报错）才能猜到当前是 Windows 系统。

- **任务**：

  1. 在 `main.py` 启动时，使用 Python 的 `platform.system()` 和 `os.getcwd()` 获取当前操作系统类型和工作目录全路径。

  2. 动态修改 `SYSTEM_PROMPT`，在开头加入类似提示：
     
     > [系统信息] 你当前运行在 Windows 系统中，初始工作目录为 `D:\pycharm\HQin_Harness`。请使用适合该系统的终端命令。

### Step 2: 持久化会话模式 (Interactive Mode)

- **痛点**：当前代码完成一个任务后就会 `break` 退出程序，每次都要重新运行。

- **任务**：

  1. 将 `main.py` 底部的执行逻辑改为一个 `while True` 循环，类似终端 CLI。

  2. 当模型调用 `finish` 汇报后，不退出程序，而是再次弹出 `> 请输入你的需求：`。

  3. 保留历史 `messages` 数组，让模型记住刚才做过的事情。

### Step 3: 上下文截断与保护 (防 8k 爆显存)

- **痛点**：如果开启了持久化会话，或者模型反复读写大文件，`messages` 数组会迅速膨胀，撑爆 4B 模型的 8k Token 限制，导致 API 报错。

- **任务**：

  1. 在 `main.py` 的 Agent Loop 中加入简单的上下文长度估算（例如按字符数粗略估算，1 个汉字≈1 token，3 个英文字母≈1 token）。

  2. 当 `messages` 长度接近危险阈值（如 6000 tokens）时，触发滑动窗口截断：保留最初的 `system` prompt，丢弃中间较老的 `User(Observation)/Assistant(Action)` 对话，只保留最近的 3-5 轮核心交互。

### Step 4: 引入“全局搜索”工具 (增强探索能力)

- **痛点**：目前模型不知道文件在哪时，只能用 `dir` 盲目查找，难以应对真实复杂项目。

- **任务**：

  1. 在 `tools.py` 新增 `search_in_files(keyword, directory=".")` 工具，递归遍历历目录并返回包含关键字的文件路径及行号。

  2. 更新 `parser.py` 解析 `<action name="search_in_files">`。

  3. 更新 `prompts.py`，告诉模型现在拥有了第 5 个工具。

### Step 5: Linter 语法校验防线

- **任务**：在 `edit_file` 执行替换之后，立刻在后台使用 Python 原生的 `ast.parse()` 或 `py_compile.compile()`
对修改后的 `.py` 文件进行语法检查。如果存在缩进错误或语法错误，直接撤销修改并把报错信息返回给模型，强制其自纠错，从而不将崩溃代码留给用户。
