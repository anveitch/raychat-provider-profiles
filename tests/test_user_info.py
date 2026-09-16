"""Stored provider identity obeys the rules the exported environment obeys."""

from __future__ import annotations

import json
import os
import stat
import tempfile
from pathlib import Path

from raychat.provider_settings import provider_settings
from raychat.user_info import (
    SCHEMA_VERSION,
    UserInfo,
    load_user_info,
    save_user_info,
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


def _written(directory: str, payload: object) -> Path:
    target = Path(directory) / "user_info.json"
    target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return target


class UserInfoStorageTests(TypedTestCase):
    """Check the round trip, its permissions and its atomic replacement."""

    def test_absent_file_reports_no_configuration_rather_than_failing(self) -> None:
        """A first launch has nothing stored and must not be an error."""
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "home" / "user_info.json"
            stored = load_user_info(target)
            self.equal(stored, UserInfo())
            self.require(not target.parent.exists())

    def test_round_trip_normalizes_the_url_and_keeps_the_credential_hidden(
        self,
    ) -> None:
        """Persist one identity and reduce a chat endpoint back to its root."""
        for suffix in ("", "/", "/chat/completions", "/chat/completions/"):
            with (
                self.subTest(suffix=suffix),
                tempfile.TemporaryDirectory() as directory,
            ):
                target = Path(directory) / "user_info.json"
                save_user_info(
                    UserInfo(
                        auth_token=_FIXTURE_CREDENTIAL,
                        model="vendor/configured-model",
                        base_url=_FIXTURE_URL + suffix,
                    ),
                    target,
                )
                stored = load_user_info(target)
                self.equal(stored.base_url, _FIXTURE_URL)
                self.equal(stored.model, "vendor/configured-model")
                self.equal(stored.auth_token, _FIXTURE_CREDENTIAL)
                self.require(_FIXTURE_CREDENTIAL not in repr(stored))

    def test_partial_configuration_stays_valid_for_a_single_recorded_choice(
        self,
    ) -> None:
        """A model menu records one field without inventing a URL or credential."""
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "user_info.json"
            save_user_info(UserInfo(model="vendor/chosen"), target)
            stored = load_user_info(target)
            self.equal(stored, UserInfo(model="vendor/chosen"))
            written = object_field(json_object(target.read_bytes()), "stored")
            self.equal(sorted(written), ["model", "schema_version"])

    def test_stored_credential_is_readable_only_by_its_owner(self) -> None:
        """A plaintext credential at rest must never widen beyond its owner."""
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "home" / "user_info.json"
            saved = save_user_info(
                UserInfo(auth_token=_FIXTURE_CREDENTIAL),
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
            target = Path(directory) / "user_info.json"
            save_user_info(UserInfo(model="vendor/first"), target)
            save_user_info(UserInfo(model="vendor/second"), target)
            self.equal(load_user_info(target).model, "vendor/second")
            self.equal(
                [item.name for item in Path(directory).iterdir()],
                [
                    "user_info.json",
                ],
            )

    def test_an_invalid_value_is_rejected_before_the_previous_file_changes(
        self,
    ) -> None:
        """Validation precedes the write, so a bad input cannot destroy a good file."""
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "user_info.json"
            save_user_info(UserInfo(model="vendor/good"), target)
            with self.rejected(ValueError):
                save_user_info(UserInfo(base_url="not-a-url"), target)
            self.equal(load_user_info(target).model, "vendor/good")

    def test_default_location_sits_in_the_operator_home_directory(self) -> None:
        """Resolve the same home the supervisor uses rather than the workspace."""
        path = user_info_path()
        self.equal(path.name, "user_info.json")
        self.equal(path.parent.name, ".raychat")
        self.equal(path.parent.parent, Path.home())


class UserInfoValidationTests(TypedTestCase):
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
                # The stored file must reject it for the same reason.
                target = _written(
                    directory,
                    {"schema_version": SCHEMA_VERSION, name: value},
                )
                with self.rejected(ValueError, name):
                    load_user_info(target)

    def test_rejection_messages_never_echo_the_stored_credential(self) -> None:
        """Error text is copied into logs and support requests; keep it clean."""
        with tempfile.TemporaryDirectory() as directory:
            target = _written(
                directory,
                {
                    "schema_version": SCHEMA_VERSION,
                    "auth_token": "synthetic-sensitive-value\r\n",
                    "base_url": "not-a-url",
                },
            )
            error = ""
            try:
                load_user_info(target)
            except ValueError as exc:
                error = str(exc)
            self.require(error, "Expected the malformed file to be rejected.")
            self.require("synthetic-sensitive-value" not in error)
            self.require("not-a-url" not in error)

    def test_unknown_and_missing_fields_are_reported_rather_than_ignored(
        self,
    ) -> None:
        """A misspelled field silently ignored would look like a saved setting."""
        for payload in (
            {"schema_version": SCHEMA_VERSION, "auth_tokenn": _FIXTURE_CREDENTIAL},
            {"schema_version": SCHEMA_VERSION, "url": _FIXTURE_URL},
            {"model": "vendor/configured-model"},
        ):
            with (
                self.subTest(payload=payload),
                tempfile.TemporaryDirectory() as directory,
                self.rejected(ValueError),
            ):
                load_user_info(_written(directory, payload))

    def test_an_unsupported_schema_version_is_refused(self) -> None:
        """Refuse a format this build cannot interpret instead of guessing."""
        for version in (0, 2, "1"):
            with (
                self.subTest(version=version),
                tempfile.TemporaryDirectory() as directory,
                self.rejected(ValueError),
            ):
                load_user_info(_written(directory, {"schema_version": version}))

    def test_malformed_and_non_object_documents_are_refused(self) -> None:
        """A truncated or replaced file must not resolve to a partial identity."""
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "user_info.json"
            for text in ("", "{", "[]", '"text"', "null", '{"a": 1, "a": 2}'):
                with self.subTest(text=text):
                    target.write_text(text, encoding="utf-8")
                    with self.rejected(ValueError):
                        load_user_info(target)

    def test_an_oversized_file_is_refused_without_reading_it_entirely(self) -> None:
        """Bound the read so a substituted file cannot exhaust memory."""
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "user_info.json"
            oversized: dict[str, object] = {
                "schema_version": SCHEMA_VERSION,
                "model": "v/" + "m" * 100_000,
            }
            target.write_text(
                json.dumps(oversized, ensure_ascii=False),
                encoding="utf-8",
            )
            with self.rejected(ValueError, "exceeds"):
                load_user_info(target)

    def test_a_symlinked_configuration_is_refused(self) -> None:
        """Owner-only permissions describe this file, not a target it points at."""
        if os.name != "posix":
            return
        with tempfile.TemporaryDirectory() as directory:
            actual = Path(directory) / "elsewhere.json"
            valid: dict[str, object] = {
                "schema_version": SCHEMA_VERSION,
                "model": "vendor/m",
            }
            actual.write_text(
                json.dumps(valid, ensure_ascii=False),
                encoding="utf-8",
            )
            link = Path(directory) / "user_info.json"
            link.symlink_to(actual)
            with self.rejected(ValueError, "symlink"):
                load_user_info(link)
