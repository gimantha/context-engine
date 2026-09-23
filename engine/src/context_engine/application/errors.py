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


class UnauthenticatedError(ApplicationError):
    """Indicate that no verifiable credential accompanied the request."""

    def __init__(self, message: str = "Authentication is required") -> None:
        super().__init__("unauthenticated", message)


class AccessDeniedError(ApplicationError):
    """Indicate a denied action on a resource the caller may know exists.

    The message is deliberately generic so a denial reveals nothing about the resource.
    """

    def __init__(self, message: str = "The requested context is not available.") -> None:
        super().__init__("access_denied", message)


class PayloadTooLargeError(ApplicationError):
    """Indicate that an upload exceeded the configured size limit."""

    def __init__(self, message: str = "Upload exceeds the size limit") -> None:
        super().__init__("payload_too_large", message)


class UnsupportedContentTypeError(ApplicationError):
    """Indicate that an upload's content type is not accepted."""

    def __init__(self, message: str = "Content type is not accepted") -> None:
        super().__init__("unsupported_content_type", message)
