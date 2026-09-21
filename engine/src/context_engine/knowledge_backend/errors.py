"""Stable errors returned by knowledge backend implementations."""

from __future__ import annotations

from enum import StrEnum


class BackendErrorCode(StrEnum):
    """Stable provider-neutral categories for backend failures."""

    INVALID_INPUT = "invalid_input"
    ACCESS_DENIED = "access_denied"
    CONFLICT = "conflict"
    NOT_FOUND = "not_found"
    TIMEOUT = "timeout"
    UNAVAILABLE = "unavailable"
    PARTIAL_WRITE = "partial_write"
    UNSUPPORTED = "unsupported"


class BackendError(RuntimeError):
    """Provider-neutral failure safe to handle outside a provider module."""

    def __init__(self, code: BackendErrorCode, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable
