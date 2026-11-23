"""FastAPI application entrypoint."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.adapters.inbound.http_api import router as api_router
from app.infrastructure.config import get_settings
from app.infrastructure.container import ApplicationContainer

settings = get_settings()
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    container = ApplicationContainer(settings)
    await container.startup()
    app.state.container = container
    try:
        yield
    finally:
        await container.shutdown()


app = FastAPI(
    title=settings.app_name,
    version="1.0.0",
    description=(
        "Production-ready Ports & Adapters (Hexagonal) Retail Data Analysis Chat Assistant "
        "built with FastAPI, asyncio, Pydantic v2, LangChain, BigQuery, PostgreSQL, and Redis."
    ),
    lifespan=lifespan,
)

app.include_router(api_router)


@app.get("/health", tags=["System"])
async def health_check() -> dict[str, str]:
    return {
        "status": "healthy",
        "service": settings.app_name,
        "environment": settings.app_env,
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=settings.app_env.lower() in ("development", "dev", "local"),
    )
