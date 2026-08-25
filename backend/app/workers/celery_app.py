"""Celery application definition shared by all worker processes.

Each worker process (worker-http, worker-browser, worker-notifications,
worker-exports) boots this same Celery app but is started with `-Q
<queue-name>` so it only pulls tasks from its own queue. The routing
below is what maps each task module to its queue.

Acceptance-review fix (real mypy errors, real Docker/Postgres run):
`celery.*` has no installable stub package this project can pin (see
this repo's own `pyproject.toml`, `[[tool.mypy.overrides]]`, `module =
"celery.*"`, `ignore_missing_imports = true` -- that override exists
specifically because none exists), so every attribute off `celery_app`,
including `.task`, resolves as `Any` under mypy. Decorating a function
directly with `@celery_app.task(...)` therefore makes the decorated
function untyped end to end: `disallow_untyped_decorators` (part of this
project's `strict = true`) reports "Untyped decorator makes function ...
untyped" at every such site -- six of them, across four task modules,
all fixed the same way rather than six separate local suppressions.

Acceptance-review correction to the first version of this fix: an
earlier revision of `typed_task` cast the decorated name straight to
`Callable[_P, _R]` -- i.e. the ORIGINAL undecorated function's own
signature. That is a real, substantive type error, not just an
imprecision, caught in review before it shipped: `celery_app.task(...)`
does not return the original function at all -- it returns a Celery
`Task` object, a genuinely different runtime type with its own
`__call__`/`.delay`/`.apply_async` interface. For a `bind=True` task
(`scrape_source_url` below), the wrapped function's own first parameter
is the `self: CeleryTask` Celery injects internally; no real caller --
not this codebase's own tests, not `.delay()`, not Celery's dispatch
machinery -- ever supplies that argument. Casting to `Callable[_P, _R]`
therefore asserted two false things at once: that callers must pass a
`CeleryTask` as the first argument (they never do and never can), and
that no `.delay`/`.apply_async` exist on the result (they do, and are
the real way this codebase's dispatch path -- `celery_app.send_task`,
by name -- and any future direct-reference caller would use them). A
`cast()` that asserts something false is worse than the untyped-decorator
error it silences: it would have hidden a genuine mismatch at exactly
the boundary this fix exists to make safe, instead of catching one.

`CeleryTaskHandle` and the two wrappers below fix this by modeling the
REAL returned interface instead of the original function:

  - `CeleryTaskHandle[_P, _R]` is a `Protocol`, generic over `_P`/`_R`,
    declaring exactly the slice of Celery's real `Task` interface this
    codebase's callers use: calling it directly (`__call__`), and
    `.delay(...)` -- both typed with the same `_P`, i.e. the PUBLIC
    calling convention with any injected `self` already excluded -- plus
    `.apply_async(...)`, typed more loosely (`Any`-ish `args`/`kwargs`/
    `**options`) since this codebase never calls it and does not need
    that call's shape pinned precisely to be honest about its existence.
  - `typed_task` is for a plain (unbound) task: the wrapped function's
    own `Callable[_P, _R]` already has no `self` to strip, so `_P` is
    reused as-is.
  - `typed_bound_task` is for a `bind=True` task: the wrapped function's
    type is declared `Callable[Concatenate[CeleryTask, _P], _R]` --
    its real first parameter genuinely is a `CeleryTask` -- while the
    returned `CeleryTaskHandle[_P, _R]` is callable with only `_P`,
    correctly modeling that Celery supplies `self` internally and no
    caller ever does.

Both still call the real, untyped `celery_app.task(*args, **kwargs)`
in exactly one place each, `cast()` to `CeleryTaskHandle[_P, _R]` --
a real, true fact about what Celery returns, not an assertion about the
original function's own shape. At runtime nothing changes: same real
Celery `Task`, same registration, same `.delay`/`.apply_async`/bound-
`self` behavior; only the *static* type is now the real one.
"""

from collections.abc import Callable
from typing import Any, Concatenate, ParamSpec, Protocol, TypeVar, cast

from celery import Celery
from celery import Task as CeleryTask

from app.core.config import get_settings
from app.core.logging import configure_logging

configure_logging()

settings = get_settings()

celery_app = Celery(
    "scraper_automation_system",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=[
        "app.workers.tasks_http",
        "app.workers.tasks_browser",
        "app.workers.tasks_notifications",
        "app.workers.tasks_exports",
    ],
)

celery_app.conf.task_routes = {
    "app.workers.tasks_http.*": {"queue": "http"},
    "app.workers.tasks_browser.*": {"queue": "browser"},
    "app.workers.tasks_notifications.*": {"queue": "notifications"},
    "app.workers.tasks_exports.*": {"queue": "exports"},
}

celery_app.conf.task_default_queue = "http"
celery_app.conf.timezone = "UTC"
celery_app.conf.worker_hijack_root_logger = False

_P = ParamSpec("_P")
# covariant=True: `_R` is used only in return position throughout
# `CeleryTaskHandle` below (never as a parameter type there) -- mypy's
# own protocol-variance check requires a return-only type variable on a
# Protocol to be declared covariant. Safe here for the same reason it's
# required: nothing in this file ever needs `_R` to accept a value, only
# to produce one.
_R = TypeVar("_R", covariant=True)


class CeleryTaskHandle(Protocol[_P, _R]):
    """The real, public interface of a Celery `Task` after `@app.task(...)`
    decoration -- see this module's docstring for why this exists instead
    of casting to the original decorated function's own signature. `_P`
    here is always the PUBLIC calling convention: any `bind=True` `self`
    is already excluded by the caller (`typed_bound_task` below), never
    part of `_P`. Deliberately minimal -- only the interface this
    codebase's own tests and dispatch path actually use, not Celery's
    entire real `Task` API surface.
    """

    def __call__(self, *args: _P.args, **kwargs: _P.kwargs) -> _R: ...

    def delay(self, *args: _P.args, **kwargs: _P.kwargs) -> Any: ...

    def apply_async(
        self,
        args: tuple[Any, ...] = (),
        kwargs: dict[str, Any] | None = None,
        **options: Any,
    ) -> Any: ...


def typed_task(
    *task_args: Any, **task_kwargs: Any
) -> Callable[[Callable[_P, _R]], CeleryTaskHandle[_P, _R]]:
    """For a plain (unbound) task -- see this module's docstring. Every
    `tasks_*.py` module's non-`bind=True` task uses `@typed_task(...)`
    in place of `@celery_app.task(...)`.
    """

    def decorator(func: Callable[_P, _R]) -> CeleryTaskHandle[_P, _R]:
        # The one real, untyped call this wrapper exists to contain -- see
        # module docstring. A real Celery Task is still what's actually
        # returned and registered at runtime; only mypy's static view of
        # it changes here, to the real interface, not the original
        # function's.
        return cast(
            CeleryTaskHandle[_P, _R], celery_app.task(*task_args, **task_kwargs)(func)
        )

    return decorator


def typed_bound_task(
    *task_args: Any, **task_kwargs: Any
) -> Callable[[Callable[Concatenate[CeleryTask, _P], _R]], CeleryTaskHandle[_P, _R]]:
    """For a `bind=True` task -- see this module's docstring. The wrapped
    function's own first parameter is the injected `CeleryTask` `self`
    (`Concatenate[CeleryTask, _P]`); the returned handle is callable with
    only `_P` -- `self` is supplied by Celery internally, never by any
    caller.
    """

    def decorator(
        func: Callable[Concatenate[CeleryTask, _P], _R],
    ) -> CeleryTaskHandle[_P, _R]:
        # Same one real, untyped call as `typed_task` above -- see module
        # docstring.
        return cast(
            CeleryTaskHandle[_P, _R], celery_app.task(*task_args, **task_kwargs)(func)
        )

    return decorator
