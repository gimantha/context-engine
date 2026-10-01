"""Turn staged bytes into normalized text, recording which extractor produced it.

Besides the text, extraction records the structure a citation needs: which source line each
extracted line came from, where headings start, and which span of the text each JSON value
occupies. The text itself is exactly what earlier releases produced for the same parser
version, because the provider's chunks are slices of it and passages are located in it by
exact match (M5 slice 2, ADR 0008 revision).
"""

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
class Heading:
    """A heading in the extracted text: where its line starts, its level, and its title."""

    offset: int
    level: int
    title: str


@dataclass(frozen=True, slots=True)
class JsonSpan:
    """The span of the extracted text one JSON value occupies, with its path.

    A member's span starts at its key, so a passage holding `"name": "x"` resolves to the
    member and not to its parent object.
    """

    start: int
    end: int
    path: str


@dataclass(frozen=True, slots=True)
class ExtractedText:
    """Normalized text, the extractor version that produced it, and its citation structure.

    `lines` holds, for each line of `text`, the line of the source file it came from, counted
    the way editors count (`\\n`, `\\r\\n`, and `\\r` end a line); it is empty when lines of
    the text do not correspond to lines of the source. `prose` is False for text that is not
    made of sentences, such as re-serialized JSON.
    """

    text: str
    parser_version: str
    lines: tuple[int, ...] = ()
    headings: tuple[Heading, ...] = ()
    json_spans: tuple[JsonSpan, ...] = ()
    prose: bool = True


_SPACES = re.compile(r"[ \t\f\v]+")


def _decode(data: bytes) -> str:
    """Decode UTF-8, dropping a byte-order mark, or refuse with a caller-safe code.

    Only UTF-8 is accepted so the same bytes always give the same text, which offsets into the
    text depend on.
    """

    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ExtractionError("content_not_utf8", "Content is not valid UTF-8 text") from exc


def _clean(line: str) -> str:
    """Collapse runs of spaces and trim, exactly as earlier releases normalized each line."""

    return _SPACES.sub(" ", line).strip()


def _source_lines(text: str) -> list[tuple[str, int]]:
    """Split text the way normalization does, pairing each piece with its editor line number.

    Normalization splits with `str.splitlines`, which also breaks on form feeds and Unicode
    separators that editors show inside a line, so the editor line advances only on `\\n`,
    `\\r\\n`, and `\\r`.
    """

    pieces: list[tuple[str, int]] = []
    number = 1
    for body, full in zip(text.splitlines(), text.splitlines(keepends=True), strict=True):
        pieces.append((body, number))
        if full.endswith(("\n", "\r")):
            number += 1
    return pieces


def _normalize(pieces: list[tuple[str, int]]) -> tuple[str, tuple[int, ...], dict[int, int]]:
    """Collapse spaces, drop blank lines, and record where each kept piece landed.

    Returns the text, the source line of each kept line, and a map from piece index to the
    offset where that piece's line starts in the text.
    """

    kept: list[str] = []
    numbers: list[int] = []
    offsets: dict[int, int] = {}
    position = 0
    for index, (body, number) in enumerate(pieces):
        cleaned = _clean(body)
        if not cleaned:
            continue
        if kept:
            position += 1
        offsets[index] = position
        kept.append(cleaned)
        numbers.append(number)
        position += len(cleaned)
    return "\n".join(kept), tuple(numbers), offsets


def _plain(data: bytes) -> ExtractedText:
    """Extract plain text, keeping which source line each extracted line came from.

    Normalization drops blank lines, so the line map is what lets a citation name the line
    a reader sees in the original file.
    """

    text, lines, _ = _normalize(_source_lines(_decode(data)))
    return ExtractedText(text, "plain@1", lines)


_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_ATX = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?))?(?:[ \t]+#+)?[ \t]*$")
_SETEXT = re.compile(r"^ {0,3}(=+|-+)[ \t]*$")
_NOT_PARAGRAPH = re.compile(r"^(?: {4}|\t| {0,3}(?:[-*+][ \t]|\d{1,9}[.)][ \t]|>|#))")


def _markdown_headings(pieces: list[tuple[str, int]]) -> list[tuple[int, int, str]]:
    """Find ATX and setext headings outside fenced code, as (piece index, level, title).

    A setext underline counts only under a plain paragraph line; under a list item, quote, or
    indented code it is a thematic break or content, as CommonMark reads it.
    """

    found: list[tuple[int, int, str]] = []
    fence: str | None = None
    for index, (body, _) in enumerate(pieces):
        opening = _FENCE.match(body)
        if fence is not None:
            if opening and opening.group(1)[0] == fence[0] and len(opening.group(1)) >= len(fence):
                fence = None
            continue
        if opening:
            fence = opening.group(1)
            continue
        atx = _ATX.match(body)
        if atx:
            title = _clean(atx.group(2) or "")
            if title:
                found.append((index, len(atx.group(1)), title))
            continue
        underline = _SETEXT.match(body)
        if underline and index:
            previous = pieces[index - 1][0]
            title = _clean(previous)
            already = bool(found) and found[-1][0] == index - 1
            if title and not already and not _NOT_PARAGRAPH.match(previous):
                found.append((index - 1, 1 if underline.group(1)[0] == "=" else 2, title))
    return found


def _markdown(data: bytes) -> ExtractedText:
    """Extract Markdown as plain text, with its line map and its headings.

    The text is the plain-text normalization, unchanged from earlier releases; headings are
    found on the source lines, where code fences and underlines can still be told apart.
    """

    pieces = _source_lines(_decode(data))
    text, lines, offsets = _normalize(pieces)
    headings = tuple(
        Heading(offsets[index], level, title)
        for index, level, title in _markdown_headings(pieces)
        if index in offsets
    )
    return ExtractedText(text, "markdown@1", lines, headings)


class _TextCollector(HTMLParser):
    """Collect visible text, skipping script, style, and template content, and headings."""

    _SKIPPED = frozenset({"script", "style", "noscript", "template"})
    _BLOCKS = frozenset(
        {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article"}
    )
    _HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}

    def __init__(self) -> None:
        """Start with no text, no headings, and nothing skipped."""

        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.headings: list[tuple[int, str]] = []
        self._skipping = 0
        self._heading: tuple[int, list[str]] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Enter skipped content, or break the line for a block and start a heading."""

        if tag in self._SKIPPED:
            self._skipping += 1
        elif tag in self._BLOCKS:
            self.parts.append("\n")
            if tag in self._HEADINGS and not self._skipping:
                self._heading = (self._HEADINGS[tag], [])

    def handle_endtag(self, tag: str) -> None:
        """Leave skipped content, or break the line after a block and close its heading."""

        if tag in self._SKIPPED and self._skipping:
            self._skipping -= 1
        elif tag in self._BLOCKS:
            self.parts.append("\n")
            if tag in self._HEADINGS and self._heading is not None:
                level, pieces = self._heading
                self.headings.append((level, "".join(pieces)))
                self._heading = None

    def handle_data(self, data: str) -> None:
        """Keep visible text, and add it to the heading being read, if any."""

        if not self._skipping:
            self.parts.append(data)
            if self._heading is not None:
                self._heading[1].append(data)


def _html(data: bytes) -> ExtractedText:
    """Extract visible HTML text, with the offset of each heading in it.

    Headings are matched to whole lines of the normalized text in document order, because a
    heading is a block; HTML lines are block boundaries, so no source line map is kept.
    """

    collector = _TextCollector()
    collector.feed(_decode(data))
    collector.close()
    text, _, _ = _normalize(_source_lines("".join(collector.parts)))
    # A heading is a block, so its first line is a whole line of the text; match them in order.
    starts: list[int] = []
    position = 0
    for line in text.split("\n"):
        starts.append(position)
        position += len(line) + 1
    lines = text.split("\n")
    headings: list[Heading] = []
    cursor = 0
    for level, raw in collector.headings:
        title = next((cleaned for piece in raw.splitlines() if (cleaned := _clean(piece))), "")
        if not title:
            continue
        for index in range(cursor, len(lines)):
            if lines[index] == title:
                headings.append(Heading(starts[index], level, title))
                cursor = index + 1
                break
    # HTML lines are block boundaries of the extracted text, not lines of the source.
    return ExtractedText(text, "html@1", (), tuple(headings))


_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _path(parts: tuple[str | int, ...]) -> str:
    """Write a JSON path as `$.key[0]["odd key"]`."""

    written = ["$"]
    for part in parts:
        if isinstance(part, int):
            written.append(f"[{part}]")
        elif _IDENTIFIER.match(part):
            written.append(f".{part}")
        else:
            written.append(f"[{json.dumps(part, ensure_ascii=False)}]")
    return "".join(written)


def _json_with_spans(value: object) -> tuple[str, tuple[JsonSpan, ...]]:
    """Serialize as `json.dumps(indent=2, sort_keys=True)` would, recording each value's span."""

    parts: list[str] = []
    spans: list[JsonSpan] = []
    length = 0

    def emit(piece: str) -> None:
        """Append output and advance the offset that spans are measured in."""

        nonlocal length
        parts.append(piece)
        length += len(piece)

    def dump(node: object, path: tuple[str | int, ...], level: int, start: int) -> None:
        """Write one value as `json.dumps` would and record its span, children first."""

        if isinstance(node, dict) and node:
            emit("{")
            for position, key in enumerate(sorted(node)):
                emit(",\n" if position else "\n")
                emit("  " * (level + 1))
                key_start = length
                emit(json.dumps(key, ensure_ascii=False) + ": ")
                dump(node[key], (*path, key), level + 1, key_start)
            emit("\n" + "  " * level + "}")
        elif isinstance(node, list) and node:
            emit("[")
            for position, item in enumerate(node):
                emit(",\n" if position else "\n")
                emit("  " * (level + 1))
                dump(item, (*path, position), level + 1, length)
            emit("\n" + "  " * level + "]")
        else:
            emit(json.dumps(node, ensure_ascii=False))
        spans.append(JsonSpan(start, length, _path(path)))

    dump(value, (), 0, 0)
    return "".join(parts), tuple(spans)


def _json(data: bytes) -> ExtractedText:
    """Extract JSON as its canonical pretty form, with the span of every value.

    The text is `json.dumps` output as before; the spans come from a mirror serializer and are
    kept only when its output is identical, so a path is never attached to the wrong offset.
    """

    try:
        value = json.loads(_decode(data))
    except json.JSONDecodeError as exc:
        raise ExtractionError("content_invalid_json", "Content is not valid JSON") from exc
    # Pretty JSON keeps keys next to values so both stay searchable.
    text = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
    try:
        mirrored, spans = _json_with_spans(value)
    except RecursionError:
        mirrored, spans = "", ()
    # The spans are only trusted when they describe exactly the text that was indexed.
    return ExtractedText(text, "json@1", (), (), spans if mirrored == text else (), prose=False)


_EXTRACTORS = {
    "text/plain": _plain,
    "text/markdown": _markdown,
    "text/html": _html,
    "application/json": _json,
}


def extract(content_type: str, data: bytes) -> ExtractedText:
    """Return normalized text for a supported type, or raise with a stable code.

    PDF and other binary formats are accepted for staging but not yet extracted.
    """

    function = _EXTRACTORS.get(content_type.split(";", 1)[0].strip().lower())
    if function is None:
        raise ExtractionError("extraction_unsupported", "This content type cannot be indexed yet")
    extracted = function(data)
    if not extracted.text.strip():
        raise ExtractionError("content_empty", "Content has no text to index")
    return extracted
