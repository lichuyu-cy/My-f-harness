"""
详细测试 s09 记忆系统：捕获模型回复，验证记忆是否被加载和使用。
"""

import sys
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import io
from contextlib import redirect_stdout

PROMPTS = [
    "I prefer using tabs for indentation, not spaces. Remember that.",
    'Create a Python file called test2.py that prints "hello world"',
    "What did I tell you about my preferences?",
    "I also prefer single quotes over double quotes for strings.",
]

MEMORY_DIR = Path(os.getcwd()) / ".memory"
MEMORY_DIR_ABS = MEMORY_DIR.resolve()


def print_memory_state(round_num):
    print(f"\n{'='*60}")
    print(f"  [CHECK] 第 {round_num} 轮后的 .memory/ 状态")
    print(f"{'='*60}")
    if not MEMORY_DIR_ABS.exists():
        print("  .memory/ 目录不存在")
        return
    files = sorted(MEMORY_DIR_ABS.iterdir())
    if not files:
        print("  .memory/ 目录为空")
        return
    for f in files:
        size = f.stat().st_size
        print(f"  {f.name} ({size} bytes)")
        if f.name == "MEMORY.md":
            print(f.read_text(encoding="utf-8").strip())


print("=" * 60)
print("  s09 Memory System Test (verbose)")
print("=" * 60)

from AgentLoop import agent_loop

# 本地 history，不从 AgentLoop 导入
history = []

for i, prompt in enumerate(PROMPTS, 1):
    print(f"\n{'#'*60}")
    print(f"  >>> Round {i}: {prompt}")
    print(f"{'#'*60}")

    # 捕获 print 输出
    captured = io.StringIO()
    with redirect_stdout(captured):
        history.append({"role": "user", "content": prompt})
        agent_loop(history)

    output = captured.getvalue()

    # 提取 [Memory:] 消息
    memory_lines = [l for l in output.split("\n") if "Memory:" in l]
    if memory_lines:
        for ml in memory_lines:
            print(f"  {ml.strip()}")
    else:
        print("  (no memory extraction)")

    # 提取模型的文本回复
    print(f"\n  --- Agent 回复 ---")
    response_content = history[-1]["content"]
    if isinstance(response_content, list):
        for block in response_content:
            if getattr(block, "type", None) == "text":
                print(f"  {block.text}")
    elif isinstance(response_content, str):
        print(f"  {response_content}")
    print(f"  --- Agent 回复结束 ---")

    print_memory_state(i)

# 检查 test2.py 是否用了单引号（记忆影响）
test2 = Path(os.getcwd()) / "test2.py"
if test2.exists():
    print(f"\n  [CHECK] test2.py 内容:")
    content = test2.read_text(encoding="utf-8")
    print(f"  {content.strip()}")
    if "'" in content and '"' not in content:
        print(f"  >>> 使用了单引号 ✓")
    elif '"' in content and "'" not in content:
        print(f"  >>> 使用了双引号")
    else:
        print(f"  >>> 混合使用")

print(f"\n{'='*60}")
print("  TEST COMPLETE")
print(f"{'='*60}")
