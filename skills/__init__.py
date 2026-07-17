"""
技能加载模块：两级按需注入能力（Layer 1 + Layer 2）。

Layer 1（目录）：启动时扫描 skills/ 目录，将技能名 + 一句话描述注入 SYSTEM prompt
  → 每轮对话都可见，~100 tokens/技能，无需 API 调用

Layer 2（内容）：Agent 运行时调用 load_skill 工具，通过注册表返回完整 SKILL.md
  → 被调用时花费 ~2000 tokens/技能，按需加载

SKILL.md 格式：
  ---
  name: skill-name
  description: One-line description
  ---
  Full skill content...
"""

import yaml
from pathlib import Path

SKILLS_DIR = Path(__file__).parent          # skills/ 目录路径
SKILL_REGISTRY: dict[str, dict] = {}        # 技能注册表：name → {name, description, content}


# ── Frontmatter 解析 ──────────────────────────────────────

def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """
    解析 SKILL.md 的 YAML frontmatter。

    格式：
        ---
        name: my-skill
        description: What this skill does
        ---
        Body content...

    返回 (metadata_dict, body_text)。
    如果没有 frontmatter 或解析失败，返回 ({}, 原文)。
    """
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    try:
        meta = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        meta = {}
    return meta, parts[2].strip()


# ── 启动时扫描 ────────────────────────────────────────────

def _scan_skills():
    """
    启动时扫描 skills/ 目录，填充 SKILL_REGISTRY。

    遍历每个子目录，读取其中的 SKILL.md，解析 frontmatter 获取名称和描述，
    将完整的原始内容存入注册表。只在进程启动时执行一次。
    """
    if not SKILLS_DIR.exists():
        return
    for d in sorted(SKILLS_DIR.iterdir()):
        if not d.is_dir():
            continue
        manifest = d / "SKILL.md"
        if manifest.exists():
            raw = manifest.read_text(encoding="utf-8")
            meta, body = _parse_frontmatter(raw)
            name = meta.get("name", d.name)
            desc = meta.get("description", raw.split("\n")[0].lstrip("#").strip())
            SKILL_REGISTRY[name] = {"name": name, "description": desc, "content": raw}


_scan_skills()  # 进程启动时执行一次


# ── Layer 1：生成技能目录 ──────────────────────────────────

def list_skills() -> str:
    """生成技能目录字符串（名称 + 一句话描述），供 SYSTEM prompt 使用。"""
    if not SKILL_REGISTRY:
        return "(no skills found)"
    return "\n".join(f"- **{s['name']}**: {s['description']}" for s in SKILL_REGISTRY.values())


def build_system() -> str:
    """
    构建带技能目录的 SYSTEM prompt。

    内容包含：
    - 工作目录
    - 所有可用技能的名字和描述（Layer 1）
    - todo_write 规划引导
    - task 子 Agent 指引
    """
    catalog = list_skills()
    return (
        f"You are a coding agent at {Path.cwd()}. "
        f"Skills available:\n{catalog}\n"
        "Use load_skill to get full details when needed. "
        "Before starting any multi-step task, use todo_write to plan your steps. "
        "Update status as you go. "
        "For complex sub-problems, use the task tool to spawn a subagent."
    )


# ── Layer 2：按需加载技能全文 ──────────────────────────────

def load_skill(name: str) -> str:
    """
    按技能名加载完整 SKILL.md 内容。

    通过注册表查找，不走文件路径，没有路径遍历风险。
    这是 Layer 2：只在 Agent 明确请求时才消费 token。
    """
    skill = SKILL_REGISTRY.get(name)
    if not skill:
        return f"Skill not found: {name}"
    return skill["content"]
