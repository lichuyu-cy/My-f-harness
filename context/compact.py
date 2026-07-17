"""
上下文压缩管线：让 Agent 在长对话中不会因上下文超限而崩溃。

核心设计原则：便宜的先跑，贵的后跑（0 API → 1 API → 应急）。

管线顺序（L3 → L1 → L2 → 阈值判断 → L4）：
  L3 (tool_result_budget)    — 大结果落盘到磁盘，0 API
  L1 (snip_compact)          — 裁掉中间过时的消息，0 API
  L2 (micro_compact)         — 旧 tool_result 替换为占位符，0 API
  超阈值？ → L4 (compact_history) — LLM 全量摘要，1 API
  API 报错 → reactive_compact — 更激进的应急压缩

为什么 L3 在 L1/L2 前面？因为 micro_compact 会直接把旧的大结果替换成
一行占位符，budget 必须在它之前把完整内容落盘到磁盘。

使用方式：
    from context import compact_context
    messages[:] = compact_context(messages)
"""

import json
import os
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(override=True)


# ═══════════════════════════════════════════════════════════
#  常量配置
# ═══════════════════════════════════════════════════════════

CONTEXT_LIMIT = 50000             # 字符数阈值，超限触发 L4 LLM 全量摘要
KEEP_RECENT = 3                   # L2 保留最近几条 tool_result 的完整内容
PERSIST_THRESHOLD = 30000          # L3 大结果落盘阈值（超过此字符数则写入磁盘）
MAX_REACTIVE_RETRIES = 1           # 应急压缩的重试上限（防止无限循环）

TRANSCRIPT_DIR = Path(os.getcwd()) / ".transcripts"                 # L4 转录本目录
TOOL_RESULTS_DIR = Path(os.getcwd()) / ".task_outputs" / "tool-results"  # L3 落盘目录


# ═══════════════════════════════════════════════════════════
#  依赖注入（避免循环导入）
#  由 AgentLoop 在启动时注入 client 和 MODEL
# ═══════════════════════════════════════════════════════════

_client = None
_MODEL = None


def init_compact(client, model):
    """注入 Anthropic 客户端和模型名（用于 L4 LLM 摘要）。"""
    global _client, _MODEL
    _client = client
    _MODEL = model


# ═══════════════════════════════════════════════════════════
#  辅助函数
# ═══════════════════════════════════════════════════════════

def estimate_size(messages):
    """估算消息列表总大小（字符数）。"""
    return len(str(messages))


def _block_type(block):
    """获取 block 的类型：兼容 dict 和 Anthropic ContentBlock 对象。"""
    return block.get("type") if isinstance(block, dict) else getattr(block, "type", None)


def _message_has_tool_use(msg):
    """检查一条消息是否包含工具调用（assistant 的 tool_use block）。"""
    if msg.get("role") != "assistant":
        return False
    content = msg.get("content")
    if not isinstance(content, list):
        return False
    return any(_block_type(block) == "tool_use" for block in content)


def _is_tool_result_message(msg):
    """检查一条消息是否包含工具结果（user 的 tool_result block）。"""
    if msg.get("role") != "user":
        return False
    content = msg.get("content")
    if not isinstance(content, list):
        return False
    return any(isinstance(block, dict) and block.get("type") == "tool_result"
               for block in content)


def collect_tool_results(messages):
    """遍历 messages，收集所有 tool_result block 的（消息索引, block索引, block引用）。"""
    blocks = []
    for mi, msg in enumerate(messages):
        if msg.get("role") != "user" or not isinstance(msg.get("content"), list):
            continue
        for bi, block in enumerate(msg["content"]):
            if isinstance(block, dict) and block.get("type") == "tool_result":
                blocks.append((mi, bi, block))
    return blocks


# ═══════════════════════════════════════════════════════════
#  L1: snip_compact — 裁中间消息
#  消息数超 50 条时，保留头 3 条 + 尾 47 条，中间裁掉
#  保护机制：确保 tool_use 和紧随其后的 tool_result 不被拆散
# ═══════════════════════════════════════════════════════════

def snip_compact(messages, max_messages=50):
    """消息数超 max_messages 时裁中间，保留头 3 + 尾 (max-3)，成对保护。"""
    if len(messages) <= max_messages:
        return messages
    keep_head, keep_tail = 3, max_messages - 3
    head_end, tail_start = keep_head, len(messages) - keep_tail

    # 保护头部切口：如果 head_end 前一条是 tool_use，把后续的 tool_result 也保留
    if head_end > 0 and _message_has_tool_use(messages[head_end - 1]):
        while head_end < len(messages) and _is_tool_result_message(messages[head_end]):
            head_end += 1

    # 保护尾部切口：如果 tail_start 是 tool_result，把前一条 tool_use 也保留
    if (tail_start > 0 and tail_start < len(messages)
            and _is_tool_result_message(messages[tail_start])
            and _message_has_tool_use(messages[tail_start - 1])):
        tail_start -= 1

    if head_end >= tail_start:
        return messages
    snipped = tail_start - head_end
    return messages[:head_end] + [{"role": "user", "content": f"[snipped {snipped} messages]"},
                                  *messages[tail_start:]]


# ═══════════════════════════════════════════════════════════
#  L2: micro_compact — 旧 tool_result 占位
#  只保留最近 KEEP_RECENT 条 tool_result 的完整内容，
#  更早的替换为一行的占位符文本，减少 token 占用
# ═══════════════════════════════════════════════════════════

def micro_compact(messages):
    """旧 tool_result 替换为占位符，只保留最近 KEEP_RECENT 条。"""
    tool_results = collect_tool_results(messages)
    if len(tool_results) <= KEEP_RECENT:
        return messages
    for _, _, block in tool_results[:-KEEP_RECENT]:
        if len(block.get("content", "")) > 120:
            block["content"] = "[Earlier tool result compacted. Re-run if needed.]"
    return messages


# ═══════════════════════════════════════════════════════════
#  L3: tool_result_budget — 大结果落盘
#  统计最后一条 user 消息中所有 tool_result 的总大小，
#  超过 max_bytes 时，从最大的开始，将内容写入磁盘文件，
#  上下文中替换为 <persisted-output> 标记 + 前 2000 字符预览
# ═══════════════════════════════════════════════════════════

def _persist_large_output(tool_use_id, output):
    """将超大 tool_result 写入 .task_outputs/tool-results/{id}.txt。"""
    if len(output) <= PERSIST_THRESHOLD:
        return output
    TOOL_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = TOOL_RESULTS_DIR / f"{tool_use_id}.txt"
    if not path.exists():
        path.write_text(output)
    return (f"<persisted-output>\nFull output: {path}\n"
            f"Preview:\n{output[:2000]}\n</persisted-output>")


def tool_result_budget(messages, max_bytes=200_000):
    """最后一条 user 消息里 tool_result 超 max_bytes 时落盘，从最大的开始。"""
    last = messages[-1] if messages else None
    if not last or last.get("role") != "user" or not isinstance(last.get("content"), list):
        return messages
    blocks = [(i, b) for i, b in enumerate(last["content"])
              if isinstance(b, dict) and b.get("type") == "tool_result"]
    total = sum(len(str(b.get("content", ""))) for _, b in blocks)
    if total <= max_bytes:
        return messages
    ranked = sorted(blocks, key=lambda p: len(str(p[1].get("content", ""))), reverse=True)
    for _, block in ranked:
        if total <= max_bytes:
            break
        content = str(block.get("content", ""))
        if len(content) <= PERSIST_THRESHOLD:
            continue
        tid = block.get("tool_use_id", "unknown")
        block["content"] = _persist_large_output(tid, content)
        total = sum(len(str(b.get("content", ""))) for _, b in blocks)
    return messages


# ═══════════════════════════════════════════════════════════
#  L4: compact_history — LLM 全量摘要（唯一消耗 API 的层）
#  三步：保存完整 transcript → LLM 生成摘要 → 替换所有消息
# ═══════════════════════════════════════════════════════════

def _write_transcript(messages):
    """将完整对话写入 .transcripts/ 目录，JSONL 格式（每行一条 JSON）。"""
    TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
    path = TRANSCRIPT_DIR / f"transcript_{int(time.time())}.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for msg in messages:
            f.write(json.dumps(msg, default=str) + "\n")
    return path


def _summarize_history(messages):
    """把对话历史发给 LLM，要求保留关键信息，返回摘要文本。"""
    conversation = json.dumps(messages, default=str)[:80000]
    prompt = ("Summarize this coding-agent conversation so work can continue.\n"
              "Preserve: 1. current goal, 2. key findings/decisions, 3. files read/changed, "
              "4. remaining work, 5. user constraints.\nBe compact but concrete.\n\n"
              + conversation)
    response = _client.messages.create(
        model=_MODEL,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=2000,
    )
    result = "\n".join(
        getattr(block, "text", "")
        for block in response.content
        if getattr(block, "type", None) == "text"
    ).strip()
    return result or "(empty summary)"


def compact_history(messages):
    """L4：保存 transcript → LLM 摘要 → 替换为单条摘要消息。"""
    transcript_path = _write_transcript(messages)
    print(f"[transcript saved: {transcript_path}]")
    summary = _summarize_history(messages)
    return [{"role": "user", "content": f"[Compacted]\n\n{summary}"}]


# ═══════════════════════════════════════════════════════════
#  应急：reactive_compact
#  当 API 仍然返回 prompt_too_long 时触发，比 compact_history 更激进：
#  只保留尾部 5 条消息，前面全部 LLM 摘要
# ═══════════════════════════════════════════════════════════

def reactive_compact(messages):
    """应急压缩：保存 transcript → 尾部 5 条保留，前面全摘要。"""
    transcript = _write_transcript(messages)
    tail_start = max(0, len(messages) - 5)
    # 保护尾部切口的 tool_use/tool_result 配对
    if (tail_start > 0 and tail_start < len(messages)
            and _is_tool_result_message(messages[tail_start])
            and _message_has_tool_use(messages[tail_start - 1])):
        tail_start -= 1
    summary = _summarize_history(messages[:tail_start])
    return [{"role": "user", "content": f"[Reactive compact]\n\n{summary}"},
            *messages[tail_start:]]


# ═══════════════════════════════════════════════════════════
#  完整管线入口
#  由 AgentLoop 在每轮 LLM 调用前执行
# ═══════════════════════════════════════════════════════════

def compact_context(messages):
    """
    运行完整压缩管线。

    执行顺序（和 CC 源码一致）：budget → snip → micro → 阈值判断 → auto compact
    为什么 budget 在最前面？因为 micro 会把旧的大结果替换成占位符，
    budget 必须在那之前把完整内容落盘到磁盘。
    """
    messages[:] = tool_result_budget(messages)    # L3：先落盘，再裁切
    messages[:] = snip_compact(messages)          # L1：裁中间消息
    messages[:] = micro_compact(messages)         # L2：旧结果占位
    if estimate_size(messages) > CONTEXT_LIMIT:
        print("[auto compact]")
        messages[:] = compact_history(messages)   # L4：还是太大，全量摘要
    return messages
