"""
后台任务模块：慢操作（install/build/test 等）放 daemon 线程执行，不阻塞主循环。

bash schema 新增 run_in_background 参数让模型显式请求后台执行，
is_slow_operation 关键词匹配做兜底。

后台完成后，结果以 <task_notification> 格式注入到下一轮对话中。
通知不复用 tool_use_id（原始 tool call 已用占位回复），而是作为独立 text block。
"""

import threading
from typing import Callable


# ═══════════════════════════════════════════════════════════
#  全局状态
# ═══════════════════════════════════════════════════════════

_bg_counter = 0                                           # 后台任务自增 ID 计数器
background_tasks: dict[str, dict] = {}                     # bg_id → {tool_use_id, command, status}
background_results: dict[str, str] = {}                    # bg_id → 执行输出
background_lock = threading.Lock()                         # 线程安全锁


# ═══════════════════════════════════════════════════════════
#  判断函数
# ═══════════════════════════════════════════════════════════

def is_slow_operation(tool_name: str, tool_input: dict) -> bool:
    """启发式判断：根据关键词判断命令是否可能耗时 > 30s。"""
    if tool_name != "bash":
        return False
    cmd = tool_input.get("command", "").lower()
    slow_keywords = ["install", "build", "test", "deploy", "compile",
                     "docker build", "pip install", "npm install",
                     "cargo build", "pytest", "make"]
    return any(kw in cmd for kw in slow_keywords)


def should_run_background(tool_name: str, tool_input: dict) -> bool:
    """判断工具调用是否应后台执行。模型显式请求优先，启发式兜底。"""
    if tool_input.get("run_in_background"):
        return True
    return is_slow_operation(tool_name, tool_input)


# ═══════════════════════════════════════════════════════════
#  后台执行
# ═══════════════════════════════════════════════════════════

def start_background_task(block, execute_tool_fn: Callable) -> str:
    """在 daemon 线程中执行工具调用，返回 bg_id。"""
    global _bg_counter
    _bg_counter += 1
    bg_id = f"bg_{_bg_counter:04d}"
    cmd = block.input.get("command", block.name)

    def worker():
        result = execute_tool_fn(block)
        with background_lock:
            background_tasks[bg_id]["status"] = "completed"
            background_results[bg_id] = result

    with background_lock:
        background_tasks[bg_id] = {
            "tool_use_id": block.id,
            "command": cmd,
            "status": "running",
        }
    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    print(f"  \033[33m[background] dispatched {bg_id}: {cmd[:40]}\033[0m")
    return bg_id


def collect_background_results() -> list[str]:
    """收集已完成的后台任务，返回 <task_notification> 格式的通知列表。"""
    with background_lock:
        ready_ids = [bid for bid, task in background_tasks.items()
                     if task["status"] == "completed"]
    notifications = []
    for bg_id in ready_ids:
        with background_lock:
            task = background_tasks.pop(bg_id)
            output = background_results.pop(bg_id, "")
        summary = output[:200] if len(output) > 200 else output
        notifications.append(
            f"<task_notification>\n"
            f"  <task_id>{bg_id}</task_id>\n"
            f"  <status>completed</status>\n"
            f"  <command>{task['command']}</command>\n"
            f"  <summary>{summary}</summary>\n"
            f"</task_notification>")
        print(f"  \033[32m[background done] {bg_id}: "
              f"{task['command'][:40]} ({len(output)} chars)\033[0m")
    return notifications
