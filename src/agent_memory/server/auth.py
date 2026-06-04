"""Bearer-token auth.

The expected token is fixed on the app at startup (`app.state.token`, sourced
from `AGENT_MEMORY_API_TOKEN` by `__main__`). Every protected route depends on
`require_token`; `/health` does not. Comparison is constant-time.
"""

import hmac
from typing import Optional

from fastapi import Header, HTTPException, Request


def require_token(request: Request, authorization: Optional[str] = Header(default=None)):
    expected = request.app.state.token
    if not expected:
        # Fail closed: a server with no token configured must not serve data.
        raise HTTPException(status_code=503, detail="Server has no API token configured")
    prefix = "Bearer "
    if not authorization or not authorization.startswith(prefix):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    provided = authorization[len(prefix):].strip()
    if not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="Invalid token")
