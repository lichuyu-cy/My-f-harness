
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

from tool import TOOLS, TOOL_HANDLERS, execute_tool   # 所有工具的 schema、handler 和通用执行器
from hook import trigger_hooks                 # 钩子触发：权限检查、日志、Stop 等
from subagent import init_subagent             # 子 Agent 依赖注入
from system import get_system_prompt, update_context     # s10 风格：分段定义 + 按需拼接 + 缓存
from memory import init_memory, load_memories, extract_memories, consolidate_memories
from context import init_compact, compact_context, compact_history
from error_handling import (RecoveryState, with_retry, is_prompt_too_long_error,
                      reactive_compact, ESCALATED_MAX_TOKENS, DEFAULT_MAX_TOKENS,
                      MAX_RECOVERY_RETRIES, CONTINUATION_PROMPT)
from background_task import (should_run_background, start_background_task,
                              collect_background_results)

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

    # ── 构建 SYSTEM + 加载记忆 ──
    # 每轮用户输入时重新构建 SYSTEM，因为 extract_memories 可能在上一轮
    # 更新了记忆索引，需要让最新的索引进入本轮 SYSTEM prompt
    context = update_context()
    system = get_system_prompt(context)
    # 从记忆库中选出与本轮对话相关的记忆（LLM 按名称+描述筛选）
    memories_content = load_memories(messages)
    # 记录当前用户消息的位置，后续将记忆内容注入到这条消息前
    # 仅当最后一条消息是普通文本时才注入（tool_result 消息不需要）
    memory_turn = len(messages) - 1 if messages and isinstance(messages[-1].get("content"), str) else None

    # s11：恢复状态追踪器 + max_tokens 初始值
    state = RecoveryState(MODEL)
    max_tokens = DEFAULT_MAX_TOKENS

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
            # s11：with_retry 处理 429/529 临时故障，其他异常抛给外层
            response = with_retry(
                lambda mt=max_tokens, mdl=state.current_model:
                    client.messages.create(
                        model=mdl, system=system, messages=request_messages,
                        tools=TOOLS, max_tokens=mt),
                state)
        except Exception as e:
            # ── 路径 2：上下文超限 -> reactive_compact 应急压缩后重试（仅一次）──
            if is_prompt_too_long_error(e):
                if not state.has_attempted_reactive_compact:
                    messages[:] = reactive_compact(messages)
                    state.has_attempted_reactive_compact = True
                    continue
                # 压缩过后还是超限，不可恢复
                print("  \033[31m[unrecoverable] still too long after compact\033[0m")
                messages.append({"role": "assistant", "content": [
                    {"type": "text",
                     "text": "[Error] Context too large, cannot continue."}]})
                return
            # ── 非可恢复错误 ──
            name = type(e).__name__
            print(f"  \033[31m[unrecoverable] {name}: {str(e)[:100]}\033[0m")
            messages.append({"role": "assistant", "content": [
                {"type": "text", "text": f"[Error] {name}: {str(e)[:200]}"}]})
            return

        # ── 路径 1：输出截断 ──
        # 先检查 max_tokens，再走正常流程（如果先 append 再检查，
        # 截断的回复也会进入历史，浪费上下文）
        if response.stop_reason == "max_tokens":
            # 第一次：8K→64K 升级，不追加截断内容，保持原始请求重试
            if not state.has_escalated:
                max_tokens = ESCALATED_MAX_TOKENS
                state.has_escalated = True
                print(f"  \033[33m[max_tokens] escalating"
                      f" {DEFAULT_MAX_TOKENS} -> {ESCALATED_MAX_TOKENS}\033[0m")
                continue
            # 64K 仍然截断：保存截断输出 + 续写提示（最多 3 次）
            messages.append({"role": "assistant", "content": response.content})
            if state.recovery_count < MAX_RECOVERY_RETRIES:
                messages.append({"role": "user", "content": CONTINUATION_PROMPT})
                state.recovery_count += 1
                print(f"  \033[33m[max_tokens] continuation"
                      f" {state.recovery_count}/{MAX_RECOVERY_RETRIES}\033[0m")
                continue
            print("  \033[31m[max_tokens] recovery limit reached\033[0m")
            return

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
                # s10：compact 也会改变上下文状态，重新评估
                context = update_context()
                system = get_system_prompt(context)
                break

            # s13：检查是否应后台执行（慢操作放 daemon 线程）
            if should_run_background(block.name, block.input):
                bg_id = start_background_task(block, execute_tool)
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": f"[Background task {bg_id} started] "
                                           f"Command: {block.input.get('command', '')}. "
                                           f"Result will be available when complete."})
                continue

            # 前置钩子：权限检查（deny list、破坏性命令确认等）
            blocked = trigger_hooks("PreToolUse", block)
            if blocked:
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": str(blocked)})
                continue

            # 同步执行工具
            output = execute_tool(block)

            # 后置钩子：大输出警告等
            trigger_hooks("PostToolUse", block, output)

            # todo_write 被调用时重置 nag 计数器
            if block.name == "todo_write":
                rounds_since_todo = 0

            results.append({"type": "tool_result", "tool_use_id": block.id,
                            "content": output})
        else:
            # 正常路径：没有调用 compact，回传结果后继续循环
            # s13：收集后台通知，合并到 tool_result 后一起注入
            user_content = list(results)
            bg_notifications = collect_background_results()
            if bg_notifications:
                for notif in bg_notifications:
                    user_content.append({"type": "text", "text": notif})
            messages.append({"role": "user", "content": user_content})
            # s10：每轮工具执行后重新评估 context，system prompt 随真实状态更新
            context = update_context()
            system = get_system_prompt(context)
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
