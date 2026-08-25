"""Re-exports so `from app.db.models import Base, Source, ...` works, and so
that importing this package (directly, or transitively via
`app.db.models.base`) always registers every ORM model on `Base.metadata`
-- required for `app/db/migrations/env.py`'s `target_metadata` to see the
real table definitions.
"""

from app.db.models.base import Base
from app.db.models.scraping import CurrentObservation, Product, Source

__all__ = ["Base", "CurrentObservation", "Product", "Source"]
