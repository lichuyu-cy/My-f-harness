"""
错误恢复模块：LLM 调用的三层自适应恢复机制。

设计目标：
  - 将 AgentLoop 中的错误处理逻辑封装到此模块，保持 AgentLoop 只保留循环控制
  - 三种恢复路径互不干扰，各自独立判断

三种恢复路径：
  路径 1（输出截断）：max_tokens → 8K→64K 升级（不追加截断内容）→ 续写提示（最多 3 次）
  路径 2（上下文超限）：prompt_too_long → reactive_compact 应急压缩 → 重试（仅一次）
  路径 3（临时故障）：429/529 → 指数退避 + 抖动 → 连续 3 次 529 切换备用模型
"""

import os
import random
import time
from typing import Callable


# ═══════════════════════════════════════════════════════════
#  常量
# ═══════════════════════════════════════════════════════════

ESCALATED_MAX_TOKENS = 64000       # 升级后的 max_tokens（8 倍空间）
DEFAULT_MAX_TOKENS = 8000           # 默认 max_tokens
MAX_RECOVERY_RETRIES = 3            # 续写提示最多 3 次
MAX_RETRIES = 10                    # 429/529 最多重试 10 次
BASE_DELAY_MS = 500                 # 退避基延迟（毫秒）
MAX_CONSECUTIVE_529 = 3             # 连续 529 次数阈值，达到后切换备用模型

# 续写提示：让模型直接接着刚才断掉的地方继续，不要道歉不要复述
CONTINUATION_PROMPT = (
    "Output token limit hit. Resume directly — "
    "no apology, no recap. Pick up mid-thought."
)


# ═══════════════════════════════════════════════════════════
#  RecoveryState — 追踪恢复状态
#  贯穿整个 agent_loop 的生命周期，记录是否升级、已压缩次数等
# ═══════════════════════════════════════════════════════════

class RecoveryState:
    """追踪整个 agent_loop 中的恢复状态。"""
    def __init__(self, primary_model: str):
        self.has_escalated = False               # 是否已从 8K 升级到 64K
        self.recovery_count = 0                  # 续写次数（最多 MAX_RECOVERY_RETRIES）
        self.consecutive_529 = 0                 # 连续 529 计数
        self.has_attempted_reactive_compact = False  # 是否已执行应急压缩
        self.current_model = primary_model       # 当前使用的模型，切换后更新


# ═══════════════════════════════════════════════════════════
#  工具函数
# ═══════════════════════════════════════════════════════════

def retry_delay(attempt: int, retry_after: float | None = None) -> float:
    """指数退避 + 抖动（秒）。服务器返回 Retry-After 时优先使用。

    公式：min(500 × 2^attempt, 32000) + random(0~25%)
    第 1 次 ~0.5s，第 2 次 ~1s，第 4 次 ~4s，第 7 次后 ~32s（上限）
    """
    if retry_after:
        return retry_after
    base = min(BASE_DELAY_MS * (2 ** attempt), 32000) / 1000
    jitter = random.uniform(0, base * 0.25)
    return base + jitter


def is_prompt_too_long_error(e: Exception) -> bool:
    """判断异常是否为上下文超长。"""
    msg = str(e).lower()
    return (("prompt" in msg and "long" in msg)
            or "prompt_is_too_long" in msg
            or "context_length_exceeded" in msg
            or "max_context_window" in msg
            or "too many tokens" in msg)


def reactive_compact(messages: list) -> list:
    """应急压缩：保留尾部 5 条消息，前面用一条标记替换。

    这是比 auto compact 更激进的压缩，发生在 auto compact 跑过后
    仍然超限的紧急情况下。教学版保留最后 5 条并加一条恢复标记；
    实际 CC 还会用 LLM 做摘要，但 compact_history 已覆盖了摘要路径。
    """
    print("  \033[31m[reactive compact] trimming to last 5 messages\033[0m")
    tail = messages[-5:]
    return [{"role": "user",
             "content": "[Reactive compact] Earlier conversation trimmed. "
                        "Continue from where you left off."}, *tail]


# ═══════════════════════════════════════════════════════════
#  with_retry — 处理 429/529 临时故障
#  捕获限流和过载异常，走指数退避，连续 529 切换备用模型
#  非临时错误（如认证失败、参数错误）原样抛出
# ═══════════════════════════════════════════════════════════

def with_retry(fn: Callable, state: RecoveryState):
    """指数退避重试包装器。

    处理两种可恢复异常：
      - 429 RateLimitError：指数退避 + 抖动
      - 529 OverloadedError：指数退避 + 抖动，连续 3 次切换备用模型
    其他异常原样抛出，留给外层 try/except 处理（如 prompt_too_long）。
    """
    fallback_model = os.getenv("FALLBACK_MODEL_ID")

    for attempt in range(MAX_RETRIES):
        try:
            result = fn()
            state.consecutive_529 = 0  # 成功调用，重置 529 计数
            return result
        except Exception as e:
            name = type(e).__name__
            msg = str(e).lower()

            # ── 429 限流 ──
            if "ratelimit" in name.lower() or "429" in msg:
                delay = retry_delay(attempt)
                print(f"  \033[33m[429 rate limit] retry {attempt+1}/{MAX_RETRIES},"
                      f" wait {delay:.1f}s\033[0m")
                time.sleep(delay)
                continue

            # ── 529 过载 ──
            if "overloaded" in name.lower() or "529" in msg or "overloaded" in msg:
                state.consecutive_529 += 1
                # 连续 3 次 529 -> 切换备用模型（如果配置了 FALLBACK_MODEL_ID）
                if state.consecutive_529 >= MAX_CONSECUTIVE_529:
                    if fallback_model:
                        state.current_model = fallback_model
                        state.consecutive_529 = 0
                        print(f"  \033[31m[529 x{MAX_CONSECUTIVE_529}]"
                              f" switching to {fallback_model}\033[0m")
                    else:
                        state.consecutive_529 = 0
                        print(f"  \033[31m[529 x{MAX_CONSECUTIVE_529}]"
                              f" no FALLBACK_MODEL_ID configured, continuing retry\033[0m")
                delay = retry_delay(attempt)
                print(f"  \033[33m[529 overloaded] retry {attempt+1}/{MAX_RETRIES},"
                      f" wait {delay:.1f}s\033[0m")
                time.sleep(delay)
                continue

            # ── 非临时错误：让外层处理 ──
            raise

    raise RuntimeError(f"Max retries ({MAX_RETRIES}) exceeded")
