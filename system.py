"""统一系统提示组装 — s10 风格：分段定义 + 按需拼接 + 缓存。

AgentLoop 只需 from system import build_system，无需关心内部来源。
"""

import json
from pathlib import Path

from skills import list_skills
from memory import read_memory_index


# ═══════════════════════════════════════════════════════════
#  PROMPT_SECTIONS — 每个 section 独立维护
#  新增 section 不影响已有的，修改一个不影响其他
# ═══════════════════════════════════════════════════════════

PROMPT_SECTIONS = {
    # 身份：始终加载
    "identity": f"You are a coding agent at {Path.cwd()}.",
    # 技能指引：Layer 2 的入口
    "skills_hint": "Use load_skill to get full details when needed.",
    # 规划引导
    "planning": (
        "Before starting any multi-step task, use todo_write to plan your steps. "
        "Update status as you go."
    ),
    "task_hint": "For complex sub-problems, use the task tool to spawn a subagent.",
    # 任务系统指引（s12）
    "task_system": (
        "Use create_task/list_tasks/get_task/claim_task/complete_task to manage "
        "persistent tasks with blockedBy dependencies. "
        "Break large goals into dependent tasks via blockedBy."
    ),
    # 记忆占位（按需加载）
    "memory_header": "Relevant memories are injected below when available. "
                     "Respect user preferences from memory.",
}


# ═══════════════════════════════════════════════════════════
#  context — 从真实状态推导，不在消息里搜关键词
# ═══════════════════════════════════════════════════════════

def update_context() -> dict:
    """从当前运行态推导 context。

    反映真实状态：技能注册表是否有内容、记忆索引文件是否存在。
    section 是否加载基于这些真实状态，不在消息里搜关键词。
    """
    return {
        "skills_catalog": list_skills(),
        "memory_index": read_memory_index(),
    }


# ═══════════════════════════════════════════════════════════
#  assemble_system_prompt — 根据 context 按需拼接
# ═══════════════════════════════════════════════════════════

def assemble_system_prompt(context: dict) -> str:
    """根据 context 的真实状态选择 section 并拼接为 SYSTEM prompt。

    - 始终加载的：identity、skills_hint、planning、task_hint、task_system
    - 按需加载的：技能目录（有技能时才加）、记忆索引（有文件时才加）
    """
    sections = []

    # 始终加载 — 身份
    sections.append(PROMPT_SECTIONS["identity"])

    # 按需加载 — 技能目录（有技能注册表内容时才加）
    catalog = context.get("skills_catalog", "")
    if catalog:
        sections.append(f"Skills available:\n{catalog}")
    sections.append(PROMPT_SECTIONS["skills_hint"])

    # 按需加载 — 记忆索引（有对应文件时才加）
    memory_idx = context.get("memory_index", "")
    if memory_idx:
        sections.append(f"\nMemories available:\n{memory_idx}")
        sections.append(PROMPT_SECTIONS["memory_header"])

    # 始终加载 — 规划引导
    sections.append(PROMPT_SECTIONS["planning"])
    sections.append(PROMPT_SECTIONS["task_hint"])
    sections.append(PROMPT_SECTIONS["task_system"])

    return "\n".join(sections)


# ═══════════════════════════════════════════════════════════
#  get_system_prompt — 缓存避免重复拼接
#  json.dumps 做确定性序列化（非 Python hash），
#  避免进程随机化和 unhashable type 问题
# ═══════════════════════════════════════════════════════════

_last_context_key = None
_last_prompt = None


def get_system_prompt(context: dict) -> str:
    """带缓存的 system prompt 获取。context 没变时跳过拼接。"""
    global _last_context_key, _last_prompt
    key = json.dumps(context, sort_keys=True, ensure_ascii=False, default=str)
    if key == _last_context_key and _last_prompt:
        return _last_prompt
    _last_context_key = key
    _last_prompt = assemble_system_prompt(context)
    return _last_prompt


# ═══════════════════════════════════════════════════════════
#  build_system — 向后兼容的便捷入口
#  AgentLoop 可直接调 build_system()，无需接触 context
# ═══════════════════════════════════════════════════════════

def build_system() -> str:
    """便捷函数：一步完成 update_context + get_system_prompt。"""
    return get_system_prompt(update_context())
