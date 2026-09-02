"""The Playwright engine's failure handling.

A half-started Playwright poisons every later call with a misleading
"Sync API inside the asyncio loop" error that hides the real cause, so the
cleanup path matters more than the happy path here.
"""
from __future__ import annotations

import pytest

from ebayparts.config import Settings
from ebayparts.fetch import FetchError, Fetcher


@pytest.fixture
def fetcher(tmp_path):
    settings = Settings.load()
    settings.browser_profile_dir = str(tmp_path / "profile")
    settings.max_retries = 1
    f = Fetcher(settings, use_cache=False, engine="playwright")
    f.pacing.delay_seconds = 0
    f.pacing.delay_jitter = 0
    yield f
    f.close()


class _Boom:
    """Stands in for a playwright handle that fails on launch."""

    def __init__(self, error="Executable doesn't exist at /nowhere/chrome"):
        self.error = error
        self.stopped = False

    def start(self):
        return self

    @property
    def chromium(self):
        return self

    def launch_persistent_context(self, **kwargs):
        raise RuntimeError(self.error)

    def stop(self):
        self.stopped = True


def test_failed_launch_is_cleaned_up(fetcher, monkeypatch):
    boom = _Boom()
    monkeypatch.setattr("playwright.sync_api.sync_playwright", lambda: boom)

    with pytest.raises(FetchError) as exc:
        fetcher._ensure_browser()

    assert fetcher._playwright is None, "a half-started playwright was left behind"
    assert fetcher._browser is None
    assert boom.stopped, "playwright was not stopped after a failed launch"
    assert "could not start the browser" in str(exc.value)


def test_missing_browser_gets_an_actionable_hint(fetcher, monkeypatch):
    monkeypatch.setattr("playwright.sync_api.sync_playwright", lambda: _Boom())
    with pytest.raises(FetchError) as exc:
        fetcher._ensure_browser()
    message = str(exc.value)
    assert "playwright install chromium" in message
    assert "browser_executable" in message


def test_a_second_attempt_reports_the_same_real_error(fetcher, monkeypatch):
    """The regression: attempt two used to fail with an unrelated asyncio error."""
    monkeypatch.setattr("playwright.sync_api.sync_playwright", lambda: _Boom())
    messages = []
    for _ in range(2):
        with pytest.raises(FetchError) as exc:
            fetcher._ensure_browser()
        messages.append(str(exc.value))
    assert messages[0] == messages[1]
    assert "asyncio" not in messages[1]


def test_other_launch_errors_have_no_install_hint(fetcher, monkeypatch):
    monkeypatch.setattr("playwright.sync_api.sync_playwright",
                        lambda: _Boom("Target page crashed"))
    with pytest.raises(FetchError) as exc:
        fetcher._ensure_browser()
    assert "playwright install" not in str(exc.value)
    assert "Target page crashed" in str(exc.value)
