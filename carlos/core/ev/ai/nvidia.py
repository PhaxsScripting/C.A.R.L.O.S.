"""NVIDIA Chat Completions transport using E.V.'s validated cloud tool loop."""

from typing import Any

from .base import ProviderError
from .openai_responses import OpenAIResponsesProvider


class NvidiaProvider(OpenAIResponsesProvider):
    name = "nvidia"
    # For requests not handled by a high-confidence service plan, NVIDIA
    # receives the whole utterance. Its tools still execute through TaskPlanner.
    # Never silently substitute the local LLM for this cloud fallback.
    interprets_all_requests = True

    async def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        messages: list[dict[str, Any]] = [{"role": "system", "content": payload["instructions"]}]
        for item in payload["input"]:
            kind = item.get("type")
            if kind == "function_call":
                call = {
                    "id": item["call_id"],
                    "type": "function",
                    "function": {"name": item["name"], "arguments": item["arguments"]},
                }
                if messages[-1]["role"] != "assistant":
                    messages.append({"role": "assistant", "content": None})
                messages[-1].setdefault("tool_calls", []).append(call)
            elif kind == "function_call_output":
                messages.append(
                    {"role": "tool", "tool_call_id": item["call_id"], "content": item["output"]}
                )
            elif kind == "message":
                messages.append(
                    {
                        "role": "assistant",
                        "content": "\n".join(
                            part.get("text", "") for part in item.get("content", [])
                        ),
                    }
                )
            elif item.get("role") == "developer":
                messages[0]["content"] += "\n" + item["content"]
            elif item.get("role") in {"user", "assistant"}:
                messages.append({"role": item["role"], "content": item["content"]})
        request = {
            "model": self.model,
            "messages": messages,
            "max_tokens": payload["max_output_tokens"],
            "stream": False,
            "temperature": 0.4,
        }
        if self.model == "nvidia/nemotron-3-super-120b-a12b":
            request.update(
                temperature=1.0, top_p=0.95, chat_template_kwargs={"enable_thinking": False}
            )
        if payload["tools"]:
            request["tools"] = [
                {
                    "type": "function",
                    "function": {key: tool[key] for key in ("name", "description", "parameters")},
                }
                for tool in payload["tools"]
            ]
            request["tool_choice"] = "auto"
            request["parallel_tool_calls"] = False
        response = await self._request(request, "/chat/completions")
        try:
            choice = response["choices"][0]
            if choice.get("finish_reason") not in {"stop", "tool_calls"}:
                raise ProviderError(
                    "NVIDIA returned an incomplete response; no new actions were executed"
                )
            message = choice["message"]
            output = []
            if message.get("content"):
                if not isinstance(message["content"], str):
                    raise ValueError("Invalid content")
                output.append(
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": message["content"]}],
                    }
                )
            seen = set()
            for call in message.get("tool_calls") or []:
                if (
                    call.get("type") != "function"
                    or not isinstance(call.get("id"), str)
                    or not call["id"]
                    or call["id"] in seen
                ):
                    raise ValueError("Invalid call ID")
                seen.add(call["id"])
                function = call["function"]
                if not isinstance(function["arguments"], str):
                    raise ValueError("Invalid arguments")
                output.append(
                    {
                        "type": "function_call",
                        "call_id": call["id"],
                        "name": function["name"],
                        "arguments": function["arguments"],
                    }
                )
            return {
                "output": output,
                "model": response.get("model", self.model),
                "usage": response.get("usage", {}),
                "id": response.get("id", ""),
            }
        except (KeyError, IndexError, TypeError, ValueError) as error:
            raise ProviderError(
                "NVIDIA returned a malformed response; no new actions were executed"
            ) from error
