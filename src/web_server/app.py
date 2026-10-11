"""HTTP application composition root."""

from fastapi import FastAPI


def create_app() -> FastAPI:
    """Create an isolated Web API application."""
    app = FastAPI(title="Wind Hub Web API", version="1.0.0")

    @app.get("/health/live", tags=["health"])
    async def liveness() -> dict[str, str]:
        """Report only Web Server process availability."""
        return {"status": "ok"}

    return app
