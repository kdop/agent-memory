"""FastAPI service — the one process that touches the database.

Async routes (``app.py``) delegate to plain repository functions (``repository.py``)
over a SQLAlchemy 2.0 async session; models live in ``models.py``. Bearer-token auth
guards every route except ``/health``. Ships in the ``[server]`` extra.
"""

from .app import create_app

__all__ = ["create_app"]
