
import asyncio
import json
import os

from mcp.client.stdio import stdio_client
from mcp import ClientSession, StdioServerParameters


class MCPToolDiscovery:
    def __init__(
        self,
        project_id: str | None = None,
        owner_id: str | int | None = None,
        workspace_path: str | None = None,
    ):
        self.project_id = project_id
        self.owner_id = str(owner_id) if owner_id is not None else None
        self.workspace_path = workspace_path

        env = os.environ.copy()

        if project_id and owner_id and workspace_path:
            env["ENTROPY_PROJECT_ID"] = project_id
            env["ENTROPY_OWNER_ID"] = str(owner_id)
            env["ENTROPY_WORKSPACE"] = workspace_path

            self.server_params = StdioServerParameters(
                command="uv",
                args=["run", "python", "-m", "server.project_server"],
                env=env,
            )
        else:
            self.server_params = StdioServerParameters(
                command="uv",
                args=["run", "mcp", "run", "server/server.py"],
                env=env,
            )

    async def discover(
         self,
    task,
    on_progress=None,
    preview_base_url=None,
    effort="medium",
    coding_level="college",
    ):
        async with stdio_client(self.server_params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                discovered = await session.list_tools()
                llm_tools = self.to_llm_tools(discovered.tools)

                system_prompt = f"""
You are Entropy, an autonomous MCP-powered coding agent.

USER CONFIGURATION
- Effort: {effort}
- Coding level: {coding_level}

EFFORT GUIDANCE
- low: Prefer direct, efficient execution for straightforward tasks.
  Avoid unnecessary planning and exploration.
- medium: Balance planning, implementation, debugging, and verification.
- max: Invest more effort in understanding requirements, exploring
  alternatives, planning complex work, testing, and iterative improvement.

CODING LEVEL GUIDANCE
- basic: Prefer simple, readable, beginner-friendly implementations.
- medium: Use appropriate modularity, validation, and error handling.
- college: Use suitable academic structure, algorithms, explanations,
  and testing when relevant to the task.
- advanced: Consider architecture, maintainability, security, testing,
  and production engineering practices when appropriate.

AGENT BEHAVIOR
- Understand the user's actual request before choosing an approach.
- Decide whether planning is useful for the task. Do not create a plan
  for every request.
- For complex or multi-step tasks, use the available planner tools to
  create actionable tasks and dependencies when useful.
- Discover and use available MCP tools dynamically. Never invent tools.
- Use actual tool results to decide what to do next.
- Adapt your strategy when an approach fails.
- Verify important outcomes using available evidence.
- Do not claim that code was tested or work was completed unless the
  available results support that claim.
- Continue while useful work remains and execution is safe.
- Respect cancellation, approval requirements, and runtime resource
  limits. Do not stop merely because a predetermined number of turns
  has elapsed.
- Return a concise, accurate final response when the task is finished
  or explain clearly what prevents completion.
"""

                if self.project_id and self.workspace_path:
                    system_prompt += """PROJECT MEMORY — memory.entropy
You are working inside an isolated Entropy project workspace.
Before making changes:
- If memory.entropy exists, use it as a concise project summary.
- Inspect the actual project files before relying on remembered details.
- Treat the summary as potentially outdated; actual files and the user's current request take priority.
After completing the user's task:
- Use the available MCP file tools to create or update memory.entropy.
- Keep the summary concise and useful for future sessions.
- Record the project's purpose, architecture, important file paths, completed work, relevant design decisions, known issues, and next steps.
- Update existing information instead of blindly appending duplicates.
- Record only facts supported by the files, tool results, or user requests.
- Never store API keys, passwords, tokens, or other secrets.
- Never store hidden chain-of-thought or private internal reasoning.
- If verification failed, clearly record what remains unverified.
- Do not claim a memory update succeeded unless the tool confirms it.
"""

                messages = [
                        {
        "role": "system",
        "content": system_prompt,
    },
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

                        call_summary = f"Executing tool: {tool_name}"
                        if arguments.get("path"):
                            call_summary += f" (path: {arguments['path']})"

                        if on_progress:
                            on_progress(call_summary)

                        try:
                            project_result = None

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
                            project_result = {}
                            if tool_name == "start_project" and not tool_error:
                                try:
                                    project_result = json.loads(tool_output)
                                except (json.JSONDecodeError, TypeError):
                                    project_result = {}

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
                            completed_message = call_summary.replace(
                                "Executing tool:",
                                "Tool completed:",
                                1,
                            )
                            on_progress(completed_message)

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

    async def run_task(
        self,
    task,
    on_progress=None,
    preview_base_url=None,
    effort="medium",
    coding_level="college",
    ):
        return await self.discover(
            task,
        on_progress=on_progress,
        preview_base_url=preview_base_url,
        effort=effort,
        coding_level=coding_level,
        )


async def main():
    discovery = MCPToolDiscovery()

    task = input("Enter a task for Entropy: ")

    if not task.strip():
        raise ValueError("Task cannot be empty.")

    answer = await discovery.run_task(task)
    print(answer)


if __name__ == "__main__":
    asyncio.run(main())
