"""Fast, DB-less regression test for the timestamp timezone bug (doc 17
hotfix): every `Mapped[datetime]` column across the Phase 1 ORM models
must compile to a timezone-aware SQL type (`TIMESTAMP WITH TIME ZONE` /
`timestamptz`), matching migration `0002`'s actual DDL
(`sa.TIMESTAMP(timezone=True)`) and doc 05 §1's convention.

Before the `Base.type_annotation_map` fix (app/db/models/base.py), a bare
`Mapped[datetime]` fell back to SQLAlchemy's own default mapping for
`datetime.datetime`, which is a *naive* `DateTime()` (timezone=False) --
a client-side-only mismatch against the real timestamptz columns that
only surfaced once a timezone-aware Python value (this codebase uses
`datetime.now(UTC)` throughout) was actually bound as a parameter. This
test inspects the mapped `Table` objects directly and would have failed
before that fix, without needing a live Postgres to prove it.
"""

from app.db.models.business_data import (
    BusinessDataRecord,
    BusinessDataRecordHistory,
    BusinessDataRun,
    BusinessDataSchedule,
    BusinessDataSource,
)
from app.db.models.scraping import CurrentObservation, Product, Source

_DATETIME_COLUMNS = {
    Source: ("created_at", "updated_at"),
    Product: ("created_at", "updated_at"),
    CurrentObservation: ("created_at", "updated_at", "scraped_at"),
    BusinessDataSource: ("created_at", "updated_at"),
    BusinessDataSchedule: (
        "created_at",
        "updated_at",
        "next_run_at",
        "last_run_at",
    ),
    BusinessDataRun: ("created_at", "started_at", "finished_at"),
    BusinessDataRecord: ("created_at", "updated_at", "captured_at"),
    BusinessDataRecordHistory: ("captured_at", "version_created_at"),
}

def test_all_datetime_columns_are_timezone_aware() -> None:
    for model, column_names in _DATETIME_COLUMNS.items():
        table = model.__table__
        for column_name in column_names:
            column = table.c[column_name]
            assert column.type.timezone is True, (
                f"{model.__name__}.{column_name} must be timezone-aware "
                f"(DateTime(timezone=True) / timestamptz) -- got "
                f"timezone={column.type.timezone!r}"
            )
