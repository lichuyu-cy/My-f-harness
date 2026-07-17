"""
任务系统：文件持久化的任务图，支持 blockedBy 依赖。

每个任务是一个 JSON 文件，存于 .tasks/ 目录。
任务状态：pending → in_progress → completed。
Agent 通过 create_task / list_tasks / get_task / claim_task / complete_task 管理任务。
"""

import json
import os
import random
import time
from dataclasses import dataclass, asdict
from pathlib import Path


# ═══════════════════════════════════════════════════════════
#  常量
# ═══════════════════════════════════════════════════════════

WORKDIR = Path(os.getcwd())              # 工作目录
TASKS_DIR = WORKDIR / ".tasks"           # 任务文件存储目录


# ═══════════════════════════════════════════════════════════
#  Task 数据类
# ═══════════════════════════════════════════════════════════

@dataclass
class Task:
    id: str                          # 任务 ID：task_{timestamp}_{random}
    subject: str                     # 标题
    description: str                 # 详细描述
    status: str                      # pending | in_progress | completed
    owner: str | None                # 认领者（多 Agent 场景）
    blockedBy: list[str]             # 依赖的任务 ID 列表


# ═══════════════════════════════════════════════════════════
#  文件 CRUD
# ═══════════════════════════════════════════════════════════

def _task_path(task_id: str) -> Path:
    """返回任务 JSON 文件的完整路径。"""
    return TASKS_DIR / f"{task_id}.json"


def create_task(subject: str, description: str = "",
                blockedBy: list[str] | None = None) -> Task:
    """创建新任务（自动保存到 .tasks/{id}.json）。"""
    TASKS_DIR.mkdir(exist_ok=True)
    task = Task(
        id=f"task_{int(time.time())}_{random.randint(0, 9999):04d}",
        subject=subject,
        description=description,
        status="pending",
        owner=None,
        blockedBy=blockedBy or [],
    )
    save_task(task)
    return task


def save_task(task: Task):
    """将任务写入磁盘。"""
    _task_path(task.id).write_text(json.dumps(asdict(task), indent=2, ensure_ascii=False))


def load_task(task_id: str) -> Task:
    """从磁盘加载任务。"""
    return Task(**json.loads(_task_path(task_id).read_text(encoding="utf-8")))


def list_tasks() -> list[Task]:
    """列出 .tasks/ 目录下所有任务，按文件名排序。"""
    TASKS_DIR.mkdir(exist_ok=True)
    return [Task(**json.loads(p.read_text(encoding="utf-8")))
            for p in sorted(TASKS_DIR.glob("task_*.json"))]


def get_task(task_id: str) -> str:
    """返回任务的完整 JSON 格式。"""
    task = load_task(task_id)
    return json.dumps(asdict(task), indent=2, ensure_ascii=False)


# ═══════════════════════════════════════════════════════════
#  逻辑层：依赖检查 + 认领 + 完成
# ═══════════════════════════════════════════════════════════

def can_start(task_id: str) -> bool:
    """检查任务的所有 blockedBy 依赖是否都已 completed。

    不存在的依赖视为 blocked（避免引用错误 ID 时崩溃）。
    """
    task = load_task(task_id)
    for dep_id in task.blockedBy:
        dep_path = _task_path(dep_id)
        if not dep_path.exists():
            return False
        if load_task(dep_id).status != "completed":
            return False
    return True


def claim_task(task_id: str, owner: str = "agent") -> str:
    """认领任务：pending → in_progress，设置 owner。

    前置条件：
      - 任务状态必须为 pending
      - 所有 blockedBy 依赖必须已完成
    任一条件不满足则拒绝认领。
    """
    task = load_task(task_id)
    if task.status != "pending":
        return f"Task {task_id} is {task.status}, cannot claim"
    if not can_start(task_id):
        deps = [d for d in task.blockedBy
                if not _task_path(d).exists() or load_task(d).status != "completed"]
        return f"Blocked by: {deps}"
    task.owner = owner
    task.status = "in_progress"
    save_task(task)
    print(f"  \033[36m[claim] {task.subject} -> in_progress (owner: {owner})\033[0m")
    return f"Claimed {task.id} ({task.subject})"


def complete_task(task_id: str) -> str:
    """完成任务：in_progress → completed，同时报告被解锁的下游任务。"""
    task = load_task(task_id)
    if task.status != "in_progress":
        return f"Task {task_id} is {task.status}, cannot complete"
    task.status = "completed"
    save_task(task)
    unblocked = [t.subject for t in list_tasks()
                 if t.status == "pending" and t.blockedBy and can_start(t.id)]
    print(f"  \033[32m[complete] {task.subject} [done]\033[0m")
    msg = f"Completed {task.id} ({task.subject})"
    if unblocked:
        msg += f"\nUnblocked: {', '.join(unblocked)}"
        print(f"  \033[33m[unblocked] {', '.join(unblocked)}\033[0m")
    return msg


# ═══════════════════════════════════════════════════════════
#  工具 handler（供 tool.py 注册）
# ═══════════════════════════════════════════════════════════

def run_create_task(subject: str, description: str = "",
                    blockedBy: list[str] | None = None) -> str:
    """create_task 工具的 handler。"""
    task = create_task(subject, description, blockedBy)
    deps = f" (blockedBy: {', '.join(blockedBy)})" if blockedBy else ""
    print(f"  \033[34m[create] {task.subject}{deps}\033[0m")
    return f"Created {task.id}: {task.subject}{deps}"


def run_list_tasks() -> str:
    """list_tasks 工具的 handler。"""
    tasks = list_tasks()
    if not tasks:
        return "No tasks. Use create_task to add some."
    lines = []
    for t in tasks:
        icon = {"pending": "[ ]", "in_progress": "[>]",
                "completed": "[x]"}.get(t.status, "?")
        deps = f" (blockedBy: {', '.join(t.blockedBy)})" if t.blockedBy else ""
        owner = f" [{t.owner}]" if t.owner else ""
        lines.append(f"  {icon} {t.id}: {t.subject} [{t.status}]{owner}{deps}")
    return "\n".join(lines)


def run_get_task(task_id: str) -> str:
    """get_task 工具的 handler。"""
    try:
        return get_task(task_id)
    except FileNotFoundError:
        return f"Error: Task {task_id} not found"


def run_claim_task(task_id: str) -> str:
    """claim_task 工具的 handler。"""
    return claim_task(task_id, owner="agent")


def run_complete_task(task_id: str) -> str:
    """complete_task 工具的 handler。"""
    return complete_task(task_id)
