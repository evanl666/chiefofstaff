"""How each owner input came in (its ingress): `page` (the page, under its private launch link),
`away` (an answer read from an away notification), `send` (Send to Chief), `slack` (a delegate
reaction). Recorded on every owner message (`meta.ingress`) and on each decision card answered
(kv `ingress:<card>`), whether or not Autopilot is used; never drawn, never in a prompt. Only
`page` is the owner's authenticated word (Autopilot's basis, autopilot/basis.py); input with no
ingress (from before it was recorded) is data.

The channel is set where the input enters (the web app's guard for each signed-in request, the
messenger, the socket) and read where it is stored, for the request's whole task."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

PAGE, AWAY, SEND, SLACK = "page", "away", "send", "slack"
CHANNEL: ContextVar[str] = ContextVar("ingress", default="")


def current() -> str:
    return CHANNEL.get()


@contextmanager
def through(channel: str) -> Iterator[None]:
    """Input handled inside this block came in through `channel`."""
    token = CHANNEL.set(channel)
    try:
        yield
    finally:
        CHANNEL.reset(token)
