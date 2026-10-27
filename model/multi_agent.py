
import os

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()


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
