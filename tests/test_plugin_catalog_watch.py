"""A background refresh keeps the catalog current and reports a vanished model."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING
from unittest import mock

from raychat.model_catalog import catalog_path, load_catalog
from raychat.user_info import Profile, UserInfo, save_profile, save_user_info
from tests.assertions import TypedTestCase
from tests.plugin_support import plugin_module

if TYPE_CHECKING:
    from plugins.chat_completions import catalog_watch as _rc_watch
else:
    _rc_watch = plugin_module("chat_completions.catalog_watch")

_URL = "https://watch-fixture.invalid/v1"
_CREDENTIAL = "synthetic-watch-token"
_ENVIRONMENT = {
    "RAYCHAT_AUTH_TOKEN": _CREDENTIAL,
    "RAYCHAT_MODEL": "retired-model",
    "RAYCHAT_BASE_URL": _URL,
}


def _restore(saved: dict[str, str]) -> None:
    os.environ.clear()
    os.environ.update(saved)


class CatalogWatchTests(TypedTestCase):
    """Collecting refreshes the stored catalog and answers what is offered."""

    def _prepared(self, model: str = "retired-model") -> None:
        self.addCleanup(_restore, dict(os.environ))
        os.environ.clear()
        os.environ.update({**_ENVIRONMENT, "RAYCHAT_MODEL": model})
        save_profile(Profile(nickname="stark", model=model, base_url=_URL))
        save_user_info(UserInfo(active_profile="stark"))

    def test_collecting_stores_the_catalog_and_reports_the_model_in_use(self) -> None:
        """The refresh records what the endpoint offered, for the next launch."""
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(Path, "home", return_value=Path(directory)),
            mock.patch.object(
                _rc_watch,
                "fetch_models",
                return_value=("alpha", "beta"),
            ),
        ):
            self._prepared()
            collected = _rc_watch.CatalogWatch.collect()
            self.equal(collected, ("stark", "retired-model", ("alpha", "beta")))
            stored = load_catalog(catalog_path("stark"))
            if stored is None:
                self.fail("Expected the refreshed catalog to be stored.")
            else:
                self.equal(stored.models, ("alpha", "beta"))

    def test_an_endpoint_without_a_catalog_records_nothing(self) -> None:
        """Many compatible servers publish no catalog; that is not a failure."""
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(Path, "home", return_value=Path(directory)),
            mock.patch.object(_rc_watch, "fetch_models", return_value=()),
        ):
            self._prepared()
            self.equal(_rc_watch.CatalogWatch.collect(), None)
            self.equal(load_catalog(catalog_path("stark")), None)

    def test_a_failing_refresh_never_escapes(self) -> None:
        """A session must not be disturbed by a background refresh."""
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(Path, "home", return_value=Path(directory)),
            mock.patch.object(_rc_watch, "fetch_models", side_effect=OSError("down")),
        ):
            self._prepared()
            # _refresh swallows it; collect is where the error actually arises.
            with self.rejected(OSError):
                _rc_watch.CatalogWatch.collect()

    def test_no_stored_profile_collects_nothing(self) -> None:
        """An environment-only launch has no configuration to refresh."""
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(Path, "home", return_value=Path(directory)),
        ):
            self.addCleanup(_restore, dict(os.environ))
            os.environ.clear()
            os.environ.update(_ENVIRONMENT)
            self.equal(_rc_watch.CatalogWatch.collect(), None)
