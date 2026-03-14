"""
RepoGuardian FastAPI application.

Startup:
  - Initialises database tables
  - Connects to Redis
  - Mounts all routers

The background worker (tasks/worker.py) runs as a separate process.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import structlog
import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from backend.config import get_settings
from backend.models.database import init_db
from backend.routers import (
    findings_router,
    health_router,
    hitl_router,
    repositories_router,
    webhooks_router,
)
from backend.services.redis_service import close_redis, get_redis

settings = get_settings()

# ── Structured logging ─────────────────────────────────────────────────────────

structlog.configure(
    processors=[
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.dev.ConsoleRenderer() if settings.debug else structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.stdlib.BoundLogger,
    logger_factory=structlog.stdlib.LoggerFactory(),
)

logging.basicConfig(
    level=getattr(logging, settings.log_level),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

logger = logging.getLogger(__name__)


# ── Lifespan ───────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown tasks."""
    logger.info("Starting %s v%s", settings.app_name, settings.app_version)

    # Initialise database
    await init_db()
    logger.info("Database initialised")

    # Connect to Redis
    try:
        redis = await get_redis()
        await redis.ping()
        logger.info("Redis connected at %s", settings.redis_url)
    except Exception as e:
        logger.warning("Redis connection failed: %s (events won't be queued)", e)

    yield

    # Cleanup
    from backend.services.redis_service import close_redis
    await close_redis()
    logger.info("Shutdown complete")


# ── Application ────────────────────────────────────────────────────────────────

app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description=(
        "Autonomous AI Agent System for Code Repository Management. "
        "Automatically reviews pull requests, detects vulnerabilities, "
        "monitors repository health, and provides actionable developer feedback."
    ),
    lifespan=lifespan,
    docs_url="/docs" if settings.debug else None,
    redoc_url="/redoc",
)

# ── CORS ───────────────────────────────────────────────────────────────────────

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Routers ────────────────────────────────────────────────────────────────────

app.include_router(webhooks_router)
app.include_router(health_router)
app.include_router(repositories_router)
app.include_router(findings_router)
app.include_router(hitl_router)


# ── Root & health-check endpoints ─────────────────────────────────────────────

@app.get("/", include_in_schema=False)
async def root():
    return {
        "service": settings.app_name,
        "version": settings.app_version,
        "status": "running",
        "docs": "/redoc",
    }


@app.get("/ping", include_in_schema=False)
async def ping():
    """Simple liveness probe."""
    return {"status": "ok"}


@app.get("/ready", include_in_schema=False)
async def ready():
    """Readiness probe — checks DB and Redis."""
    checks = {}

    # DB check
    try:
        from backend.models.database import AsyncSessionLocal
        async with AsyncSessionLocal() as db:
            await db.execute(__import__("sqlalchemy").text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as e:
        checks["database"] = f"error: {e}"

    # Redis check
    try:
        redis = await get_redis()
        await redis.ping()
        checks["redis"] = "ok"
    except Exception as e:
        checks["redis"] = f"error: {e}"

    all_ok = all(v == "ok" for v in checks.values())
    return JSONResponse(
        status_code=200 if all_ok else 503,
        content={"status": "ready" if all_ok else "degraded", "checks": checks},
    )


# ── Entrypoint ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run(
        "backend.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
        log_level=settings.log_level.lower(),
    )
