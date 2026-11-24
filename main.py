"""FastAPI application entrypoint."""

from __future__ import annotations

from app.infrastructure.config import get_settings
from app.main import app, health_check, lifespan

__all__ = ["app", "health_check", "lifespan"]

if __name__ == "__main__":
    import uvicorn

    cfg = get_settings()
    uvicorn.run(
        "main:app",
        host=cfg.api_host,
        port=cfg.api_port,
        reload=cfg.app_env.lower() in ("development", "dev", "local"),
    )
