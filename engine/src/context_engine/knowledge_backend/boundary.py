"""Serialization guard for values allowed to cross public boundaries."""

from __future__ import annotations

from dataclasses import fields, is_dataclass
from enum import Enum
from typing import Any

from .types import AccessPartitionRef, BackendReference


class PrivateValueError(TypeError):
    """Indicate that a private engine value reached public serialization."""


_PRIVATE_TYPES = (AccessPartitionRef, BackendReference)


def to_public_value(value: Any) -> Any:
    """Convert engine values to primitives and reject private backend values."""

    # Check private wrappers before generic dataclass traversal can reveal their inner strings.
    if isinstance(value, _PRIVATE_TYPES):
        raise PrivateValueError(f"{type(value).__name__} cannot cross a public boundary")
    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: to_public_value(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): to_public_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [to_public_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"Unsupported public value: {type(value).__name__}")
