import re
from typing import Dict, Any, Optional


def parse_action(text: str) -> Optional[Dict[str, Any]]:
    """
    从模型的文本回复中提取 <action name="xxx">...</action> 块。
    """
    # 匹配 <action name="工具名"> ... </action>
    action_pattern = re.compile(r'<action\s+name="([^"]+)">\s*(.*?)\s*</action>', re.DOTALL)
    match = action_pattern.search(text)

    if not match:
        return None

    action_name = match.group(1).strip()
    inner_content = match.group(2)

    # 提取具体的工具参数
    if action_name == "run_bash":
        cmd_match = re.search(r'<command>(.*?)</command>', inner_content, re.DOTALL)
        if cmd_match:
            return {"action": "run_bash", "command": cmd_match.group(1).strip()}

    elif action_name == "read_file":
        path_match = re.search(r'<path>(.*?)</path>', inner_content, re.DOTALL)
        if path_match:
            return {"action": "read_file", "path": path_match.group(1).strip()}

    elif action_name == "edit_file":
        path_match = re.search(r'<path>(.*?)</path>', inner_content, re.DOTALL)

        # 【关键修复】兼容 ==== 和 >>>> REPLACE 前面的任意空格和缩进，保证代码原本的缩进不被破坏
        sr_match = re.search(
            r'<<<< SEARCH[ \t]*\n(.*?)\n[ \t]*====[ \t]*\n(.*?)\n[ \t]*>>>> REPLACE',
            inner_content,
            re.DOTALL
        )

        if path_match and sr_match:
            return {
                "action": "edit_file",
                "path": path_match.group(1).strip(),
                "search": sr_match.group(1),  # 不做 strip，保留代码的原始缩进
                "replace": sr_match.group(2)  # 不做 strip，保留代码的原始缩进
            }

    elif action_name == "write_file":
        path_match = re.search(r'<path>(.*?)</path>', inner_content, re.DOTALL)
        content_match = re.search(r'<content>(.*?)</content>', inner_content, re.DOTALL)
        if path_match and content_match:
            return {
                "action": "write_file",
                "path": path_match.group(1).strip(),
                "content": content_match.group(1).strip()
            }

    elif action_name == "search_in_files":
        kw_match = re.search(r'<keyword>(.*?)</keyword>', inner_content, re.DOTALL)
        # directory 是可选参数，没写就默认搜索当前目录
        dir_match = re.search(r'<directory>(.*?)</directory>', inner_content, re.DOTALL)

        if kw_match:
            return {
                "action": "search_in_files",
                "keyword": kw_match.group(1).strip(),
                "directory": dir_match.group(1).strip() if dir_match else "."
            }

    elif action_name == "finish":
        res_match = re.search(r'<result>(.*?)</result>', inner_content, re.DOTALL)
        return {
            "action": "finish",
            "result": res_match.group(1).strip() if res_match else "任务已完成。"
        }

    # 如果没匹配上格式，则抛出失败
    return {"action": "parse_error", "raw_content": inner_content}


# ================= 单元测试 =================
if __name__ == "__main__":
    # 模拟带有复杂缩进、Markdown 代码块以及多余空格的模型输出
    mock_llm_reply = """
    <thought>
    这里有问题，我要修复它。
    </thought>
    ```xml
    <action name="edit_file">
      <path>  utils.py  </path>
      <<<< SEARCH
          print("error")
      ====
          print("success")
      >>>> REPLACE
    </action>
    ```
    """

    result = parse_action(mock_llm_reply)
    print("解析结果：")
    import json

    print(json.dumps(result, indent=2, ensure_ascii=False))