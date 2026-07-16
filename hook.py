"""
钩子层：基于事件的扩展框架。

钩子（hook）是在 Agent 主流程关键节点插入的自定义回调。
新增功能时，只需注册一个 hook，不需要改动 AgentLoop.py。

使用方式：
    register_hook("PreToolUse", my_hook)
    trigger_hooks("PreToolUse", block)

当前已注册的钩子：
- UserPromptSubmit → context_inject_hook（打印工作目录）
- PreToolUse → permission_hook（危险命令拦截）+ log_hook（日志记录）
- PostToolUse → large_output_hook（大输出警告）
- Stop → summary_hook（会话摘要）
"""

import os
from pathlib import Path

from tool import WORKDIR


# ═══════════════════════════════════════════════════════════
#  钩子框架
#  事件 → 回调列表，按注册顺序依次执行
#  如果某个回调返回非 None 值，后续回调不再执行（拦截短路）
# ═══════════════════════════════════════════════════════════

HOOKS = {"UserPromptSubmit": [], "PreToolUse": [], "PostToolUse": [], "Stop": []}


def register_hook(event: str, callback):
    """注册一个钩子回调到指定事件。"""
    HOOKS[event].append(callback)


def trigger_hooks(event: str, *args):
    """触发指定事件的所有回调，返回第一个非 None 结果（表示拦截）。"""
    for callback in HOOKS[event]:
        result = callback(*args)
        if result is not None:
            return result
    return None


# ═══════════════════════════════════════════════════════════
#  钩子实现
# ═══════════════════════════════════════════════════════════

# 硬禁止列表：匹配则直接拦截，不给用户确认机会
DENY_LIST = ["rm -rf /", "sudo", "shutdown", "reboot", "mkfs", "dd if="]

# 破坏性关键词：匹配时询问用户是否允许
DESTRUCTIVE = ["rm ", "> /etc/", "chmod 777"]


def permission_hook(block):
    """PreToolUse：三层权限检查。
    1. Deny list：硬禁止，直接拦截
    2. 破坏性命令：询问用户确认
    3. 写文件到工作目录外：询问用户确认
    """
    if block.name == "bash":
        for pattern in DENY_LIST:
            if pattern in block.input.get("command", ""):
                print(f"\n\033[31m[BLOCKED] '{pattern}'\033[0m")
                return "Permission denied by deny list"
        for kw in DESTRUCTIVE:
            if kw in block.input.get("command", ""):
                print(f"\n\033[33m[WARNING] Potentially destructive command\033[0m")
                print(f"   Tool: {block.name}({block.input})")
                choice = input("   Allow? [y/N] ").strip().lower()
                if choice not in ("y", "yes"):
                    return "Permission denied by user"
    if block.name in ("write_file", "edit_file"):
        path = block.input.get("path", "")
        if not (WORKDIR / path).resolve().is_relative_to(WORKDIR):
            print(f"\n\033[33m[WARNING] Writing outside workspace\033[0m")
            print(f"   Tool: {block.name}({block.input})")
            choice = input("   Allow? [y/N] ").strip().lower()
            if choice not in ("y", "yes"):
                return "Permission denied by user"
    return None


def log_hook(block):
    """PreToolUse：记录每次工具调用。"""
    args_preview = str(list(block.input.values())[:2])[:60]
    print(f"\033[90m[HOOK] {block.name}({args_preview})\033[0m")
    return None


def large_output_hook(block, output):
    """PostToolUse：大输出警告（超过 100KB 时打印警告）。"""
    if len(str(output)) > 100000:
        print(f"\033[33m[HOOK] Large output from {block.name}: {len(str(output))} chars\033[0m")
    return None


def context_inject_hook(query: str):
    """UserPromptSubmit：用户输入到达模型前的上下文注入。"""
    print(f"\033[90m[HOOK] UserPromptSubmit: working in {WORKDIR}\033[0m")
    return None


def summary_hook(messages: list):
    """Stop：循环退出时打印会话摘要（工具调用次数）。"""
    tool_count = sum(1 for m in messages
                     for b in (m.get("content") if isinstance(m.get("content"), list) else [])
                     if isinstance(b, dict) and b.get("type") == "tool_result")
    print(f"\033[90m[HOOK] Stop: session used {tool_count} tool calls\033[0m")
    return None


# ═══════════════════════════════════════════════════════════
#  注册所有钩子
# ═══════════════════════════════════════════════════════════

register_hook("UserPromptSubmit", context_inject_hook)  # 用户输入前
register_hook("PreToolUse", permission_hook)            # 工具执行前：权限检查
register_hook("PreToolUse", log_hook)                   # 工具执行前：日志记录
register_hook("PostToolUse", large_output_hook)         # 工具执行后：大输出警告
register_hook("Stop", summary_hook)                    # 循环结束时：打印摘要
