"""
工具层：定义 Agent 可调用的所有工具。

每个工具包含两部分：
1. 执行函数（run_*）：实现工具的具体逻辑
2. 注册信息（TOOLS + TOOL_HANDLERS）：描述工具的 schema 和对应的处理函数

当 AgentLoop 收到模型的 tool_use 请求时，通过 TOOL_HANDLERS 查找 handler 并调用。
"""

import ast
import json
import os
import subprocess
import glob as _glob
from pathlib import Path

# ═══════════════════════════════════════════════════════════
# 全局配置
# ═══════════════════════════════════════════════════════════

WORKDIR = Path(os.getcwd())           # 工作目录，所有路径基于此解析
CURRENT_TODOS: list[dict] = []        # todo_write 工具的状态：当前任务列表


# ═══════════════════════════════════════════════════════════
#  工具执行函数
# ═══════════════════════════════════════════════════════════

def safe_path(p: str) -> Path:
    """校验并解析安全路径：确保解析后的路径仍在 WORKDIR 内，防止路径逃逸。"""
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


def execute_tool(block):
    """通用工具执行器：从 TOOL_HANDLERS 找 handler 并调用。"""
    handler = TOOL_HANDLERS.get(block.name)
    return handler(**block.input) if handler else f"Unknown: {block.name}"


def run_bash(command: str) -> str:
    """执行 shell 命令，返回 stdout + stderr（最多 50000 字符）。"""
    try:
        r = subprocess.run(command, shell=True, cwd=WORKDIR,
                           capture_output=True, timeout=120)
        out = ((r.stdout or b"").decode("utf-8", errors="replace")
               + (r.stderr or b"").decode("utf-8", errors="replace")).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"


def run_read(path: str, limit: int | None = None) -> str:
    """读取文件内容，可选限制行数。"""
    try:
        lines = safe_path(path).read_text().splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


def run_write(path: str, content: str) -> str:
    """写入文件，自动创建父目录。"""
    try:
        file_path = safe_path(path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content)
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"


def run_edit(path: str, old_text: str, new_text: str) -> str:
    """替换文件中第一处匹配的文本。"""
    try:
        file_path = safe_path(path)
        text = file_path.read_text()
        if old_text not in text:
            return f"Error: text not found in {path}"
        file_path.write_text(text.replace(old_text, new_text, 1))
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"


def run_glob(pattern: str) -> str:
    """按 glob 模式搜索文件，限定在 WORKDIR 内。"""
    try:
        results = []
        for match in _glob.glob(pattern, root_dir=WORKDIR):
            if (WORKDIR / match).resolve().is_relative_to(WORKDIR):
                results.append(match)
        return "\n".join(results) if results else "(no matches)"
    except Exception as e:
        return f"Error: {e}"


# ═══════════════════════════════════════════════════════════
#  todo_write 工具——任务规划
#  让 Agent 在动手前列出步骤，执行过程中更新状态
#  不增加 Agent 的执行能力，只增加规划能力
# ═══════════════════════════════════════════════════════════

def _normalize_todos(todos):
    """校验并规范化 todo 列表：确保格式正确、状态值合法。"""
    if isinstance(todos, str):
        try:
            todos = json.loads(todos)
        except json.JSONDecodeError:
            try:
                todos = ast.literal_eval(todos)
            except (SyntaxError, ValueError):
                return None, "Error: todos must be a list or JSON array string"
    if not isinstance(todos, list):
        return None, "Error: todos must be a list"
    for i, t in enumerate(todos):
        if not isinstance(t, dict):
            return None, f"Error: todos[{i}] must be an object"
        if "content" not in t or "status" not in t:
            return None, f"Error: todos[{i}] missing 'content' or 'status'"
        if t["status"] not in ("pending", "in_progress", "completed"):
            return None, f"Error: todos[{i}] has invalid status '{t['status']}'"
    return todos, None


def run_todo_write(todos: list) -> str:
    """更新任务列表，保存到 CURRENT_TODOS 并在终端展示进度。"""
    global CURRENT_TODOS
    todos, error = _normalize_todos(todos)
    if error:
        return error
    CURRENT_TODOS = todos
    # 带颜色和图标的终端输出
    lines = ["\n\033[33m## Current Tasks\033[0m"]
    for t in CURRENT_TODOS:
        icon = {"pending": " ", "in_progress": "\033[36m▸\033[0m", "completed": "\033[32m✓\033[0m"}[t["status"]]
        lines.append(f"  [{icon}] {t['content']}")
    print("\n".join(lines))
    return f"Updated {len(CURRENT_TODOS)} tasks"


# ═══════════════════════════════════════════════════════════
#  工具注册信息
# ═══════════════════════════════════════════════════════════

TOOLS = [
    # bash：执行任意 shell 命令
    {"name": "bash", "description": "Run a shell command.",
     "input_schema": {"type": "object", "properties": {
         "command": {"type": "string"},
         "run_in_background": {"type": "boolean"}},
      "required": ["command"]}},
    # read_file：读取文件，可选行数限制
    {"name": "read_file", "description": "Read file contents.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["path"]}},
    # write_file：写入文件
    {"name": "write_file", "description": "Write content to a file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    # edit_file：替换文件中的文本（一次一处）
    {"name": "edit_file", "description": "Replace exact text in a file once.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
    # glob：按模式搜索文件
    {"name": "glob", "description": "Find files matching a glob pattern.",
     "input_schema": {"type": "object", "properties": {"pattern": {"type": "string"}}, "required": ["pattern"]}},
    # todo_write：任务规划工具，不增加执行能力
    {"name": "todo_write", "description": "Create and manage a task list for your current coding session.",
     "input_schema": {"type": "object", "properties": {"todos": {"type": "array", "items": {"type": "object", "properties": {"content": {"type": "string"}, "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]}}, "required": ["content", "status"]}}}, "required": ["todos"]}},
    # task：派生子 Agent 处理复杂子任务
    {"name": "task", "description": "Launch a subagent to handle a complex subtask. Returns only the final conclusion.",
     "input_schema": {"type": "object", "properties": {"description": {"type": "string"}}, "required": ["description"]}},
    # load_skill：按需加载技能全文
    {"name": "load_skill", "description": "Load the full content of a skill by name.",
     "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    # compact：主动触发上下文压缩
    {"name": "compact", "description": "Summarize earlier conversation to free context space.",
     "input_schema": {"type": "object", "properties": {"focus": {"type": "string"}}}},
    # ── 任务系统工具（s12） ──
    {"name": "create_task",
     "description": "Create a new task with optional blockedBy dependencies. Tasks persist across sessions.",
     "input_schema": {"type": "object",
                      "properties": {
                          "subject": {"type": "string"},
                          "description": {"type": "string"},
                          "blockedBy": {"type": "array",
                                        "items": {"type": "string"}}},
                      "required": ["subject"]}},
    {"name": "list_tasks",
     "description": "List all tasks with status, owner, and dependencies.",
     "input_schema": {"type": "object", "properties": {},
                      "required": []}},
    {"name": "get_task",
     "description": "Get full details of a specific task by ID.",
     "input_schema": {"type": "object",
                      "properties": {"task_id": {"type": "string"}},
                      "required": ["task_id"]}},
    {"name": "claim_task",
     "description": "Claim a pending task. Sets owner, changes status to in_progress.",
     "input_schema": {"type": "object",
                      "properties": {"task_id": {"type": "string"}},
                      "required": ["task_id"]}},
    {"name": "complete_task",
     "description": "Complete an in-progress task. Reports unblocked downstream tasks.",
     "input_schema": {"type": "object",
                      "properties": {"task_id": {"type": "string"}},
                      "required": ["task_id"]}},
    # ── 团队工具（s15） ──
    {"name": "spawn_teammate",
     "description": "Spawn a teammate agent in a background thread.",
     "input_schema": {"type": "object",
                      "properties": {
                          "name": {"type": "string"},
                          "role": {"type": "string"},
                          "prompt": {"type": "string"}},
                      "required": ["name", "role", "prompt"]}},
    {"name": "send_message",
     "description": "Send a message to a teammate via MessageBus.",
     "input_schema": {"type": "object",
                      "properties": {"to": {"type": "string"},
                                     "content": {"type": "string"}},
                      "required": ["to", "content"]}},
    {"name": "check_inbox",
     "description": "Check Lead's inbox for teammate messages.",
     "input_schema": {"type": "object", "properties": {},
                      "required": []}},
]

# 工具名 → 执行函数的映射，AgentLoop 据此分发
TOOL_HANDLERS = {
    "bash": run_bash, "read_file": run_read, "write_file": run_write,
    "edit_file": run_edit, "glob": run_glob, "todo_write": run_todo_write,
}

# ── 惰性导入：避免循环依赖 ──────────────────────────
# 以下工具的 handler 在其他模块中定义，通过惰性导入在启动时注册

# task 工具：handler 在 subagent.py（子 Agent 循环）
from subagent import spawn_subagent
TOOL_HANDLERS["task"] = spawn_subagent

# load_skill 工具：handler 在 skills/__init__.py（技能注册表查找）
from skills import load_skill
TOOL_HANDLERS["load_skill"] = load_skill

# compact 工具：handler 不由 TOOL_HANDLERS 注册，
# 由 AgentLoop 在 agent_loop 中直接拦截处理

# ── s12 任务系统工具 ──
from task import run_create_task, run_list_tasks, run_get_task, run_claim_task, run_complete_task
TOOL_HANDLERS["create_task"] = run_create_task
TOOL_HANDLERS["list_tasks"] = run_list_tasks
TOOL_HANDLERS["get_task"] = run_get_task
TOOL_HANDLERS["claim_task"] = run_claim_task
TOOL_HANDLERS["complete_task"] = run_complete_task

# ── s15 团队工具 ──
from team import run_spawn_teammate, run_send_message, run_check_inbox
TOOL_HANDLERS["spawn_teammate"] = run_spawn_teammate
TOOL_HANDLERS["send_message"] = run_send_message
TOOL_HANDLERS["check_inbox"] = run_check_inbox
