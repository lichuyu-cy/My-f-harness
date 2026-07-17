"""
子 Agent 模块：独立上下文的任务执行单元。

当主 Agent 遇到复杂子任务时，通过 task 工具派生一个子 Agent。
子 Agent 拥有全新的 messages[]，不污染主对话的上下文，
完成后只返回最终结论，中间过程全部丢弃。

关键设计：
- 独立的工具集（SUB_TOOLS）：不含 task 工具，禁止递归派生子 Agent
- 独立的 SYSTEM prompt（SUB_SYSTEM）：要求直接完成任务不委派
- 30 轮安全限制：防止子 Agent 无限循环
- hook 不跳过：子 Agent 的工具调用仍经过权限检查
"""

from tool import WORKDIR, safe_path, run_bash, run_read, run_write, run_edit, run_glob
from hook import trigger_hooks
from skills import load_skill


# ═══════════════════════════════════════════════════════════
#  子 Agent 配置：工具集 + SYSTEM prompt
# ═══════════════════════════════════════════════════════════

# 子 Agent 可用工具：基础工具，不含 task（防递归），含 load_skill（可加载技能）
SUB_TOOLS = [
    {"name": "bash", "description": "Run a shell command.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "read_file", "description": "Read file contents.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "write_file", "description": "Write content to a file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "edit_file", "description": "Replace exact text in a file once.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
    {"name": "glob", "description": "Find files matching a glob pattern.",
     "input_schema": {"type": "object", "properties": {"pattern": {"type": "string"}}, "required": ["pattern"]}},
    # 子 Agent 也可以加载技能
    {"name": "load_skill", "description": "Load the full content of a skill by name.",
     "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
]

SUB_HANDLERS = {
    "bash": run_bash, "read_file": run_read, "write_file": run_write,
    "edit_file": run_edit, "glob": run_glob, "load_skill": load_skill,
}

# 子 Agent 的 SYSTEM prompt：简洁，明确禁止进一步委派
SUB_SYSTEM = (
    f"You are a coding agent at {WORKDIR}. "
    "Complete the task you were given, then return a concise summary. "
    "Do not delegate further."
)


# ═══════════════════════════════════════════════════════════
#  依赖注入（避免循环导入）
#  由 AgentLoop 在启动时注入 client 和 MODEL
# ═══════════════════════════════════════════════════════════

_client = None
_MODEL = None


def init_subagent(client, model):
    """注入 Anthropic 客户端和模型名。"""
    global _client, _MODEL
    _client = client
    _MODEL = model


# ═══════════════════════════════════════════════════════════
#  子 Agent 实现
# ═══════════════════════════════════════════════════════════

def extract_text(content) -> str:
    """从 message content blocks 中提取文本（过滤掉 tool_use block）。"""
    if not isinstance(content, list):
        return str(content)
    return "\n".join(getattr(b, "text", "") for b in content if getattr(b, "type", None) == "text")


def spawn_subagent(description: str) -> str:
    """
    生成一个子 Agent 处理复杂子任务。

    流程：
    1. 用 description 创建全新的 messages[]
    2. 在独立的 while 循环中调 LLM + 执行工具（最多 30 轮）
    3. 工具调用也经过 PreToolUse/PostToolUse hook（权限不跳过）
    4. 只返回最后的文本结论，中间过程全部丢弃
    """
    print(f"\n\033[35m[Subagent spawned]\033[0m")
    messages = [{"role": "user", "content": description}]

    for _ in range(30):  # safety limit：最多 30 轮
        response = _client.messages.create(
            model=_MODEL, system=SUB_SYSTEM,
            messages=messages, tools=SUB_TOOLS, max_tokens=8000,
        )
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            break

        results = []
        for block in response.content:
            if block.type == "tool_use":
                # 子 Agent 也走 hook（权限不跳过）
                blocked = trigger_hooks("PreToolUse", block)
                if blocked:
                    results.append({"type": "tool_result", "tool_use_id": block.id,
                                    "content": str(blocked)})
                    continue

                handler = SUB_HANDLERS.get(block.name)
                output = handler(**block.input) if handler else f"Unknown: {block.name}"
                trigger_hooks("PostToolUse", block, output)
                print(f"  \033[90m[sub] {block.name}: {str(output)[:100]}\033[0m")
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": output})

        messages.append({"role": "user", "content": results})

    # 只回传最后的文本结论，中间过程全部丢弃
    result = extract_text(messages[-1]["content"])
    if not result:
        # 如果最后一条消息是 tool_result，往前找 assistant 的文本
        for msg in reversed(messages):
            if msg["role"] == "assistant":
                result = extract_text(msg["content"])
                if result:
                    break
        if not result:
            result = "Subagent stopped after 30 turns without final answer."

    print(f"\033[35m[Subagent done]\033[0m")
    return result
