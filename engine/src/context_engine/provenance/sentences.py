"""Sentence boundaries in extracted text, by the engine's own rules (M5 lineage appendix).

The provider splits sentences on any period, so its boundaries cannot be cited. The engine
segments the text it extracted itself: a line break always ends a sentence, because extracted
lines are blocks; terminal punctuation ends one when whitespace and a sentence opener follow
it, unless the word before the period is a common abbreviation or a single initial.
"""

from __future__ import annotations

from bisect import bisect_right

_TERMINAL = ".!?"
_CLOSERS = "\"')]}’”»"
_OPENERS = "\"'([{‘“«"
_ABBREVIATIONS = frozenset(
    {"e.g", "i.e", "vs", "mr", "mrs", "ms", "dr", "prof", "st", "jr", "sr", "fig", "no", "cf"}
)


def _abbreviation(text: str, period: int) -> bool:
    """Return whether the period at `period` closes an abbreviation rather than a sentence."""

    start = period
    while start > 0 and (text[start - 1].isalpha() or text[start - 1] == "."):
        start -= 1
    word = text[start:period].lstrip(".")
    if not word:
        return False
    return word.lower() in _ABBREVIATIONS or (len(word) == 1 and word.isupper())


def sentence_starts(text: str) -> tuple[int, ...]:
    """Return the offset where each sentence of the text begins, in order.

    Offsets rather than sentence strings, so a passage's sentence range is found by bisecting
    its character range, and a passage cut mid-sentence at a chunk edge still gets one.
    """

    length = len(text)

    def skip_space(position: int) -> int:
        """Return the first non-space offset at or after `position`, where a sentence starts."""

        while position < length and text[position].isspace():
            position += 1
        return position

    starts: list[int] = []
    position = skip_space(0)
    if position < length:
        starts.append(position)
    while position < length:
        character = text[position]
        if character == "\n":
            following = skip_space(position + 1)
            if following < length:
                starts.append(following)
            position = max(following, position + 1)
            continue
        if character in _TERMINAL:
            end = position + 1
            while end < length and text[end] in _TERMINAL:
                end += 1
            while end < length and text[end] in _CLOSERS:
                end += 1
            if end < length and text[end].isspace():
                following = skip_space(end)
                opener = following < length and (
                    text[following].isupper()
                    or text[following].isdigit()
                    or text[following] in _OPENERS
                )
                crosses_line = "\n" in text[end:following]
                if following < length and (crosses_line or opener):
                    if crosses_line or not (
                        character == "." and end == position + 1 and _abbreviation(text, position)
                    ):
                        starts.append(following)
                        position = following
                        continue
            position = end
            continue
        position += 1
    return tuple(starts)


def sentence_range(starts: tuple[int, ...], start: int, end: int) -> tuple[int, int] | None:
    """Return the 1-based first and last sentence that the span `start:end` touches."""

    if not starts or end <= start:
        return None
    first = max(bisect_right(starts, start), 1)
    last = max(bisect_right(starts, end - 1), 1)
    return first, last
