"""Place retrieved passages in the text the engine indexed, and cite them in source terms.

The provider's chunker keeps each chunk as an exact slice of the text the engine sent, but
records no offsets (M5 lineage appendix). The engine holds that text, so it finds each passage
by exact match and derives everything a citation needs from the offsets: sentences, source
lines, the nearest heading, and the JSON path. A passage that occurs more than once is placed
only when chunk order settles which occurrence it is; otherwise it keeps its chunk index alone,
because a citation must never point at the wrong place (ADR 0008 revision, M5 slice 2).
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Sequence

from context_engine.domain import EvidenceLocator
from context_engine.ingestion.extraction import ExtractedText

from .sentences import sentence_range, sentence_starts


class TextIndex:
    """One record version's extracted text, with the lookups citations need, built once."""

    def __init__(self, extracted: ExtractedText) -> None:
        """Precompute line starts, sentence starts, and heading offsets for repeated lookups.

        A query may place several passages of one record, so the per-text work is done once.
        """

        self.extracted = extracted
        text = extracted.text
        self._line_starts = [0] + [i + 1 for i, character in enumerate(text) if character == "\n"]
        # Line numbers are trusted only when they describe exactly the lines of this text.
        self._lines = extracted.lines if len(extracted.lines) == len(self._line_starts) else ()
        self._sentences = sentence_starts(text) if extracted.prose else ()
        self._headings = extracted.headings
        self._heading_offsets = [heading.offset for heading in extracted.headings]

    def occurrences(self, passage: str) -> list[int]:
        """Return every offset where the passage occurs, overlapping ones included.

        All of them are needed: a passage found more than once may be placed only when chunk
        order settles which occurrence each chunk is.
        """

        found: list[int] = []
        text = self.extracted.text
        position = text.find(passage)
        while position != -1:
            found.append(position)
            position = text.find(passage, position + 1)
        return found

    def locator(self, chunk_index: int | None, start: int, end: int) -> EvidenceLocator:
        """Describe the span `start:end` in every way this text supports."""

        text = self.extracted.text
        # Surrounding whitespace belongs to no sentence or line worth citing.
        inner_start, inner_end = start, end
        while inner_start < inner_end and text[inner_start].isspace():
            inner_start += 1
        while inner_end > inner_start and text[inner_end - 1].isspace():
            inner_end -= 1
        if inner_start == inner_end:
            return EvidenceLocator(chunk_index=chunk_index, start=start, end=end)
        lines = None
        if self._lines:
            first = bisect_right(self._line_starts, inner_start) - 1
            last = bisect_right(self._line_starts, inner_end - 1) - 1
            lines = (self._lines[first], self._lines[last])
        heading = None
        if self._headings:
            position = bisect_right(self._heading_offsets, inner_start) - 1
            if position >= 0:
                heading = self._headings[position].title
        return EvidenceLocator(
            chunk_index=chunk_index,
            start=start,
            end=end,
            sentences=sentence_range(self._sentences, inner_start, inner_end),
            lines=lines,
            heading=heading,
            path=self._path(inner_start, inner_end),
        )

    def _path(self, start: int, end: int) -> str | None:
        """Return the path of the smallest JSON value whose span holds the whole passage."""

        best = None
        for span in self.extracted.json_spans:
            if span.start <= start and end <= span.end:
                if best is None or span.end - span.start < best.end - best.start:
                    best = span
        return best.path if best is not None else None


def locate(index: TextIndex, passages: Sequence[tuple[str, int | None]]) -> list[EvidenceLocator]:
    """Return a locator for each (passage, chunk index) retrieved from one record version.

    Chunks are ordered, non-overlapping slices, so when every occurrence of a repeated passage
    was retrieved under distinct chunk indexes, the n-th chunk is the n-th occurrence. With
    fewer retrieved chunks than occurrences nothing settles which is which, and the passage
    keeps its chunk index alone. A passage the text does not contain, as when the provider
    trimmed it, is retried without its surrounding whitespace before it is given up.
    """

    located: list[EvidenceLocator] = [EvidenceLocator(chunk_index=c) for _, c in passages]
    by_passage: dict[str, list[int]] = {}
    for position, (passage, _) in enumerate(passages):
        by_passage.setdefault(passage, []).append(position)
    for passage, positions in by_passage.items():
        needle = passage
        occurrences = index.occurrences(needle) if needle else []
        if not occurrences and needle.strip() and needle.strip() != needle:
            needle = needle.strip()
            occurrences = index.occurrences(needle)
        if not occurrences:
            continue
        chunks = sorted({passages[position][1] for position in positions}, key=_chunk_order)
        if len(occurrences) == 1:
            placement = {chunk: occurrences[0] for chunk in chunks}
        elif None not in chunks and len(chunks) == len(occurrences):
            placement = dict(zip(chunks, occurrences, strict=True))
        else:
            continue
        for position in positions:
            chunk = passages[position][1]
            start = placement[chunk]
            located[position] = index.locator(chunk, start, start + len(needle))
    return located


def _chunk_order(chunk: int | None) -> int:
    """Sort key for chunk indexes; an unknown index sorts first and never pairs by order."""

    return -1 if chunk is None else chunk
