"""FastAPI composition for the public REST boundary."""

from __future__ import annotations

import time
from uuid import uuid4

from fastapi import FastAPI, Header, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse

from context_engine.application import (
    ApplicationError,
    ConflictError,
    ContextEngineService,
    NotFoundError,
)
from context_engine.config import Settings
from context_engine.observability import (
    MetricsRegistry,
    configure_logging,
    get_logger,
    log_event,
)
from context_engine.persistence import ControlDatabase, ControlPlaneRepository

from .schemas import (
    ContextSpaceResponse,
    CreateContextSpaceRequest,
    ErrorResponse,
    HealthResponse,
    IngestionRequest,
    JobAcceptedResponse,
    JobResponse,
)

logger = get_logger(__name__)


def _trace_id(request: Request) -> str:
    return getattr(request.state, "trace_id", f"trace_{uuid4().hex}")


def _error_status(error: ApplicationError) -> int:
    if isinstance(error, NotFoundError):
        return 404
    if isinstance(error, ConflictError):
        return 409
    return 400


def _error_body(code: str, message: str, trace_id: str) -> dict[str, str]:
    return ErrorResponse(code=code, message=message, traceId=trace_id).model_dump(
        by_alias=True, exclude_none=True
    )


def create_app(
    settings: Settings | None = None,
    service: ContextEngineService | None = None,
    database: ControlDatabase | None = None,
    metrics: MetricsRegistry | None = None,
) -> FastAPI:
    """Build the REST application and wire its control-plane dependencies."""

    settings = settings or Settings.from_env()
    configure_logging(settings.log_level)
    metrics = metrics or MetricsRegistry()
    database = database or ControlDatabase(settings.database_path, settings.migrations_path)
    database.migrate()
    if service is None:
        repository = ControlPlaneRepository(database)
        service = ContextEngineService(repository, metrics, settings.worker_max_attempts)

    app = FastAPI(title="Context Engine API", version="0.2.0")
    app.state.database = database
    app.state.metrics = metrics
    app.state.service = service

    @app.middleware("http")
    async def trace_requests(request: Request, call_next):
        # Keep request bodies and credentials out of logs; only bounded trace metadata is retained.
        incoming = request.headers.get("X-Trace-Id", "").strip()
        request.state.trace_id = incoming[:200] if incoming else f"trace_{uuid4().hex}"
        started = time.perf_counter()
        response = await call_next(request)
        response.headers["X-Trace-Id"] = request.state.trace_id
        log_event(
            logger,
            "http_request_completed",
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
            duration_ms=round((time.perf_counter() - started) * 1000, 2),
            trace_id=request.state.trace_id,
        )
        metrics.increment("context_engine_http_requests_total")
        return response

    @app.exception_handler(ApplicationError)
    async def application_error(request: Request, error: ApplicationError) -> JSONResponse:
        return JSONResponse(
            status_code=_error_status(error),
            content=_error_body(error.code, error.message, _trace_id(request)),
        )

    @app.exception_handler(RequestValidationError)
    async def request_validation_error(
        request: Request, _error: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content=_error_body("invalid_request", "Request is invalid", _trace_id(request)),
        )

    @app.get("/v1/health/live", response_model=HealthResponse)
    async def liveness() -> HealthResponse:
        return HealthResponse(status="ok")

    @app.get("/v1/health/ready", response_model=HealthResponse)
    async def readiness(response: Response) -> HealthResponse:
        ready = database.ping()
        if not ready:
            response.status_code = 503
        return HealthResponse(status="ok" if ready else "unavailable")

    @app.get("/internal/metrics", include_in_schema=False)
    async def process_metrics() -> PlainTextResponse:
        return PlainTextResponse(metrics.render_prometheus(), media_type="text/plain")

    @app.post(
        "/v1/spaces",
        response_model=ContextSpaceResponse,
        response_model_by_alias=True,
        response_model_exclude_none=True,
        status_code=201,
    )
    async def create_context_space(body: CreateContextSpaceRequest) -> ContextSpaceResponse:
        return ContextSpaceResponse.from_domain(
            service.create_context_space(body.name, body.description)
        )

    @app.get(
        "/v1/spaces",
        response_model=list[ContextSpaceResponse],
        response_model_by_alias=True,
        response_model_exclude_none=True,
    )
    async def list_context_spaces() -> list[ContextSpaceResponse]:
        return [ContextSpaceResponse.from_domain(space) for space in service.list_context_spaces()]

    @app.get(
        "/v1/spaces/{space_id}",
        response_model=ContextSpaceResponse,
        response_model_by_alias=True,
        response_model_exclude_none=True,
    )
    async def get_context_space(space_id: str) -> ContextSpaceResponse:
        return ContextSpaceResponse.from_domain(service.get_context_space(space_id))

    @app.post(
        "/v1/ingestions",
        response_model=JobAcceptedResponse,
        response_model_by_alias=True,
        status_code=202,
    )
    async def accept_ingestion(
        body: IngestionRequest,
        request: Request,
        response: Response,
        idempotency_key: str = Header(alias="Idempotency-Key", min_length=8, max_length=200),
    ) -> JobAcceptedResponse:
        job = service.accept_ingestion(body.to_command(), idempotency_key, _trace_id(request))
        status_url = f"/v1/jobs/{job.id}"
        response.headers["Location"] = status_url
        return JobAcceptedResponse(jobId=job.id, statusUrl=status_url)

    @app.get(
        "/v1/jobs/{job_id}",
        response_model=JobResponse,
        response_model_by_alias=True,
        response_model_exclude_none=True,
    )
    async def get_job(job_id: str) -> JobResponse:
        return JobResponse.from_domain(service.get_job(job_id))

    return app
