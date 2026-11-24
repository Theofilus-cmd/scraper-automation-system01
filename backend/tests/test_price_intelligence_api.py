"""Phase 4 API: internal products, product matches, price intelligence.

docker compose exec api pytest -m integration tests/test_price_intelligence_api.py
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.db.models.repository import upsert_scrape_result
from app.db.models.scraping import Source
from app.main import app
from app.scraping.types import NormalizedRecord, ValidationResult
from app.workers.async_bridge import run_async
from tests.factories import (
    create_isolated_workspace_source,
    create_test_run,
    create_test_source,
    create_test_task,
)

pytestmark = pytest.mark.integration

IP = "/api/v1/internal-products"
PM = "/api/v1/product-matches"
PI = "/api/v1/price-intelligence"


def _sku() -> str:
    return f"PI-{uuid.uuid4().hex[:12].upper()}"


async def _scrape(
    source: Source, *, sku: str | None, price: str, currency: str = "USD", valid: bool = True
) -> uuid.UUID:
    run = await create_test_run(source.id, status="completed")
    task = await create_test_task(run.id, source.id, status="succeeded")
    result = await upsert_scrape_result(
        source_id=source.id,
        run_id=run.id,
        task_id=task.id,
        record=NormalizedRecord(
            product_url=source.url,
            scraped_at=datetime.now(UTC),
            stock_status="in_stock",
            product_name="Competitor Widget",
            price=Decimal(price),
            currency=currency,
            sku=sku,
        ),
        validation=ValidationResult(is_valid=valid, errors={} if valid else {"x": "bad"}),
    )
    assert result.product_id is not None
    return result.product_id


def _competitor(*, sku: str | None, price: str, currency: str = "USD") -> tuple[Source, uuid.UUID]:
    source = run_async(create_test_source())
    return source, run_async(_scrape(source, sku=sku, price=price, currency=currency))


def _create(client: TestClient, **over: Any) -> dict[str, Any]:
    body = {"name": "My Widget", "own_price": "100.00", "currency": "USD", "sku": _sku(), **over}
    resp = client.post(IP, json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.fixture
def other_client() -> Any:
    """A client authenticated as a *different* workspace's owner."""
    from app.core.security import create_access_token
    from app.db.models.identity import Workspace
    from app.db.session import get_session

    workspace, _ = run_async(create_isolated_workspace_source())

    async def _owner() -> uuid.UUID:
        async with get_session() as session:
            ws = await session.get(Workspace, workspace.id)
            assert ws is not None
            return ws.owner_user_id

    token = create_access_token(subject=str(run_async(_owner())))
    with TestClient(app) as c:
        c.headers.update({"Authorization": f"Bearer {token}"})
        yield c


# --- auth ------------------------------------------------------------------


def test_endpoints_require_authentication() -> None:
    zero = "00000000-0000-0000-0000-000000000000"
    with TestClient(app) as anon:
        for method, url in [
            ("get", IP),
            ("post", IP),
            ("get", f"{IP}/{zero}"),
            ("get", PM),
            ("post", PM),
            ("post", f"{PM}/auto-match"),
            ("get", PI),
            ("get", f"{PI}/{zero}"),
        ]:
            assert getattr(anon, method)(url).status_code == 401, (method, url)


# --- internal products -----------------------------------------------------


def test_internal_product_crud(client: TestClient) -> None:
    item = _create(client, barcode="012345678905", brand="Acme", category="Tools", map_price="90")
    assert item["currency"] == "USD" and item["own_price"] == "100.00"
    assert item["map_price"] == "90.00" and item["status"] == "active"

    assert client.get(f"{IP}/{item['id']}").json()["sku"] == item["sku"]

    patched = client.patch(
        f"{IP}/{item['id']}", json={"own_price": "95.50", "map_price": None, "currency": "eur"}
    )
    assert patched.status_code == 200
    assert (patched.json()["own_price"], patched.json()["map_price"]) == ("95.50", None)
    assert patched.json()["currency"] == "EUR"

    assert client.delete(f"{IP}/{item['id']}").status_code == 204
    assert client.get(f"{IP}/{item['id']}").json()["status"] == "archived"
    archived = client.get(IP, params={"status": "archived", "sku": item["sku"]}).json()
    assert [r["id"] for r in archived["data"]] == [item["id"]]
    assert client.get(IP, params={"status": "active", "sku": item["sku"]}).json()["data"] == []


def test_internal_product_validation_errors(client: TestClient) -> None:
    ok = {"name": "X", "own_price": "1", "currency": "USD"}
    for bad in (
        {**ok, "own_price": "-1"},
        {**ok, "currency": "DOLLAR"},
        {**ok, "barcode": "abc"},
        {**ok, "workspace_id": str(uuid.uuid4())},
        {"name": "X"},
    ):
        resp = client.post(IP, json=bad)
        assert resp.status_code == 422, bad
        assert resp.json()["error"]["code"]
    item = _create(client)
    assert client.patch(f"{IP}/{item['id']}", json={}).status_code == 422
    assert client.patch(f"{IP}/{item['id']}", json={"name": None}).status_code == 422
    assert client.get(IP, params={"status": "bogus"}).status_code == 422
    assert client.get(IP, params={"cursor": "!!"}).status_code == 422


def test_duplicate_sku_is_case_insensitive_and_workspace_scoped(
    client: TestClient, other_client: TestClient
) -> None:
    sku = _sku()
    _create(client, sku=sku)
    dup = client.post(
        IP, json={"name": "Dup", "own_price": "1", "currency": "USD", "sku": sku.lower()}
    )
    assert dup.status_code == 409 and dup.json()["error"]["code"] == "DUPLICATE_SKU"

    # Same SKU in another workspace is fine.
    assert (
        other_client.post(
            IP, json={"name": "Other", "own_price": "1", "currency": "USD", "sku": sku}
        ).status_code
        == 201
    )

    # Patch into a taken SKU -> 409, and the row is unchanged.
    other = _create(client)
    clash = client.patch(f"{IP}/{other['id']}", json={"sku": sku})
    assert clash.status_code == 409
    assert client.get(f"{IP}/{other['id']}").json()["sku"] == other["sku"]


def test_internal_products_workspace_isolation(
    client: TestClient, other_client: TestClient
) -> None:
    item = _create(client)
    url = f"{IP}/{item['id']}"
    assert other_client.get(url).status_code == 404
    assert other_client.patch(url, json={"name": "hack"}).status_code == 404
    assert other_client.delete(url).status_code == 404
    assert client.get(url).json()["name"] == "My Widget"
    ids = [r["id"] for r in other_client.get(IP, params={"limit": 100}).json()["data"]]
    assert item["id"] not in ids


def test_internal_products_pagination_and_search(client: TestClient) -> None:
    tag = uuid.uuid4().hex[:8]
    for i in range(3):
        _create(client, name=f"Paged {tag} {i}")
    first = client.get(IP, params={"q": f"paged {tag}", "limit": 2}).json()
    assert len(first["data"]) == 2 and first["pagination"]["has_more"]
    second = client.get(
        IP, params={"q": f"paged {tag}", "limit": 2, "cursor": first["pagination"]["next_cursor"]}
    ).json()
    assert len(second["data"]) == 1 and not second["pagination"]["has_more"]
    # LIKE wildcards in q are literal.
    assert client.get(IP, params={"q": "%"}).json()["data"] == []


# --- matches ---------------------------------------------------------------


def test_manual_and_url_match_and_duplicates(client: TestClient) -> None:
    item = _create(client)
    source, product_id = _competitor(sku=None, price="80.00")

    manual = client.post(
        PM, json={"internal_product_id": item["id"], "product_id": str(product_id)}
    )
    assert manual.status_code == 201, manual.text
    body = manual.json()
    assert body["match_type"] == "manual" and body["competitor"]["price"] == "80.00"

    again = client.post(PM, json={"internal_product_id": item["id"], "product_id": str(product_id)})
    assert again.status_code == 409 and again.json()["error"]["code"] == "DUPLICATE_PRODUCT_MATCH"

    item2 = _create(client)
    unknown = client.post(
        PM,
        json={
            "internal_product_id": item2["id"],
            "product_url": "http://mock-store:4000/products/nope",
        },
    )
    assert unknown.status_code == 404
    by_url = client.post(
        PM, json={"internal_product_id": item2["id"], "product_url": source.url + "/"}
    )
    assert by_url.status_code == 201, by_url.text
    assert by_url.json()["match_type"] == "url"

    got = client.get(f"{PM}/{body['id']}")
    assert got.status_code == 200 and got.json()["id"] == body["id"]
    listed = client.get(PM, params={"internal_product_id": item["id"]}).json()["data"]
    assert [m["id"] for m in listed] == [body["id"]]
    assert client.get(PM, params={"match_type": "url", "internal_product_id": item2["id"]}).json()[
        "data"
    ]
    assert client.get(PM, params={"match_type": "bogus"}).status_code == 422

    assert client.delete(f"{PM}/{body['id']}").status_code == 204
    assert client.get(f"{PM}/{body['id']}").status_code == 404
    assert client.delete(f"{PM}/{body['id']}").status_code == 404


def test_match_validation(client: TestClient) -> None:
    item = _create(client)
    zero = str(uuid.uuid4())
    assert client.post(PM, json={"internal_product_id": item["id"]}).status_code == 422
    assert (
        client.post(
            PM,
            json={
                "internal_product_id": item["id"],
                "product_id": zero,
                "product_url": "http://x.test/a",
            },
        ).status_code
        == 422
    )
    assert (
        client.post(
            PM, json={"internal_product_id": item["id"], "product_url": "ftp://x.test/a"}
        ).status_code
        == 422
    )
    r = client.post(PM, json={"internal_product_id": zero, "product_id": zero})
    assert r.status_code == 404 and r.json()["error"]["code"] == "INTERNAL_PRODUCT_NOT_FOUND"
    r = client.post(PM, json={"internal_product_id": item["id"], "product_id": zero})
    assert r.status_code == 404 and r.json()["error"]["code"] == "PRODUCT_NOT_FOUND"


def test_matches_workspace_isolation(client: TestClient, other_client: TestClient) -> None:
    item = _create(client)
    _source, product_id = _competitor(sku=None, price="80")
    match = client.post(
        PM, json={"internal_product_id": item["id"], "product_id": str(product_id)}
    ).json()

    # Other workspace can't read/delete it, nor link to our internal/competitor product.
    assert other_client.get(f"{PM}/{match['id']}").status_code == 404
    assert other_client.delete(f"{PM}/{match['id']}").status_code == 404
    assert other_client.get(PM).json()["data"] == []
    assert other_client.get(f"{PI}/{item['id']}").status_code == 404

    other_item = other_client.post(
        IP, json={"name": "O", "own_price": "1", "currency": "USD"}
    ).json()
    cross = other_client.post(
        PM, json={"internal_product_id": other_item["id"], "product_id": str(product_id)}
    )
    assert cross.status_code == 404 and cross.json()["error"]["code"] == "PRODUCT_NOT_FOUND"
    cross = other_client.post(
        PM, json={"internal_product_id": item["id"], "product_id": str(product_id)}
    )
    assert cross.status_code == 404
    assert (
        other_client.post(f"{PM}/auto-match", json={"internal_product_id": item["id"]}).status_code
        == 404
    )
    assert client.get(f"{PM}/{match['id']}").status_code == 200


def test_auto_match_by_sku(client: TestClient, other_client: TestClient) -> None:
    sku = _sku()
    item = _create(client, sku=sku)
    archived = _create(client, sku=_sku())
    _, product_id = _competitor(sku=sku.lower(), price="70")

    # A same-SKU product in another workspace must never be matched.
    other_ws, other_source = run_async(create_isolated_workspace_source())
    run_async(_scrape(other_source, sku=sku, price="1"))
    assert other_ws.id

    resp = client.post(f"{PM}/auto-match", json={})
    assert resp.status_code == 200 and resp.json()["created"] >= 1
    matches = client.get(PM, params={"internal_product_id": item["id"]}).json()["data"]
    assert [(m["match_type"], m["product_id"]) for m in matches] == [("sku", str(product_id))]

    # Idempotent.
    assert client.post(f"{PM}/auto-match", json={"internal_product_id": item["id"]}).json() == {
        "created": 0
    }
    assert client.get(PM, params={"internal_product_id": archived["id"]}).json()["data"] == []
    assert other_client.get(PM).json()["data"] == []


# --- price intelligence ----------------------------------------------------


def test_price_intelligence_gap_position_and_map(client: TestClient) -> None:
    item = _create(client, own_price="100.00", map_price="90.00")
    _, p_low = _competitor(sku=None, price="80.00")
    _, p_high = _competitor(sku=None, price="125.00")
    _, p_eur = _competitor(sku=None, price="1.00", currency="EUR")
    for p in (p_low, p_high, p_eur):
        assert (
            client.post(
                PM, json={"internal_product_id": item["id"], "product_id": str(p)}
            ).status_code
            == 201
        )

    detail = client.get(f"{PI}/{item['id']}")
    assert detail.status_code == 200, detail.text
    d = detail.json()
    assert d["price_position"] == "average"
    assert d["competitor_count"] == 3 and d["compared_count"] == 2
    assert d["market"] == {
        "min_price": "80.00",
        "max_price": "125.00",
        "avg_price": "102.50",
        "median_price": "102.50",
    }
    by_price = {c["price"]: c for c in d["competitors"]}
    assert by_price["80.00"]["price_gap"] == "20.00"
    assert by_price["80.00"]["price_gap_pct"] == "25.00"
    assert by_price["80.00"]["map_violation"] is True
    assert by_price["80.00"]["map_gap"] == "10.00"
    assert by_price["125.00"]["price_gap"] == "-25.00"
    assert by_price["125.00"]["price_gap_pct"] == "-20.00"
    assert by_price["125.00"]["map_violation"] is False
    assert [e["reason"] for e in d["excluded"]] == ["currency_mismatch"]
    assert d["map"] == {
        "map_price": "90.00",
        "own_price_below_map": False,
        "violation_count": 1,
        "has_violation": True,
    }

    # Summary list + MAP filter include this product.
    summary = client.get(PI, params={"limit": 100}).json()["data"]
    row = next(r for r in summary if r["internal_product"]["id"] == item["id"])
    assert "competitors" not in row and row["map"]["violation_count"] == 1
    flagged = client.get(PI, params={"map_violation": "true", "limit": 100}).json()["data"]
    assert item["id"] in [r["internal_product"]["id"] for r in flagged]


def test_price_intelligence_map_filter_excludes_clean_products(client: TestClient) -> None:
    clean = _create(client, own_price="100.00", map_price="90.00")
    _, p = _competitor(sku=None, price="95.00")
    client.post(PM, json={"internal_product_id": clean["id"], "product_id": str(p)})
    own_low = _create(client, own_price="80.00", map_price="90.00")
    client.post(PM, json={"internal_product_id": own_low["id"], "product_id": str(p)})

    flagged = {
        r["internal_product"]["id"]
        for r in client.get(PI, params={"map_violation": "true", "limit": 100}).json()["data"]
    }
    assert clean["id"] not in flagged
    assert own_low["id"] in flagged
    res = client.get(f"{PI}/{own_low['id']}").json()
    assert res["map"]["own_price_below_map"] is True and res["price_position"] == "lowest"


def test_price_intelligence_without_matches_or_when_archived(client: TestClient) -> None:
    item = _create(client)
    d = client.get(f"{PI}/{item['id']}").json()
    assert d["price_position"] is None and d["competitors"] == [] and d["compared_count"] == 0
    ids = [
        r["internal_product"]["id"] for r in client.get(PI, params={"limit": 100}).json()["data"]
    ]
    assert item["id"] not in ids  # no matches -> not in the summary list
    assert client.get(f"{PI}/{uuid.uuid4()}").status_code == 404


def test_price_intelligence_tracks_updated_observation(client: TestClient) -> None:
    item = _create(client, own_price="100.00")
    source, product_id = _competitor(sku=None, price="120.00")
    client.post(PM, json={"internal_product_id": item["id"], "product_id": str(product_id)})
    assert client.get(f"{PI}/{item['id']}").json()["price_position"] == "lowest"
    run_async(_scrape(source, sku=None, price="60.00"))
    d = client.get(f"{PI}/{item['id']}").json()
    assert d["price_position"] == "highest"
    assert d["competitors"][0]["price_gap"] == "40.00"
