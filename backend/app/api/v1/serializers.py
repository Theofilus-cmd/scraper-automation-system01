"""Shared dict-serialization helpers for doc 18's new endpoints -- one
place so every router produces the same field shapes (Decimal -> str,
datetime -> isoformat, uuid.UUID -> str) rather than each hand-rolling it,
matching the conversions `app/workers/tasks_http.py::_jsonify_observation()`
already established for the legacy endpoint.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from app.db.models.alerts import Alert
from app.db.models.intelligence import InternalProduct, ProductMatch
from app.db.models.lifecycle import ObservationHistory, Run, Schedule, Task
from app.db.models.scraping import CurrentObservation, Product, Source
from app.domain.price_intelligence import IntelligenceResult


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _num(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def source_to_dict(source: Source) -> dict[str, Any]:
    return {
        "id": str(source.id),
        "url": source.url,
        "normalized_url": source.normalized_url,
        "adapter_type": source.adapter_type,
        "status": source.status,
        "created_at": _iso(source.created_at),
        "updated_at": _iso(source.updated_at),
    }


def schedule_to_dict(schedule: Schedule) -> dict[str, Any]:
    return {
        "id": str(schedule.id),
        "source_id": str(schedule.source_id),
        "interval_minutes": schedule.interval_minutes,
        "is_active": schedule.is_active,
        "next_run_at": _iso(schedule.next_run_at),
        "last_run_at": _iso(schedule.last_run_at),
        "created_at": _iso(schedule.created_at),
        "updated_at": _iso(schedule.updated_at),
    }


def run_to_dict(run: Run) -> dict[str, Any]:
    return {
        "id": str(run.id),
        "source_id": str(run.source_id),
        "schedule_id": str(run.schedule_id) if run.schedule_id is not None else None,
        "status": run.status,
        "triggered_by": run.triggered_by,
        "total_tasks": run.total_tasks,
        "succeeded_tasks": run.succeeded_tasks,
        "failed_tasks": run.failed_tasks,
        "started_at": _iso(run.started_at),
        "finished_at": _iso(run.finished_at),
        "created_at": _iso(run.created_at),
    }


def task_to_dict(task: Task) -> dict[str, Any]:
    return {
        "id": str(task.id),
        "run_id": str(task.run_id),
        "source_id": str(task.source_id),
        "status": task.status,
        "attempt_count": task.attempt_count,
        "max_attempts": task.max_attempts,
        "error_reason": task.error_reason,
        "error_detail": task.error_detail,
        "queued_at": _iso(task.queued_at),
        "started_at": _iso(task.started_at),
        "finished_at": _iso(task.finished_at),
    }


def product_to_dict(product: Product) -> dict[str, Any]:
    return {
        "id": str(product.id),
        "source_id": str(product.source_id),
        "product_identity_key": product.product_identity_key,
        "product_url": product.product_url,
        "sku": product.sku,
        "source_product_id": product.source_product_id,
        "created_at": _iso(product.created_at),
        "updated_at": _iso(product.updated_at),
    }


def current_observation_to_dict(observation: CurrentObservation) -> dict[str, Any]:
    return {
        "product_name": observation.product_name,
        "brand": observation.brand,
        "category": observation.category,
        "price": _num(observation.price),
        "currency": observation.currency,
        "original_price": _num(observation.original_price),
        "discount": _num(observation.discount),
        "variant": observation.variant,
        "stock_status": observation.stock_status,
        "rating": _num(observation.rating),
        "review_count": observation.review_count,
        "description": observation.description,
        "image_url": observation.image_url,
        "is_valid": observation.is_valid,
        "validation_errors": observation.validation_errors,
        "scraped_at": _iso(observation.scraped_at),
    }


def observation_history_to_dict(row: ObservationHistory) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "product_id": str(row.product_id),
        "run_id": str(row.run_id),
        "task_id": str(row.task_id),
        "product_name": row.product_name,
        "brand": row.brand,
        "category": row.category,
        "price": _num(row.price),
        "currency": row.currency,
        "original_price": _num(row.original_price),
        "discount": _num(row.discount),
        "variant": row.variant,
        "stock_status": row.stock_status,
        "rating": _num(row.rating),
        "review_count": row.review_count,
        "description": row.description,
        "image_url": row.image_url,
        "is_valid": row.is_valid,
        "validation_errors": row.validation_errors,
        "scraped_at": _iso(row.scraped_at),
        "change_summary": row.change_summary,
        "version_created_at": _iso(row.version_created_at),
    }


def alert_to_dict(alert: Alert) -> dict[str, Any]:
    return {
        "id": str(alert.id),
        "source_id": str(alert.source_id),
        "product_id": str(alert.product_id),
        "run_id": str(alert.run_id) if alert.run_id is not None else None,
        "task_id": str(alert.task_id) if alert.task_id is not None else None,
        "kind": alert.kind,
        "product_name": alert.product_name,
        "product_url": alert.product_url,
        "currency": alert.currency,
        "old_value": alert.old_value,
        "new_value": alert.new_value,
        "email_status": alert.email_status,
        "sent_at": _iso(alert.sent_at),
        "created_at": _iso(alert.created_at),
    }


def internal_product_to_dict(item: InternalProduct) -> dict[str, Any]:
    return {
        "id": str(item.id),
        "name": item.name,
        "sku": item.sku,
        "barcode": item.barcode,
        "brand": item.brand,
        "category": item.category,
        "own_price": _num(item.own_price),
        "map_price": _num(item.map_price),
        "currency": item.currency,
        "status": item.status,
        "created_at": _iso(item.created_at),
        "updated_at": _iso(item.updated_at),
    }


def _competitor_dict(row: dict[str, Any]) -> dict[str, Any]:
    product, source, obs = row["product"], row["source"], row["observation"]
    return {
        "product_id": str(product.id),
        "source_id": str(source.id),
        "adapter_type": source.adapter_type,
        "product_url": product.product_url,
        "sku": product.sku,
        "product_name": obs.product_name if obs is not None else None,
        "price": _num(obs.price) if obs is not None else None,
        "currency": obs.currency if obs is not None else None,
        "stock_status": obs.stock_status if obs is not None else None,
        "is_valid": obs.is_valid if obs is not None else None,
        "scraped_at": _iso(obs.scraped_at) if obs is not None else None,
    }


def match_to_dict(match: ProductMatch, row: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "id": str(match.id),
        "internal_product_id": str(match.internal_product_id),
        "product_id": str(match.product_id),
        "match_type": match.match_type,
        "competitor": _competitor_dict(row) if row is not None else None,
        "created_at": _iso(match.created_at),
        "updated_at": _iso(match.updated_at),
    }


def intelligence_to_dict(
    item: InternalProduct,
    result: IntelligenceResult,
    rows: list[dict[str, Any]],
    *,
    detail: bool,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "internal_product": internal_product_to_dict(item),
        "competitor_count": len(rows),
        "compared_count": len(result.comparisons),
        "price_position": result.price_position,
        "market": {
            "min_price": _num(result.min_price),
            "max_price": _num(result.max_price),
            "avg_price": _num(result.avg_price),
            "median_price": _num(result.median_price),
        },
        "map": {
            "map_price": _num(item.map_price),
            "own_price_below_map": result.own_price_below_map,
            "violation_count": result.map_violation_count,
            "has_violation": result.own_price_below_map or result.map_violation_count > 0,
        },
    }
    if detail:
        by_match = {str(r["match"].id): r for r in rows}
        out["competitors"] = [
            {
                "match_id": c.match_id,
                "match_type": by_match[c.match_id]["match"].match_type,
                **_competitor_dict(by_match[c.match_id]),
                "price_gap": _num(c.price_gap),
                "price_gap_pct": _num(c.price_gap_pct),
                "map_violation": c.map_violation,
                "map_gap": _num(c.map_gap),
            }
            for c in result.comparisons
        ]
        out["excluded"] = [
            {
                "match_id": e.match_id,
                "reason": e.reason,
                **_competitor_dict(by_match[e.match_id]),
            }
            for e in result.excluded
        ]
    return out
