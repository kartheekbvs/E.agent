
import os
import json
from pathlib import Path
from dotenv import load_dotenv
from openai import OpenAI
import asyncio
import json

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
load_dotenv()
WORKSPACE_ROOT = Path("workspace").resolve()

class MultiAgent:
    def __init__(self):
        base_url = os.getenv("multi_agent_base_url")
        api_key = os.getenv("multi_agent_api_key")
        model = os.getenv("multi_agent_model")

        if not base_url:
            raise ValueError("multi_agent_base_url is missing from .env")

        if not api_key:
            raise ValueError("multi_agent_api_key is missing from .env")

        if not model:
            raise ValueError("MULTI_AGENT_MODEL is missing from .env")

        self.model = model
        self.client = OpenAI(
            base_url=base_url,
            api_key=api_key,
        )

    def generate(self, messages, tools=None):
        request = {
            "model": self.model,
            "messages": messages,
        }

        if tools:
            request["tools"] = tools
            request["tool_choice"] = "auto"

        return self.client.chat.completions.create(**request)

    async def run_task(
        self,
        task,
        context=None,
        effort="medium",
        coding_level="college",
        on_progress=None,
    ):
        s
        
        workspace = os.environ.get("ENTROPY_WORKSPACE")

        child_env = os.environ.copy()

        if workspace:
            child_env["ENTROPY_WORKSPACE"] = workspace

        server_params = StdioServerParameters(
    command="uv",
    args=["run", "mcp", "run", "server/child_server.py"],
    env=child_env,
)


        if on_progress:
            on_progress("Connecting to child MCP server")

        async with stdio_client(server_params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                discovered = await session.list_tools()

                llm_tools = [
                    {
                        "type": "function",
                        "function": {
                            "name": tool.name,
                            "description": tool.description or "",
                            "parameters": tool.inputSchema,
                        },
                    }
                    for tool in discovered.tools
                ]

                if on_progress:
                    on_progress(
                        f"Discovered {len(llm_tools)} child tools"
                    )

                system_prompt = f"""
You are Entropy's child agent.

You are a helper to the main agent, not an independent orchestrator.
Complete only the task assigned to you.

TASK
{task}

ADDITIONAL CONTEXT
{json.dumps(context or {}, ensure_ascii=False, default=str)}

SETTINGS
Effort: {effort}
Coding level: {coding_level}

RULES
- Discover and use only the tools provided to you.
- Choose tool arguments based on the task and tool results.
- Do not invent tool results or claim unverified work.
- Stay within the assigned task and workspace boundaries.
- Use concise outputs and avoid unnecessary exploration.
- Return a useful summary of findings and remaining work.
- Do not claim that files were changed or tests were run
  unless you have evidence.
"""

                messages = [
                    {
                        "role": "system",
                        "content": system_prompt,
                    },
                    {
                        "role": "user",
                        "content": task,
                    },
                ]

                response = await asyncio.to_thread(
                    self.generate,
                    messages,
                    llm_tools,
                )

                message = response.choices[0].message

                while message.tool_calls:
                    messages.append({
                        "role": "assistant",
                        "content": message.content,
                        "tool_calls": [
                            {
                                "id": call.id,
                                "type": "function",
                                "function": {
                                    "name": call.function.name,
                                    "arguments": call.function.arguments,
                                },
                            }
                            for call in message.tool_calls
                        ],
                    })

                    for call in message.tool_calls:
                        name = call.function.name

                        try:
                            arguments = json.loads(
                                call.function.arguments
                            )

                            if not isinstance(arguments, dict):
                                raise ValueError(
                                    "Tool arguments must be an object."
                                )

                            if on_progress:
                                on_progress(
                                    f"Child executing: {name}"
                                )

                            result = await session.call_tool(
                                name,
                                arguments,
                            )

                            output = "\n".join(
                                item.text
                                for item in (result.content or [])
                                if hasattr(item, "text")
                            )

                            if not output:
                                structured = getattr(
                                    result,
                                    "structuredContent",
                                    None,
                                )
                                output = json.dumps(
                                    structured
                                    if structured is not None
                                    else {"success": not result.isError},
                                    default=str,
                                )

                            if result.isError:
                                output = "Tool error: " + output

                        except Exception as error:
                            output = (
                                f"Tool call failed: "
                                f"{type(error).__name__}: {error}"
                            )

                        messages.append({
                            "role": "tool",
                            "tool_call_id": call.id,
                            "content": output,
                        })

                        if on_progress:
                            on_progress(
                                f"Child completed tool: {name}"
                            )

                    response = await asyncio.to_thread(
                        self.generate,
                        messages,
                        llm_tools,
                    )

                    message = response.choices[0].message

                answer = message.content or ""

                if on_progress:
                    on_progress("Child task finished")

                return answer
