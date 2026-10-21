import uvicorn

from app.config import load_config


def main() -> None:
    config = load_config()
    uvicorn.run("app.main:app", host=config.server_host, port=config.server_port)


if __name__ == "__main__":
    main()
