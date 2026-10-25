# tool_schemas.py
# Route B 核心：所有工具的 OpenAI Function Calling JSON Schema 定义。
# 新增工具时只需在此文件追加一个 dict，无需修改任何解析逻辑。

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "run_bash",
            "description": (
                "用显式 bash 执行命令（bash -c，不经过 cmd.exe/PowerShell）。"
                "适合 Unix 风格的管道、grep、find 和跨平台脚本；"
                "执行前会弹出人工审批确认。若系统没有 bash，会返回 ConfigurationRequired 并提示安装或设置 HQIN_BASH。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "要交给 bash -c 的完整命令，例如 'grep -rn TODO . | head'"
                    }
                },
                "required": ["command"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "run_pwsh",
            "description": (
                "用 PowerShell 7（pwsh）执行命令，适合 Windows 管理任务、.NET 类型和 PowerShell 管道。"
                "执行前会弹出人工审批确认。若没有 pwsh，会返回 ConfigurationRequired 并提示设置 HQIN_PWSH。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "要交给 pwsh -Command 的完整命令，例如 'Get-ChildItem -Recurse | Select-String TODO'"
                    }
                },
                "required": ["command"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "run_tests",
            "description": (
                "运行项目测试：优先 pytest，未安装时自动回退 unittest。"
                "workspace/full 权限模式免审批，适合频繁自测。"
                "输出超长时保留头尾（失败摘要在末尾）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "description": "测试目标：目录（tests）、文件（tests/test_x.py）或测试 ID（tests/test_x.py::TestY）；默认 '.' 跑全部"
                    },
                    "extra_args": {
                        "type": "string",
                        "description": "可选的 pytest 开关，如 '-x -q -k name'；只允许 -k/-x/-v/-q/-s/--tb/-m/--maxfail/--junitxml/--cov"
                    }
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "读取本地文件内容，每行前面带有行号，方便精确定位。"
                "支持通过 start/end 参数只读取指定行范围；单次输出还有字符预算，"
                "达到上限会给出下一次读取的 start 提示，避免大文件撑爆上下文。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "要读取的文件路径（绝对路径或相对于工作目录的路径）"
                    },
                    "start": {
                        "type": "integer",
                        "description": "起始行号（1-indexed，包含该行），不填则从第 1 行开始"
                    },
                    "end": {
                        "type": "integer",
                        "description": "结束行号（1-indexed，包含该行），不填则读到文件末尾"
                    }
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "创建一个新文件并写入完整内容；文件已存在则覆盖。"
                "单次内容上限 6000 字符，超过会要求拆块。"
                "适合创建全新脚本或配置文件；对 .py 文件会自动做语法检查。"
                "大文件请先用 write_file 写骨架，再用 append_file 追加。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "要写入的文件路径"
                    },
                    "content": {
                        "type": "string",
                        "description": "文件的完整内容；单次不超过 6000 字符"
                    }
                },
                "required": ["path", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "append_file",
            "description": (
                "向已有文件末尾追加文本，用于分块写大文件；文件不存在时会创建。"
                "不做 Python 语法检查，所以 Python 文件不要用它拼半截代码，"
                "应先用 write_file 写完整骨架，再用 edit_file 分段替换。"
                "单次内容上限 6000 字符。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "要追加的文件路径"
                    },
                    "content": {
                        "type": "string",
                        "description": "要追加的文本；单次不超过 6000 字符"
                    }
                },
                "required": ["path", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "finish_check",
            "description": (
                "交付前自检。检查文件是否存在、非空、可解析/可打开："
                "JSON 能否解析、CSV 列数是否一致、Python 能否编译、"
                ".docx 能否重新打开，并扫描 TODO/待补充等占位标记。"
                "不传 files 时检查本次会话成功写入的文件。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "files": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "要检查的交付物路径列表；省略则使用本次会话写入的文件"
                    }
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "create_docx",
            "description": (
                "在工作区内生成真正的 Word .docx 文档（不是纯文本改后缀）。"
                "title 作为文档标题；content 支持简单 Markdown："
                "#/##/### 标题、- 或 * 列表、1. 编号列表、**粗体**。"
                "路径若省略 .docx 会自动补全。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "输出文件路径，例如 '姚遥.docx'；省略后缀时自动补 .docx"
                    },
                    "title": {
                        "type": "string",
                        "description": "文档标题，会使用 Word 的 Title 样式"
                    },
                    "content": {
                        "type": "string",
                        "description": "正文内容，按行解析简单 Markdown 结构"
                    }
                },
                "required": ["path", "title", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": (
                "在已存在的文件中执行精确的 Search & Replace 编辑。"
                "search 参数必须与文件原内容完全一致（包括缩进、空格）。"
                "对 .py 文件修改后会自动做语法检查，语法错误时撤销修改。"
                "每次只替换第一处匹配到的内容。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "要编辑的文件路径"
                    },
                    "search": {
                        "type": "string",
                        "description": "要被替换的原始代码块，必须与文件内容完全一致，包括所有空格和缩进"
                    },
                    "replace": {
                        "type": "string",
                        "description": "用于替换的新代码块"
                    }
                },
                "required": ["path", "search", "replace"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_in_files",
            "description": (
                "在目录树中全局搜索包含指定关键字的代码行，"
                "返回匹配的文件路径和行号。"
                "当你不确定某个函数/变量/配置写在哪个文件时使用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {
                        "type": "string",
                        "description": "要搜索的关键字或代码片段"
                    },
                    "directory": {
                        "type": "string",
                        "description": "搜索的起始目录，默认为当前工作目录 '.'"
                    },
                    "glob": {
                        "type": "string",
                        "description": "只搜索匹配此 glob 的文件，例如 '*.py' 或 '**/*.ts'；不填则搜索全部文本文件"
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "最多返回多少条匹配（默认 40，上限 500）"
                    }
                },
                "required": ["keyword"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "glob_files",
            "description": (
                "按 glob 模式查找文件，返回相对路径、大小和修改时间。"
                "适合在阅读前快速定位文件；输出有数量上限，优先用 pattern 缩小范围。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "搜索起始目录，默认为当前工作目录 '.'"
                    },
                    "pattern": {
                        "type": "string",
                        "description": "相对 glob 模式，例如 '**/*.py'、'src/**/*.ts'、'*.md'"
                    },
                    "limit": {
                        "type": "integer",
                        "description": "最多返回多少个文件（默认 200，上限 1000）"
                    }
                },
                "required": ["pattern"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": (
                "列出指定目录下的文件和子目录结构（树形展示，自动过滤 .git/.venv 等无关目录）。"
                "在开始任务前先用此工具了解项目结构，避免盲目猜测文件位置。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "要列出的目录路径，默认为当前工作目录 '.'"
                    }
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "git_status",
            "description": (
                "查看 Git 仓库当前状态，包括已修改、已暂存、未跟踪的文件列表。"
                "在提交前先用此工具确认变更范围。"
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "git_diff",
            "description": (
                "查看文件的 Git diff（未暂存的变更）。"
                "path 为空时查看全部变更；指定 path 时只查看该文件。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "要查看 diff 的文件路径，留空表示查看所有变更"
                    }
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "git_commit",
            "description": (
                "将当前所有变更提交到 Git（带人工审批确认）。"
                "默认会先执行 'git add -A' 暂存所有修改再提交。"
                "完成代码修改并通过测试后使用此工具。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {
                        "type": "string",
                        "description": "Git commit message，简明描述本次变更内容，例如 'fix: 修复 add 函数返回值错误'"
                    },
                    "add_all": {
                        "type": "boolean",
                        "description": "是否先执行 git add -A 暂存所有变更，默认为 true"
                    }
                },
                "required": ["message"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "使用 Bing 中国站搜索网络内容，失败时回退 DuckDuckGo（无需 API Key）。"
                "适合查询：第三方库文档、Python/JS API 用法、报错信息含义、最佳实践等。"
                "不适合需要实时数据的查询（股价、天气等）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "搜索关键词或问题，英文效果更好，例如 'Python requests timeout handling'"
                    }
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "web_fetch_url",
            "description": "后台读取用户提供的公开 HTTP(S) 网页，返回 JavaScript 渲染后的 Markdown。不保留 Cookie，不能点击页面元素。",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "完整 http:// 或 https:// URL"}
                },
                "required": ["url"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "browser_open",
            "description": "用已配置的后台浏览器打开 URL 并建立会话。仅在需要点击、分页或多页浏览时使用；随后必须先 browser_state。",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "完整 http:// 或 https:// URL"}
                },
                "required": ["url"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "browser_state",
            "description": "返回当前后台网页的可交互元素及其编号。每次页面导航或点击后都必须重新调用，旧编号不可复用。",
            "parameters": {"type": "object", "properties": {}, "required": []}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "browser_click",
            "description": "点击 browser_state 返回的元素编号。仅用于链接、分页或页面内导航；会要求用户本地确认，点击后自动等待页面稳定。",
            "parameters": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer", "description": "本次 browser_state 返回的正整数元素编号"}
                },
                "required": ["index"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "browser_get_markdown",
            "description": "读取当前后台浏览器页面的 Markdown 内容。",
            "parameters": {"type": "object", "properties": {}, "required": []}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "browser_navigate",
            "description": "在当前后台浏览器会话访问另一个 URL；完成后必须重新调用 browser_state。",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "完整 http:// 或 https:// URL"}
                },
                "required": ["url"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "browser_close",
            "description": "关闭当前 Harness 创建的后台浏览器会话。完成交互浏览后必须调用。",
            "parameters": {"type": "object", "properties": {}, "required": []}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "context_status",
            "description": (
                "查看当前上下文的 token 估算、可压缩的旧轮次、受保护轮次和磁盘归档块。"
                "长任务中或 system 提示上下文较高时先调用它。"
            ),
            "parameters": {"type": "object", "properties": {}, "required": []}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "compress_context",
            "description": (
                "把连续的旧对话轮次压缩成一个自包含摘要，原始消息会完整归档到 "
                ".hqin_memory，之后可检索/解压。只能压缩 context_status 标记为"
                "“可压缩”的完整轮次；当前任务和最近轮次由代码保护。"
                "summary 必须包含：任务目标、已完成、关键文件/命令、未完成/下一步、约束。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "start_turn": {"type": "integer", "description": "起始轮次编号，从 context_status 获取"},
                    "end_turn": {"type": "integer", "description": "结束轮次编号（含）"},
                    "summary": {
                        "type": "string",
                        "description": "自包含摘要，不要只写“完成了任务”；保留文件路径、命令、结论和未完成事项"
                    },
                    "topic": {"type": "string", "description": "可选短标题，例如“初始化项目”"}
                },
                "required": ["start_turn", "end_turn", "summary"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "decompress_context",
            "description": (
                "读取已归档的记忆块。preview 返回摘要和原文预览；full 返回全文；"
                "file 把完整 JSON 写入 .hqin_memory/restored/。不会自动塞回当前上下文。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "block_id": {"type": "string", "description": "归档块编号，例如 b0001"},
                    "mode": {
                        "type": "string",
                        "enum": ["preview", "full", "file"],
                        "description": "默认 preview；需要完整内容时用 full 或 file"
                    }
                },
                "required": ["block_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_context",
            "description": "在归档块的摘要和原始消息中按关键词检索，返回最相关的块和片段。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "要查找的关键词、文件名、命令或结论"},
                    "limit": {"type": "integer", "description": "最多返回几个块，默认 3，最大 5"}
                },
                "required": ["query"]
            }
        }
    }
]
