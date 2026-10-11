"""Web API process entry point."""

import uvicorn

from web_server.app import create_app


def main() -> None:
    """Start the Web API process."""
    uvicorn.run(create_app(), host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
