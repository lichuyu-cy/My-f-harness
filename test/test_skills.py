"""Test: skill system — two-level loading."""
import sys, os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.chdir(os.path.dirname(os.path.dirname(__file__)))

from dotenv import load_dotenv

load_dotenv(override=True)

import AgentLoop

tests = [
    "What skills are available?",
    "Load the code-review skill and follow its instructions",
    "I need to do a code review -- load the relevant skill first",
]

for i, q in enumerate(tests):
    print(f"\n{'='*60}")
    print(f"TEST {i+1}: {q}")
    print(f"{'='*60}")

    history = []
    AgentLoop.trigger_hooks("UserPromptSubmit", q)
    history.append({"role": "user", "content": q})
    AgentLoop.agent_loop(history)

    response_content = history[-1]["content"]
    if isinstance(response_content, list):
        for block in response_content:
            if getattr(block, "type", None) == "text":
                text = block.text.encode("utf-8", errors="replace").decode("utf-8", errors="replace")
                print(text)
    print()
