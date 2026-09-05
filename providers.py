"""Provider abstraction.

Callers hold one vendor-neutral history - a list of Turn - and each provider
translates it into its own wire format on the way out. Providers disagree sharply
about how a tool call is represented, and without this layer switching between
them would mean rewriting the application.

- anthropic: the official SDK (pip install anthropic)
- openai   : standard-library HTTP with SSE
- ollama   : standard-library HTTP with NDJSON, local, no credentials
- llamacpp : bundled llama-server over a localhost OpenAI-compatible endpoint
- echo     : a dry run that calls no API
"""

from __future__ import annotations

import json
import os
import ipaddress
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Iterator

from config import Config
from i18n import t
from tools import Tool, ToolCall, ToolResult


# --------------------------------------------------------------------------
# Vendor-neutral conversation history
# --------------------------------------------------------------------------


@dataclass
class Turn:
    """One turn. The role is "user", "assistant" or "tool"."""

    role: str
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_results: list[ToolResult] = field(default_factory=list)
    # Provider-native payload. Preferred when replaying to the same provider,
    # because Claude's thinking blocks must go back exactly as they arrived.
    raw: object = None
    provider: str = ""


@dataclass
class Chunk:
    """One piece of a streamed response."""

    kind: str  # "text" | "thinking" | "tool_call" | "final"
    text: str = ""
    tool_call: ToolCall | None = None
    raw: object = None


class ProviderError(RuntimeError):
    """An error meant for the operator. Callers show the message and nothing more."""


# --------------------------------------------------------------------------
# Shared HTTP streaming helpers, used by the openai and ollama providers
# --------------------------------------------------------------------------


def _post_lines(url: str, headers: dict[str, str], payload: dict) -> Iterator[str]:
    """POST, then yield the response one line at a time."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ProviderError("Provider URL must be an HTTP(S) URL without embedded credentials.")
    try:
        local = ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        local = parsed.hostname.casefold() == "localhost"
    if parsed.scheme != "https" and not local:
        raise ProviderError("Plain HTTP provider URLs are allowed only on localhost.")
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        response = urllib.request.urlopen(request, timeout=600)  # nosec B310
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:600]
        raise ProviderError(f"HTTP {exc.code} {exc.reason}\n{detail}") from None
    except urllib.error.URLError as exc:
        raise ProviderError(f"Could not connect: {exc.reason}") from None
    with response:
        for raw in response:
            yield raw.decode("utf-8", "replace").rstrip("\r\n")


def _sse_payloads(lines: Iterator[str]) -> Iterator[dict]:
    """Pull JSON out of the `data:` lines of an SSE stream."""
    for line in lines:
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            yield json.loads(data)
        except json.JSONDecodeError:
            continue


def _parse_arguments(raw: str) -> tuple[dict, str]:
    """Parse tool-call arguments. Malformed JSON must not crash the loop."""
    if not raw:
        return {}, ""
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}, raw
    return (parsed, "") if isinstance(parsed, dict) else ({}, raw)


# --------------------------------------------------------------------------
# Anthropic, via the official SDK
# --------------------------------------------------------------------------


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, config: Config) -> None:
        try:
            import anthropic
        except ImportError:
            raise ProviderError(
                "The anthropic package is required:  pip install anthropic"
            ) from None
        self._sdk = anthropic
        try:
            # The SDK resolves credentials itself, in order: ANTHROPIC_API_KEY,
            # ANTHROPIC_AUTH_TOKEN, then an `ant auth login` profile.
            self._client = anthropic.Anthropic(base_url=config.base_url or None)
        except Exception as exc:
            raise ProviderError(f"Could not initialize the client: {exc}") from None
        self.config = config
        self.model = config.model

    # -- History translation -----------------------------------------------
    def _messages(self, history: list[Turn]) -> list[dict]:
        out: list[dict] = []
        for turn in history:
            if turn.role == "user":
                out.append({"role": "user", "content": turn.text})
            elif turn.role == "assistant":
                if turn.raw is not None and turn.provider == self.name:
                    # Thinking blocks must be returned exactly as received.
                    out.append({"role": "assistant", "content": turn.raw})
                    continue
                blocks: list[dict] = []
                if turn.text:
                    blocks.append({"type": "text", "text": turn.text})
                for call in turn.tool_calls:
                    blocks.append({
                        "type": "tool_use",
                        "id": call.id,
                        "name": call.name,
                        "input": call.arguments,
                    })
                if blocks:
                    out.append({"role": "assistant", "content": blocks})
            elif turn.role == "tool":
                blocks = []
                for result in turn.tool_results:
                    block: dict = {
                        "type": "tool_result",
                        "tool_use_id": result.id,
                        "content": result.content,
                    }
                    if result.is_error:
                        block["is_error"] = True
                    blocks.append(block)
                out.append({"role": "user", "content": blocks})
        return out

    def stream(self, system: str, history: list[Turn], tools: list[Tool]) -> Iterator[Chunk]:
        kwargs: dict = {
            "model": self.model,
            "max_tokens": self.config.max_tokens,
            "messages": self._messages(history),
            # Pays off as the conversation grows: places a cache breakpoint on
            # the most recent block automatically.
            "cache_control": {"type": "ephemeral"},
            # Adaptive thinking. Without an explicit display the thinking text
            # arrives empty.
            "thinking": {"type": "adaptive", "display": "summarized"},
            "output_config": {"effort": self.config.effort},
        }
        if system:
            kwargs["system"] = system
        if tools:
            lang = self.config.ui_lang
            kwargs["tools"] = [
                {
                    "name": tool.name,
                    "description": tool.description(lang),
                    "input_schema": tool.parameters(lang),
                }
                for tool in tools
            ]
        # Note: claude-opus-5 rejects temperature, top_p and top_k with a 400,
        # so they are never sent.

        sdk = self._sdk
        try:
            with self._client.messages.stream(**kwargs) as stream:
                for event in stream:
                    if event.type != "content_block_delta":
                        continue
                    delta = event.delta
                    if delta.type == "text_delta":
                        yield Chunk("text", delta.text)
                    elif delta.type == "thinking_delta":
                        yield Chunk("thinking", delta.thinking)
                final = stream.get_final_message()

            if final.stop_reason == "refusal":
                detail = getattr(final.stop_details, "explanation", None) or ""
                raise ProviderError(f"The model declined to respond. {detail}".strip())

            for block in final.content:
                if block.type == "tool_use":
                    yield Chunk("tool_call", tool_call=ToolCall(
                        id=block.id, name=block.name, arguments=dict(block.input or {})
                    ))
            # Keep the raw blocks so the next turn can replay them unchanged.
            yield Chunk("final", raw=final.content)

            if final.stop_reason == "max_tokens":
                yield Chunk("text", t(self.config.ui_lang, "truncated"))
        except sdk.BadRequestError as exc:
            # Running past the context window is something the operator can act
            # on, so it gets its own message rather than a raw API error.
            if "prompt is too long" in str(exc).lower() or "context" in str(exc).lower():
                raise ProviderError(t(self.config.ui_lang, "context_too_long")) from None
            raise ProviderError(f"Invalid request: {exc}") from None
        except sdk.RateLimitError:
            raise ProviderError("Rate limited. Wait a moment and retry.") from None
        except sdk.AuthenticationError:
            raise ProviderError("Authentication failed. Check ANTHROPIC_API_KEY.") from None
        except sdk.NotFoundError:
            raise ProviderError(f"Model not found: {self.model}") from None
        except sdk.APIConnectionError:
            raise ProviderError("Could not reach the API. Check your network.") from None
        except sdk.APIStatusError as exc:
            raise ProviderError(f"API error ({exc.status_code}): {exc.message}") from None
        except TypeError as exc:
            # With no credential resolvable at all, the SDK raises TypeError
            # while building the request rather than on the wire.
            if "authentication method" in str(exc):
                raise ProviderError(
                    "No credentials found. Set one of the following:\n"
                    "  - the ANTHROPIC_API_KEY environment variable\n"
                    "  - a .env file (see .env.example)\n"
                    "  - ant auth login"
                ) from None
            raise


# --------------------------------------------------------------------------
# OpenAI-compatible, over raw HTTP and SSE
# --------------------------------------------------------------------------


def _openai_messages(history: list[Turn], system: str) -> list[dict]:
    out: list[dict] = []
    if system:
        out.append({"role": "system", "content": system})
    for turn in history:
        if turn.role == "user":
            out.append({"role": "user", "content": turn.text})
        elif turn.role == "assistant":
            message: dict = {"role": "assistant", "content": turn.text or None}
            if turn.tool_calls:
                message["tool_calls"] = [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": json.dumps(call.arguments, ensure_ascii=False),
                        },
                    }
                    for call in turn.tool_calls
                ]
            out.append(message)
        elif turn.role == "tool":
            # OpenAI wants one message per tool result.
            for result in turn.tool_results:
                out.append({
                    "role": "tool",
                    "tool_call_id": result.id,
                    "content": result.content,
                })
    return out


def _ollama_messages(history: list[Turn], system: str) -> list[dict]:
    """Translate history to Ollama's object-valued tool-call format."""
    out: list[dict] = []
    if system:
        out.append({"role": "system", "content": system})
    for turn in history:
        if turn.role == "user":
            out.append({"role": "user", "content": turn.text})
        elif turn.role == "assistant":
            message: dict = {"role": "assistant", "content": turn.text or ""}
            if turn.tool_calls:
                message["tool_calls"] = [
                    {"function": {"name": call.name, "arguments": call.arguments}}
                    for call in turn.tool_calls
                ]
            out.append(message)
        elif turn.role == "tool":
            for result in turn.tool_results:
                out.append({"role": "tool", "tool_name": result.name,
                            "content": result.content})
    return out


class OpenAIProvider:
    name = "openai"

    def __init__(self, config: Config) -> None:
        key = os.environ.get("OPENAI_API_KEY")
        if not key:
            raise ProviderError("OPENAI_API_KEY is not set.")
        self._key = key
        self._base = (config.base_url or "https://api.openai.com/v1").rstrip("/")
        self.config = config
        self.model = config.model

    def stream(self, system: str, history: list[Turn], tools: list[Tool]) -> Iterator[Chunk]:
        payload: dict = {
            "model": self.model,
            "messages": _openai_messages(history, system),
            "stream": True,
        }
        if self.config.temperature is not None:
            payload["temperature"] = self.config.temperature
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description(self.config.ui_lang),
                        "parameters": tool.parameters(self.config.ui_lang),
                    },
                }
                for tool in tools
            ]

        headers = {"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"}
        # tool_calls arrive split across deltas, keyed by index; reassemble them.
        pending: dict[int, dict] = {}

        for data in _sse_payloads(_post_lines(f"{self._base}/chat/completions", headers, payload)):
            for choice in data.get("choices", []):
                delta = choice.get("delta") or {}
                text = delta.get("content")
                if text:
                    yield Chunk("text", text)
                for fragment in delta.get("tool_calls") or []:
                    index = fragment.get("index", 0)
                    slot = pending.setdefault(index, {"id": "", "name": "", "arguments": ""})
                    if fragment.get("id"):
                        slot["id"] = fragment["id"]
                    function = fragment.get("function") or {}
                    if function.get("name"):
                        slot["name"] = function["name"]
                    if function.get("arguments"):
                        slot["arguments"] += function["arguments"]

        for index in sorted(pending):
            slot = pending[index]
            if not slot["name"]:
                continue
            arguments, broken = _parse_arguments(slot["arguments"])
            yield Chunk("tool_call", tool_call=ToolCall(
                id=slot["id"] or f"call_{index}",
                name=slot["name"],
                arguments=arguments,
                raw_arguments=broken,
            ))
        yield Chunk("final")


class LlamaCppProvider(OpenAIProvider):
    """Local OpenAI-compatible provider served by the bundled llama.cpp runtime."""

    name = "llamacpp"

    def __init__(self, config: Config) -> None:
        self._key = "local-no-credential"
        self._base = (config.base_url or "http://127.0.0.1:11435/v1").rstrip("/")
        self.config = config
        self.model = config.model


# --------------------------------------------------------------------------
# Ollama, local, over NDJSON
# --------------------------------------------------------------------------


class OllamaProvider:
    name = "ollama"

    def __init__(self, config: Config) -> None:
        self._base = (config.base_url or "http://localhost:11434").rstrip("/")
        self.config = config
        self.model = config.model

    def stream(self, system: str, history: list[Turn], tools: list[Tool]) -> Iterator[Chunk]:
        try:
            context_window = max(2048, min(int(os.environ.get("FRONTDESK_OLLAMA_NUM_CTX", "8192")), 131072))
            batch_size = max(32, min(int(os.environ.get("FRONTDESK_OLLAMA_NUM_BATCH", "256")), 2048))
        except ValueError:
            raise ProviderError("FRONTDESK_OLLAMA_NUM_CTX and NUM_BATCH must be integers.") from None
        payload: dict = {
            "model": self.model,
            "messages": _ollama_messages(history, system),
            "stream": True,
            "think": bool(self.config.show_thinking),
            "keep_alive": os.environ.get("FRONTDESK_OLLAMA_KEEP_ALIVE", "30m"),
            "options": {"num_predict": max(1, min(self.config.max_tokens, 4096)),
                        "num_ctx": context_window, "num_batch": batch_size},
        }
        if self.config.temperature is not None:
            payload["options"]["temperature"] = self.config.temperature
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description(self.config.ui_lang),
                        "parameters": tool.parameters(self.config.ui_lang),
                    },
                }
                for tool in tools
            ]

        headers = {"Content-Type": "application/json"}
        counter = 0
        for line in _post_lines(f"{self._base}/api/chat", headers, payload):
            if not line.strip():
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if data.get("error"):
                raise ProviderError(str(data["error"]))
            message = data.get("message") or {}
            thinking = message.get("thinking")
            if thinking:
                yield Chunk("thinking", thinking)
            text = message.get("content")
            if text:
                yield Chunk("text", text)
            for call in message.get("tool_calls") or []:
                function = call.get("function") or {}
                arguments = function.get("arguments")
                if isinstance(arguments, str):
                    arguments, _ = _parse_arguments(arguments)
                counter += 1
                yield Chunk("tool_call", tool_call=ToolCall(
                    id=call.get("id") or f"call_{counter}",
                    name=function.get("name", ""),
                    arguments=arguments or {},
                ))
            if data.get("done"):
                break
        yield Chunk("final")


# --------------------------------------------------------------------------
# echo: a dry run, so the flow can be inspected without calling any API
# --------------------------------------------------------------------------


class EchoProvider:
    name = "echo"

    def __init__(self, config: Config) -> None:
        self.config = config
        self.model = config.model

    def stream(self, system: str, history: list[Turn], tools: list[Tool]) -> Iterator[Chunk]:
        import time

        last = next((turn.text for turn in reversed(history) if turn.role == "user"), "")
        reply = t(
            self.config.ui_lang, "echo_reply",
            last=last, system=len(system), turns=len(history),
            tools=len(tools), model=self.model,
        )
        for index in range(0, len(reply), 8):  # Reproduce how streaming looks
            yield Chunk("text", reply[index : index + 8])
            time.sleep(0.01)
        yield Chunk("final")


PROVIDERS = {
    "anthropic": AnthropicProvider,
    "openai": OpenAIProvider,
    "ollama": OllamaProvider,
    "llamacpp": LlamaCppProvider,
    "echo": EchoProvider,
}


def build_provider(config: Config):
    """Build the provider instance the configuration asks for."""
    try:
        factory = PROVIDERS[config.provider]
    except KeyError:
        raise ProviderError(f"Unknown provider: {config.provider}") from None
    return factory(config)
