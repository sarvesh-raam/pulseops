"""API gateway: the only public entry point of the shop. Thin by design —
routing, request validation, and resilience in front of the internal
services."""

import uuid

from fastapi import FastAPI, HTTPException, Query, Request, Response

from pulseops_common import BaseServiceSettings, create_service_app
from pulseops_common.resilience import CircuitOpenError, ResilientClient, UpstreamError


class Settings(BaseServiceSettings):
    orders_url: str = "http://orders:8000"
    inventory_url: str = "http://inventory:8000"


settings = Settings()
clients: dict[str, ResilientClient] = {}


async def lifespan(app: FastAPI):
    # orders can take a while (it calls two services and retries them)
    clients["orders"] = ResilientClient("gateway", "orders", settings.orders_url, timeout=8.0)
    clients["inventory"] = ResilientClient("gateway", "inventory", settings.inventory_url)
    yield
    for client in clients.values():
        await client.aclose()


app = create_service_app("gateway", settings=settings, lifespan=lifespan)


async def _forward(target: str, method: str, path: str, **kwargs) -> Response:
    try:
        upstream = await clients[target].request(method, path, **kwargs)
    except CircuitOpenError:
        raise HTTPException(status_code=503, detail=f"{target} is unavailable (circuit open)") from None
    except UpstreamError as exc:
        raise HTTPException(status_code=502, detail=f"{target} failed: {exc}") from None
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type", "application/json"),
    )


@app.get("/api/products")
async def products() -> Response:
    return await _forward("inventory", "GET", "/products")


@app.post("/api/orders")
async def create_order(request: Request) -> Response:
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(status_code=422, detail="body must be valid JSON") from None
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="body must be a JSON object")
    # Stamp an idempotency key (unless the client sent one) so this POST
    # becomes safe to retry: if an orders pod dies mid-request, the retry
    # lands on another replica and resumes the same order instead of
    # creating — and charging — a second one.
    body.setdefault("idempotency_key", str(uuid.uuid4()))
    return await _forward("orders", "POST", "/orders", json=body)


@app.get("/api/orders/{order_id}")
async def get_order(order_id: uuid.UUID) -> Response:
    return await _forward("orders", "GET", f"/orders/{order_id}")


@app.get("/api/orders")
async def list_orders(limit: int = Query(20, ge=1, le=100)) -> Response:
    return await _forward("orders", "GET", "/orders", params={"limit": limit})
