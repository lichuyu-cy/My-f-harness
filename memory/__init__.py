"""持久化记忆系统 — 全自动管理，跨会话持续学习。

使用方式：
    from memory import init_memory, load_memories, extract_memories, consolidate_memories
    init_memory(client, MODEL)
    memories_content = load_memories(messages)   # 注入前调用
    extract_memories(pre_compress_snapshot)      # 每轮结束后调用
    consolidate_memories()                       # 定期调用
"""

from .memory import (
    MEMORY_DIR,
    MEMORY_INDEX_FILE,
    MEMORY_TYPES,
    CONSOLIDATE_THRESHOLD,
    init_memory,
    write_memory_file,
    read_memory_index,
    read_memory_file,
    list_memory_files,
    load_memories,
    extract_memories,
    consolidate_memories,
)
