"""Static audit: every `extra={...}` dict literal passed to a logging
call anywhere under app/ must use keys that don't collide with a stdlib
`logging.LogRecord` attribute (doc 17 hotfix: `created` in
repository.py's success log call crashed every valid scrape with
KeyError: "Attempt to overwrite 'created' in LogRecord", AFTER the
database write had already succeeded).

Walks the actual source tree via `ast` -- catches any future occurrence
of this bug class anywhere in the app, automatically, without needing a
live logging call, a live database, or even this project's third-party
dependencies installed (this file only uses the stdlib), so it runs
everywhere pytest can run at all -- including this generation sandbox,
where it was actually executed (not just written) before delivery.
"""

import ast
from collections.abc import Iterator
from pathlib import Path

# Empirically confirmed against a real Python 3.12 interpreter
# (logging.Logger.makeRecord()'s own reserved-key check), not just
# recalled from the LogRecord.__init__ source -- Python 3.12 added
# "taskName" (asyncio task-name tracking) to this set, which is easy to
# miss from memory alone.
_RESERVED_LOGRECORD_KEYS = frozenset(
    {
        "name",
        "msg",
        "args",
        "levelname",
        "levelno",
        "pathname",
        "filename",
        "module",
        "exc_info",
        "exc_text",
        "stack_info",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "threadName",
        "processName",
        "process",
        "taskName",
        # Not in LogRecord.__dict__ at construction time (set later, by
        # formatting) but special-cased by makeRecord() and just as fatal.
        "message",
        "asctime",
    }
)

_BACKEND_DIR = Path(__file__).resolve().parent.parent
_APP_DIR = _BACKEND_DIR / "app"


def _iter_extra_dict_key_nodes(tree: ast.AST) -> Iterator[ast.expr]:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if keyword.arg != "extra" or not isinstance(keyword.value, ast.Dict):
                continue
            for key_node in keyword.value.keys:
                if key_node is not None:
                    yield key_node


def _find_violations() -> list[str]:
    violations: list[str] = []
    python_files = sorted(_APP_DIR.rglob("*.py"))
    assert python_files, f"expected to find .py files under {_APP_DIR}, found none"

    for path in python_files:
        tree = ast.parse(path.read_text(), filename=str(path))
        for key_node in _iter_extra_dict_key_nodes(tree):
            if isinstance(key_node, ast.Constant) and isinstance(key_node.value, str):
                if key_node.value in _RESERVED_LOGRECORD_KEYS:
                    violations.append(
                        f"{path.relative_to(_BACKEND_DIR)}:{key_node.lineno}: "
                        f"extra key {key_node.value!r} collides with a reserved "
                        f"logging.LogRecord attribute"
                    )
    return violations


def test_no_logging_extra_dict_uses_a_reserved_logrecord_key() -> None:
    violations = _find_violations()
    assert not violations, (
        "Reserved LogRecord key(s) found in extra={...} calls:\n" + "\n".join(violations)
    )


def test_the_audit_itself_actually_detects_a_known_bad_key() -> None:
    """A test for the test: proves _find_violations()'s detection logic
    isn't vacuously passing (e.g. because the glob silently matched
    nothing). Parses a small synthetic snippet containing the exact bug
    this hotfix fixed, independent of the real source tree.
    """
    tree = ast.parse('logger.info("x", extra={"created": True, "safe_key": 1})')
    keys = [
        key_node.value
        for key_node in _iter_extra_dict_key_nodes(tree)
        if isinstance(key_node, ast.Constant)
    ]
    assert "created" in keys
    assert "safe_key" in keys
    assert any(k in _RESERVED_LOGRECORD_KEYS for k in keys)
    assert not all(k in _RESERVED_LOGRECORD_KEYS for k in keys)
