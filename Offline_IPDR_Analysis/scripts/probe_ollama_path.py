"""Measure where time goes between this host and Ollama (tunnel vs model).

Usage:
  python scripts/probe_ollama_path.py
  python scripts/probe_ollama_path.py https://xxxx.trycloudflare.com qwen2.5-coder-14b:latest

Prints connect/TLS/TTFB for /v1/models and a tiny streamed /api/chat so you can
compare path delay against Ollama's own total_duration.
"""

from __future__ import annotations

import json
import socket
import ssl
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _load_kaggle_defaults() -> tuple[str, str]:
    try:
        from app.config import load_config

        config = load_config()
        for engine in config.engines:
            if engine.id == "kaggle" or engine.kind == "ollama":
                return engine.base_url, engine.model or "qwen2.5-coder-14b:latest"
    except Exception as exc:  # noqa: BLE001 — diagnostic tool should keep going
        print(f"(config load skipped: {exc})")
    return "http://127.0.0.1:11434", "qwen2.5-coder-14b:latest"


def _tcp_tls_timing(base_url: str) -> dict[str, float | str | None]:
    parsed = urlparse(base_url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    out: dict[str, float | str | None] = {"host": host, "port": port}
    started = time.perf_counter()
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        out["dns_s"] = time.perf_counter() - started
        out["addrs"] = ", ".join(sorted({item[4][0] for item in infos}))
    except OSError as exc:
        out["dns_error"] = str(exc)
        return out
    sock = socket.socket(socket.AF_INET if ":" not in str(infos[0][4][0]) else socket.AF_INET6)
    sock.settimeout(20)
    connect_started = time.perf_counter()
    try:
        sock.connect(infos[0][4])
        out["tcp_s"] = time.perf_counter() - connect_started
    except OSError as exc:
        out["tcp_error"] = str(exc)
        sock.close()
        return out
    if parsed.scheme == "https":
        tls_started = time.perf_counter()
        try:
            context = ssl.create_default_context()
            wrapped = context.wrap_socket(sock, server_hostname=host)
            out["tls_s"] = time.perf_counter() - tls_started
            wrapped.close()
        except OSError as exc:
            out["tls_error"] = str(exc)
            sock.close()
    else:
        sock.close()
        out["tls_s"] = 0.0
    return out


def _import_llama_server():
    try:
        from app.llama_client import LlamaServer
    except ModuleNotFoundError as exc:
        missing = getattr(exc, "name", None) or str(exc)
        venv_python = ROOT / ".venv" / "bin" / "python"
        print("--- dependency error ---")
        print(f"Missing module: {missing}")
        print("Use the app virtualenv, not system python3:")
        if venv_python.exists():
            print(f"  {venv_python} scripts/probe_ollama_path.py")
        else:
            print("  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt")
            print("  .venv/bin/python scripts/probe_ollama_path.py")
        raise SystemExit(2) from exc
    return LlamaServer


def main() -> None:
    default_url, default_model = _load_kaggle_defaults()
    base_url = (sys.argv[1] if len(sys.argv) > 1 else default_url).rstrip("/")
    model = sys.argv[2] if len(sys.argv) > 2 else default_model
    print(f"Target: {base_url}")
    print(f"Model:  {model}")
    print("--- DNS / TCP / TLS ---")
    net = _tcp_tls_timing(base_url)
    for key, value in net.items():
        print(f"{key}: {value}")
    if net.get("dns_error"):
        print("--- verdict ---")
        print("DNS failed: this tunnel hostname does not resolve from this host.")
        print("The Kaggle quick tunnel URL has likely changed or the tunnel is down.")
        print("1) On Kaggle, restart cloudflared and copy the new https://….trycloudflare.com URL")
        print("2) Set it in Offline_IPDR_Analysis/config.yaml under kaggle.base_url")
        print("3) Restart ipdr-assistant, then re-run this probe with the new URL:")
        print("   .venv/bin/python scripts/probe_ollama_path.py https://NEW-HOST.trycloudflare.com")
        raise SystemExit(1)

    LlamaServer = _import_llama_server()
    server = LlamaServer(base_url, model, timeout=180, sql_max_tokens=32, explain_max_tokens=32, kind="ollama")
    print("--- GET /v1/models (x3) ---")
    for index in range(3):
        stats = server.probe_latency()
        print(
            f"probe {index + 1}: wall={stats.get('probe_wall_s')} "
            f"ttfb={stats.get('probe_ttfb_s')} status={stats.get('probe_status')} "
            f"error={stats.get('probe_error')}"
        )
        time.sleep(0.2)
    print("--- tiny streamed /api/chat ---")
    started = time.perf_counter()
    try:
        text = server.chat(
            [{"role": "user", "content": 'Reply with JSON only: {"sql":"SELECT 1 AS ok"}'}],
            json_mode=True,
            temperature=0,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"chat failed after {time.perf_counter() - started:.3f}s: {exc}")
        print(json.dumps(server.last_call, indent=2, default=str))
        raise SystemExit(1) from exc
    print(f"reply: {text[:180]!r}")
    print(json.dumps(server.last_call, indent=2, default=str))
    wall = float(server.last_call.get("wall_s") or 0)
    model_s = server.last_call.get("model_s")
    pre = server.last_call.get("pre_model_s")
    print("--- verdict ---")
    if model_s is not None and wall - float(model_s) > 20:
        print(
            f"Path delay dominates: wall {wall:.1f}s vs Ollama {float(model_s):.1f}s "
            f"(pre_model={pre}). Check the Cloudflare quick tunnel / named tunnel stability."
        )
    else:
        print(f"Path looks healthy relative to the model (wall {wall:.1f}s, model {model_s}).")


if __name__ == "__main__":
    main()
