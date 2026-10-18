# tool_schemas.py
# Route B 核心：所有工具的 OpenAI Function Calling JSON Schema 定义。
# 新增工具时只需在此文件追加一个 dict，无需修改任何解析逻辑。

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "run_bash",
            "description": (
                "执行一条终端/Shell 命令（Windows 环境请使用 CMD/PowerShell 语法）。"
                "每次只能执行一条命令。执行前会弹出人工审批确认。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "要执行的完整 shell 命令，例如 'dir' 或 'python main.py'"
                    }
                },
                "required": ["command"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "读取本地文件内容，每行前面带有行号，方便精确定位。"
                "支持通过 start/end 参数只读取指定行范围，避免大文件撑爆上下文。"
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
                "创建一个新文件并写入完整内容。"
                "如果文件已存在则覆盖。适合创建全新脚本或配置文件。"
                "对 .py 文件会自动做语法检查，语法错误时拒绝写入。"
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
                        "description": "文件的完整内容"
                    }
                },
                "required": ["path", "content"]
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
                    }
                },
                "required": ["keyword"]
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
                "使用 DuckDuckGo 搜索网络内容（无需 API Key）。"
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
    }
]
