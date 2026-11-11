"""Runs ONE agent task with the Claude Agent SDK inside the hardened container.
No Bash/web tools in Phase 0: file tools only, confined to /work."""
import asyncio
import base64
import os

from claude_agent_sdk import ClaudeAgentOptions, query


async def main():
    prompt = base64.b64decode(os.environ["TASK_PROMPT_B64"]).decode()
    opts = ClaudeAgentOptions(
        cwd="/work",
        allowed_tools=["Read", "Write", "Edit", "Glob", "Grep"],
        disallowed_tools=["Bash", "WebFetch", "WebSearch"],
        permission_mode="acceptEdits",
        max_turns=int(os.environ.get("MAX_TURNS", "8")),
    )
    async for m in query(prompt=prompt, options=opts):
        name = type(m).__name__
        if name == "AssistantMessage":
            for b in getattr(m, "content", []):
                if hasattr(b, "text"):
                    print(b.text)
        elif name == "ResultMessage":
            print("RESULT subtype=%s is_error=%s turns=%s sdk_cost_usd=%s" % (
                getattr(m, "subtype", None), getattr(m, "is_error", None),
                getattr(m, "num_turns", None), getattr(m, "total_cost_usd", None)))


asyncio.run(main())
