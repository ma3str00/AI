from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Config:
    mysql_host: str
    mysql_port: int
    mysql_user: str
    mysql_password: str
    mysql_database: str
    llama_base_url: str
    llama_model: str
    llama_timeout: float
    llama_sql_max_tokens: int
    llama_explain_max_tokens: int
    example_count: int
    server_host: str
    server_port: int
    stored_as: str
    display_offset: str
    max_rows: int
    max_execution_ms: int
    mapping_csv: Path
    examples_jsonl: Path

    @property
    def session_time_zone(self) -> str:
        # utc_epoch: the integer is a real Unix instant, so the session must be GMT+5
        # for UNIX_TIMESTAMP('wall clock') to mean GMT+5.
        # local_epoch: the integer was built from a GMT+5 wall clock. Keep the session
        # at UTC so the same SQL literal still matches the stored integer.
        if self.stored_as == "local_epoch":
            return "+00:00"
        return self.display_offset

    @property
    def time_note(self) -> str:
        if self.stored_as == "local_epoch":
            return "Stored integers are GMT+5 wall-clock values encoded as unix numbers. Display is GMT+5."
        return "Stored integers are Unix epoch instants. Display and question times are GMT+5."


def load_config() -> Config:
    path = Path(os.environ.get("IPDR_CONFIG", ROOT / "config.yaml"))
    if not path.exists():
        raise FileNotFoundError(
            f"Missing {path}. Copy config.example.yaml to config.yaml and set the MySQL host."
        )
    with path.open(encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}

    def resolve(value: str) -> Path:
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = (path.parent / candidate).resolve()
        return candidate

    mysql = raw.get("mysql") or {}
    llama = raw.get("llama") or {}
    server = raw.get("server") or {}
    timing = raw.get("time") or {}
    query = raw.get("query") or {}
    paths = raw.get("paths") or {}
    stored_as = timing.get("stored_as", "utc_epoch")
    if stored_as not in {"utc_epoch", "local_epoch"}:
        raise ValueError("time.stored_as must be utc_epoch or local_epoch")
    return Config(
        mysql_host=str(mysql.get("host", "127.0.0.1")),
        mysql_port=int(mysql.get("port", 3306)),
        mysql_user=str(mysql.get("user", "ipdr_ro")),
        mysql_password=str(mysql.get("password", "")),
        mysql_database=str(mysql.get("database", "ipdr")),
        llama_base_url=str(llama.get("base_url", "http://127.0.0.1:11434")),
        llama_model=str(llama.get("model", "qwen2.5-coder-3b-instruct-q4_k_m.gguf")),
        llama_timeout=float(llama.get("timeout_seconds", 300)),
        llama_sql_max_tokens=int(llama.get("sql_max_tokens", 512)),
        llama_explain_max_tokens=int(llama.get("explain_max_tokens", 400)),
        example_count=int(llama.get("example_count", 4)),
        server_host=str(server.get("host", "0.0.0.0")),
        server_port=int(server.get("port", 8090)),
        stored_as=stored_as,
        display_offset=str(timing.get("display_offset", "+05:00")),
        max_rows=int(query.get("max_rows", 200)),
        max_execution_ms=int(query.get("max_execution_ms", 45000)),
        mapping_csv=resolve(str(paths.get("mapping_csv", "data/app_category_to_app_label_mapping.csv"))),
        examples_jsonl=resolve(str(paths.get("examples_jsonl", "data/training_examples.jsonl"))),
    )
