"""Turn staged bytes into normalized text, recording which extractor produced it."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from html.parser import HTMLParser


class ExtractionError(Exception):
    """Content cannot be turned into text; the code is caller-safe."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class ExtractedText:
    """Normalized text and the extractor version that produced it."""

    text: str
    parser_version: str


def _decode(data: bytes) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ExtractionError("content_not_utf8", "Content is not valid UTF-8 text") from exc


def _normalize(text: str) -> str:
    lines = (re.sub(r"[ \t\f\v]+", " ", line).strip() for line in text.splitlines())
    return "\n".join(line for line in lines if line)


def _plain(data: bytes) -> str:
    return _normalize(_decode(data))


class _TextCollector(HTMLParser):
    """Collect visible text, skipping script, style, and template content."""

    _SKIPPED = frozenset({"script", "style", "noscript", "template"})
    _BLOCKS = frozenset(
        {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article"}
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skipping = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIPPED:
            self._skipping += 1
        elif tag in self._BLOCKS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIPPED and self._skipping:
            self._skipping -= 1
        elif tag in self._BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skipping:
            self.parts.append(data)


def _html(data: bytes) -> str:
    collector = _TextCollector()
    collector.feed(_decode(data))
    collector.close()
    return _normalize("".join(collector.parts))


def _json(data: bytes) -> str:
    try:
        value = json.loads(_decode(data))
    except json.JSONDecodeError as exc:
        raise ExtractionError("content_invalid_json", "Content is not valid JSON") from exc
    # Pretty JSON keeps keys next to values so both stay searchable.
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


_EXTRACTORS = {
    "text/plain": ("plain@1", _plain),
    "text/markdown": ("markdown@1", _plain),
    "text/html": ("html@1", _html),
    "application/json": ("json@1", _json),
}


def extract(content_type: str, data: bytes) -> ExtractedText:
    """Return normalized text for a supported type, or raise with a stable code.

    PDF and other binary formats are accepted for staging but not yet extracted.
    """

    entry = _EXTRACTORS.get(content_type.split(";", 1)[0].strip().lower())
    if entry is None:
        raise ExtractionError("extraction_unsupported", "This content type cannot be indexed yet")
    parser_version, function = entry
    text = function(data)
    if not text.strip():
        raise ExtractionError("content_empty", "Content has no text to index")
    return ExtractedText(text, parser_version)
