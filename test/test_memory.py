"""
测试 s09 记忆系统：分多轮输入，观察记忆的累积和加载。

每轮结束后，打印 extract_memories 产生的输出（如果有），
以及当前 .memory/ 目录的状态。
"""

import sys
import os
from pathlib import Path

# 确保能找到项目根目录
sys.path.insert(0, str(Path(__file__).parent.parent))

# 覆盖导入 AgentLoop 会触发所有初始化
# 但我们需要捕获 print 输出，特别是 [Memory: ...]
import io
from contextlib import redirect_stdout

# 预置交互命令
PROMPTS = [
    "I prefer using tabs for indentation, not spaces. Remember that.",
    'Create a Python file called test.py that prints "hello world"',
    "What did I tell you about my preferences?",
    "I also prefer single quotes over double quotes for strings.",
]

MEMORY_DIR = Path(os.getcwd()) / ".memory"
MEMORY_INDEX = MEMORY_DIR / "MEMORY.md"


def print_memory_state(round_num):
    """打印当前 .memory/ 目录状态"""
    print(f"\n{'='*60}")
    print(f"  [CHECK] 第 {round_num} 轮后的 .memory/ 状态")
    print(f"{'='*60}")
    if not MEMORY_DIR.exists():
        print("  .memory/ 目录不存在")
        return
    files = sorted(MEMORY_DIR.iterdir())
    if not files:
        print("  .memory/ 目录为空")
        return
    for f in files:
        size = f.stat().st_size
        print(f"  {f.name} ({size} bytes)")
        if f.name == "MEMORY.md":
            print(f.read_text(encoding="utf-8").strip())


# ── 运行测试 ──────────────────────────────────────
print("=" * 60)
print("  s09 Memory System Test")
print("=" * 60)

from AgentLoop import agent_loop

history = []

for i, prompt in enumerate(PROMPTS, 1):
    print(f"\n{'#'*60}")
    print(f"  >>> Round {i}: {prompt}")
    print(f"{'#'*60}")

    # 捕获标准输出中的 [Memory: ...] 行
    captured = io.StringIO()
    with redirect_stdout(captured):
        history.append({"role": "user", "content": prompt})
        agent_loop(history)

    output = captured.getvalue()

    # 提取 [Memory: ...] 行和其他关键输出
    memory_lines = [l for l in output.split("\n") if "Memory:" in l]
    if memory_lines:
        for ml in memory_lines:
            print(f"  {ml.strip()}")
    else:
        print("  (no memory extraction message)")

    print_memory_state(i)

print(f"\n{'='*60}")
print("  TEST COMPLETE")
print(f"{'='*60}")
