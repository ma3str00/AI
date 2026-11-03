"""Client for llama.cpp and Ollama OpenAI-compatible servers."""

from __future__ import annotations

import json
import re
import time
from typing import Any

import httpx


class LlamaError(Exception):
    pass


def _ns_to_s(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value) / 1e9
    except (TypeError, ValueError):
        return None


def _http_timeout(seconds: float) -> httpx.Timeout:
    # Fail connect/pool waits quickly; keep a long read budget for generation.
    return httpx.Timeout(connect=20.0, read=float(seconds), write=60.0, pool=20.0)


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
        # Filled by the latest chat() / probe_latency().
        self.last_call: dict[str, Any] = {}
        self._client = self._make_client()

    def _make_client(self) -> httpx.Client:
        # One pooled client. HTTP/1.1 is more reliable through Cloudflare quick tunnels
        # than HTTP/2 for long LLM posts.
        return httpx.Client(
            timeout=_http_timeout(self.timeout),
            limits=httpx.Limits(max_keepalive_connections=4, max_connections=8, keepalive_expiry=30.0),
            http2=False,
            follow_redirects=True,
            trust_env=True,
        )

    def reset_client(self, reason: str | None = None) -> None:
        """Drop the pooled connection (stale tunnel sockets cause multi-minute waits)."""
        try:
            self._client.close()
        except Exception:
            pass
        self._client = self._make_client()
        self.last_call = {
            **(self.last_call or {}),
            "client_reset": reason or "manual",
        }

    def chat(self, messages: list[dict], *, json_mode: bool, temperature: float) -> str:
        max_tokens = self.sql_max_tokens if json_mode else self.explain_max_tokens
        # If the previous call spent a long time outside Ollama, the tunnel socket is suspect.
        prev = self.last_call or {}
        unaccounted = prev.get("unaccounted_s")
        if unaccounted is not None and float(unaccounted) >= 20.0:
            self.reset_client(reason=f"prior unaccounted {float(unaccounted):.1f}s")
        started = time.perf_counter()
        try:
            content = self._complete(messages, json_mode=json_mode, temperature=temperature, max_tokens=max_tokens)
        except LlamaError as exc:
            if json_mode and "response_format" in str(exc):
                content = self._complete(
                    messages, json_mode=False, temperature=temperature, max_tokens=max_tokens
                )
            else:
                self.last_call = {
                    **(self.last_call or {}),
                    "wall_s": time.perf_counter() - started,
                    "error": str(exc),
                }
                # Broken socket / tunnel: force a fresh client for the next try.
                if any(token in str(exc).lower() for token in ("timeout", "disconnect", "reset", "tunnel", "530")):
                    self.reset_client(reason=str(exc)[:120])
                raise
        self.last_call = {
            **(self.last_call or {}),
            "wall_s": time.perf_counter() - started,
        }
        return content

    def probe_latency(self) -> dict[str, Any]:
        """Cheap round-trip to the model host (not a generation)."""
        url = f"{self.base_url}/v1/models"
        started = time.perf_counter()
        marks: dict[str, float] = {"start": started}
        try:
            response = self._client.send(
                self._client.build_request("GET", url),
                stream=True,
            )
            marks["headers"] = time.perf_counter()
            body = response.read()
            marks["body"] = time.perf_counter()
            response.close()
        except httpx.HTTPError as exc:
            self.last_call = {
                "probe_error": str(exc),
                "probe_wall_s": time.perf_counter() - started,
            }
            self.reset_client(reason=f"probe failed: {exc}")
            return self.last_call
        self.last_call = {
            "probe_url": url,
            "probe_status": response.status_code,
            "probe_bytes": len(body),
            "probe_wall_s": marks["body"] - started,
            "probe_ttfb_s": marks["headers"] - started,
            "probe_body_s": marks["body"] - marks["headers"],
            "probe_http_elapsed_s": response.elapsed.total_seconds() if response.elapsed else None,
        }
        return self.last_call

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
            response = self._client.get(f"{self.base_url}/v1/models", timeout=5.0)
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
        return self._post_json(
            f"{self.base_url}/v1/chat/completions",
            payload,
            openai=True,
            json_mode=json_mode,
        )

    def _ollama_chat(self, messages: list[dict], *, json_mode: bool, temperature: float, max_tokens: int) -> str:
        # Stream tokens so Cloudflare/quick-tunnel sees activity instead of a silent 150s POST.
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
            "stream": True,
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
        return self._ollama_stream(f"{self.base_url}/api/chat", payload)

    def _ollama_stream(self, url: str, payload: dict) -> str:
        started = time.perf_counter()
        marks: dict[str, float] = {"start": started}
        pieces: list[str] = []
        final: dict[str, Any] = {}
        try:
            request = self._client.build_request("POST", url, json=payload)
            marks["request_built"] = time.perf_counter()
            with self._client.stream("POST", url, json=payload) as response:
                marks["headers"] = time.perf_counter()
                if response.status_code != 200:
                    raw = response.read()
                    marks["body"] = time.perf_counter()
                    self._record_http_timings(
                        url=url,
                        request_bytes=len(request.content or b""),
                        response_bytes=len(raw),
                        status_code=response.status_code,
                        marks=marks,
                        http_elapsed=response.elapsed.total_seconds() if response.elapsed else None,
                        streamed=True,
                    )
                    detail = raw.decode("utf-8", errors="replace")[:500]
                    if response.status_code == 530 or "cloudflare tunnel error" in detail.lower():
                        raise LlamaError(
                            "The Kaggle tunnel is down, so Ollama never received this question. "
                            "Restart ollama and the Cloudflare tunnel on the notebook, or choose Local 7B."
                        )
                    raise LlamaError(f"Ollama returned HTTP {response.status_code}: {detail}")
                first_token_at: float | None = None
                for line in response.iter_lines():
                    if not line:
                        continue
                    try:
                        chunk = json.loads(line)
                    except ValueError as exc:
                        raise LlamaError("Ollama returned a non-JSON stream chunk.") from exc
                    message = chunk.get("message") or {}
                    delta = message.get("content") or ""
                    if delta:
                        if first_token_at is None:
                            first_token_at = time.perf_counter()
                        pieces.append(delta)
                    if chunk.get("done"):
                        final = chunk
                marks["body"] = time.perf_counter()
                marks["first_token"] = first_token_at or marks["body"]
        except httpx.HTTPError as exc:
            self.last_call = {
                "wall_s": time.perf_counter() - started,
                "url": url,
                "error": str(exc),
                "request_build_s": (marks.get("request_built") or started) - started,
                "streamed": True,
            }
            raise LlamaError(f"Cannot reach Ollama at {self.base_url}. {exc}") from exc

        content = "".join(pieces).strip()
        response_bytes = sum(len(part.encode("utf-8")) for part in pieces)
        self._record_http_timings(
            url=url,
            request_bytes=len(request.content or b""),
            response_bytes=response_bytes,
            status_code=200,
            marks=marks,
            http_elapsed=None,
            streamed=True,
        )
        self._store_ollama_timings(final)
        if not content:
            raise LlamaError("Ollama returned an empty answer.")
        return content

    def _post_json(self, url: str, payload: dict, *, openai: bool, json_mode: bool) -> str:
        started = time.perf_counter()
        marks: dict[str, float] = {"start": started}
        try:
            request = self._client.build_request("POST", url, json=payload)
            marks["request_built"] = time.perf_counter()
            response = self._client.send(request, stream=True)
            marks["headers"] = time.perf_counter()
            raw = response.read()
            marks["body"] = time.perf_counter()
            response.close()
        except httpx.HTTPError as exc:
            self.last_call = {
                "wall_s": time.perf_counter() - started,
                "url": url,
                "error": str(exc),
                "request_build_s": (marks.get("request_built") or started) - started,
            }
            host = "Ollama" if not openai else "llama-server"
            raise LlamaError(f"Cannot reach {host} at {self.base_url}. {exc}") from exc

        http_elapsed = response.elapsed.total_seconds() if response.elapsed else None
        self._record_http_timings(
            url=url,
            request_bytes=len(request.content or b""),
            response_bytes=len(raw),
            status_code=response.status_code,
            marks=marks,
            http_elapsed=http_elapsed,
            streamed=False,
        )
        if response.status_code != 200:
            detail = raw.decode("utf-8", errors="replace")[:500]
            if response.status_code == 530 or "cloudflare tunnel error" in detail.lower():
                raise LlamaError(
                    "The Kaggle tunnel is down, so Ollama never received this question. "
                    "Restart ollama and the Cloudflare tunnel on the notebook, or choose Local 7B."
                )
            if json_mode and response.status_code == 400:
                raise LlamaError(f"response_format was rejected: {detail}")
            host = "Ollama" if not openai else "llama-server"
            raise LlamaError(f"{host} returned HTTP {response.status_code}: {detail}")
        try:
            body = httpx.Response(200, content=raw).json()
        except ValueError as exc:
            raise LlamaError("Model returned a non-JSON answer.") from exc
        if not openai:
            self._store_ollama_timings(body)
            message = body.get("message") or {}
            content = (message.get("content") or "").strip()
            if not content:
                raise LlamaError("Ollama returned an empty answer.")
            return content
        return self._content_from_openai_body(body)

    def _record_http_timings(
        self,
        *,
        url: str,
        request_bytes: int,
        response_bytes: int,
        status_code: int,
        marks: dict[str, float],
        http_elapsed: float | None,
        streamed: bool,
    ) -> None:
        start = marks["start"]
        headers_at = marks.get("headers", start)
        body_at = marks.get("body", headers_at)
        first_token_at = marks.get("first_token")
        self.last_call = {
            "url": url,
            "status_code": status_code,
            "request_bytes": request_bytes,
            "response_bytes": response_bytes,
            "request_build_s": (marks.get("request_built") or start) - start,
            "ttfb_s": headers_at - start,
            "time_to_first_token_s": (first_token_at - start) if first_token_at else None,
            "body_read_s": body_at - headers_at,
            "http_elapsed_s": http_elapsed,
            "wall_s": body_at - start,
            "streamed": streamed,
            "model_s": None,
            "load_s": None,
            "prompt_eval_s": None,
            "eval_s": None,
            "prompt_tokens": None,
            "eval_tokens": None,
            "unaccounted_s": None,
            "pre_model_s": None,
        }

    def _store_ollama_timings(self, body: dict) -> None:
        model_s = _ns_to_s(body.get("total_duration"))
        load_s = _ns_to_s(body.get("load_duration"))
        prompt_eval_s = _ns_to_s(body.get("prompt_eval_duration"))
        eval_s = _ns_to_s(body.get("eval_duration"))
        wall_s = self.last_call.get("wall_s")
        ttfb_s = self.last_call.get("ttfb_s")
        unaccounted = None
        pre_model = None
        if wall_s is not None and model_s is not None:
            unaccounted = max(0.0, float(wall_s) - float(model_s))
        # For streaming, TTFB is usually connect+queue; generation continues after.
        # pre_model ≈ time until Ollama accepted work ≈ TTFB - load (when load is tiny).
        if ttfb_s is not None and model_s is not None and not self.last_call.get("streamed"):
            pre_model = max(0.0, float(ttfb_s) - float(model_s))
        elif ttfb_s is not None and self.last_call.get("streamed"):
            pre_model = max(0.0, float(ttfb_s) - float(load_s or 0.0))
        self.last_call.update(
            {
                "model_s": model_s,
                "load_s": load_s,
                "prompt_eval_s": prompt_eval_s,
                "eval_s": eval_s,
                "prompt_tokens": body.get("prompt_eval_count"),
                "eval_tokens": body.get("eval_count"),
                "unaccounted_s": unaccounted,
                "pre_model_s": pre_model,
            }
        )

    def _content_from_openai_body(self, body: dict) -> str:
        choices = body.get("choices") or []
        if not choices:
            raise LlamaError("llama-server returned no choices.")
        message = choices[0].get("message") or {}
        content = (message.get("content") or "").strip()
        if not content:
            raise LlamaError("llama-server returned an empty answer.")
        return content
