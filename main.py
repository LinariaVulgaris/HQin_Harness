# main.py
import os
import platform
from openai import OpenAI
from rich.console import Console
from prompts import SYSTEM_PROMPT
from parser import parse_action
import tools

console = Console()

client = OpenAI(
    base_url="http://127.0.0.1:1234/v1",
    api_key="lm-studio"
)


def trim_messages(messages, max_chars=8000):
    """
    【选项A - 防爆显存】简单的滑动窗口截断逻辑：
    如果历史记录总字符数超过 max_chars，就从最旧的对话开始丢弃。
    """

    def get_total_chars(msgs):
        return sum(len(str(m.get("content", ""))) for m in msgs)

    total_chars = get_total_chars(messages)

    # 如果还在安全范围内，直接返回
    if total_chars <= max_chars:
        return messages

    console.print(
        f"\n[dim yellow]⚠️ 记忆即将超出限制 ({total_chars}/{max_chars} 字符)，触发自动记忆压缩...[/dim yellow]")

    # 循环移除最旧的消息，直到满足条件。
    # 始终保留 messages[0] (System Prompt)
    # 始终保留最后一条 messages[-1] (当前亟待解决的任务或反馈)
    while get_total_chars(messages) > max_chars and len(messages) > 2:
        messages.pop(1)  # 每次扔掉最前面的一条历史记录

    return messages


def start_interactive_session():
    """启动持久化交互会话"""
    console.print("[bold cyan]=========================================[/bold cyan]")
    console.print("[bold cyan]  欢迎使用 HQin Harness (Phase 2 - 交互版) [/bold cyan]")
    console.print("[bold cyan]=========================================[/bold cyan]")
    console.print("[dim]提示: 输入 'exit' 或 'quit' 退出程序。[/dim]\n")

    os_name = platform.system()
    current_dir = os.getcwd()
    env_info = f"\n\n【系统信息】\n你当前运行在 {os_name} 操作系统中。\n当前工作目录为: {current_dir}\n请直接使用适合该系统的终端命令（如 Windows 请用 dir，不要用 ls）。"

    dynamic_system_prompt = SYSTEM_PROMPT + env_info

    messages = [
        {"role": "system", "content": dynamic_system_prompt}
    ]

    while True:
        try:
            user_task = input("\n[HQin] 请输入你的需求:\n> ")
            if user_task.strip().lower() in ['exit', 'quit']:
                console.print("[bold green]👋 拜拜，下次见！[/bold green]")
                break
            if not user_task.strip():
                continue

            messages.append({"role": "user", "content": f"当前任务：{user_task}\n请开始思考并调用工具。"})
            console.print(f"\n[bold magenta]🚀 任务启动:[/bold magenta] {user_task}\n")

            max_steps = 10
            task_completed = False

            for step in range(max_steps):
                console.print(f"[dim]>> Agent Step {step + 1}/{max_steps} ...[/dim]")

                # 【防爆显存拦截器接入】每次呼叫大模型前，先检查并修剪历史记录
                messages = trim_messages(messages, max_chars=8000)

                # 1. 呼叫大模型
                response = client.chat.completions.create(
                    model="qwen3.5-4b",
                    messages=messages,
                    temperature=0.1
                )
                reply = response.choices[0].message.content

                console.print("\n[bold green]🤖 模型输出:[/bold green]")
                console.print(reply)

                messages.append({"role": "assistant", "content": reply})

                # 2. 解析动作
                action_data = parse_action(reply)

                if not action_data:
                    console.print(
                        "[bold yellow]⚠️ 未检测到工具调用。可能是模型闲聊或迷失，强制中断当前任务。[/bold yellow]")
                    break

                action = action_data["action"]

                # 3. 路由并执行动作
                observation = ""
                if action == "run_bash":
                    observation = tools.run_bash(action_data["command"])
                elif action == "read_file":
                    observation = tools.read_file(action_data["path"])
                elif action == "edit_file":
                    observation = tools.edit_file(
                        action_data["path"],
                        action_data["search"],
                        action_data["replace"]
                    )
                elif action == "write_file":
                    observation = tools.write_file(action_data["path"], action_data["content"])
                elif action == "search_in_files":
                    observation = tools.search_in_files(
                        action_data["keyword"],
                        action_data.get("directory", ".")
                    )
                elif action == "finish":
                    console.print(f"\n[bold green]🎉 任务圆满完成![/bold green]")
                    console.print(f"[bold cyan]🤖 最终汇报:[/bold cyan] {action_data.get('result', '')}\n")
                    task_completed = True
                    break
                elif action == "parse_error":
                    observation = "Error: XML 格式错误或缺少必要参数，请检查你的标签拼写并重试。"

                console.print(f"\n[bold blue]⚙️ 系统执行结果:[/bold blue]\n{observation}\n")
                console.print("-" * 50)

                # 4. 反馈给模型
                messages.append({
                    "role": "user",
                    "content": f"[系统执行反馈]:\n{observation}\n请根据结果决定下一步操作。"
                })

            if not task_completed:
                console.print("[bold red]🛑 达到最大步数或任务意外中断，请下达新指令。[/bold red]")

        except KeyboardInterrupt:
            console.print("\n[bold green]👋 收到中断信号，正在退出 HQin Harness...[/bold green]")
            break
        except Exception as e:
            console.print(f"\n[bold red]❌ 发生意外错误: {e}[/bold red]")


if __name__ == "__main__":
    start_interactive_session()