"""Vercel entrypoint shim.

Vercel's Python build expects a top-level entrypoint file. This module
re-exports the webhook app callable from the API implementation so the
deployment can discover a valid Python entrypoint while preserving the
existing webhook handler logic.
"""

from api.index import app

