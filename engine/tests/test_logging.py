"""Process logging keeps the engine's allowlist and drops third-party chatter."""

from __future__ import annotations

import io
import json
import logging

from context_engine.observability import configure_logging
from context_engine.observability.logging import log_event


def test_engine_handler_keeps_engine_records_and_third_party_warnings_only():
    root = logging.getLogger()
    before = list(root.handlers), root.level
    try:
        root.handlers.clear()
        configure_logging("INFO")
        [handler] = root.handlers
        stream = io.StringIO()
        handler.setStream(stream)

        log_event(logging.getLogger("context_engine.worker"), "job_started", job_id="job_1")
        logging.getLogger("uvicorn.error").info("Started server process")
        logging.getLogger("ChunksRetriever").info("Starting chunk retrieval for query: 'secret'")
        logging.getLogger("ChunksRetriever").warning("collection missing")

        lines = [json.loads(line) for line in stream.getvalue().splitlines()]
    finally:
        root.handlers[:] = before[0]
        root.setLevel(before[1])

    assert [line["message"] for line in lines] == [
        "job_started",
        "Started server process",
        "collection missing",
    ]
    assert lines[0]["job_id"] == "job_1"
    assert "secret" not in json.dumps(lines)
