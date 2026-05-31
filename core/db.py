"""Async database engine and session factory.

Usage:
    from core.db import get_session

    async with get_session() as session:
        user = await session.get(User, telegram_id)

The async session is configured with `expire_on_commit=False` so attribute
access after `commit()` doesn't trigger surprise reloads inside handlers.
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import AsyncIterator
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


_DB_URL = os.environ.get("DATABASE_URL")
if not _DB_URL:
    raise RuntimeError("DATABASE_URL not set \u2014 see .env.example")


def _normalise_for_asyncpg(url: str) -> str:
    """Make any Postgres connection string usable with asyncpg.

    Handles three foot-guns specific to managed Postgres providers:
      1. Heroku/Railway scheme `postgres://` -> `postgresql+asyncpg://`
      2. Missing driver in `postgresql://` -> add `+asyncpg`
      3. Query params asyncpg rejects:
           - `sslmode=...`        (psycopg2 syntax; asyncpg uses `ssl=...`)
           - `channel_binding=...` (Neon-added; asyncpg has no equivalent;
                                    SCRAM-SHA-256 channel binding is
                                    negotiated automatically when supported)
    """
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+asyncpg://", 1)
    elif url.startswith("postgresql://") and "+asyncpg" not in url:
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)

    parts = urlsplit(url)
    pairs = parse_qsl(parts.query, keep_blank_values=True)

    # Translate sslmode -> ssl (asyncpg accepts `ssl=require` / `ssl=disable`)
    translated: list[tuple[str, str]] = []
    _DROP = {"channel_binding"}
    for key, value in pairs:
        if key in _DROP:
            continue
        if key == "sslmode":
            translated.append(("ssl", value))
        else:
            translated.append((key, value))

    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(translated), parts.fragment)
    )


_DB_URL = _normalise_for_asyncpg(_DB_URL)

engine = create_async_engine(
    _DB_URL,
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,        # survives Postgres restarts on Railway
    pool_recycle=1800,         # 30 min \u2014 below Railway's idle disconnect
    echo=False,
)

async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


@asynccontextmanager
async def get_session() -> AsyncIterator[AsyncSession]:
    """Open a session, commit on success, rollback on exception."""
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
