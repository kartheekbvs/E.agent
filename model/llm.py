
import os

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()


class LLM:
    def __init__(self):
        self.client = OpenAI(
            base_url=os.getenv("base_url"),
            api_key=os.getenv("api_key"),
        )
        self.model = os.getenv("LLM_MODEL")

    def generate(self, messages, tools=None):
        request = {
            "model": self.model,
            "messages": messages,
        }

        if tools:
            request["tools"] = tools
            request["tool_choice"] = "auto"

        return self.client.chat.completions.create(**request)
