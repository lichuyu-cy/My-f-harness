"""
团队模块：文件收件箱 + 队友线程，支持多 Agent 异步协作。

MessageBus 基于 .mailboxes/*.jsonl 实现，每个 Agent 一个收件箱文件。
teammate 线程使用全量工具集（排除 task 和 spawn_teammate 防递归），
完成后自动发 summary 到 Lead 收件箱。
"""

import json
import os
import threading
import time
from pathlib import Path

from tool import WORKDIR, TOOLS, TOOL_HANDLERS, execute_tool


# ═══════════════════════════════════════════════════════════
#  常量
# ═══════════════════════════════════════════════════════════

MAILBOX_DIR = WORKDIR / ".mailboxes"          # 收件箱目录


# ═══════════════════════════════════════════════════════════
#  teammate 可用工具集（排除 task 和 spawn_teammate 防递归）
# ═══════════════════════════════════════════════════════════

_TEAMMATE_TOOL_NAMES = [
    t["name"] for t in TOOLS
    if t["name"] not in ("task", "spawn_teammate")
]
TEAMMATE_TOOLS = [t for t in TOOLS if t["name"] in _TEAMMATE_TOOL_NAMES]
TEAMMATE_HANDLERS = {k: v for k, v in TOOL_HANDLERS.items()
                     if k in _TEAMMATE_TOOL_NAMES and k != "compact"}
# compact 由 agent_loop 拦截，teammate 不需要


# ═══════════════════════════════════════════════════════════
#  MessageBus — 文件收件箱
# ═══════════════════════════════════════════════════════════

class MessageBus:
    """文件收件箱：每个 Agent 一个 .mailboxes/{name}.jsonl。

    send：向目标 Agent 的收件箱 append 一行 JSON。
    read_inbox：读取收件箱 + 删除文件（消费式）。
    教学版省略文件锁，真实 CC 使用 proper-lockfile。
    """

    def send(self, from_agent: str, to_agent: str, content: str,
             msg_type: str = "message"):
        """向目标 Agent 发送消息。"""
        MAILBOX_DIR.mkdir(parents=True, exist_ok=True)
        msg = {"from": from_agent, "to": to_agent,
               "content": content, "type": msg_type,
               "ts": time.time()}
        inbox = MAILBOX_DIR / f"{to_agent}.jsonl"
        with open(inbox, "a", encoding="utf-8") as f:
            f.write(json.dumps(msg, ensure_ascii=False) + "\n")
        print(f"  \033[33m[bus] {from_agent} -> {to_agent}: {content[:50]}\033[0m")

    def read_inbox(self, agent: str) -> list[dict]:
        """读取并消费收件箱。返回消息列表，读完后删除文件。"""
        inbox = MAILBOX_DIR / f"{agent}.jsonl"
        if not inbox.exists():
            return []
        msgs = [json.loads(line) for line in
                inbox.read_text(encoding="utf-8").splitlines() if line.strip()]
        inbox.unlink()
        return msgs


BUS = MessageBus()                               # 全局消息总线实例
active_teammates: dict[str, bool] = {}            # 活跃队友列表


# ═══════════════════════════════════════════════════════════
#  spawn_teammate_thread — daemon 线程启动队友
# ═══════════════════════════════════════════════════════════

def spawn_teammate_thread(name: str, role: str, prompt: str) -> str:
    """启动队友线程。

    队友运行在自己的 daemon 线程中，使用全量工具集（排除 task/spawn_teammate），
    最多 10 轮循环，完成后自动发 summary 到 Lead 收件箱。
    """
    if name in active_teammates:
        return f"Teammate '{name}' already exists"

    system = (f"You are '{name}', a {role}. "
              f"Use tools to complete your task. "
              f"Send results via send_message to 'lead' when done.")

    def run():
        messages = [{"role": "user", "content": prompt}]
        for _ in range(10):
            # 每轮前检查收件箱
            inbox = BUS.read_inbox(name)
            if inbox:
                messages.append({"role": "user",
                                 "content": f"<inbox>{json.dumps(inbox, ensure_ascii=False)}</inbox>"})

            # 调 LLM
            try:
                from AgentLoop import client, MODEL
                response = client.messages.create(
                    model=MODEL, system=system, messages=messages[-20:],
                    tools=TEAMMATE_TOOLS, max_tokens=8000)
            except Exception:
                break

            messages.append({"role": "assistant", "content": response.content})
            if response.stop_reason != "tool_use":
                break

            # 执行工具
            results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                handler = TEAMMATE_HANDLERS.get(block.name)
                if handler:
                    output = handler(**block.input)
                else:
                    output = f"Unknown: {block.name}"
                results.append({"type": "tool_result",
                                "tool_use_id": block.id,
                                "content": str(output)})
            messages.append({"role": "user", "content": results})

        # 完成后发 summary 给 Lead
        summary = "Done."
        for msg in reversed(messages):
            if msg["role"] == "assistant" and isinstance(msg["content"], list):
                for b in msg["content"]:
                    if getattr(b, "type", None) == "text":
                        summary = b.text
                        break
                else:
                    continue
                break
        BUS.send(name, "lead", summary, "result")
        active_teammates.pop(name, None)
        print(f"  \033[32m[teammate] {name} finished\033[0m")

    active_teammates[name] = True
    threading.Thread(target=run, daemon=True).start()
    print(f"  \033[36m[teammate] {name} spawned as {role}\033[0m")
    return f"Teammate '{name}' spawned as {role}"


# ═══════════════════════════════════════════════════════════
#  工具 handler（供 tool.py 注册）
# ═══════════════════════════════════════════════════════════

def run_spawn_teammate(name: str, role: str, prompt: str) -> str:
    """spawn_teammate 工具的 handler。"""
    return spawn_teammate_thread(name, role, prompt)


def run_send_message(to: str, content: str) -> str:
    """send_message 工具的 handler。"""
    BUS.send("lead", to, content)
    return f"Sent to {to}"


def run_check_inbox() -> str:
    """check_inbox 工具的 handler。"""
    msgs = BUS.read_inbox("lead")
    if not msgs:
        return "(inbox empty)"
    lines = []
    for m in msgs:
        lines.append(f"  [{m['from']}] {m['content'][:200]}")
    return "\n".join(lines)
