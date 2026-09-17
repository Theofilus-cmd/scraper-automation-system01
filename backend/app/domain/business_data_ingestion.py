"""Orchestration for ingesting normalized My Business Hub data records."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable
from app.db.models.business_data_repository import (
    finish_business_data_run,
    get_business_data_run,
    mark_business_data_run_running,
    upsert_business_data_record,
)
from app.db.session import get_session


@dataclass(frozen=True, slots=True)
class BusinessDataIngestionItem:
    """One normalized external record ready for persistence."""

    external_id: str
    fields: dict[str, object]
    captured_at: datetime


@dataclass(frozen=True, slots=True)
class BusinessDataIngestionItemError:
    """One item-level failure that did not stop the rest of a batch."""

    external_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class BusinessDataIngestionResult:
    """Summary of one business-data run after ingestion completes."""

    run_id: uuid.UUID
    status: str
    total_records: int
    succeeded_records: int
    failed_records: int
    errors: tuple[BusinessDataIngestionItemError, ...]


def _validate_item(item: BusinessDataIngestionItem) -> str | None:
    """Return an item error reason, or None when the item is valid."""

    if not isinstance(item.external_id, str) or not item.external_id.strip():
        return "external_id must be a non-empty string"

    if not isinstance(item.fields, dict):
        return "fields must be a dictionary"

    if not isinstance(item.captured_at, datetime):
        return "captured_at must be a datetime"

    if item.captured_at.tzinfo is None or item.captured_at.utcoffset() is None:
        return "captured_at must be timezone-aware"

    return None


def _result(
    *,
    run_id: uuid.UUID,
    status: str,
    total_records: int,
    succeeded_records: int,
    errors: list[BusinessDataIngestionItemError],
) -> BusinessDataIngestionResult:
    return BusinessDataIngestionResult(
        run_id=run_id,
        status=status,
        total_records=total_records,
        succeeded_records=succeeded_records,
        failed_records=len(errors),
        errors=tuple(errors),
    )

async def _mark_run_failed_after_fatal_error(
    run_id: uuid.UUID,
    *,
    workspace_id: uuid.UUID,
    reason: str,
    total_records: int,
) -> None:
    """Best-effort terminal failure after the batch transaction rolls back."""

    async with get_session() as session:
        failed = await finish_business_data_run(
            session,
            run_id,
            workspace_id=workspace_id,
            status="failed",
            total_records=total_records,
            succeeded_records=0,
            failed_records=total_records,
            error_reason=reason,
            error_detail={"message": reason},
        )
        if failed is not None:
            await session.commit()


async def ingest_business_data_records(
    run_id: uuid.UUID,
    *,
    workspace_id: uuid.UUID,
    items: Iterable[BusinessDataIngestionItem],
) -> BusinessDataIngestionResult:
    """Persist normalized items and finalize their owning business-data run.

    The transition from pending to running commits first. The record writes
    and terminal run status then commit together, so a fatal batch error
    leaves no partial record changes and is finalized as failed separately.
    """

    items = tuple(items)

    async with get_session() as session:
        run = await get_business_data_run(
            session,
            run_id,
            workspace_id=workspace_id,
        )
        if run is None:
            return _result(
                run_id=run_id,
                status="failed",
                total_records=0,
                succeeded_records=0,
                errors=[
                    BusinessDataIngestionItemError(
                        external_id="",
                        reason="business data run not found",
                    )
                ],
            )

        started = await mark_business_data_run_running(
            session,
            run_id,
            workspace_id=workspace_id,
        )
        if started is None:
            return _result(
                run_id=run_id,
                status=run.status,
                total_records=0,
                succeeded_records=0,
                errors=[
                    BusinessDataIngestionItemError(
                        external_id="",
                        reason="business data run is not pending",
                    )
                ],
            )

        source_id = started.business_data_source_id
        await session.commit()
    errors: list[BusinessDataIngestionItemError] = []
    succeeded_records = 0

    try:
        async with get_session() as session:
            for item in items:
                item_error = _validate_item(item)
                if item_error is not None:
                    errors.append(
                        BusinessDataIngestionItemError(
                            external_id=(
                                item.external_id
                                if isinstance(item.external_id, str)
                                else ""
                            ),
                            reason=item_error,
                        )
                    )
                    continue

                persisted = await upsert_business_data_record(
                    session,
                    source_id,
                    workspace_id=workspace_id,
                    run_id=run_id,
                    external_id=item.external_id.strip(),
                    fields=item.fields,
                    captured_at=item.captured_at,
                )

                if persisted is None:
                    raise RuntimeError("run or source ownership validation failed")

                succeeded_records += 1

            status = "completed_with_errors" if errors else "completed"
            finished = await finish_business_data_run(
                session,
                run_id,
                workspace_id=workspace_id,
                status=status,
                total_records=len(items),
                succeeded_records=succeeded_records,
                failed_records=len(errors),
                error_reason=(
                    "one or more records failed to persist" if errors else None
                ),
                error_detail=(
                    {
                        "item_errors": [
                            {
                                "external_id": error.external_id,
                                "reason": error.reason,
                            }
                            for error in errors
                        ]
                    }
                    if errors
                    else None
                ),
            )
            if finished is None:
                raise RuntimeError("business data run could not be finalized")

            await session.commit()

    except Exception as exc:
        await _mark_run_failed_after_fatal_error(
            run_id,
            workspace_id=workspace_id,
            reason=str(exc) or exc.__class__.__name__,
            total_records=len(items),
        )
        return _result(
            run_id=run_id,
            status="failed",
            total_records=len(items),
            succeeded_records=0,
            errors=[
                BusinessDataIngestionItemError(
                    external_id="",
                    reason=str(exc) or exc.__class__.__name__,
                )
            ],
        )

    return _result(
        run_id=run_id,
        status=status,
        total_records=len(items),
        succeeded_records=succeeded_records,
        errors=errors,
    )
