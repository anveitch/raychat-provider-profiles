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
from raychat.first_run import configure_interactively, fetch_models
from raychat.user_info import load_profile, load_user_info, profile_path, user_info_path
from tests.assertions import TypedTestCase
from tools.provider_stub import StubConfig, handler

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

_CREDENTIAL = "stub-token"
_CATALOG = ("vendor/first", "vendor/second", "vendor/third")


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
        """Choosing a model belongs in the application, where the list is visible."""
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
                self.equal(profile.model, _CATALOG[0])
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
