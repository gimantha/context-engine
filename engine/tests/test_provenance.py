"""Locating passages in extracted text: source lines, sentences, headings, JSON paths, and the
rule that a passage is placed only where its position is certain."""

from __future__ import annotations

import json

from context_engine.domain import EvidenceLocator
from context_engine.ingestion.extraction import extract
from context_engine.provenance import TextIndex, locate, sentence_range, sentence_starts


def _index(content_type: str, data: str | bytes) -> TextIndex:
    raw = data.encode() if isinstance(data, str) else data
    return TextIndex(extract(content_type, raw))


def _one(index: TextIndex, passage: str, chunk: int | None = 0) -> EvidenceLocator:
    [located] = locate(index, [(passage, chunk)])
    return located


def _sentences(text: str) -> list[str]:
    starts = sentence_starts(text)
    ends = [*starts[1:], len(text)]
    return [text[start:end].strip() for start, end in zip(starts, ends, strict=True)]


def test_plain_text_lines_count_source_lines_across_blank_lines_and_crlf():
    index = _index(
        "text/plain",
        "Intro line.\r\n\r\n   \r\nRollback   starts here.\r\nThen verify.\r\n\r\nDone.",
    )
    text = index.extracted.text
    assert text == "Intro line.\nRollback starts here.\nThen verify.\nDone."

    located = _one(index, "Rollback starts here.\nThen verify.")
    assert text[located.start : located.end] == "Rollback starts here.\nThen verify."
    assert located.lines == (4, 5), "blank and whitespace-only source lines still count"
    assert located.sentences == (2, 3)
    assert located.chunk_index == 0 and located.heading is None and located.path is None


def test_sentences_follow_the_engines_rules():
    assert _sentences(
        "Use the rollback checkpoint, e.g. before remediation. Version 2.5 is fine! "
        'Call Dr. Smith at 5. "Quoted start." J. R. Tolkien wrote it. Why not? '
        "It works approx. as planned.\nA new line"
    ) == [
        "Use the rollback checkpoint, e.g. before remediation.",
        "Version 2.5 is fine!",
        "Call Dr. Smith at 5.",
        '"Quoted start."',
        "J. R. Tolkien wrote it.",
        "Why not?",
        "It works approx. as planned.",
        "A new line",
    ]
    starts = sentence_starts("One. Two. Three.")
    assert sentence_range(starts, 0, 4) == (1, 1)
    assert sentence_range(starts, 2, 7) == (1, 2), "a partial sentence still counts"
    assert sentence_range(starts, 3, 3) is None


def test_markdown_headings_skip_code_fences_and_list_underlines():
    index = _index(
        "text/markdown",
        "Preamble text.\n\n"
        "# Rollback ##\n"
        "Confirm the checkpoint.\n\n"
        "```\n# not a heading\n```\n"
        "Gateway Restart\n"
        "===============\n"
        "Restart the gateway.\n"
        "- list item\n"
        "---\n"
        "After the list.\n",
    )
    assert [(h.level, h.title) for h in index.extracted.headings] == [
        (1, "Rollback"),
        (1, "Gateway Restart"),
    ]
    assert _one(index, "Preamble text.").heading is None
    assert _one(index, "Confirm the checkpoint.").heading == "Rollback"
    assert _one(index, "# not a heading").heading == "Rollback"
    after = _one(index, "After the list.")
    assert after.heading == "Gateway Restart" and after.lines == (14, 14)


def test_html_has_headings_and_sentences_but_no_source_lines():
    index = _index(
        "text/html",
        "<html><script>var x = 1;</script><h2>Escalation <em>path</em></h2>"
        "<p>Page the on-call engineer. Then open a ticket.</p></html>",
    )
    located = _one(index, "Then open a ticket.")
    assert located.heading == "Escalation path"
    assert located.sentences == (3, 3) and located.lines is None


def test_json_passages_resolve_to_the_smallest_value_holding_them():
    value = {"runbook": {"steps": ["drain traffic", "restore checkpoint"]}, "odd key": 1}
    index = _index("application/json", json.dumps(value))
    text = index.extracted.text

    step = _one(index, '"restore checkpoint"')
    assert step.path == "$.runbook.steps[1]"
    assert step.sentences is None and step.lines is None, "JSON is not cited by sentence"
    assert _one(index, '"odd key": 1').path == '$["odd key"]'
    both = text[text.index('"drain') : text.index("checkpoint") + 11]
    assert _one(index, both).path == "$.runbook.steps"
    assert _one(index, text).path == "$"


def test_a_repeated_passage_is_placed_only_when_chunk_order_settles_it():
    index = _index("text/plain", "Restart the gateway.\nCheck logs.\nRestart the gateway.")
    first, second = locate(index, [("Restart the gateway.", 7), ("Restart the gateway.", 2)])
    assert (second.start, first.start) == (0, 33), "lower chunk index, earlier occurrence"
    assert (second.lines, first.lines) == ((1, 1), (3, 3))

    alone = _one(index, "Restart the gateway.", 2)
    assert alone == EvidenceLocator(chunk_index=2), "one of two occurrences: no guess"
    unknown = locate(index, [("Restart the gateway.", None), ("Restart the gateway.", 4)])
    assert unknown == [EvidenceLocator(), EvidenceLocator(chunk_index=4)]


def test_passages_the_text_does_not_hold_keep_their_chunk_index():
    index = _index("text/plain", "Confirm the rollback checkpoint.")
    assert _one(index, "Something else entirely.", 3) == EvidenceLocator(chunk_index=3)
    trimmed = _one(index, "  Confirm the rollback checkpoint.\n", 0)
    assert (trimmed.start, trimmed.end, trimmed.sentences) == (0, 32, (1, 1))
