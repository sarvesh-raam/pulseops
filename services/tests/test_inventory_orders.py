import uuid

import httpx

from pulseops_common.resilience import ResilientClient


async def _stock(main, sku):
    async with main.state["pool"].acquire() as conn:
        return await conn.fetchval("SELECT stock FROM products WHERE sku = $1", sku)


# --- inventory -----------------------------------------------------------------


async def test_reserve_is_atomic_and_idempotent(inventory_client):
    client, main = inventory_client
    order = str(uuid.uuid4())
    body = {"order_id": order, "items": [{"sku": "KB-01", "qty": 2}, {"sku": "MS-02", "qty": 1}]}
    r = await client.post("/reserve", json=body)
    assert r.status_code == 200 and r.json()["total_cents"] == 2 * 8900 + 3900
    assert await _stock(main, "KB-01") == 48
    # retried call (e.g. first response lost when a pod died): no double decrement
    assert (await client.post("/reserve", json=body)).status_code == 200
    assert await _stock(main, "KB-01") == 48


async def test_insufficient_stock_rolls_back_every_item(inventory_client):
    client, main = inventory_client
    body = {"order_id": str(uuid.uuid4()), "items": [{"sku": "KB-01", "qty": 1}, {"sku": "MN-03", "qty": 99}]}
    r = await client.post("/reserve", json=body)
    assert r.status_code == 409
    assert await _stock(main, "KB-01") == 50  # the first item was not left decremented


async def test_release_restores_stock_once(inventory_client):
    client, main = inventory_client
    order = str(uuid.uuid4())
    await client.post("/reserve", json={"order_id": order, "items": [{"sku": "HS-04", "qty": 5}]})
    assert await _stock(main, "HS-04") == 45
    await client.post("/release", json={"order_id": order})
    await client.post("/release", json={"order_id": order})
    assert await _stock(main, "HS-04") == 50


async def test_unknown_sku_is_404(inventory_client):
    client, _ = inventory_client
    r = await client.post("/reserve", json={"order_id": str(uuid.uuid4()), "items": [{"sku": "NOPE", "qty": 1}]})
    assert r.status_code == 404


# --- orders saga (downstreams mocked) ------------------------------------------------


def _wire(main, inventory_handler, payments_handler):
    main.state["inventory"] = ResilientClient(
        "orders", "inventory-test", "http://inventory", transport=httpx.MockTransport(inventory_handler), max_attempts=2
    )
    main.state["payments"] = ResilientClient(
        "orders", "payments-test", "http://payments", transport=httpx.MockTransport(payments_handler), max_attempts=2
    )


def _inventory(calls):
    def handler(request):
        calls.append(request.url.path)
        if request.url.path == "/reserve":
            return httpx.Response(200, json={"order_id": "x", "total_cents": 1234})
        return httpx.Response(204)

    return handler


async def test_order_confirmed_on_happy_path(orders_client):
    client, main = orders_client
    calls = []
    _wire(main, _inventory(calls), lambda r: httpx.Response(200, json={"payment_id": str(uuid.uuid4()), "status": "captured"}))
    r = await client.post("/orders", json={"items": [{"sku": "KB-01", "qty": 1}]})
    assert r.status_code == 201 and r.json()["status"] == "confirmed" and r.json()["total_cents"] == 1234
    assert "/release" not in calls


async def test_declined_payment_releases_stock(orders_client):
    client, main = orders_client
    calls = []
    _wire(main, _inventory(calls), lambda r: httpx.Response(402, json={"detail": "card declined"}))
    r = await client.post("/orders", json={"items": [{"sku": "KB-01", "qty": 1}]})
    assert r.json()["status"] == "failed" and r.json()["reason"] == "card_declined"
    assert calls == ["/reserve", "/release"]  # compensation ran


async def test_payments_down_fails_order_and_compensates(orders_client):
    client, main = orders_client
    calls = []

    def down(request):
        raise httpx.ConnectError("refused", request=request)

    _wire(main, _inventory(calls), down)
    r = await client.post("/orders", json={"items": [{"sku": "KB-01", "qty": 1}]})
    assert r.status_code == 503
    assert "/release" in calls


async def test_idempotency_key_returns_the_same_order(orders_client):
    client, main = orders_client
    charges = []

    def pay(request):
        charges.append(1)
        return httpx.Response(200, json={"payment_id": str(uuid.uuid4()), "status": "captured"})

    _wire(main, _inventory([]), pay)
    key = str(uuid.uuid4())
    first = await client.post("/orders", json={"items": [{"sku": "KB-01", "qty": 1}], "idempotency_key": key})
    second = await client.post("/orders", json={"items": [{"sku": "KB-01", "qty": 1}], "idempotency_key": key})
    assert first.json()["id"] == second.json()["id"] == key
    assert len(charges) == 1


async def test_reserve_again_after_release_works(inventory_client):
    """A resumed order re-reserves after its first attempt was compensated."""
    client, main = inventory_client
    body = {"order_id": str(uuid.uuid4()), "items": [{"sku": "DK-05", "qty": 2}]}
    await client.post("/reserve", json=body)
    await client.post("/release", json={"order_id": body["order_id"]})
    r = await client.post("/reserve", json=body)
    assert r.status_code == 200 and await _stock(main, "DK-05") == 48


async def test_payment_is_captured_once_across_retries(payments_client):
    client, _ = payments_client
    order = str(uuid.uuid4())
    first = await client.post("/charge", json={"order_id": order, "amount_cents": 500})
    second = await client.post("/charge", json={"order_id": order, "amount_cents": 500})
    assert first.status_code == second.status_code == 200
    assert first.json()["payment_id"] == second.json()["payment_id"]


async def test_retry_resumes_an_order_that_failed_on_unavailability(orders_client):
    """The gateway retries with the same idempotency key; once the dependency
    is back the retry must complete the order, not replay the failure."""
    client, main = orders_client
    key = str(uuid.uuid4())

    def down(request):
        raise httpx.ConnectError("refused", request=request)

    _wire(main, _inventory([]), down)
    first = await client.post("/orders", json={"items": [{"sku": "KB-01", "qty": 1}], "idempotency_key": key})
    assert first.status_code == 503

    _wire(main, _inventory([]), lambda r: httpx.Response(200, json={"payment_id": str(uuid.uuid4()), "status": "captured"}))
    second = await client.post("/orders", json={"items": [{"sku": "KB-01", "qty": 1}], "idempotency_key": key})
    assert second.status_code == 201 and second.json()["status"] == "confirmed"
