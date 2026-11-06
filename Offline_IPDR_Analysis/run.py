import sys
import traceback

import uvicorn

from app.config import load_config


def main() -> None:
    try:
        config = load_config()
    except Exception as exc:
        print(f"IPDR config error: {exc}", file=sys.stderr)
        traceback.print_exc()
        raise SystemExit(1) from exc
    try:
        # Import after config so auth/ClickHouse setup failures print a clear traceback.
        import app.main  # noqa: F401
    except Exception as exc:
        print(f"IPDR startup error: {exc}", file=sys.stderr)
        traceback.print_exc()
        raise SystemExit(1) from exc
    uvicorn.run("app.main:app", host=config.server_host, port=config.server_port)


if __name__ == "__main__":
    main()
