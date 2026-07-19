from fastapi.routing import APIRoute

from backend.api.router import api_v1_router
from main import app


def _api_routes(router):
    return [
        route
        for route in router.routes
        if isinstance(route, APIRoute)
    ]


def test_every_production_business_route_is_under_api_v1():
    paths = {route.path for route in _api_routes(app.router)}

    assert paths
    assert all(path.startswith("/api/v1/") for path in paths)
    assert not any(path.startswith("/api/v1/api/") for path in paths)
    assert "/api/v1/health" in paths
    assert "/api/v1/handshake" in paths
    assert "/api/v1/conversations" in paths


def test_api_v1_health_has_one_handler():
    matches = [
        route
        for route in _api_routes(api_v1_router)
        if route.path == "/api/v1/health" and "GET" in route.methods
    ]
    assert len(matches) == 1


def test_api_v1_path_method_signatures_are_unique():
    signatures = [
        (route.path, method)
        for route in _api_routes(app.router)
        for method in route.methods
        if method not in {"HEAD", "OPTIONS"}
    ]

    assert len(signatures) == len(set(signatures))


def test_api_v1_openapi_operation_ids_are_unique():
    operation_ids = [
        operation["operationId"]
        for path_item in app.openapi()["paths"].values()
        for operation in path_item.values()
        if isinstance(operation, dict) and "operationId" in operation
    ]
    assert len(operation_ids) == len(set(operation_ids))


def test_api_v1_openapi_uses_the_real_error_envelope_for_validation():
    schema = app.openapi()
    error_ref = {"$ref": "#/components/schemas/ErrorEnvelope"}

    assert "ErrorEnvelope" in schema["components"]["schemas"]
    assert "HTTPValidationError" not in schema["components"]["schemas"]
    for path, path_item in schema["paths"].items():
        if not path.startswith("/api/v1/"):
            continue
        for operation in path_item.values():
            if not isinstance(operation, dict) or "responses" not in operation:
                continue
            assert operation["responses"]["422"]["content"][
                "application/json"
            ]["schema"] == error_ref


def test_active_run_delete_documents_its_domain_conflict_envelope():
    operation = app.openapi()["paths"][
        "/api/v1/conversations/{conversation_id}/nodes/{node_id}"
    ]["delete"]

    assert operation["responses"]["409"]["content"]["application/json"][
        "schema"
    ] == {"$ref": "#/components/schemas/ErrorEnvelope"}
