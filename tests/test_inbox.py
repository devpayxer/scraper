"""Importing pages saved from a browser.

This is the fallback when eBay refuses automated requests, so it has to be
honest about what it did and did not manage to read -- silently importing
nothing from a captcha page would be the worst outcome.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from ebayparts.inbox import find_pages, import_folder, import_page
from ebayparts.store import Store

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "inbox.db") as s:
        yield s


@pytest.fixture
def inbox(tmp_path):
    folder = tmp_path / "inbox"
    folder.mkdir()
    return folder


def place(inbox: Path, fixture: str, name: str) -> Path:
    target = inbox / name
    shutil.copy(FIXTURES / fixture, target)
    return target


class TestFindPages:
    def test_picks_up_html_only(self, inbox):
        (inbox / "a.html").write_text("x")
        (inbox / "b.htm").write_text("x")
        (inbox / "notes.txt").write_text("x")
        (inbox / "sheet.csv").write_text("x")
        assert {p.name for p in find_pages(inbox)} == {"a.html", "b.htm"}

    def test_skips_the_archive_folder(self, inbox):
        (inbox / "new.html").write_text("x")
        done = inbox / "imported" / "2026-09-02"
        done.mkdir(parents=True)
        (done / "old.html").write_text("x")
        assert [p.name for p in find_pages(inbox)] == ["new.html"]

    def test_missing_folder_is_not_an_error(self, tmp_path):
        assert find_pages(tmp_path / "nope") == []


class TestImportPage:
    def test_imports_a_saved_results_page(self, inbox, store):
        path = place(inbox, "sold_classic.html", "saved.html")
        result = import_page(path, store)
        assert result.status == "ok"
        assert result.listings == 3
        assert result.new == 3
        assert result.sellers == ["spartan_auto"]

    def test_a_captcha_page_is_reported_not_silently_skipped(self, inbox, store):
        path = place(inbox, "blocked_challenge.html", "oops.html")
        result = import_page(path, store)
        assert result.status == "challenge"
        assert "captcha" in result.note or "interstitial" in result.note
        assert store.count() == 0

    def test_an_unrelated_page_is_reported(self, inbox, store):
        path = inbox / "random.html"
        path.write_text("<html><body>hello</body></html>", encoding="utf-8")
        result = import_page(path, store)
        assert result.status == "empty"
        assert result.listings == 0

    def test_reimporting_the_same_page_adds_nothing(self, inbox, store):
        path = place(inbox, "sold_classic.html", "saved.html")
        assert import_page(path, store).new == 3
        assert import_page(path, store).new == 0
        assert store.count() == 3


class TestImportFolder:
    def test_mixed_folder(self, inbox, store):
        place(inbox, "sold_classic.html", "one.html")
        place(inbox, "sold_new_layout.html", "two.html")
        place(inbox, "blocked_challenge.html", "bad.html")
        results = import_folder(inbox, store)
        assert sum(r.new for r in results) == 5
        assert {r.status for r in results} == {"ok", "challenge"}
        assert store.count() == 5

    def test_archive_moves_only_successful_files(self, inbox, store):
        place(inbox, "sold_classic.html", "good.html")
        place(inbox, "blocked_challenge.html", "bad.html")
        import_folder(inbox, store, archive=True)
        remaining = {p.name for p in find_pages(inbox)}
        assert remaining == {"bad.html"}, "a failed page must stay for a retry"
        assert list((inbox / "imported").rglob("good.html"))

    def test_empty_folder(self, inbox, store):
        assert import_folder(inbox, store) == []
