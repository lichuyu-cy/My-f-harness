"""
测试 s10 system prompt 分段组装和缓存。
观察 [assembled] sections 和 [cache hit]。
"""

import sys, os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import io
from contextlib import redirect_stdout

MEMORY_DIR = Path(os.getcwd()) / ".memory"
MEMORY_INDEX = MEMORY_DIR / "MEMORY.md"

# 清理 .memory 以展示从无到有的过程
if MEMORY_DIR.exists():
    import shutil
    shutil.rmtree(MEMORY_DIR)
    print("  [setup] 已清除 .memory/ 目录\n")


print("=" * 65)
print("  s10 System Prompt — [assembled] + [cache hit] 观察")
print("=" * 65)

from system import get_system_prompt, update_context
from AgentLoop import agent_loop

history = []

# ── Round 1：无记忆索引 ──
print(f"\n{'#'*65}")
print(f"  >>> Round 1: Read the file README.md")
print(f"{'#'*65}")
captured = io.StringIO()
with redirect_stdout(captured):
    history.append({"role": "user", "content": "Read the file README.md"})
    agent_loop(history)
for line in captured.getvalue().split("\n"):
    if "assembled" in line or "cache hit" in line:
        print(f"  {line.strip()}")

# ── Round 2：创建记忆索引 ──
print(f"\n{'#'*65}")
print(f"  >>> Round 2: Create .memory/MEMORY.md")
print(f"{'#'*65}")
captured = io.StringIO()
with redirect_stdout(captured):
    history.append({"role": "user",
        "content": 'Create a file called .memory/MEMORY.md with content "- [test](test.md) — test memory"'})
    agent_loop(history)
for line in captured.getvalue().split("\n"):
    if "assembled" in line or "cache hit" in line:
        print(f"  {line.strip()}")

# 检查文件是否创建成功
if MEMORY_INDEX.exists():
    print(f"\n  [check] MEMORY.md: {MEMORY_INDEX.read_text(encoding='utf-8', errors='replace').strip()}")
else:
    print(f"\n  [check] MEMORY.md 不存在")

# ── Round 3：观察 memory section ──
print(f"\n{'#'*65}")
print(f"  >>> Round 3: Read the file code.py")
print(f"{'#'*65}")
captured = io.StringIO()
with redirect_stdout(captured):
    history.append({"role": "user", "content": "Read the file code.py"})
    agent_loop(history)
for line in captured.getvalue().split("\n"):
    if "assembled" in line or "cache hit" in line:
        print(f"  {line.strip()}")

print(f"\n{'='*65}")
print("  DONE")
print(f"{'='*65}")
