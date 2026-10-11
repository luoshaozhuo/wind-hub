"""Web API process entry point."""

import os

import uvicorn

from web_server.app import create_app


def main() -> None:
    """Start the Web API process."""
    uvicorn.run(create_app(write_token=os.environ.get('WIND_HUB_WEB_WRITE_TOKEN')), host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
