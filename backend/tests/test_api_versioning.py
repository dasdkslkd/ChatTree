from fastapi import APIRouter, FastAPI
from fastapi.routing import APIRoute

from backend.api.router import api_v1_router, legacy_router


SERVER_ONLY_SIGNATURES = {
    ("/api/v1/health", "GET"),
    ("/api/v1/handshake", "GET"),
}
LEGACY_ONLY_SIGNATURES = {
    ("/health", "GET"),
    ("/api/tool-results/{tool_result_id}", "GET"),
}


def _signatures(router: APIRouter) -> set[tuple[str, str]]:
    return {
        (route.path, method)
        for route in router.routes
        if isinstance(route, APIRoute)
        for method in route.methods
        if method not in {"HEAD", "OPTIONS"}
    }


def test_legacy_compatibility_is_not_mirrored_into_v1():
    legacy = _signatures(legacy_router)
    v1 = _signatures(api_v1_router)

    assert LEGACY_ONLY_SIGNATURES <= legacy
    assert ("/api/v1/api/tool-results/{tool_result_id}", "GET") not in v1
    assert sum(
        1
        for route in api_v1_router.routes
        if isinstance(route, APIRoute)
        and route.path == "/api/v1/health"
        and "GET" in route.methods
    ) == 1


def test_every_canonical_legacy_business_route_has_v1_mirror():
    legacy_business = _signatures(legacy_router) - LEGACY_ONLY_SIGNATURES
    v1_business = _signatures(api_v1_router) - SERVER_ONLY_SIGNATURES

    assert legacy_business
    assert v1_business == {
        (f"/api/v1{path}", method)
        for path, method in legacy_business
    }


def test_handshake_is_v1_only():
    legacy_paths = {path for path, _ in _signatures(legacy_router)}
    v1_paths = {path for path, _ in _signatures(api_v1_router)}

    assert "/handshake" not in legacy_paths
    assert {path for path, _ in SERVER_ONLY_SIGNATURES} <= v1_paths


def test_dual_routes_have_unique_openapi_operation_ids():
    app = FastAPI()
    app.include_router(legacy_router)
    app.include_router(api_v1_router)

    operation_ids = [
        operation["operationId"]
        for path_item in app.openapi()["paths"].values()
        for operation in path_item.values()
        if isinstance(operation, dict) and "operationId" in operation
    ]
    assert len(operation_ids) == len(set(operation_ids))
