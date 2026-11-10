"""Exceptions raised by the Grohm client."""

from __future__ import annotations


class GrohmError(Exception):
    """Base error for everything the client raises."""


class GrohmConnectionError(GrohmError):
    """The Grohm cloud could not be reached or sent an invalid response."""


class GrohmAuthenticationError(GrohmError):
    """The credentials were rejected, or the session could not be renewed."""


class GrohmCommandError(GrohmError):
    """The Grohm cloud refused a command."""
