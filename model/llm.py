
import os

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()


def _provider_supports_tools() -> bool:
    base_url = (os.getenv("base_url") or "").lower()
    return "reflection.ai" not in base_url


def _extract_status(error) -> int | None:
    response = getattr(error, "response", None)
    if response is not None:
        status = getattr(response, "status_code", None)
        if status is not None:
            return int(status)
    return getattr(error, "status_code", None)


def _extract_error_text(error) -> str:
    body = getattr(error, "body", None)
    if body is not None:
        return str(body)

    response = getattr(error, "response", None)
    if response is not None:
        try:
            return str(response.text)
        except Exception:
            pass

    return str(error)


def _should_retry_without_tools(error) -> bool:
    status = _extract_status(error)
    message = _extract_error_text(error).lower()

    if status not in {400, 422}:
        return False

    return any(
        token in message
        for token in (
            "tool",
            "tool_choice",
            "tool_calls",
            "parallel_tool_calls",
            "json schema",
            "response format",
            "messages",
            "invalid_request",
        )
    )


def _trim_messages(messages, max_items=12):
    if len(messages) <= max_items:
        return messages

    first = messages[:1]
    tail = messages[-(max_items - 1):]
    return first + tail


class LLM:
    def __init__(self):
        self.client = OpenAI(
            base_url=os.getenv("base_url"),
            api_key=os.getenv("api_key"),
            max_retries=3,
            timeout=60.0,
        )
        self.model = os.getenv("LLM_MODEL")

    def generate(self, messages, tools=None):
        safe_messages = _trim_messages(messages)
        request = {
            "model": self.model,
            "messages": safe_messages,
        }

        if tools and _provider_supports_tools():
            request["tools"] = tools
            request["tool_choice"] = "auto"

        try:
            return self.client.chat.completions.create(**request)
        except Exception as error:
            if tools and _should_retry_without_tools(error):
                fallback_request = {
                    "model": self.model,
                    "messages": safe_messages,
                }
                return self.client.chat.completions.create(**fallback_request)
            raise
