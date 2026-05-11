"""Session hash resolution for MCP tools.

Provides a stable per-session identifier for correlating usage events
across tool calls in the same Claude Code session.
"""

from __future__ import annotations

import hashlib
import logging
import os
import time

log = logging.getLogger(__name__)

# Process-level fallback hash, computed once at import time.
_PROCESS_HASH: str | None = None
_fallback_logged: bool = False

try:
    _pid = os.getpid()
    _start_time = time.time()
    _PROCESS_HASH = hashlib.sha256(
        f"{_pid}:{_start_time}".encode()
    ).hexdigest()[:16]
except Exception:  # noqa: BLE001
    _PROCESS_HASH = None


def session_hash_from_ctx(ctx) -> str | None:  # noqa: ANN001
    """Extract or derive a stable session hash from a FastMCP Context.

    Resolution order:
    1. ctx.request_context.session.id   (real session id when available)
    2. getattr(ctx.request_context, "session_id", None)
    3. Process-level fallback: sha256(pid + start_time)[:16]
    """
    global _fallback_logged

    try:
        # Attempt 1: real session id from transport session
        try:
            session_id = ctx.request_context.session.id
            if session_id:
                return hashlib.sha256(str(session_id).encode()).hexdigest()[:16]
        except AttributeError:
            pass

        # Attempt 2: session_id attribute directly on request_context
        try:
            session_id = getattr(ctx.request_context, "session_id", None)
            if session_id:
                return hashlib.sha256(str(session_id).encode()).hexdigest()[:16]
        except AttributeError:
            pass

        # Attempt 3: process-level fallback
        if _PROCESS_HASH is not None:
            if not _fallback_logged:
                log.info(
                    "session_hash_from_ctx: no session id available, "
                    "using process-level fallback hash"
                )
                _fallback_logged = True
            return _PROCESS_HASH

    except Exception:  # noqa: BLE001
        pass

    return None
