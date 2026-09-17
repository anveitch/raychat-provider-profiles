"""An incomplete environment is completed from the profile, and never overridden."""

from __future__ import annotations

import tempfile
from dataclasses import replace
from pathlib import Path
from unittest import mock

from raychat.configuration import SETTINGS
from raychat.provider_resolution import (
    IGNORE_PROFILES_VARIABLE,
    SUPPLIED_VARIABLE,
    active_profile,
    apply_stored_identity,
    supplied_by_profile,
)
from raychat.provider_settings import provider_settings
from raychat.user_info import Profile, UserInfo, save_profile, save_user_info
from tests.assertions import TypedTestCase
from tests.environment_support import provider_environment

_STORED_URL = "https://stored-provider.example/v1"
_STORED_CREDENTIAL = "stored-token"
_STORED_MODEL = "vendor/stored-model"
_VARIABLE_NAMES = (
    "RAYCHAT_AUTH_TOKEN",
    "RAYCHAT_MODEL",
    "RAYCHAT_BASE_URL",
)


class _Home:
    """A temporary operator home holding one selected profile."""

    def __init__(self, directory: str) -> None:
        """Create the profile directory and the pointer beside it."""
        self.root = Path(directory)
        self.profiles = self.root / "profiles"
        self.pointer = self.root / "user_info.json"

    def store(self, profile: Profile, *, selected: bool = True) -> None:
        """Write one profile and optionally mark it as the selected one."""
        save_profile(profile, self.profiles / (profile.slug + ".json"))
        if selected:
            save_user_info(UserInfo(active_profile=profile.nickname), self.pointer)

    def apply(self, environ: dict[str, str]) -> Profile | None:
        """Complete the environment from this home.

        Returns
        -------
        Profile | None
            The profile consulted, if any.

        """
        return apply_stored_identity(environ, self.pointer, self.profiles)


def _complete() -> Profile:
    return Profile(
        nickname="Work",
        auth_token=_STORED_CREDENTIAL,
        model=_STORED_MODEL,
        base_url=_STORED_URL,
    )


class ResolutionTests(TypedTestCase):
    """Complete what the environment omits, and nothing more."""

    def test_an_empty_environment_is_completed_from_the_selected_profile(self) -> None:
        """A user who configured RayChat in the application can just launch it."""
        with tempfile.TemporaryDirectory() as directory:
            home = _Home(directory)
            home.store(_complete())
            environ: dict[str, str] = {}
            self.equal(home.apply(environ), _complete())
            settings = provider_settings(environ)
            self.equal(settings.auth_token, _STORED_CREDENTIAL)
            self.equal(settings.model, _STORED_MODEL)
            self.equal(settings.base_url, _STORED_URL)

    def test_exported_variables_always_win(self) -> None:
        """Nothing stored on disk may redirect a session that states its own target."""
        with tempfile.TemporaryDirectory() as directory:
            home = _Home(directory)
            home.store(_complete())
            environ = provider_environment()
            before = dict(environ)
            home.apply(environ)
            self.equal(environ, before)
            self.equal(provider_settings(environ).auth_token, "fixture-token")

    def test_each_variable_is_completed_independently(self) -> None:
        """A shell may supply the credential while the profile supplies the rest."""
        with tempfile.TemporaryDirectory() as directory:
            home = _Home(directory)
            home.store(_complete())
            environ = {"RAYCHAT_AUTH_TOKEN": "exported-token"}
            home.apply(environ)
            settings = provider_settings(environ)
            self.equal(settings.auth_token, "exported-token")
            self.equal(settings.model, _STORED_MODEL)
            self.equal(settings.base_url, _STORED_URL)

    def test_a_blank_variable_counts_as_absent(self) -> None:
        """An exported but empty value is the failure the profile should repair."""
        with tempfile.TemporaryDirectory() as directory:
            home = _Home(directory)
            home.store(_complete())
            environ = {**provider_environment(), "RAYCHAT_MODEL": "  "}
            home.apply(environ)
            self.equal(provider_settings(environ).model, _STORED_MODEL)

    def test_a_partial_profile_completes_only_what_it_holds(self) -> None:
        """A profile recording one field must not invent the others."""
        with tempfile.TemporaryDirectory() as directory:
            home = _Home(directory)
            home.store(Profile(nickname="Work", model=_STORED_MODEL))
            environ: dict[str, str] = {}
            home.apply(environ)
            self.equal(environ.get("RAYCHAT_MODEL"), _STORED_MODEL)
            self.equal(environ.get("RAYCHAT_AUTH_TOKEN"), None)
            self.equal(environ.get("RAYCHAT_BASE_URL"), None)
            with self.rejected(ValueError, "RAYCHAT_AUTH_TOKEN"):
                provider_settings(environ)

    def test_no_selection_leaves_the_environment_untouched(self) -> None:
        """A stored profile that was never selected must not be used by surprise."""
        with tempfile.TemporaryDirectory() as directory:
            home = _Home(directory)
            home.store(_complete(), selected=False)
            environ: dict[str, str] = {}
            self.equal(home.apply(environ), None)
            self.equal(environ, {})

    def test_nothing_stored_leaves_the_environment_untouched(self) -> None:
        """The existing environment error must still be what a new user sees."""
        with tempfile.TemporaryDirectory() as directory:
            environ: dict[str, str] = {}
            self.equal(_Home(directory).apply(environ), None)
            self.equal(environ, {})

    def test_a_selection_naming_a_missing_profile_is_reported(self) -> None:
        """A deleted profile is a configuration error, not a silent fallback."""
        with tempfile.TemporaryDirectory() as directory:
            home = _Home(directory)
            save_user_info(UserInfo(active_profile="Work"), home.pointer)
            with self.rejected(ValueError, "not stored"):
                home.apply({})

    def test_scripted_runs_can_refuse_stored_profiles_entirely(self) -> None:
        """CI and wrappers need a launch that cannot pick up operator state."""
        with tempfile.TemporaryDirectory() as directory:
            home = _Home(directory)
            home.store(_complete())
            environ = {IGNORE_PROFILES_VARIABLE: "1"}
            self.equal(home.apply(environ), None)
            self.equal(environ, {IGNORE_PROFILES_VARIABLE: "1"})

    def test_supplied_values_are_distinguishable_from_exported_ones(self) -> None:
        """Once resolved they look alike, yet only one of them can be changed."""
        with tempfile.TemporaryDirectory() as directory:
            home = _Home(directory)
            home.store(_complete())
            environ = {"RAYCHAT_MODEL": "exported-model"}
            home.apply(environ)
            # The shell owns the model; the profile supplied the rest.
            self.require(not supplied_by_profile(environ, "RAYCHAT_MODEL"))
            self.require(supplied_by_profile(environ, "RAYCHAT_AUTH_TOKEN"))
            self.require(supplied_by_profile(environ, "RAYCHAT_BASE_URL"))

    def test_a_fully_exported_environment_records_no_supplied_values(self) -> None:
        """Nothing was taken from disk, so nothing may claim it can be changed."""
        with tempfile.TemporaryDirectory() as directory:
            home = _Home(directory)
            home.store(_complete())
            environ = provider_environment()
            home.apply(environ)
            self.equal(environ.get(SUPPLIED_VARIABLE), None)
            for name in _VARIABLE_NAMES:
                self.require(not supplied_by_profile(environ, name))

    def test_an_unresolvable_home_directory_falls_back_to_the_environment(
        self,
    ) -> None:
        """Windows cannot name a home with its variables cleared; POSIX still can.

        A process launched without USERPROFILE or HOMEDRIVE/HOMEPATH has nowhere
        a profile could be stored, so resolution must leave the environment
        alone and let the ordinary missing-variable error stand, rather than
        failing startup with an error about the home directory.
        """

        def _no_home() -> Path:
            message = "Could not determine home directory"
            raise RuntimeError(message)

        environ: dict[str, str] = {}
        with mock.patch.object(Path, "home", _no_home):
            self.equal(apply_stored_identity(environ), None)
        self.equal(environ, {})
        with self.rejected(ValueError, "RAYCHAT_AUTH_TOKEN"):
            provider_settings(environ)

    def test_the_stored_context_role_is_exported_for_the_agent(self) -> None:
        """A provider that refuses a system role needs that recorded, not retyped."""
        with tempfile.TemporaryDirectory() as directory:
            home = _Home(directory)
            home.store(replace(_complete(), instruction_role="user"))
            environ: dict[str, str] = {}
            home.apply(environ)
            self.equal(environ.get(SETTINGS.chat.environment.instruction_role), "user")

    def test_an_exported_context_role_still_wins(self) -> None:
        """The shell keeps control of this exactly as it does of the identity."""
        with tempfile.TemporaryDirectory() as directory:
            home = _Home(directory)
            home.store(replace(_complete(), instruction_role="user"))
            name = SETTINGS.chat.environment.instruction_role
            environ = {name: "developer"}
            home.apply(environ)
            self.equal(environ[name], "developer")

    def test_the_selected_profile_is_reported_for_display(self) -> None:
        """A command showing the current configuration needs the name, not the file."""
        with tempfile.TemporaryDirectory() as directory:
            home = _Home(directory)
            home.store(_complete())
            selected = active_profile(home.pointer, home.profiles)
            if selected is None:
                self.fail("Expected the stored selection to load.")
            else:
                self.equal(selected.nickname, "Work")
                self.require(_STORED_CREDENTIAL not in repr(selected))
