"""What this service says about itself while it is running.

Deliberately not an access log. Uvicorn writes one, the platform team's
gateway writes one, and a third would be noise. What neither of them can
write is the thing that actually matters at three in the morning on a
floor: *which rig, which shift, and what did the service decide*.

So this is domain logging. A batch refused, a checksum that did not
match, a token that did not belong to the rig it spoke for - those are
the lines somebody needs, and until now none of them existed. An
unhandled exception did not exist either: it became a 500 with an empty
body and nothing anywhere said what it was.

Three rules, because logs on a floor outlive the person who wrote them:

**A request id, taken not invented.** If something upstream already set
one, it is used, so a line here joins up with a line in their gateway. A
new one is minted only when there is nothing to join to.

**Nothing secret, ever.** No token, no password, no connection string.
`Settings.safe_url` exists for exactly this and the rule is that a log
line is world-readable, because eventually it is.

**Say what was decided, not that something happened.** "refused: 3
events, rig RIG-03, unknown event 'episode_finished'" is a line somebody
can act on. "ingest error" is not.
"""

from __future__ import annotations

import logging
import uuid
from contextvars import ContextVar

from fastapi import FastAPI, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

log = logging.getLogger("rigs.service")

HEADER = "X-Request-ID"

# Also carried on the ASGI scope, not only in the ContextVar. An
# unhandled exception is caught by ServerErrorMiddleware, which sits
# OUTSIDE the middleware below - so by the time that handler runs, the
# `finally` here has already reset the ContextVar and the id is gone
# exactly when it is most wanted. The scope is one dict for the whole
# request and survives the unwinding.
SCOPE_KEY = "rigs.request_id"

# Set per request, read by the filter below, so a domain module deep in
# core/ can log without being handed a request it has no business seeing.
_request_id: ContextVar[str] = ContextVar("request_id", default="-")


class RequestIdFilter(logging.Filter):
    """Puts the request id on every record, so a format string can use it.

    A filter rather than an adapter: it applies to records from anywhere,
    including `core/`, which knows nothing about HTTP and should not.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = _request_id.get()
        return True


def install(app: FastAPI) -> None:
    """Attach the request id and the last-resort handler."""

    @app.middleware("http")
    async def request_id(request: Request, call_next):
        incoming = request.headers.get(HEADER)
        rid = incoming or uuid.uuid4().hex[:12]
        request.scope[SCOPE_KEY] = rid
        token = _request_id.set(rid)
        try:
            response = await call_next(request)
        finally:
            _request_id.reset(token)
        response.headers[HEADER] = rid
        return response

    @app.exception_handler(RequestValidationError)
    async def refused(request: Request, exc: RequestValidationError) -> JSONResponse:
        """A body this service will not accept.

        Worth a line rather than a silent 422, because of what the rig
        does with one: it sets the batch aside permanently instead of
        retrying, on the grounds that a rig filing events its own schema
        rejects is a bug in the rig. That is the right behaviour and it
        means the events are never coming back - so this is the only
        place the reason is ever recorded.
        """
        where = [
            "%s: %s" % (".".join(str(p) for p in e.get("loc", ())), e.get("msg", ""))
            for e in exc.errors()[:3]
        ]
        log.warning(
            "refused %s %s [request %s]: %s",
            request.method, request.url.path,
            request.scope.get(SCOPE_KEY) or _request_id.get(),
            "; ".join(where) or "unreadable body",
        )
        # FastAPI's own handler, not a reimplementation. This one exists to
        # add a line, and a handler that also rewrites the body is a
        # handler that changed the API by accident - which is what the
        # first version of it did: `exc.errors()` carries a live
        # ValueError in `ctx` for a custom validator, so serialising it
        # turned three well-formed 422s into 500s.
        return await request_validation_exception_handler(request, exc)

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:
        """The line that did not exist.

        An unhandled exception used to become a 500 with an empty body and
        no record of what it was. It still becomes a 500 with a body that
        says nothing useful to a caller - that part is correct, because a
        stack trace is not something to hand out - but it is written down
        here first, with the request id, so the two can be joined.
        """
        rid = request.scope.get(SCOPE_KEY) or _request_id.get()
        log.exception(
            "unhandled %s on %s %s [request %s]",
            type(exc).__name__, request.method, request.url.path, rid,
        )
        return JSONResponse(
            status_code=500,
            content={"detail": "internal error", "requestId": rid},
            headers={HEADER: rid},
        )


def configure(level: str = "INFO") -> None:
    """A sensible default when this runs on its own.

    Does nothing if handlers are already configured, because in their tree
    the gateway owns logging and a service that reconfigures it underneath
    them is a service that loses their formatting.
    """
    root = logging.getLogger()
    if root.handlers:
        for h in root.handlers:
            h.addFilter(RequestIdFilter())
        return

    handler = logging.StreamHandler()
    handler.addFilter(RequestIdFilter())
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s [%(request_id)s] %(message)s"
    ))
    root.addHandler(handler)
    root.setLevel(level)
