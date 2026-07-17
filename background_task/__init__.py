"""后台任务包 — 慢操作放 daemon 线程，通知用 <task_notification> 注入。

使用方式：
    from background_task import should_run_background, start_background_task, collect_background_results
"""

from .background_task import (
    should_run_background,
    is_slow_operation,
    start_background_task,
    collect_background_results,
    background_tasks,
    background_results,
    background_lock,
)
