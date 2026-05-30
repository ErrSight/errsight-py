"""FastAPI alias for :mod:`errsight.starlette`.

FastAPI is built on Starlette, so the Starlette ASGI middleware works
unchanged. This module re-exports it under the name that FastAPI users
will search for::

    from fastapi import FastAPI
    from errsight.fastapi import ErrsightMiddleware
    import errsight

    errsight.init(api_key=...)
    app = FastAPI()
    app.add_middleware(ErrsightMiddleware)
"""
from __future__ import annotations

from errsight.starlette import ErrsightMiddleware

__all__ = ["ErrsightMiddleware"]
