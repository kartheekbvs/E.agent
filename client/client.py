
import asyncio
import json

from mcp.client.stdio import stdio_client
from mcp import ClientSession, StdioServerParameters


class MCPToolDiscovery:
    def __init__(self):
        self.server_params = StdioServerParameters(
            command="uv",
            args=["run", "mcp", "run", "server/server.py"],
        )

    async def discover(self, task, on_progress=None):
        async with stdio_client(self.server_params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                discovered = await session.list_tools()
                llm_tools = self.to_llm_tools(discovered.tools)

                messages = [
                    {
                        "role": "user",
                        "content": task,
                    }
                ]

                # Preserve actual preview URLs returned by start_project.
                preview_urls = []

                from model.llm import LLM

                llm = LLM()

                response = llm.generate(messages, tools=llm_tools)
                message = response.choices[0].message

                while message.tool_calls:
                    messages.append(
                        {
                            "role": "assistant",
                            "content": message.content or "",
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
                        }
                    )

                    for call in message.tool_calls:
                        tool_name = call.function.name

                        try:
                            arguments = json.loads(
                                call.function.arguments
                            )
                        except (json.JSONDecodeError, TypeError):
                            arguments = {}

                        if on_progress:
                            on_progress(
                                f"Executing tool: {tool_name}"
                            )

                        try:
                            project_result = None

                            if tool_name == "start_project":
                                from server.server import start_project

                                project_result = await asyncio.to_thread(
                                    start_project,
                                    **arguments,
                                )
                                tool_output = json.dumps(project_result)
                                tool_error = not project_result.get(
                                    "success",
                                    False,
                                )
                            else:
                                result = await session.call_tool(
                                    tool_name,
                                    arguments,
                                )

                                tool_output = "\n".join(
                                    content.text
                                    for content in (result.content or [])
                                    if hasattr(content, "text")
                                )
                                tool_error = result.isError

                            if (
                                tool_name == "start_project"
                                and project_result
                                and project_result.get("success")
                                and project_result.get("preview_url")
                            ):
                                preview = {
                                    "url": project_result["preview_url"],
                                    "project_id": project_result.get(
                                        "project_id"
                                    ),
                                    "port": project_result.get("port"),
                                }

                                preview_urls.append(preview)

                                if on_progress:
                                    on_progress(
                                        {
                                            "type": "preview",
                                            **preview,
                                        }
                                    )

                            # Send tool results back to the LLM.
                            if tool_error:
                                tool_output = (
                                    "Tool returned an error: "
                                    + tool_output
                                )

                        except Exception as error:
                            tool_output = (
                                f"Tool call failed: "
                                f"{type(error).__name__}: {error}"
                            )

                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": call.id,
                                "content": tool_output,
                            }
                        )

                        if on_progress:
                            on_progress(
                                f"Tool completed: {tool_name}"
                            )

                    response = llm.generate(
                        messages,
                        tools=llm_tools,
                    )
                    message = response.choices[0].message

                if on_progress:
                    on_progress("Generating final answer")

                answer = message.content or ""

                # Include the URL in the returned result as well, so the
                # caller can preserve it even if preview events aren't used.
                if preview_urls:
                    answer += "\n\nProject preview(s):\n"
                    for preview in preview_urls:
                        answer += f"- {preview['url']}\n"

                return answer

    def to_llm_tools(self, discovered_tools):
        llm_tools = []

        for tool in discovered_tools:
            llm_tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description or "",
                        "parameters": tool.inputSchema,
                    },
                }
            )

        return llm_tools

    async def run_task(self, task, on_progress=None):
        return await self.discover(
            task,
            on_progress=on_progress,
        )


async def main():
    discovery = MCPToolDiscovery()

    task = (
        "Read model/llm.py and explain how Entropy calls the LLM. "
        "Do not use run_command; only read and explain the file."
    )

    answer = await discovery.discover(task)
    print(answer)


if __name__ == "__main__":
    asyncio.run(main())
