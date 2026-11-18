"""Tiny OpenAI-compatible chat client for the local model server (stdlib only)."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

DEFAULT_URL = "http://100.89.28.64:8080/v1"
DEFAULT_MODEL = "qwen-exec"


class LlmError(Exception):
    """The model server could not be reached or returned an error."""


def base_url() -> str:
    return os.environ.get("CNLP_LLM_URL", DEFAULT_URL).rstrip("/")


def model() -> str:
    return os.environ.get("CNLP_LLM_MODEL", DEFAULT_MODEL)


def complete(
    messages: list[dict],
    grammar: str | None = None,
    temperature: float = 0.7,
    max_tokens: int = 1500,
    seed: int | None = None,
    timeout: float = 300,
    json_schema: dict | None = None,
) -> str:
    """One chat completion; returns the reply text. No tools (grammar and tools clash)."""
    url = base_url()
    body: dict = {
        "model": model(),
        "messages": messages,
        "temperature": temperature,
        "top_p": 0.8,
        "top_k": 20,
        "max_tokens": max_tokens,
        "cache_prompt": True,  # llama.cpp: reuse the KV cache of an identical prompt prefix
        "chat_template_kwargs": {"enable_thinking": False},
    }
    if grammar:
        body["grammar"] = grammar
    if json_schema:
        body["response_format"] = {"type": "json_object", "schema": json_schema}
    if seed is not None:
        body["seed"] = seed
    req = urllib.request.Request(
        url + "/chat/completions",
        json.dumps(body).encode(),
        {"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        raise LlmError(f"The local model server at {url} refused the request "
                       f"(HTTP {e.code}): {detail}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise LlmError(f"The local model server at {url} isn't reachable "
                       f"({getattr(e, 'reason', e)}). Check that it is running, or set "
                       "CNLP_LLM_URL.") from e
    except json.JSONDecodeError as e:
        raise LlmError(f"The local model server at {url} sent a reply that isn't JSON.") from e
    try:
        return data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError) as e:
        raise LlmError(f"The local model server at {url} sent an unexpected reply.") from e
