---
name: agent-builder
description: Guidance for building and modifying coding agents
---

# Agent Builder Skill

## Architecture

An agent consists of four layers:

- **Tool layer** (`tool.py`): tool implementations + `TOOLS`/`TOOL_HANDLERS`
- **Hook layer** (`hook.py`): hook framework + callbacks for permissions, logging
- **Subagent layer** (`subagent.py`): independent sub-process with fresh context
- **Loop layer** (`AgentLoop.py`): main while-loop that orchestrates everything

## Adding a New Tool

1. Add the `run_*` function in `tool.py`
2. Add its schema to `TOOLS` list
3. Add handler to `TOOL_HANDLERS` dict
4. If handler comes from another module, use lazy import at bottom of `tool.py`

## Adding a New Hook

1. Add the event to `HOOKS` dict in `hook.py` if new event type
2. Write the hook callback function
3. Call `register_hook(event, callback)` at module level
