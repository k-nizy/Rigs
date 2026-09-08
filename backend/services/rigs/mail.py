"""Sending one email, which is the only kind this service sends.

Stdlib `smtplib`, not a client library. Same argument as
`core/domains/accounts/passwords.py` makes for scrypt: this service has
nine runtime dependencies and has been careful about them, and one
message to one relay is squarely inside what the standard library does
well. The seam is `send()` - if a deployment ever needs an API-based
provider, that is the one function to reimplement.

**It runs on a worker thread.** `smtplib` is blocking, and a relay that
has stopped answering blocks for as long as the socket timeout. On the
event loop that is not a slow reply, it is the whole service stopping -
every rig filing events, every screen polling - because one mail server
is unwell. `asyncio.to_thread` keeps it to the one request.

**A failure to send is not a failure of the request.** The caller must
answer the same way whether an address exists or not, so it cannot make
the reply depend on whether mail went out. What it can do is say so in
the log, loudly, which is why `send()` returns a bool rather than
raising: undelivered reset mail is invisible from the floor, and the log
line is the only place anybody will ever see it.
"""

from __future__ import annotations

import asyncio
import logging
import smtplib
from email.message import EmailMessage

from core.infrastructure.config import Settings

log = logging.getLogger("rigs.mail")


def is_configured(settings: Settings) -> bool:
    """Whether this deployment can deliver anything at all.

    The host alone. User and password are empty on a relay that accepts
    from inside the network, which is the ordinary arrangement on a
    factory network, so demanding them would refuse a working setup.
    """
    return bool(settings.smtp_host)


def _build(to: str, subject: str, body: str, settings: Settings) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = settings.smtp_from or settings.smtp_user or "rigs@localhost"
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    return msg


def _send_blocking(msg: EmailMessage, settings: Settings) -> None:
    """The blocking half, run on a thread by `send` below.

    A timeout, because the default is no timeout at all: a relay that
    accepts the connection and then says nothing would hold the thread
    for as long as the process lives.
    """
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as s:
        if settings.smtp_starttls:
            s.starttls()
        if settings.smtp_user:
            s.login(settings.smtp_user, settings.smtp_password)
        s.send_message(msg)


async def send(to: str, subject: str, body: str, settings: Settings) -> bool:
    """True if the relay took it. Never raises.

    The caller is answering a request that must look identical whether
    the address exists or not, so an exception here would be a signal it
    cannot afford to leak - a 500 on a real address and a 200 on an
    invented one says which addresses are real.
    """
    if not is_configured(settings):
        log.warning("asked to send mail with no SMTP_HOST configured")
        return False

    msg = _build(to, subject, body, settings)
    try:
        await asyncio.to_thread(_send_blocking, msg, settings)
    except Exception as e:                       # noqa: BLE001 - see docstring
        # The address is not logged. A failure line naming who was mailed
        # would put a list of real addresses in the log of a service
        # whose whole reply is built to not say which addresses are real.
        log.error("could not send mail via %s:%s - %s: %s",
                  settings.smtp_host, settings.smtp_port, type(e).__name__, e)
        return False

    log.info("sent %r via %s", subject, settings.smtp_host)
    return True
