"""错误处理包 — 三层自适应恢复机制。

使用方式：
    from error_handling import RecoveryState, with_retry, is_prompt_too_long_error, \
        reactive_compact, ESCALATED_MAX_TOKENS, DEFAULT_MAX_TOKENS, \
        MAX_RECOVERY_RETRIES, CONTINUATION_PROMPT
"""

from .error_handling import (
    RecoveryState,
    retry_delay,
    with_retry,
    is_prompt_too_long_error,
    reactive_compact,
    ESCALATED_MAX_TOKENS,
    DEFAULT_MAX_TOKENS,
    MAX_RECOVERY_RETRIES,
    MAX_RETRIES,
    BASE_DELAY_MS,
    MAX_CONSECUTIVE_529,
    CONTINUATION_PROMPT,
)
