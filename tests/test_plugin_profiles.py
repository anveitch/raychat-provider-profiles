"""The configuration menu records which stored identity later sessions use."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING
from unittest import mock

from raychat.resources import create_resources
from raychat.sdk import PluginError
from raychat.user_info import (
    Profile,
    UserInfo,
    load_user_info,
    profile_path,
    save_profile,
    save_user_info,
)
from raychat.validation import text_field
from tests.assertions import TypedTestCase
from tests.tui_support import arguments

if TYPE_CHECKING:
    from collections.abc import Mapping

_URL = "https://profile-fixture.invalid/v1"
_FIXTURE_CREDENTIAL = "synthetic-profile-token"
_ENVIRONMENT = {
    "RAYCHAT_AUTH_TOKEN": _FIXTURE_CREDENTIAL,
    "RAYCHAT_MODEL": "gpt-4o",
    "RAYCHAT_BASE_URL": _URL,
}


def _restore_environment(saved: dict[str, str]) -> None:
    os.environ.clear()
    os.environ.update(saved)


class ProfileMenuTests(TypedTestCase):
    """Switching records a choice; it never re-points the running session."""

    def _notices(self, directory: str, chosen: str) -> list[str]:
        args = arguments(["--workspace", directory, "--no-memory"])
        self.addCleanup(_restore_environment, dict(os.environ))
        os.environ.clear()
        os.environ.update(_ENVIRONMENT)
        resources = create_resources(args, dict(_ENVIRONMENT))
        self.addCleanup(resources.close)
        notices: list[str] = []

        def notify(kind: str, payload: Mapping[str, object]) -> None:
            if kind == "notification":
                notices.append(text_field(payload["message"], "notification"))

        resources.runtime.select_menu("profiles", chosen, notify=notify)
        return notices

    @staticmethod
    def _stored() -> None:
        save_profile(Profile(nickname="stark-dev", model="gpt-4o", base_url=_URL))
        save_profile(Profile(nickname="stark-prod", model="stark-x", base_url=_URL))
        save_user_info(UserInfo(active_profile="stark-dev"))

    def test_selecting_another_configuration_records_it_for_next_launch(self) -> None:
        """The running session keeps the identity its workers already inherited."""
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(Path, "home", return_value=Path(directory)),
        ):
            self._stored()
            notices = self._notices(directory, "stark-prod")
            self.equal(load_user_info().active_profile, "stark-prod")
            joined = " ".join(notices)
            self.require("Restart RayChat" in joined, joined)
            self.require("stark-prod" in joined, joined)

    def test_selecting_the_active_configuration_changes_nothing(self) -> None:
        """Re-choosing what is already active should say so, not rewrite state."""
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(Path, "home", return_value=Path(directory)),
        ):
            self._stored()
            notices = self._notices(directory, "stark-dev")
            self.equal(load_user_info().active_profile, "stark-dev")
            self.require("already the active" in " ".join(notices))

    def test_a_deleted_configuration_cannot_be_selected(self) -> None:
        """The menu offers what exists now, so a stale name cannot become active."""
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(Path, "home", return_value=Path(directory)),
        ):
            self._stored()
            profile_path("stark-prod").unlink()
            with self.rejected(PluginError, "no longer available"):
                self._notices(directory, "stark-prod")
            self.equal(load_user_info().active_profile, "stark-dev")
