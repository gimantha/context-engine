"""REST API process entrypoint."""

from __future__ import annotations

import argparse

import uvicorn

from context_engine.config import Settings

from .app import create_app


def main() -> None:
    """Run the REST server or perform a startup-only validation."""

    parser = argparse.ArgumentParser(description="Run the Context Engine REST API")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8000, type=int)
    parser.add_argument("--check", action="store_true", help="Migrate and validate startup")
    args = parser.parse_args()
    settings = Settings.from_env()
    app = create_app(settings)
    if args.check:
        print("Context Engine API startup check passed.")
        return
    uvicorn.run(app, host=args.host, port=args.port, log_config=None)


if __name__ == "__main__":
    main()
