"""Stored provider identities obey the rules the exported environment obeys."""

from __future__ import annotations

import json
import os
import stat
import tempfile
from pathlib import Path

from raychat.provider_settings import provider_settings
from raychat.user_info import (
    MAX_NICKNAME_CHARS,
    SCHEMA_VERSION,
    Profile,
    UserInfo,
    load_profile,
    load_user_info,
    profile_slug,
    save_profile,
    save_user_info,
    stored_profile_slugs,
    user_info_path,
)
from raychat.validation import json_object, object_field
from tests.assertions import TypedTestCase
from tests.environment_support import provider_environment

_FIXTURE_CREDENTIAL = "fixture-token"
_FIXTURE_URL = "https://provider.example/v1"
_VARIABLES = {
    "auth_token": "RAYCHAT_AUTH_TOKEN",
    "model": "RAYCHAT_MODEL",
    "base_url": "RAYCHAT_BASE_URL",
}


def _written(directory: str, payload: object, name: str = "work.json") -> Path:
    target = Path(directory) / name
    target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return target


class ProfileSlugTests(TypedTestCase):
    """Derive file names that survive every supported platform."""

    def test_nicknames_reduce_to_portable_lowercase_names(self) -> None:
        """Strip whatever a file system would reject or compare inconsistently."""
        for nickname, expected in (
            ("work", "work"),
            ("Work", "work"),
            ("  Work Laptop  ", "work-laptop"),
            ("work/../etc", "work-etc"),
            ("a:b|c?d*e", "a-b-c-d-e"),
            ("my.key", "my-key"),
            ("under_score", "under_score"),
            ("--dashes--", "dashes"),
        ):
            with self.subTest(nickname=nickname):
                self.equal(profile_slug(nickname), expected)

    def test_names_that_differ_only_in_case_address_one_profile(self) -> None:
        """Windows and macOS compare case-insensitively; resolve it deliberately."""
        self.equal(profile_slug("Work"), profile_slug("WORK"))

    def test_reserved_device_names_never_become_file_names(self) -> None:
        """Windows refuses these names with any extension, on every drive."""
        for nickname in ("con", "PRN", "aux", "NUL", "com1", "lpt9"):
            with self.subTest(nickname=nickname):
                self.require(profile_slug(nickname).startswith("profile-"))

    def test_a_nickname_in_another_script_still_yields_a_usable_name(self) -> None:
        """A name with no ASCII must not be refused; fall back to a stable digest."""
        first = profile_slug("仕事")
        self.equal(first, profile_slug("仕事"))
        self.require(first.startswith("profile-"))
        self.require(first != profile_slug("私用"))

    def test_unusable_nicknames_are_refused(self) -> None:
        """A blank or oversized name cannot identify anything."""
        for nickname in ("", "   ", "work\x00", "w" * (MAX_NICKNAME_CHARS + 1)):
            with self.subTest(nickname=nickname), self.rejected(ValueError):
                profile_slug(nickname)


class ProfileStorageTests(TypedTestCase):
    """Check the round trip, its permissions and its atomic replacement."""

    def test_absent_profile_reports_nothing_stored_rather_than_failing(self) -> None:
        """A first launch has nothing stored and must not be an error."""
        with tempfile.TemporaryDirectory() as directory:
            self.equal(load_profile(Path(directory) / "missing.json"), None)

    def test_round_trip_normalizes_the_url_and_keeps_the_credential_hidden(
        self,
    ) -> None:
        """Persist one identity and reduce a chat endpoint back to its root."""
        for suffix in ("", "/", "/chat/completions", "/chat/completions/"):
            with (
                self.subTest(suffix=suffix),
                tempfile.TemporaryDirectory() as directory,
            ):
                target = Path(directory) / "work.json"
                save_profile(
                    Profile(
                        nickname="Work",
                        auth_token=_FIXTURE_CREDENTIAL,
                        model="vendor/configured-model",
                        base_url=_FIXTURE_URL + suffix,
                    ),
                    target,
                )
                stored = load_profile(target)
                if stored is None:
                    self.fail("Expected the saved profile to load.")
                else:
                    self.equal(stored.nickname, "Work")
                    self.equal(stored.base_url, _FIXTURE_URL)
                    self.equal(stored.model, "vendor/configured-model")
                    self.equal(stored.auth_token, _FIXTURE_CREDENTIAL)
                    self.require(_FIXTURE_CREDENTIAL not in repr(stored))

    def test_partial_profile_stays_valid_for_a_single_recorded_choice(self) -> None:
        """A model menu records one field without inventing a URL or credential."""
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "work.json"
            save_profile(Profile(nickname="Work", model="vendor/chosen"), target)
            stored = load_profile(target)
            if stored is None:
                self.fail("Expected the saved profile to load.")
            else:
                self.equal(stored.model, "vendor/chosen")
                self.equal(stored.auth_token, None)
                self.equal(stored.base_url, None)
            written = object_field(json_object(target.read_bytes()), "stored")
            self.equal(sorted(written), ["model", "nickname", "schema_version"])

    def test_stored_credential_is_readable_only_by_its_owner(self) -> None:
        """A plaintext credential at rest must never widen beyond its owner."""
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "profiles" / "work.json"
            saved = save_profile(
                Profile(nickname="Work", auth_token=_FIXTURE_CREDENTIAL),
                target,
            )
            self.require(saved.is_file())
            if os.name == "posix":
                self.equal(stat.S_IMODE(saved.stat().st_mode), 0o600)
                self.equal(stat.S_IMODE(saved.parent.stat().st_mode), 0o700)

    def test_replacement_leaves_no_temporary_files_and_keeps_one_version(
        self,
    ) -> None:
        """An overwrite replaces the file in place without scattering fragments."""
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "work.json"
            save_profile(Profile(nickname="Work", model="vendor/first"), target)
            save_profile(Profile(nickname="Work", model="vendor/second"), target)
            stored = load_profile(target)
            if stored is None:
                self.fail("Expected the saved profile to load.")
            else:
                self.equal(stored.model, "vendor/second")
            self.equal([item.name for item in Path(directory).iterdir()], ["work.json"])

    def test_an_invalid_value_is_rejected_before_the_previous_file_changes(
        self,
    ) -> None:
        """Validation precedes the write, so a bad input cannot destroy a good file."""
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "work.json"
            save_profile(Profile(nickname="Work", model="vendor/good"), target)
            with self.rejected(ValueError):
                save_profile(Profile(nickname="Work", base_url="not-a-url"), target)
            stored = load_profile(target)
            if stored is None:
                self.fail("Expected the original profile to survive.")
            else:
                self.equal(stored.model, "vendor/good")

    def test_stored_profiles_are_listed_without_parsing_them(self) -> None:
        """A menu needs the names even when one file is unreadable."""
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            self.equal(stored_profile_slugs(folder / "absent"), ())
            save_profile(Profile(nickname="Work"), folder / "work.json")
            save_profile(Profile(nickname="Home"), folder / "home.json")
            (folder / "notes.txt").write_text("ignored", encoding="utf-8")
            self.equal(stored_profile_slugs(folder), ("home", "work"))


class ActiveProfileTests(TypedTestCase):
    """Check the pointer naming the profile the next launch uses."""

    def test_absent_pointer_selects_nothing(self) -> None:
        """Having no stored selection is an ordinary first-run state."""
        with tempfile.TemporaryDirectory() as directory:
            self.equal(
                load_user_info(Path(directory) / "user_info.json"),
                UserInfo(),
            )

    def test_pointer_round_trip_stores_the_derived_name(self) -> None:
        """Record the file name, not the display name, so lookup cannot drift."""
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "user_info.json"
            save_user_info(UserInfo(active_profile="Work Laptop"), target)
            self.equal(load_user_info(target).active_profile, "work-laptop")
            written = object_field(json_object(target.read_bytes()), "stored")
            self.equal(sorted(written), ["active_profile", "schema_version"])

    def test_default_locations_sit_in_the_operator_home_directory(self) -> None:
        """Resolve the same home the supervisor uses rather than the workspace."""
        path = user_info_path()
        self.equal(path.name, "user_info.json")
        self.equal(path.parent.name, ".raychat")
        self.equal(path.parent.parent, Path.home())


class ProfileValidationTests(TypedTestCase):
    """Reject stored values the exported environment would also reject."""

    def test_stored_values_are_rejected_wherever_the_environment_rejects_them(
        self,
    ) -> None:
        """Keep one rule per field so a file cannot widen what a variable allows."""
        for name, value in (
            ("auth_token", "fixture\r\nInjected: value"),
            ("auth_token", "fixture token"),
            ("auth_token", "fixture-雪"),
            ("model", "fixture\x00model"),
            ("model", "fixture\x7fmodel"),
            ("base_url", "file:///local/path"),
            ("base_url", "provider.example/v1"),
            ("base_url", "https://user:synthetic-secret@provider.example/v1"),
            ("base_url", "https://provider.example/v1?key=synthetic-secret"),
            ("base_url", "https://provider.example/v1#fragment"),
            ("base_url", "https://provider.example:0/v1"),
            ("base_url", "https://provider.example/white space"),
        ):
            with (
                self.subTest(name=name, value=value),
                tempfile.TemporaryDirectory() as directory,
            ):
                # The exported variable rejects it.
                with self.rejected(ValueError):
                    provider_settings(
                        {**provider_environment(), _VARIABLES[name]: value},
                    )
                # The stored profile must reject it for the same reason.
                target = _written(
                    directory,
                    {
                        "schema_version": SCHEMA_VERSION,
                        "nickname": "Work",
                        name: value,
                    },
                )
                with self.rejected(ValueError, name):
                    load_profile(target)

    def test_rejection_messages_never_echo_the_stored_credential(self) -> None:
        """Error text is copied into logs and support requests; keep it clean."""
        with tempfile.TemporaryDirectory() as directory:
            target = _written(
                directory,
                {
                    "schema_version": SCHEMA_VERSION,
                    "nickname": "Work",
                    "auth_token": "synthetic-sensitive-value\r\n",
                    "base_url": "not-a-url",
                },
            )
            error = ""
            try:
                load_profile(target)
            except ValueError as exc:
                error = str(exc)
            self.require(error, "Expected the malformed profile to be rejected.")
            self.require("synthetic-sensitive-value" not in error)
            self.require("not-a-url" not in error)

    def test_unknown_and_missing_fields_are_reported_rather_than_ignored(
        self,
    ) -> None:
        """A misspelled field silently ignored would look like a saved setting."""
        for payload in (
            {
                "schema_version": SCHEMA_VERSION,
                "nickname": "Work",
                "auth_tokenn": _FIXTURE_CREDENTIAL,
            },
            {"schema_version": SCHEMA_VERSION, "nickname": "Work", "url": _FIXTURE_URL},
            {"schema_version": SCHEMA_VERSION},
            {"nickname": "Work"},
        ):
            with (
                self.subTest(payload=payload),
                tempfile.TemporaryDirectory() as directory,
                self.rejected(ValueError),
            ):
                load_profile(_written(directory, payload))

    def test_an_unsupported_schema_version_is_refused(self) -> None:
        """Refuse a format this build cannot interpret instead of guessing."""
        for version in (0, 2, "1"):
            with (
                self.subTest(version=version),
                tempfile.TemporaryDirectory() as directory,
                self.rejected(ValueError),
            ):
                load_profile(
                    _written(
                        directory,
                        {"schema_version": version, "nickname": "Work"},
                    ),
                )

    def test_malformed_and_non_object_documents_are_refused(self) -> None:
        """A truncated or replaced file must not resolve to a partial identity."""
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "work.json"
            for text in ("", "{", "[]", '"text"', "null", '{"a": 1, "a": 2}'):
                with self.subTest(text=text):
                    target.write_text(text, encoding="utf-8")
                    with self.rejected(ValueError):
                        load_profile(target)

    def test_an_oversized_file_is_refused_without_reading_it_entirely(self) -> None:
        """Bound the read so a substituted file cannot exhaust memory."""
        with tempfile.TemporaryDirectory() as directory:
            oversized: dict[str, object] = {
                "schema_version": SCHEMA_VERSION,
                "nickname": "Work",
                "model": "v/" + "m" * 100_000,
            }
            target = _written(directory, oversized)
            with self.rejected(ValueError, "exceeds"):
                load_profile(target)

    def test_a_symlinked_profile_is_refused(self) -> None:
        """Owner-only permissions describe this file, not a target it points at."""
        if os.name != "posix":
            return
        with tempfile.TemporaryDirectory() as directory:
            actual = _written(
                directory,
                {"schema_version": SCHEMA_VERSION, "nickname": "Work"},
                name="elsewhere.json",
            )
            link = Path(directory) / "work.json"
            link.symlink_to(actual)
            with self.rejected(ValueError, "symlink"):
                load_profile(link)
