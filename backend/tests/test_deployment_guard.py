"""Pure unit tests for app/main.py's production deployment guard (doc 18
§7.3, amendment 7): _enforce_production_deployment_guard() itself, plus a
thin proof that create_app() really wires it in before constructing the
FastAPI app. No DB, no network, no shared state -- see below for why the
real get_settings() singleton is deliberately never touched.

app/core/config.py::get_settings() is @lru_cache-decorated and was already
called once for real at `app.main` import time (`app = create_app()` at
module scope -- conftest.py's own `from app.main import app` already
triggers this, using whatever real environment this container has).
Mutating os.environ after that point would never reach the warm cache, so
every test below does one of two things instead: constructs a
`Settings(...)` object directly and passes it straight into the guard
function (every field has a default and explicit kwargs override
env/dotenv loading, so this is zero I/O and leaves nothing to clean up),
or -- to prove create_app()'s own wiring, not just the guard function in
isolation -- monkeypatches the name `app.main.get_settings` itself
(function-scoped, auto-reverting) rather than the cache or the
environment.
"""

import pytest
from fastapi import FastAPI

from app.core.config import Settings
from app.main import _enforce_production_deployment_guard, create_app


def test_production_without_ack_raises_with_expected_message() -> None:
    settings = Settings(environment="production", unauthenticated_source_api_ack=False)

    with pytest.raises(RuntimeError) as exc_info:
        _enforce_production_deployment_guard(settings)

    message = str(exc_info.value)
    assert "UNAUTHENTICATED_SOURCE_API_ACK" in message
    # Proves the guard's deliberate extension beyond doc 18 §7.3's literal
    # sources/runs/products text to also cover the legacy scrapes alias
    # (app/main.py's own docstring on this function explains why).
    assert "/api/v1/scrapes" in message


def test_production_with_ack_does_not_raise() -> None:
    settings = Settings(environment="production", unauthenticated_source_api_ack=True)

    _enforce_production_deployment_guard(settings)  # must not raise


@pytest.mark.parametrize("ack", [True, False])
def test_non_production_environment_is_inert_regardless_of_ack(ack: bool) -> None:
    """The guard must be a strict no-op outside environment="production" --
    proven for both ack values, since it's specifically the *combination*
    of production + unacked that's disallowed (doc 18 §7.3).
    """
    settings = Settings(environment="development", unauthenticated_source_api_ack=ack)

    _enforce_production_deployment_guard(settings)  # must not raise


def test_create_app_raises_when_wired_settings_are_production_unacked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Proves create_app() itself calls the guard (using whatever
    get_settings() returns) before ever constructing the FastAPI app --
    distinct from the direct-call tests above, which only prove the guard
    function's own logic in isolation.
    """
    monkeypatch.setattr(
        "app.main.get_settings",
        lambda: Settings(environment="production", unauthenticated_source_api_ack=False),
    )

    with pytest.raises(RuntimeError):
        create_app()


def test_create_app_returns_a_fastapi_instance_when_wired_settings_are_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.main.get_settings",
        lambda: Settings(environment="development", unauthenticated_source_api_ack=False),
    )

    result = create_app()

    assert isinstance(result, FastAPI)
