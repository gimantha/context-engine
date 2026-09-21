"""Stable application errors translated by external interfaces."""

from __future__ import annotations


class ApplicationError(Exception):
    """Base error carrying a stable code and caller-safe message."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class NotFoundError(ApplicationError):
    """Indicate that the requested visible resource does not exist."""

    def __init__(self, message: str = "Resource not found") -> None:
        super().__init__("not_found", message)


class ConflictError(ApplicationError):
    """Indicate that a request conflicts with durable state."""

    def __init__(self, message: str = "Request conflicts with existing state") -> None:
        super().__init__("conflict", message)


class ValidationError(ApplicationError):
    """Indicate that an application command is invalid."""

    def __init__(self, message: str = "Request is invalid") -> None:
        super().__init__("invalid_request", message)
