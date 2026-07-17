"""上下文压缩包 — 四层压缩管线 + 应急压缩。

使用方式：
    from context import compact_context, estimate_size, CONTEXT_LIMIT, init_compact
    init_compact(client, MODEL)
    messages[:] = compact_context(messages)
"""

from .compact import (
    CONTEXT_LIMIT,
    KEEP_RECENT,
    PERSIST_THRESHOLD,
    MAX_REACTIVE_RETRIES,
    estimate_size,
    snip_compact,
    micro_compact,
    tool_result_budget,
    compact_history,
    reactive_compact,
    compact_context,
    init_compact,
)
