
import os

try:
    import readline
    # macOS 的 libedit 在处理中文输入时有退格问题，这四行修复它
    readline.parse_and_bind('set bind-tty-special-chars off')
    readline.parse_and_bind('set input-meta on')
    readline.parse_and_bind('set output-meta on')
    readline.parse_and_bind('set convert-meta off')
except ImportError:
    pass

from anthropic import Anthropic
from dotenv import load_dotenv

from tool import TOOLS, TOOL_HANDLERS          # 所有工具的 schema 和 handler
from hook import trigger_hooks                 # 钩子触发：权限检查、日志、Stop 等
from subagent import init_subagent             # 子 Agent 依赖注入
from system import build_system                # 统一组装 SYSTEM（技能 + 记忆索引）
from memory import init_memory, load_memories, extract_memories, consolidate_memories
from context import init_compact, compact_context, compact_history, MAX_REACTIVE_RETRIES

load_dotenv(override=True)

# ── API 客户端初始化 ──────────────────────────────
# 创建 Anthropic 兼容接口的客户端，使用 .env 中的配置
client = Anthropic(
    api_key=os.getenv("ANTHROPIC_API_KEY"),
    auth_token=os.getenv("ANTHROPIC_AUTH_TOKEN"),
    base_url=os.getenv("ANTHROPIC_BASE_URL"),
)
MODEL = os.environ["MODEL_ID"]

# 将 client 和 MODEL 注入到子模块（避免循环导入）
init_subagent(client, MODEL)
init_compact(client, MODEL)
init_memory(client, MODEL)

SHELL_NAME = "cmd" if os.name == "nt" else "bash"


# ── Nag reminder 计数器 ──────────────────────────
# 连续 3 轮未调用 todo_write 时，注入提醒
rounds_since_todo = 0


# ═══════════════════════════════════════════════════════════
#  agent_loop — 核心循环
#  工作流：压缩管线 → LLM 调用 → 工具执行 → 回传结果 → 继续
# ═══════════════════════════════════════════════════════════
def agent_loop(messages: list):
    """主循环：不断调 LLM → 执行工具 → 回传结果，直到模型主动停止。"""
    global rounds_since_todo
    reactive_retries = 0  # 应急压缩重试次数

    # ── 构建 SYSTEM + 加载记忆 ──
    # 每轮用户输入时重新构建 SYSTEM，因为 extract_memories 可能在上一轮
    # 更新了记忆索引，需要让最新的索引进入本轮 SYSTEM prompt
    system = build_system()
    # 从记忆库中选出与本轮对话相关的记忆（LLM 按名称+描述筛选）
    memories_content = load_memories(messages)
    # 记录当前用户消息的位置，后续将记忆内容注入到这条消息前
    # 仅当最后一条消息是普通文本时才注入（tool_result 消息不需要）
    memory_turn = len(messages) - 1 if messages and isinstance(messages[-1].get("content"), str) else None

    while True:
        # ── 保存压缩前快照 ──
        # 提取记忆需要基于压缩前的完整消息内容，因为压缩（尤其是 L1 snipping
        # 和 L4 compact_history）会丢弃中间消息，导致 extract_memories 看不到
        # 完整对话，错过可提取的信息。快照是浅拷贝，不额外消耗内存
        pre_compress = [
            m if isinstance(m, dict) else {"role": m.get("role", ""), "content": str(m.get("content", ""))}
            for m in messages
        ]

        # ── 前置处理：上下文压缩（0 API，便宜的先跑）
        # L3 大结果落盘 → L1 裁中间消息 → L2 旧结果占位 → 超阈值？→ L4 LLM 摘要
        messages[:] = compact_context(messages)

        # ═══════════════════════════════════════════════════
        #  Nag reminder：连续 3 轮没调 todo_write 就注入提醒
        # ═══════════════════════════════════════════════════
        if rounds_since_todo >= 3 and messages:
            messages.append({"role": "user",
                             "content": "<reminder>Update your todos.</reminder>"})
            rounds_since_todo = 0

        # ── 调 LLM API ──────────────────────────────────
        # 把完整对话历史发给模型，模型决定回复文本或调用工具
        try:
            # ── 记忆注入到用户消息前 ──
            # 将相关记忆内容以 <relevant_memories> 标签包裹，插入到当前用户
            # 消息的文本前。注入到用户消息（而非 SYSTEM）是因为不同轮次
            # 激活的记忆不同，注入 SYSTEM 会导致每轮重建 SYSTEM，成本更高
            request_messages = messages
            if memories_content and memory_turn is not None and memory_turn < len(messages):
                request_messages = messages.copy()
                request_messages[memory_turn] = {
                    **messages[memory_turn],
                    "content": memories_content + "\n\n" + messages[memory_turn]["content"],
                }
            response = client.messages.create(
                model=MODEL, system=system, messages=request_messages,
                tools=TOOLS, max_tokens=8000,
            )
            reactive_retries = 0  # API 调用成功，重置应急计数器
        except Exception as e:
            # 上下文超长 → 应急压缩后重试
            if ("prompt_too_long" in str(e).lower() or "too many tokens" in str(e).lower()) and reactive_retries < MAX_REACTIVE_RETRIES:
                print("[reactive compact]")
                from context import reactive_compact
                messages[:] = reactive_compact(messages)
                reactive_retries += 1
                continue
            raise  # 非上下文错误，抛出去

        # ── 记录模型回复 ──────────────────────────────
        messages.append({"role": "assistant", "content": response.content})

        # ── 检查是否停止 ──────────────────────────────
        # stop_reason 不是 tool_use，说明模型给出了最终回答
        if response.stop_reason != "tool_use":
            # ── 从压缩前快照中提取新记忆 ──
            # 模型给出最终回答后，分析本轮和之前消息中的用户偏好、项目事实等，
            # 自动写入 .memory/ 目录。使用压缩前快照确保提取的完整性
            extract_memories(pre_compress)
            # 定期合并重复/过时记忆（文件数 ≥ 10 时触发）
            consolidate_memories()

            force = trigger_hooks("Stop", messages)
            if force:
                # Stop 钩子返回了内容，注入后继续
                messages.append({"role": "user", "content": force})
                continue
            return

        # ── 工具调用轮次计数器递增 ──────────────────────
        rounds_since_todo += 1
        results = []

        # ── 遍历模型的每个工具调用并执行 ─────────────────
        for block in response.content:
            if block.type != "tool_use":
                continue

            # compact 工具：不走 normal 分发，直接触发全量摘要
            if block.name == "compact":
                messages[:] = compact_history(messages)
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": "[Compacted. Conversation history has been summarized.]"})
                messages.append({"role": "user", "content": results})
                break

            # 前置钩子：权限检查（deny list、破坏性命令确认等）
            blocked = trigger_hooks("PreToolUse", block)
            if blocked:
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": str(blocked)})
                continue

            # 从 TOOL_HANDLERS 查找对应的执行函数并调用
            handler = TOOL_HANDLERS.get(block.name)
            output = handler(**block.input) if handler else f"Unknown: {block.name}"

            # 后置钩子：大输出警告等
            trigger_hooks("PostToolUse", block, output)

            # todo_write 被调用时重置 nag 计数器
            if block.name == "todo_write":
                rounds_since_todo = 0

            results.append({"type": "tool_result", "tool_use_id": block.id,
                            "content": output})
        else:
            # 正常路径：没有调用 compact，回传结果后继续循环
            messages.append({"role": "user", "content": results})
            continue
        # compact 路径：结果已追加，重启循环
        continue


# ── Entry point ──────────────────────────────────────────
if __name__ == "__main__":
    print("s01: Agent Loop")
    print("输入问题，回车发送。输入 q 退出。\n")

    history = []  # 跨轮次对话历史
    while True:
        try:
            query = input("\033[36ms01 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break

        # 用户输入到达模型前的钩子：上下文注入
        trigger_hooks("UserPromptSubmit", query)
        history.append({"role": "user", "content": query})
        agent_loop(history)

        # 打印模型最终文本回复
        response_content = history[-1]["content"]
        if isinstance(response_content, list):
            for block in response_content:
                if getattr(block, "type", None) == "text":
                    print(block.text)
        print()
