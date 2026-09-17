"""First-run setup asks for one identity, discovers its models and stores it."""

from __future__ import annotations

import tempfile
import threading
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING
from unittest import mock

from raychat.configuration import SETTINGS
from raychat.first_run import (
    NEW_PROFILE,
    choose_profile,
    configure_interactively,
    fetch_models,
    profile_entries,
)
from raychat.user_info import (
    Profile,
    load_profile,
    load_user_info,
    profile_path,
    save_profile,
    user_info_path,
)
from tests.assertions import TypedTestCase
from tools.provider_stub import StubConfig, handler

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

_CREDENTIAL = "stub-token"
_CATALOG = ("vendor/first", "vendor/second", "vendor/third")
_URL = "https://profile-fixture.invalid/v1"


class _Answers:
    """Supply scripted answers and capture everything written to the operator."""

    def __init__(self, answers: Sequence[str]) -> None:
        """Queue the answers the prompts will consume in order."""
        self.remaining = list(answers)
        self.written: list[str] = []
        self.prompts: list[str] = []

    def read(self, prompt: str) -> str:
        """Answer one prompt.

        Returns
        -------
        str
            The next scripted answer.

        Raises
        ------
        EOFError
            When the script is exhausted, as a closed input would.

        """
        self.prompts.append(prompt)
        if not self.remaining:
            raise EOFError
        return self.remaining.pop(0)

    def write(self, text: str) -> None:
        """Record one line of setup output."""
        self.written.append(text)

    def transcript(self) -> str:
        """Join everything written for whole-output assertions.

        Returns
        -------
        str
            Every line written during setup.

        """
        return "\n".join(self.written)


@contextmanager
def _served(*, hide_catalog: bool = False) -> Iterator[str]:
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        handler(
            StubConfig(
                models=_CATALOG,
                token=_CREDENTIAL,
                hide=hide_catalog,
                silent=True,
            ),
        ),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


class DiscoveryTests(TypedTestCase):
    """Ask a live endpoint which models a credential may use."""

    def test_the_catalog_is_read_from_a_running_endpoint(self) -> None:
        """The key and URL together decide the list; nothing is assumed."""
        with _served() as base:
            self.equal(fetch_models(base, _CREDENTIAL), _CATALOG)

    def test_a_refused_credential_is_reported(self) -> None:
        """A wrong key must fail loudly here rather than at the first message."""
        with _served() as base, self.rejected(OSError):
            fetch_models(base, "wrong-token")

    def test_a_server_without_a_catalog_is_reported(self) -> None:
        """Many compatible servers implement chat but publish no catalog."""
        with _served(hide_catalog=True) as base, self.rejected(OSError):
            fetch_models(base, _CREDENTIAL)


class InteractiveSetupTests(TypedTestCase):
    """Collect one identity, checking each answer as it is given."""

    def test_setup_stores_a_profile_and_selects_it(self) -> None:
        """A complete answer set leaves RayChat ready to launch."""
        with (
            tempfile.TemporaryDirectory() as home,
            _served() as base,
        ):
            answers = _Answers([base, _CREDENTIAL, "user", "Work Laptop"])
            with mock.patch.object(Path, "home", return_value=Path(home)):
                profile = configure_interactively(
                    answers.read,
                    answers.read,
                    answers.write,
                )
                if profile is None:
                    self.fail("Expected setup to complete.")
                else:
                    self.equal(profile.nickname, "Work Laptop")
                    self.equal(profile.instruction_role, "user")
                stored = load_profile(profile_path("Work Laptop"))
                self.equal(load_user_info().active_profile, "work-laptop")
            if stored is None:
                self.fail("Expected the profile to be stored.")
            else:
                self.equal(stored.auth_token, _CREDENTIAL)
                self.equal(stored.base_url, base)
                self.equal(stored.instruction_role, "user")

    def test_the_model_is_adopted_from_discovery_without_being_asked(self) -> None:
        """The last advertised model starts the session; the menu replaces it."""
        with (
            tempfile.TemporaryDirectory() as home,
            _served() as base,
        ):
            answers = _Answers([base, _CREDENTIAL, "user", "Work"])
            with mock.patch.object(Path, "home", return_value=Path(home)):
                profile = configure_interactively(
                    answers.read,
                    answers.read,
                    answers.write,
                )
            if profile is None:
                self.fail("Expected setup to complete.")
            else:
                self.equal(profile.model, _CATALOG[-1])
            prompts = " ".join(answers.prompts).casefold()
            self.require("model" not in prompts, "Setup must not ask for a model.")
            self.require("/models" in answers.transcript())

    def test_the_context_role_defaults_when_the_answer_is_blank(self) -> None:
        """A shown default must be accepted, not treated as giving up."""
        with (
            tempfile.TemporaryDirectory() as home,
            _served() as base,
        ):
            answers = _Answers([base, _CREDENTIAL, "", "Work"])
            with mock.patch.object(Path, "home", return_value=Path(home)):
                profile = configure_interactively(
                    answers.read,
                    answers.read,
                    answers.write,
                )
            if profile is None:
                self.fail("Expected the shown default to be accepted.")
            else:
                self.equal(profile.instruction_role, SETTINGS.chat.instruction_role)

    def test_an_unsupported_context_role_is_refused_at_the_prompt(self) -> None:
        """Only roles the configuration permits can reach a request."""
        with (
            tempfile.TemporaryDirectory() as home,
            _served() as base,
        ):
            answers = _Answers([base, _CREDENTIAL, "operator", "user", "Work"])
            with mock.patch.object(Path, "home", return_value=Path(home)):
                profile = configure_interactively(
                    answers.read,
                    answers.read,
                    answers.write,
                )
            if profile is None:
                self.fail("Expected setup to continue after a correction.")
            else:
                self.equal(profile.instruction_role, "user")
            self.require("must be one of" in answers.transcript())

    def test_a_server_without_a_catalog_asks_for_a_model(self) -> None:
        """With nothing advertised there is nothing to adopt, so one is named."""
        with (
            tempfile.TemporaryDirectory() as home,
            _served(hide_catalog=True) as base,
        ):
            answers = _Answers([base, _CREDENTIAL, "vendor/typed", "user", "Bare"])
            with mock.patch.object(Path, "home", return_value=Path(home)):
                profile = configure_interactively(
                    answers.read,
                    answers.read,
                    answers.write,
                )
            if profile is None:
                self.fail("Expected setup to complete without a catalog.")
            else:
                self.equal(profile.model, "vendor/typed")
            self.require("advertises no models" in answers.transcript())

    def test_an_invalid_answer_is_refused_at_the_prompt(self) -> None:
        """A typo is caught while the user is present, not at the first request."""
        with (
            tempfile.TemporaryDirectory() as home,
            _served() as base,
        ):
            answers = _Answers(["not-a-url", base, _CREDENTIAL, "user", "Work"])
            with mock.patch.object(Path, "home", return_value=Path(home)):
                profile = configure_interactively(
                    answers.read,
                    answers.read,
                    answers.write,
                )
            if profile is None:
                self.fail("Expected setup to continue after a correction.")
            else:
                self.equal(profile.base_url, base)
            self.require("RAYCHAT_BASE_URL must" in answers.transcript())

    def test_a_refused_credential_is_named_as_such_and_can_be_corrected(
        self,
    ) -> None:
        """A wrong token must not be reported as a server without a catalog."""
        with (
            tempfile.TemporaryDirectory() as home,
            _served() as base,
        ):
            answers = _Answers([base, "wrong-token", _CREDENTIAL, "user", "Work"])
            with mock.patch.object(Path, "home", return_value=Path(home)):
                profile = configure_interactively(
                    answers.read,
                    answers.read,
                    answers.write,
                )
            shown = answers.transcript()
            self.require("refused this credential" in shown)
            self.require("advertises no models" not in shown)
            if profile is None:
                self.fail("Expected the corrected credential to be accepted.")
            else:
                self.equal(profile.auth_token, _CREDENTIAL)

    def test_cancelling_stores_nothing(self) -> None:
        """An abandoned setup must not leave a half-written identity behind."""
        with tempfile.TemporaryDirectory() as home:
            answers = _Answers([])
            with mock.patch.object(Path, "home", return_value=Path(home)):
                self.equal(
                    configure_interactively(
                        answers.read,
                        answers.read,
                        answers.write,
                    ),
                    None,
                )
                self.require(not user_info_path().exists())

    def test_the_credential_is_never_written_to_the_transcript(self) -> None:
        """Setup output is read over shoulders and pasted into support requests."""
        with (
            tempfile.TemporaryDirectory() as home,
            _served() as base,
        ):
            answers = _Answers([base, _CREDENTIAL, "user", "Work"])
            with mock.patch.object(Path, "home", return_value=Path(home)):
                configure_interactively(
                    answers.read,
                    answers.read,
                    answers.write,
                )
            self.require(_CREDENTIAL not in answers.transcript())


class ProfileChoiceTests(TypedTestCase):
    """Pick which stored identity a session runs under, or add another."""

    @staticmethod
    def _stored(home: str) -> Path:
        directory = Path(home) / "profiles"
        save_profile(
            Profile(nickname="stark-dev", model="gpt-4o", base_url=_URL),
            directory / "stark-dev.json",
        )
        save_profile(
            Profile(nickname="stark-prod", model="stark-default", base_url=_URL),
            directory / "stark-prod.json",
        )
        return directory

    def test_a_single_profile_is_still_listed(self) -> None:
        """The identity a session runs under is stated, never assumed."""
        with tempfile.TemporaryDirectory() as home:
            directory = Path(home) / "profiles"
            save_profile(
                Profile(nickname="only", model="m", base_url=_URL),
                directory / "only.json",
            )
            entries = profile_entries(("only",), directory)
            answers = _Answers(["1"])
            self.equal(choose_profile(answers.read, answers.write, entries), "only")
            self.require("only" in answers.transcript())

    def test_each_entry_names_its_model(self) -> None:
        """Nicknames alone do not distinguish two configurations at a glance."""
        with tempfile.TemporaryDirectory() as home:
            directory = self._stored(home)
            entries = profile_entries(("stark-dev", "stark-prod"), directory)
            labels = [label for _, label in entries]
            self.require(any("gpt-4o" in label for label in labels))
            self.require(any("stark-default" in label for label in labels))

    def test_the_active_profile_is_marked_and_taken_by_default(self) -> None:
        """Pressing Enter must keep the identity the last session used."""
        with tempfile.TemporaryDirectory() as home:
            directory = self._stored(home)
            entries = profile_entries(("stark-dev", "stark-prod"), directory)
            answers = _Answers([""])
            self.equal(
                choose_profile(answers.read, answers.write, entries, "stark-prod"),
                "stark-prod",
            )
            self.require("(current)" in answers.transcript())

    def test_adding_another_configuration_is_always_offered(self) -> None:
        """With a profile stored, setup never runs again, so this is the only way."""
        with tempfile.TemporaryDirectory() as home:
            directory = self._stored(home)
            entries = profile_entries(("stark-dev", "stark-prod"), directory)
            answers = _Answers(["3"])
            self.equal(
                choose_profile(answers.read, answers.write, entries),
                NEW_PROFILE,
            )
            self.require("Add a new configuration" in answers.transcript())

    def test_an_answer_outside_the_list_is_refused(self) -> None:
        """A mistyped number must not silently select something else."""
        with tempfile.TemporaryDirectory() as home:
            directory = self._stored(home)
            entries = profile_entries(("stark-dev", "stark-prod"), directory)
            answers = _Answers(["9", "nonsense", "2"])
            self.equal(
                choose_profile(answers.read, answers.write, entries),
                "stark-prod",
            )
            self.require("between 1 and 3" in answers.transcript())

    def test_an_unreadable_profile_is_listed_with_its_reason(self) -> None:
        """Hiding a profile the operator created is worse than showing it is broken."""
        with tempfile.TemporaryDirectory() as home:
            directory = Path(home) / "profiles"
            directory.mkdir(parents=True)
            (directory / "broken.json").write_text("{not json", encoding="utf-8")
            entries = profile_entries(("broken",), directory)
            self.equal(len(entries), 1)
            self.require("unreadable" in entries[0][1])

    def test_cancelling_the_choice_launches_nothing(self) -> None:
        """Declining to choose must not fall back to an arbitrary identity."""
        with tempfile.TemporaryDirectory() as home:
            directory = self._stored(home)
            entries = profile_entries(("stark-dev", "stark-prod"), directory)
            answers = _Answers([])
            self.equal(choose_profile(answers.read, answers.write, entries), None)
