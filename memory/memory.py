"""
持久化记忆系统：跨会话持久化存储和自动管理。

设计原则：
- 全自动管理：每轮前自动注入相关记忆，每轮后自动提取新记忆
- 不提供人工读写工具（与 s09 一致）
- 记忆索引始终轻量存在于 SYSTEM prompt 中（~100 tokens）
- 完整记忆内容按需注入当前用户消息前

存储结构：
  .memory/
    MEMORY.md       ← 索引（自动重建）
    user-*.md       ← 用户偏好
    feedback-*.md   ← 用户指导反馈
    project-*.md    ← 项目事实
    reference-*.md  ← 外部参考
"""

import json
import re
import time
from pathlib import Path

from tool import WORKDIR


# ═══════════════════════════════════════════════════════════
#  常量配置
# ═══════════════════════════════════════════════════════════

MEMORY_DIR = WORKDIR / ".memory"                # 记忆文件存放目录
MEMORY_INDEX_FILE = MEMORY_DIR / "MEMORY.md"    # 索引文件
MEMORY_TYPES = ["user", "feedback", "project", "reference"]
CONSOLIDATE_THRESHOLD = 10                      # 触发合并的记忆文件数量阈值

MEMORY_DIR.mkdir(exist_ok=True)                 # 确保目录存在


# ═══════════════════════════════════════════════════════════
#  依赖注入（避免循环导入）
#  由 AgentLoop 在启动时注入 client 和 MODEL
# ═══════════════════════════════════════════════════════════

_client = None
_MODEL = None


def init_memory(client, model):
    """注入 Anthropic 客户端和模型名（用于 LLM 选择/提取/合并）。"""
    global _client, _MODEL
    _client = client
    _MODEL = model


# ═══════════════════════════════════════════════════════════
#  辅助函数
# ═══════════════════════════════════════════════════════════

def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """解析 Markdown 文件的 YAML frontmatter。

    格式：
        ---
        name: my-memory
        description: What this is
        type: user
        ---
        Body content...

    返回 (metadata_dict, body_text)。无 frontmatter 时返回 ({}, 原文)。
    """
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    # 简单的行级解析：记忆 frontmatter 只有 name/description/type 三个固定字段，
    # 用简单 split 就够，不需要引入 pyyaml 依赖
    meta = {}
    for line in parts[1].strip().splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip().strip('"').strip("'")
    return meta, parts[2].strip()


def _extract_text_content(msg) -> str:
    """从一条消息中提取纯文本内容，兼容 ContentBlock 对象和 dict。"""
    content = msg.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = []
        for block in content:
            # 兼容两种 block 格式：dict（tool_result 等自定义 block）
            # 和 ContentBlock 对象（Anthropic SDK 返回的 assistant message）
            if isinstance(block, dict):
                if block.get("type") == "text":
                    texts.append(block.get("text", "") or "")
            else:
                if getattr(block, "type", None) == "text":
                    texts.append(getattr(block, "text", ""))
        return " ".join(texts)
    return str(content)


# ═══════════════════════════════════════════════════════════
#  核心操作：读写 / 索引 / 列出
# ═══════════════════════════════════════════════════════════

def write_memory_file(name: str, mem_type: str, description: str, body: str) -> Path:
    """写入一条记忆文件（含 YAML frontmatter），自动重建索引。"""
    slug = name.lower().replace(" ", "-").replace("/", "-")
    filename = f"{slug}.md"
    filepath = MEMORY_DIR / filename
    filepath.write_text(
        f"---\nname: {name}\ndescription: {description}\ntype: {mem_type}\n---\n\n{body}\n",
        encoding="utf-8",
    )
    _rebuild_index()
    return filepath


def _rebuild_index():
    """扫描所有 .md 文件，重建 MEMORY.md 索引。"""
    lines = []
    for f in sorted(MEMORY_DIR.glob("*.md")):
        if f.name == "MEMORY.md":
            continue
        raw = f.read_text(encoding="utf-8")
        meta, body = _parse_frontmatter(raw)
        name = meta.get("name", f.stem)
        desc = meta.get("description", body.split("\n")[0][:80])
        lines.append(f"- [{name}]({f.name}) — {desc}")
    MEMORY_INDEX_FILE.write_text("\n".join(lines) + "\n" if lines else "", encoding="utf-8")


def read_memory_index() -> str:
    """读取 MEMORY.md 索引文本（用于注入 SYSTEM prompt）。"""
    if not MEMORY_INDEX_FILE.exists():
        return ""
    text = MEMORY_INDEX_FILE.read_text(encoding="utf-8", errors="replace").strip()
    return text if text else ""


def read_memory_file(filename: str) -> str | None:
    """读取一条记忆文件的完整内容。"""
    path = MEMORY_DIR / filename
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8", errors="replace")


def list_memory_files() -> list[dict]:
    """列出所有记忆文件（含元数据），排除 MEMORY.md。"""
    result = []
    for f in sorted(MEMORY_DIR.glob("*.md")):
        if f.name == "MEMORY.md":
            continue
        raw = f.read_text(encoding="utf-8")
        meta, body = _parse_frontmatter(raw)
        result.append({
            "filename": f.name,
            "name": meta.get("name", f.stem),
            "description": meta.get("description", ""),
            "type": meta.get("type", "user"),
            "body": body,
        })
    return result


# ═══════════════════════════════════════════════════════════
#  LLM 选择相关记忆（每轮调用一次）
# ═══════════════════════════════════════════════════════════

def select_relevant_memories(messages: list, max_items: int = 5) -> list[str]:
    """从对话历史中选出与当前轮次相关的记忆文件名。

    优先使用 LLM 按名称+描述筛选，fallback 到关键词匹配。
    """
    files = list_memory_files()
    if not files:
        return []

    # 收集最近几条用户消息作为上下文
    recent_texts = []
    for msg in reversed(messages):
        if msg.get("role") == "user":
            text = _extract_text_content(msg)
            if text.strip():
                recent_texts.append(text)
            if len(recent_texts) >= 3:
                break
    recent = " ".join(reversed(recent_texts))[:2000]

    if not recent.strip():
        return []

    # 构建记忆目录供 LLM 选择
    catalog_lines = []
    for i, f in enumerate(files):
        catalog_lines.append(f"{i}: {f['name']} — {f['description']}")
    catalog = "\n".join(catalog_lines)

    prompt = (
        "Given the recent conversation and the memory catalog below, "
        "select the indices of memories that are clearly relevant. "
        "Return ONLY a JSON array of integers, e.g. [0, 3]. "
        "If none are relevant, return [].\n\n"
        f"Recent conversation:\n{recent}\n\n"
        f"Memory catalog:\n{catalog}"
    )

    try:
        response = _client.messages.create(
            model=_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=200,
        )
        text = ""
        for block in response.content:
            if getattr(block, "type", None) == "text":
                text = block.text
                break
        text = text.strip()
        match = re.search(r'\[.*?\]', text, re.DOTALL)
        if match:
            indices = json.loads(match.group())
            selected = []
            for idx in indices:
                if isinstance(idx, int) and 0 <= idx < len(files):
                    selected.append(files[idx]["filename"])
                    if len(selected) >= max_items:
                        break
            return selected
    except Exception:
        pass

    # --- Fallback：关键词匹配 ---
    # try/except 兜底：LLM 调用可能因网络/API 限流失败，
    # 降级到关键词匹配，不至于完全丢失记忆加载能力
    keywords = [w.lower() for w in recent.split() if len(w) > 3]
    selected = []
    for f in files:
        text = (f["name"] + " " + f["description"]).lower()
        if any(kw in text for kw in keywords):
            selected.append(f["filename"])
            if len(selected) >= max_items:
                break
    return selected


def load_memories(messages: list) -> str:
    """加载相关记忆内容，供注入到当前用户消息前。

    返回格式：<relevant_memories>...</relevant_memories>
    """
    selected_files = select_relevant_memories(messages)
    if not selected_files:
        return ""

    parts = ["<relevant_memories>"]
    for filename in selected_files:
        content = read_memory_file(filename)
        if content:
            parts.append(content)
    parts.append("</relevant_memories>")
    return "\n\n".join(parts)


# ═══════════════════════════════════════════════════════════
#  自动提取新记忆（每轮结束后运行）
# ═══════════════════════════════════════════════════════════

def extract_memories(messages: list):
    """从最近对话中提取新记忆。

    使用 LLM 分析最近消息，识别用户偏好、项目事实等，
    写入 .memory/ 目录，自动避重。
    """
    # 收集最近对话
    dialogue_parts = []
    for msg in messages[-10:]:
        role = msg.get("role", "?")
        text = _extract_text_content(msg)
        if text.strip():
            dialogue_parts.append(f"{role}: {text}")
    dialogue = "\n".join(dialogue_parts)

    if not dialogue.strip():
        return

    # 检查已有记忆避免重复
    existing = list_memory_files()
    existing_desc = "\n".join(
        f"- {m['name']}: {m['description']}" for m in existing
    ) if existing else "(none)"

    prompt = (
        "Extract user preferences, constraints, or project facts from this dialogue.\n"
        "Return a JSON array. Each item: {name, type, description, body}.\n"
        "- name: short kebab-case identifier (e.g. 'user-preference-tabs')\n"
        "- type: one of 'user' (user preference), 'feedback' (guidance), "
        "'project' (project fact), 'reference' (external pointer)\n"
        "- description: one-line summary for index lookup\n"
        "- body: full detail in markdown\n"
        "If nothing new or already covered by existing memories, return [].\n\n"
        f"Existing memories:\n{existing_desc}\n\n"
        f"Dialogue:\n{dialogue[:4000]}"
    )

    try:
        response = _client.messages.create(
            model=_MODEL, messages=[{"role": "user", "content": prompt}], max_tokens=800
        )
        text = ""
        for block in response.content:
            if getattr(block, "type", None) == "text":
                text = block.text
                break
        text = text.strip()
        match = re.search(r'\[.*\]', text, re.DOTALL)
        if not match:
            return
        items = json.loads(match.group())
        if not items:
            return

        count = 0
        for mem in items:
            name = mem.get("name", f"memory_{int(time.time())}")
            mem_type = mem.get("type", "user")
            desc = mem.get("description", "")
            body = mem.get("body", "")
            if desc and body:
                write_memory_file(name, mem_type, desc, body)
                count += 1
        if count:
            print(f"\n\033[33m[Memory: extracted {count} new memories]\033[0m")
    except Exception:
        # try/except 兜底：提取记忆是辅助功能，失败不影响主流程
        pass


# ═══════════════════════════════════════════════════════════
#  记忆合并（文件数 ≥ 10 时触发）
# ═══════════════════════════════════════════════════════════

def consolidate_memories():
    """合并重复/过时的记忆。

    将当前所有记忆发给 LLM，要求合并去重，
    然后删旧文件、写新文件。
    """
    files = list_memory_files()
    if len(files) < CONSOLIDATE_THRESHOLD:
        return

    catalog = "\n\n".join(
        f"## {f['filename']}\nname: {f['name']}\ndescription: {f['description']}\n{f['body']}"
        for f in files
    )

    prompt = (
        "Consolidate the following memory files. Rules:\n"
        "1. Merge duplicates into one\n"
        "2. Remove outdated/contradicted memories\n"
        "3. Keep the total under 30 memories\n"
        "4. Preserve important user preferences above all\n"
        "Return a JSON array. Each item: {name, type, description, body}.\n\n"
        f"{catalog[:16000]}"
    )

    try:
        response = _client.messages.create(
            model=_MODEL, messages=[{"role": "user", "content": prompt}], max_tokens=3000
        )
        text = ""
        for block in response.content:
            if getattr(block, "type", None) == "text":
                text = block.text
                break
        text = text.strip()
        match = re.search(r'\[.*\]', text, re.DOTALL)
        if not match:
            return
        items = json.loads(match.group())

        # 删除旧记忆文件（保留 MEMORY.md 索引）
        # 采用删旧写新的策略而非原地修改，简化逻辑、避免残留
        for f in MEMORY_DIR.glob("*.md"):
            if f.name != "MEMORY.md":
                f.unlink()

        for mem in items:
            name = mem.get("name", f"memory_{int(time.time())}")
            mem_type = mem.get("type", "user")
            desc = mem.get("description", "")
            body = mem.get("body", "")
            if desc and body:
                write_memory_file(name, mem_type, desc, body)

        print(f"\n\033[33m[Memory: consolidated {len(files)} -> {len(items)} memories]\033[0m")
    except Exception:
        # try/except 兜底：合并失败不影响当前对话
        pass
