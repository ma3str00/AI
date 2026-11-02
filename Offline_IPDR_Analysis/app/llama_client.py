"""Client for llama.cpp and Ollama OpenAI-compatible servers."""

from __future__ import annotations

import re

import httpx


class LlamaError(Exception):
    pass


class LlamaServer:
    def __init__(
        self,
        base_url: str,
        model: str,
        timeout: float,
        sql_max_tokens: int,
        explain_max_tokens: int,
        kind: str = "llama.cpp",
        num_ctx: int | None = None,
        keep_alive: str | None = None,
    ):
        root = base_url.rstrip("/")
        if root.endswith("/v1"):
            root = root[:-3]
        self.base_url = root
        self.model = model
        self.timeout = timeout
        self.sql_max_tokens = sql_max_tokens
        self.explain_max_tokens = explain_max_tokens
        self.kind = kind
        self.num_ctx = num_ctx
        self.keep_alive = keep_alive

    def chat(self, messages: list[dict], *, json_mode: bool, temperature: float) -> str:
        max_tokens = self.sql_max_tokens if json_mode else self.explain_max_tokens
        try:
            return self._complete(messages, json_mode=json_mode, temperature=temperature, max_tokens=max_tokens)
        except LlamaError as exc:
            if json_mode and "response_format" in str(exc):
                return self._complete(messages, json_mode=False, temperature=temperature, max_tokens=max_tokens)
            raise

    def healthy(self) -> tuple[bool, str]:
        if self.kind == "ollama":
            loaded = self._loaded_model()
            if loaded is None:
                return False, f"Ollama is not reachable at {self.base_url}"
            if loaded and self.model and self.model not in loaded:
                return True, f"running, loaded model is {loaded}"
            return True, loaded or "ok"
        try:
            response = httpx.get(f"{self.base_url}/health", timeout=8)
        except httpx.HTTPError as exc:
            return False, f"llama-server is not reachable at {self.base_url}. {exc}"
        if response.status_code != 200:
            return False, f"llama-server /health returned HTTP {response.status_code}"
        loaded = self._loaded_model()
        if loaded and self.model and loaded != self.model and self.model not in loaded:
            return True, f"running, loaded model is {loaded}"
        return True, loaded or "ok"

    def _loaded_model(self) -> str | None:
        try:
            response = httpx.get(f"{self.base_url}/v1/models", timeout=5)
        except httpx.HTTPError:
            return None if self.kind == "ollama" else ""
        if response.status_code != 200:
            return None if self.kind == "ollama" else ""
        try:
            payload = response.json()
        except ValueError:
            return None if self.kind == "ollama" else ""
        data = payload.get("data") or [] if isinstance(payload, dict) else []
        if not data:
            return ""
        return str(data[0].get("id") or "")

    def _complete(self, messages: list[dict], *, json_mode: bool, temperature: float, max_tokens: int) -> str:
        if self.kind == "ollama":
            return self._ollama_chat(messages, json_mode=json_mode, temperature=temperature, max_tokens=max_tokens)
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "top_p": 1,
            "stream": False,
            "max_tokens": max_tokens,
            "top_k": 1 if temperature == 0 else 40,
            "repeat_penalty": 1.0,
            "cache_prompt": True,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        try:
            response = httpx.post(
                f"{self.base_url}/v1/chat/completions",
                json=payload,
                timeout=self.timeout,
            )
        except httpx.HTTPError as exc:
            raise LlamaError(f"Cannot reach llama-server at {self.base_url}. {exc}") from exc
        return self._content_from_openai(response, json_mode=json_mode)

    def _ollama_chat(self, messages: list[dict], *, json_mode: bool, temperature: float, max_tokens: int) -> str:
        # Native /api/chat supports keep_alive and num_ctx. The OpenAI path does not.
        options = {
            "temperature": temperature,
            "top_p": 1,
            "num_predict": max_tokens,
        }
        if self.num_ctx:
            options["num_ctx"] = int(self.num_ctx)
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "options": options,
        }
        if self.keep_alive is not None:
            # Ollama wants a number (-1) or a duration with a unit ("24h"), not "-1".
            text = str(self.keep_alive).strip()
            if re.fullmatch(r"-?\d+", text):
                payload["keep_alive"] = int(text)
            else:
                payload["keep_alive"] = text
        if json_mode:
            payload["format"] = "json"
        try:
            response = httpx.post(
                f"{self.base_url}/api/chat",
                json=payload,
                timeout=self.timeout,
            )
        except httpx.HTTPError as exc:
            raise LlamaError(f"Cannot reach Ollama at {self.base_url}. {exc}") from exc
        if response.status_code != 200:
            detail = response.text[:500]
            if response.status_code == 530 or "cloudflare tunnel error" in detail.lower():
                raise LlamaError(
                    "The Kaggle tunnel is down, so Ollama never received this question. "
                    "Restart ollama and the Cloudflare tunnel on the notebook, or choose Local 7B."
                )
            if json_mode and response.status_code == 400:
                raise LlamaError(f"response_format was rejected: {detail}")
            raise LlamaError(f"Ollama returned HTTP {response.status_code}: {detail}")
        try:
            body = response.json()
        except ValueError as exc:
            raise LlamaError("Ollama returned a non-JSON answer.") from exc
        message = body.get("message") or {}
        content = (message.get("content") or "").strip()
        if not content:
            raise LlamaError("Ollama returned an empty answer.")
        return content

    def _content_from_openai(self, response: httpx.Response, *, json_mode: bool) -> str:
        if response.status_code != 200:
            detail = response.text[:500]
            if response.status_code == 530 or "cloudflare tunnel error" in detail.lower():
                raise LlamaError(
                    "The Kaggle tunnel is down, so Ollama never received this question. "
                    "Restart ollama and the Cloudflare tunnel on the notebook, or choose Local 7B."
                )
            if json_mode and response.status_code == 400:
                raise LlamaError(f"response_format was rejected: {detail}")
            raise LlamaError(f"llama-server returned HTTP {response.status_code}: {detail}")
        body = response.json()
        choices = body.get("choices") or []
        if not choices:
            raise LlamaError("llama-server returned no choices.")
        message = choices[0].get("message") or {}
        content = (message.get("content") or "").strip()
        if not content:
            raise LlamaError("llama-server returned an empty answer.")
        return content
