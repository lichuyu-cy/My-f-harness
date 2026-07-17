"""任务系统包 — 文件持久化的任务图，支持 blockedBy 依赖。

使用方式：
    from task import create_task, list_tasks, get_task, claim_task, complete_task
    from task import run_create_task, run_list_tasks, run_get_task, run_claim_task, run_complete_task
"""

from .task import (
    Task,
    TASKS_DIR,
    create_task,
    save_task,
    load_task,
    list_tasks,
    get_task,
    can_start,
    claim_task,
    complete_task,
    run_create_task,
    run_list_tasks,
    run_get_task,
    run_claim_task,
    run_complete_task,
)
