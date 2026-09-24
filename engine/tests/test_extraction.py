"""Text extraction from staged bytes."""

from __future__ import annotations

import pytest

from context_engine.ingestion.extraction import ExtractionError, extract


def test_plain_and_markdown_text_is_normalized():
    plain = extract("text/plain; charset=utf-8", "﻿Line  one\n\n\t line two  \n".encode())
    markdown = extract("text/markdown", b"# Title\n\nBody")

    assert (plain.text, plain.parser_version) == ("Line one\nline two", "plain@1")
    assert (markdown.text, markdown.parser_version) == ("# Title\nBody", "markdown@1")


def test_html_keeps_visible_text_only():
    page = b"""<html><head><style>p { color: red }</style><script>alert(1)</script></head>
    <body><h1>Runbook</h1><p>Check the &amp; gateway</p><noscript>enable js</noscript>
    <ul><li>Step one</li><li>Step two</li></ul></body></html>"""

    result = extract("text/html", page)

    assert result.text == "Runbook\nCheck the & gateway\nStep one\nStep two"
    assert "alert" not in result.text and "color" not in result.text
    assert result.parser_version == "html@1"


def test_json_is_rendered_with_keys_next_to_values():
    result = extract("application/json", b'{"b": [1, 2], "a": "Colombo"}')

    assert result.text.splitlines()[1] == '  "a": "Colombo",'
    assert result.parser_version == "json@1"


@pytest.mark.parametrize(
    ("content_type", "data", "code"),
    [
        ("application/pdf", b"%PDF-1.7", "extraction_unsupported"),
        ("text/plain", b"\xff\xfe\xfa", "content_not_utf8"),
        ("application/json", b"{not json", "content_invalid_json"),
        ("text/html", b"<script>only()</script>", "content_empty"),
        ("text/plain", b"   \n\t", "content_empty"),
    ],
)
def test_unindexable_content_raises_a_stable_code(content_type, data, code):
    with pytest.raises(ExtractionError) as error:
        extract(content_type, data)

    assert error.value.code == code
