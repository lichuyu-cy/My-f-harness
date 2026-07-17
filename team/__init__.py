"""团队包 — 文件收件箱 + 队友线程，多 Agent 异步协作。

使用方式：
    from team import BUS, spawn_teammate_thread, run_spawn_teammate, \
        run_send_message, run_check_inbox
"""

from .team import (
    MessageBus,
    BUS,
    active_teammates,
    TEAMMATE_TOOLS,
    TEAMMATE_HANDLERS,
    spawn_teammate_thread,
    run_spawn_teammate,
    run_send_message,
    run_check_inbox,
)
