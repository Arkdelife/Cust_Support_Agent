"""Diagnostic only -- connection + init message, NO query() call, NO model turn,
so this should not consume any Anthropic API credit. Deleted after use."""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
os.environ["MCP_CONNECTION_NONBLOCKING"] = "0"  # wait for airtable server to fully connect

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

from claude_agent_sdk import ClaudeSDKClient, SystemMessage  # noqa: E402
import shopfast_agent as agent  # noqa: E402
import shopfast_backend as backend  # noqa: E402


async def main():
    granted = set(agent.AIRTABLE_READ_TOOLS) | set(backend.tool_names())
    options = agent.build_options()

    async def run():
        async with ClaudeSDKClient(options=options) as client:
            async for message in client.receive_messages():
                if isinstance(message, SystemMessage) and message.subtype == "init":
                    data = message.data or {}
                    offered = [t for t in data.get("tools", []) if t.startswith("mcp__")]
                    servers = data.get("mcp_servers", [])
                    print("MCP_SERVERS:", servers)
                    print(f"OFFERED_COUNT: {len(offered)}")
                    print(f"GRANTED_COUNT: {len(granted)}")
                    extra = sorted(set(offered) - granted)
                    print(f"OFFERED_BUT_NOT_GRANTED_COUNT: {len(extra)}")
                    for t in extra:
                        print("  EXTRA:", t)
                    missing = sorted(granted - set(offered))
                    if missing:
                        print(f"GRANTED_BUT_NOT_OFFERED (should be empty): {missing}")
                    return

    try:
        await asyncio.wait_for(run(), timeout=45)
    except asyncio.TimeoutError:
        print("TIMEOUT waiting for init message -- server may not have connected.")


asyncio.run(main())
