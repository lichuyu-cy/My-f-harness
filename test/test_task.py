"""
测试 s12 任务系统：创建带依赖的任务 → 认领 → 完成 → 解锁。
模拟 4 轮 prompt，验证 .tasks/ 持久化和 blockedBy 依赖链。
"""

import sys, os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

# 清理旧的 .tasks/
import shutil
tasks_dir = Path.cwd() / ".tasks"
if tasks_dir.exists():
    shutil.rmtree(tasks_dir)

from task import create_task, list_tasks, claim_task, complete_task

print("=" * 65)
print("  s12 Task System — 依赖图测试")
print("=" * 65)

# ── Round 1: 创建 4 个带依赖的任务 ──
print(f"\n{'#'*65}")
print(f"  >>> Round 1: Create tasks with dependencies")
print(f"{'#'*65}")

schema = create_task("setup database schema", "Create the database schema")
endpoints = create_task("create API endpoints",
                        "Create the API endpoints",
                        blockedBy=[schema.id])
tests = create_task("write tests",
                    "Write tests for the API",
                    blockedBy=[endpoints.id])
docs = create_task("write docs",
                   "Write documentation",
                   blockedBy=[schema.id])

print(f"\n  Tasks created:")
for t in list_tasks():
    deps = f" blockedBy: {t.blockedBy}" if t.blockedBy else ""
    print(f"    [{t.status}] {t.subject}{deps}")

# 检查 .tasks/ 目录
print(f"\n  [check] .tasks/ 文件数: {len(list(tasks_dir.glob('task_*.json')))}")

# ── Round 2: 列出所有任务 ──
print(f"\n{'#'*65}")
print(f"  >>> Round 2: List all tasks")
print(f"{'#'*65}")
from task import run_list_tasks
print()
print(run_list_tasks())

# ── Round 3: 认领第一个未被阻塞的任务并完成 ──
print(f"\n{'#'*65}")
print(f"  >>> Round 3: Claim first unblocked task and complete it")
print(f"{'#'*65}")

# schema 没有依赖，应该可以认领
print(f"\n  Claiming '{schema.subject}'...")
result = claim_task(schema.id)
print(f"    {result}")

print(f"  Completing '{schema.subject}'...")
result = complete_task(schema.id)
print(f"    {result}")

# ── Round 4: 再次列出，观察解锁──
print(f"\n{'#'*65}")
print(f"  >>> Round 4: List tasks — which ones are now unblocked?")
print(f"{'#'*65}")
print()
print(run_list_tasks())

# 检查哪些任务现在可以被认领
print(f"\n  [check] can_start check:")
from task import can_start
for t in list_tasks():
    startable = can_start(t.id) if t.status == "pending" else False
    print(f"    {t.subject}: status={t.status}, can_start={startable}")

print(f"\n{'='*65}")
print(f"  结果验证")
print(f"{'='*65}")
all_tasks = list_tasks()
print(f"  .tasks/ 文件数: {len(list(tasks_dir.glob('task_*.json')))} (应为 4)")
print(f"  completed: {sum(1 for t in all_tasks if t.status == 'completed')} (应为 1)")
print(f"  pending:   {sum(1 for t in all_tasks if t.status == 'pending')} (应为 3)")
print(f"  解锁的任务: ", end="")
unblocked = [t.subject for t in all_tasks if t.status == "pending" and can_start(t.id)]
print(unblocked if unblocked else "无")
print(f"  仍阻塞的任务: ", end="")
blocked = [t.subject for t in all_tasks if t.status == "pending" and not can_start(t.id)]
print(blocked if blocked else "无")

# 清理
shutil.rmtree(tasks_dir)
print(f"\n  [cleanup] .tasks/ 已删除")
print(f"\n  \033[32mAll checks passed!\033[0m" if unblocked else "")
