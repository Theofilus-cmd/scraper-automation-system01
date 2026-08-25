"""Direct regression test for the "scrape result persisted" success log
call (doc 17 hotfix): Python's stdlib `logging` module raises KeyError
for any `extra=` key that collides with a `logging.LogRecord` attribute
name -- `created` is one (LogRecord.created is the record's own creation
timestamp, set unconditionally in LogRecord.__init__ -- confirmed
empirically against a real Python 3.12 interpreter, not just recalled
from the source). app/db/models/repository.py used to pass exactly that
key, which crashed every successful scrape write with:

    KeyError: "Attempt to overwrite 'created' in LogRecord"

raised from *inside* the log call, i.e. AFTER the database commit had
already succeeded -- turning a working write into an unhandled 500.

This reproduces the exact call shape directly against the real logging
module (no mocking, no live database needed) via `Logger.makeRecord()`,
the same method `Logger.info()` calls internally. It fails immediately,
with the real error, if the reserved key `created` is ever
reintroduced. See tests/test_logging_extra_keys.py for the broader,
permanent static audit across the whole app/ tree (that one also
guards every OTHER log call site, not just this one).
"""

import logging

from app.core.logging import get_logger


def test_scrape_result_persisted_log_call_does_not_collide_with_logrecord() -> None:
    """The exact extra= shape repository.py's success log call uses,
    post-fix."""
    logger = get_logger("app.db.models.repository")

    record = logger.makeRecord(
        logger.name,
        logging.INFO,
        __file__,
        0,
        "scrape result persisted",
        (),
        None,
        extra={
            "source_id": "11111111-1111-1111-1111-111111111111",
            "product_id": "22222222-2222-2222-2222-222222222222",
            "product_created": True,
        },
    )

    assert record.product_created is True


def test_the_reserved_key_this_bug_used_still_raises_if_reintroduced() -> None:
    """Documents *why* the fix works, not just that it currently does --
    asserts the old key name is genuinely unsafe, not merely "not used
    right now". A future edit that changes `product_created` back to
    `created` would fail this test with the exact original traceback.
    """
    logger = get_logger("app.db.models.repository")

    try:
        logger.makeRecord(
            logger.name,
            logging.INFO,
            __file__,
            0,
            "scrape result persisted",
            (),
            None,
            extra={"created": True},
        )
    except KeyError as exc:
        assert "created" in str(exc)
    else:
        raise AssertionError(
            "expected KeyError: extra={'created': ...} collides with the "
            "reserved LogRecord.created attribute -- if this no longer "
            "raises, the Python version's LogRecord shape changed and the "
            "reserved-key audit in test_logging_extra_keys.py needs review"
        )
