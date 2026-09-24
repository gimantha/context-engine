"""Read-only pipeline progress API, kept separate from the resource API.

Progress is observability for one tenant's pipeline: it is polled often, never mutates state,
and is authorized per resource like every other public route. Process-level operator counters
stay on the internal metrics endpoint.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import APIRouter, Depends, Path

from context_engine.application import ContextEngineService
from context_engine.security.identity import AuthenticatedPrincipal

from .schemas import SourceProgressResponse, SpaceProgressResponse

ResourceId = Annotated[str, Path(min_length=1, max_length=200)]


def build_progress_router(
    service: ContextEngineService,
    current_principal: Callable[..., Awaitable[AuthenticatedPrincipal]],
) -> APIRouter:
    """Return the progress routes bound to the application service and credential check."""

    router = APIRouter(prefix="/v1/progress", tags=["progress"])

    @router.get(
        "/sources/{source_id}",
        response_model=SourceProgressResponse,
        response_model_by_alias=True,
    )
    async def source_progress(
        source_id: ResourceId,
        principal: AuthenticatedPrincipal = Depends(current_principal),
    ) -> SourceProgressResponse:
        return SourceProgressResponse.from_domain(service.source_progress(principal, source_id))

    @router.get(
        "/spaces/{space_id}",
        response_model=SpaceProgressResponse,
        response_model_by_alias=True,
    )
    async def space_progress(
        space_id: ResourceId,
        principal: AuthenticatedPrincipal = Depends(current_principal),
    ) -> SpaceProgressResponse:
        return SpaceProgressResponse(
            spaceId=space_id,
            sources=[
                SourceProgressResponse.from_domain(item)
                for item in service.space_progress(principal, space_id)
            ],
        )

    return router
