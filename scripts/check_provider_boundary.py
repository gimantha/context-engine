#!/usr/bin/env python3
"""Reject private provider concepts from public contracts and application layers."""

from __future__ import annotations

import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PUBLIC_CONTRACTS = ROOT / "contracts"
PUBLIC_SOURCE = ROOT / "engine" / "src" / "context_engine"

FORBIDDEN_CONTRACT_TERMS = (
    "cognee",
    "dataset",
    "dataitem",
    "remember",
    "recall",
    "improve",
    "forget",
    "backendreference",
    "backend_ref",
    "backendid",
    "backend_id",
    "accesspartition",
    "access_partition",
)

FORBIDDEN_IMPORT = re.compile(r"(?:^|\n)\s*(?:from|import)\s+cognee(?:\.|\s|$)", re.IGNORECASE)
PRIVATE_PROVIDER_IMPORT = re.compile(
    r"(?:^|\n)\s*(?:from|import)\s+\.*(?:context_engine\.)?knowledge_backend\.providers",
    re.IGNORECASE,
)
BACKEND_IMPORT = re.compile(
    r"(?:^|\n)\s*(?:from|import)\s+\.*(?:context_engine\.)?knowledge_backend",
    re.IGNORECASE,
)
PRIVATE_PROVIDER_TERM = re.compile(
    r"(?:cognee|dataset|dataitem|\bremember\b|\brecall\b|\bimprove\b|\bforget\b)",
    re.IGNORECASE,
)
PROVIDER_NEUTRAL_LAYERS = {
    "api",
    "application",
    "domain",
    "ingestion",
    "mcp",
    "observability",
    "persistence",
    "provenance",
    "security",
    "worker",
}


def violations() -> list[str]:
    """Return all public terminology and dependency-boundary violations."""

    failures: list[str] = []
    term_pattern = re.compile(
        r"(?:" + "|".join(re.escape(term) for term in FORBIDDEN_CONTRACT_TERMS) + r")",
        re.IGNORECASE,
    )
    for path in sorted(PUBLIC_CONTRACTS.rglob("*")):
        if path.is_file() and path.suffix.lower() in {".json", ".yaml", ".yml", ".md"}:
            for line_number, line in enumerate(path.read_text().splitlines(), start=1):
                match = term_pattern.search(line)
                if match:
                    failures.append(
                        f"{path.relative_to(ROOT)}:{line_number}: private term {match.group(0)!r}"
                    )

    private_root = PUBLIC_SOURCE / "knowledge_backend" / "providers"
    for path in sorted(PUBLIC_SOURCE.rglob("*.py")):
        if path.is_relative_to(private_root):
            continue
        content = path.read_text()
        if FORBIDDEN_IMPORT.search(content):
            failures.append(f"{path.relative_to(ROOT)}: imports the private provider package")
        relative = path.relative_to(PUBLIC_SOURCE)
        if relative.parts and relative.parts[0] in PROVIDER_NEUTRAL_LAYERS:
            if PRIVATE_PROVIDER_IMPORT.search(content):
                failures.append(
                    f"{path.relative_to(ROOT)}: imports the private provider implementation"
                )
            match = PRIVATE_PROVIDER_TERM.search(content)
            if match:
                failures.append(
                    f"{path.relative_to(ROOT)}: contains private provider term {match.group(0)!r}"
                )
            if relative.parts[0] in {"api", "mcp"} and BACKEND_IMPORT.search(content):
                failures.append(
                    f"{path.relative_to(ROOT)}: external interface imports the backend boundary"
                )
    return failures


def main() -> int:
    """Print violations and return a shell-compatible status code."""

    failures = violations()
    if failures:
        print("Provider-boundary violations:", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1
    print("Provider boundary check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
