"""Streaming reader for one-call ingestion: an event part, then an optional content part.

The body is parsed as it arrives, so size limits hold while streaming and a malformed event
is rejected before its content is read. Nothing is spooled to disk.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass

from python_multipart.exceptions import MultipartParseError
from python_multipart.multipart import MultipartParser, parse_options_header

from context_engine.application import (
    PayloadTooLargeError,
    UnsupportedContentTypeError,
    ValidationError,
)

EVENT_PART = "event"
CONTENT_PART = "content"
# An ingestion envelope is a few hundred bytes; this leaves room for long ids and audiences.
MAX_EVENT_BYTES = 64 * 1024


@dataclass(frozen=True, slots=True)
class IngestionParts[EventT]:
    """The parsed event and, for an upsert, the raw content and its declared type."""

    event: EventT
    content: bytes | None
    content_type: str | None


class _Part:
    def __init__(self) -> None:
        self.headers: dict[str, str] = {}
        self.name = ""
        self.size = 0
        self.chunks: list[bytes] = []


async def read_ingestion_parts[EventT](
    content_type_header: str,
    stream: AsyncIterator[bytes],
    parse_event: Callable[[bytes], EventT],
    max_content_bytes: int,
    max_event_bytes: int = MAX_EVENT_BYTES,
) -> IngestionParts[EventT]:
    """Read a multipart body with an `event` part first and at most one `content` part.

    `parse_event` validates the event as soon as its part ends; its errors propagate before
    any content is read.
    """

    media_type, options = parse_options_header(content_type_header)
    boundary = options.get(b"boundary")
    if media_type != b"multipart/form-data" or not boundary:
        raise UnsupportedContentTypeError("Send the event and its content as multipart/form-data")

    parts: list[_Part] = []
    header_field = bytearray()
    header_value = bytearray()
    parsed: list[EventT] = []
    finished = False

    def on_part_begin() -> None:
        if len(parts) == 2:
            raise ValidationError("Send one event part and at most one content part")
        parts.append(_Part())

    def on_header_field(data: bytes, start: int, end: int) -> None:
        header_field.extend(data[start:end])

    def on_header_value(data: bytes, start: int, end: int) -> None:
        header_value.extend(data[start:end])

    def on_header_end() -> None:
        name = header_field.decode("latin-1").strip().lower()
        parts[-1].headers[name] = header_value.decode("latin-1").strip()
        header_field.clear()
        header_value.clear()

    def on_headers_finished() -> None:
        part = parts[-1]
        _, disposition = parse_options_header(part.headers.get("content-disposition", ""))
        part.name = disposition.get(b"name", b"").decode("utf-8", "replace")
        expected = EVENT_PART if len(parts) == 1 else CONTENT_PART
        if part.name != expected:
            raise ValidationError("Send the event part first, then the content part")

    def on_part_data(data: bytes, start: int, end: int) -> None:
        part = parts[-1]
        part.size += end - start
        limit = max_event_bytes if part.name == EVENT_PART else max_content_bytes
        if part.size > limit:
            raise PayloadTooLargeError(
                "Event exceeds the size limit"
                if part.name == EVENT_PART
                else "Upload exceeds the size limit"
            )
        part.chunks.append(bytes(data[start:end]))

    def on_part_end() -> None:
        part = parts[-1]
        if part.name == EVENT_PART:
            parsed.append(parse_event(b"".join(part.chunks)))

    def on_end() -> None:
        nonlocal finished
        finished = True

    parser = MultipartParser(
        boundary,
        {
            "on_part_begin": on_part_begin,
            "on_header_field": on_header_field,
            "on_header_value": on_header_value,
            "on_header_end": on_header_end,
            "on_headers_finished": on_headers_finished,
            "on_part_data": on_part_data,
            "on_part_end": on_part_end,
            "on_end": on_end,
        },
    )
    try:
        async for chunk in stream:
            parser.write(chunk)
        parser.finalize()
    except MultipartParseError as exc:
        raise ValidationError("Request body is not valid multipart content") from exc
    if not finished or not parsed:
        raise ValidationError("Request must contain a complete event part")
    content_part = parts[1] if len(parts) == 2 else None
    return IngestionParts(
        event=parsed[0],
        content=b"".join(content_part.chunks) if content_part else None,
        content_type=content_part.headers.get("content-type") if content_part else None,
    )
