"""A stored catalog records what one identity offered, and when it was collected."""

from __future__ import annotations

import json
import os
import stat
import tempfile
from datetime import datetime
from pathlib import Path

from raychat.model_catalog import (
    ModelCatalog,
    catalog_matches,
    catalog_path,
    catalogs_directory,
    collected_now,
    discovered_catalog,
    load_catalog,
    save_catalog,
)
from raychat.user_info import SCHEMA_VERSION
from raychat.validation import json_object, object_field
from tests.assertions import TypedTestCase

_FIXTURE_URL = "https://provider.example/v1"
_OTHER_URL = "https://other-provider.example/v1"


def _written(directory: str, payload: object) -> Path:
    target = Path(directory) / "work.json"
    target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return target


def _catalog(**changes: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "nickname": "Work",
        "base_url": _FIXTURE_URL,
        "models": ["alpha", "beta"],
        "fetched_at": collected_now(),
    }
    payload.update(changes)
    return payload


class CatalogStorageTests(TypedTestCase):
    """Check the round trip and the separation from stored credentials."""

    def test_absent_catalog_reports_nothing_collected(self) -> None:
        """A profile that has never been refreshed is an ordinary state."""
        with tempfile.TemporaryDirectory() as directory:
            self.equal(load_catalog(Path(directory) / "missing.json"), None)

    def test_round_trip_preserves_the_identifiers_a_provider_returned(self) -> None:
        """The catalog is the provider's answer; do not reorder or reinterpret it."""
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "work.json"
            stamp = collected_now()
            save_catalog(
                ModelCatalog(
                    nickname="Work",
                    base_url=_FIXTURE_URL,
                    models=("zeta", "alpha", "beta"),
                    fetched_at=stamp,
                ),
                target,
            )
            stored = load_catalog(target)
            if stored is None:
                self.fail("Expected the saved catalog to load.")
            else:
                self.equal(stored.models, ("zeta", "alpha", "beta"))
                self.equal(stored.base_url, _FIXTURE_URL)
                self.equal(stored.nickname, "Work")
                self.equal(stored.fetched_at, stamp)

    def test_duplicate_identifiers_are_collapsed_in_order(self) -> None:
        """Providers repeat entries; store each once without resorting the list."""
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "work.json"
            save_catalog(
                ModelCatalog(
                    nickname="Work",
                    base_url=_FIXTURE_URL,
                    models=("beta", "alpha", "beta"),
                    fetched_at=collected_now(),
                ),
                target,
            )
            stored = load_catalog(target)
            if stored is None:
                self.fail("Expected the saved catalog to load.")
            else:
                self.equal(stored.models, ("beta", "alpha"))

    def test_a_catalog_never_stores_a_credential(self) -> None:
        """Refreshing a model list must not copy the token into a second file."""
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "work.json"
            save_catalog(
                ModelCatalog(
                    nickname="Work",
                    base_url=_FIXTURE_URL,
                    models=("alpha",),
                    fetched_at=collected_now(),
                ),
                target,
            )
            written = object_field(json_object(target.read_bytes()), "stored")
            self.equal(
                sorted(written),
                ["base_url", "fetched_at", "models", "nickname", "schema_version"],
            )

    def test_stored_catalog_is_readable_only_by_its_owner(self) -> None:
        """Model lists reveal an account's entitlements; keep them owner-only."""
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "catalogs" / "work.json"
            saved = save_catalog(
                ModelCatalog(
                    nickname="Work",
                    base_url=_FIXTURE_URL,
                    models=("alpha",),
                    fetched_at=collected_now(),
                ),
                target,
            )
            self.require(saved.is_file())
            if os.name == "posix":
                self.equal(stat.S_IMODE(saved.stat().st_mode), 0o600)
                self.equal(stat.S_IMODE(saved.parent.stat().st_mode), 0o700)

    def test_default_locations_derive_from_the_nickname(self) -> None:
        """The catalog sits beside the profiles, under one name per identity."""
        path = catalog_path("Work Laptop")
        self.equal(path.name, "work-laptop.json")
        self.equal(path.parent, catalogs_directory())
        self.equal(path.parent.name, "catalogs")


class DiscoveryTests(TypedTestCase):
    """Check the record built from identifiers just fetched from a provider."""

    def test_a_discovered_catalog_is_stamped_with_the_collection_time(self) -> None:
        """Staleness is answered by the record, not by a file name."""
        catalog = discovered_catalog("Work", _FIXTURE_URL, ("alpha", "beta"))
        self.equal(catalog.models, ("alpha", "beta"))
        self.equal(catalog.base_url, _FIXTURE_URL)
        stamp = datetime.fromisoformat(catalog.fetched_at)
        self.require(stamp.tzinfo is not None, "Expected an explicit UTC offset.")

    def test_discovery_normalizes_the_endpoint_it_recorded(self) -> None:
        """A chat endpoint and its root describe one provider, so store one form."""
        catalog = discovered_catalog(
            "Work",
            _FIXTURE_URL + "/chat/completions",
            ("alpha",),
        )
        self.equal(catalog.base_url, _FIXTURE_URL)

    def test_an_empty_catalog_is_a_valid_answer(self) -> None:
        """A provider without a model endpoint returns nothing; record that."""
        catalog = discovered_catalog("Work", _FIXTURE_URL, ())
        self.equal(catalog.models, ())

    def test_a_catalog_is_matched_against_the_endpoint_now_configured(self) -> None:
        """A catalog from another endpoint describes another provider entirely."""
        catalog = discovered_catalog("Work", _FIXTURE_URL, ("alpha",))
        self.require(catalog_matches(catalog, _FIXTURE_URL))
        self.require(catalog_matches(catalog, _FIXTURE_URL + "/chat/completions"))
        self.require(not catalog_matches(catalog, _OTHER_URL))


class CatalogValidationTests(TypedTestCase):
    """Refuse a stored catalog that no longer describes a usable answer."""

    def test_every_field_is_required(self) -> None:
        """A partial catalog cannot say which endpoint or when, so refuse it."""
        for missing in ("schema_version", "nickname", "base_url", "models"):
            payload = _catalog()
            del payload[missing]
            with (
                self.subTest(missing=missing),
                tempfile.TemporaryDirectory() as directory,
                self.rejected(ValueError),
            ):
                load_catalog(_written(directory, payload))

    def test_stored_identifiers_obey_the_model_rule(self) -> None:
        """A catalog entry becomes a request body field; check it like one."""
        for models in (["fixture\x00model"], ["ok", ""], [1], "alpha", [["alpha"]]):
            with (
                self.subTest(models=models),
                tempfile.TemporaryDirectory() as directory,
                self.rejected(ValueError),
            ):
                load_catalog(_written(directory, _catalog(models=models)))

    def test_a_stored_endpoint_obeys_the_url_rule(self) -> None:
        """The recorded endpoint is compared against a live one; validate both."""
        for url in ("not-a-url", "file:///local", "https://user:secret@p.example/v1"):
            with (
                self.subTest(url=url),
                tempfile.TemporaryDirectory() as directory,
                self.rejected(ValueError),
            ):
                load_catalog(_written(directory, _catalog(base_url=url)))

    def test_a_malformed_timestamp_is_refused(self) -> None:
        """An unreadable timestamp cannot answer how stale the catalog is."""
        for stamp in ("yesterday", "", "2026-13-45T99:99:99"):
            with (
                self.subTest(stamp=stamp),
                tempfile.TemporaryDirectory() as directory,
                self.rejected(ValueError),
            ):
                load_catalog(_written(directory, _catalog(fetched_at=stamp)))

    def test_an_unsupported_schema_version_is_refused(self) -> None:
        """Refuse a format this build cannot interpret instead of guessing."""
        for version in (0, 2, "1"):
            with (
                self.subTest(version=version),
                tempfile.TemporaryDirectory() as directory,
                self.rejected(ValueError),
            ):
                load_catalog(_written(directory, _catalog(schema_version=version)))

    def test_a_substituted_file_cannot_exhaust_memory(self) -> None:
        """Bound the read exactly as the profile reader bounds it."""
        with tempfile.TemporaryDirectory() as directory:
            target = _written(directory, _catalog(models=["m" * 200_000]))
            with self.rejected(ValueError, "exceeds"):
                load_catalog(target)
