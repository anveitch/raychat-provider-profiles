"""Resolve the single provider identity shared by the host and every plugin."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from urllib.parse import urlsplit, urlunsplit

if TYPE_CHECKING:
    from collections.abc import Mapping

CREDENTIAL_VARIABLE = "RAYCHAT_AUTH_TOKEN"
MODEL_VARIABLE = "RAYCHAT_MODEL"
BASE_URL_VARIABLE = "RAYCHAT_BASE_URL"
_REQUIRED = (CREDENTIAL_VARIABLE, MODEL_VARIABLE, BASE_URL_VARIABLE)
_HELP = (
    "Export the missing or empty variables in the same shell that launches RayChat. "
    "Environment files are not loaded automatically. In Bash/Zsh, load your "
    "filled-in file with: set -a; . ./.env; set +a\n"
    "See environment/windows.env, environment/linux.env, or environment/macos.env "
    "and README.md for setup instructions."
)


def environment_status(environ: Mapping[str, str]) -> str:
    """Describe each required variable without revealing any configured values.

    Returns
    -------
    str
        Presence in the child process, distinguishing unset and blank values.

    """
    rows = ["Provider environment visible to RayChat (values hidden):"]
    for name in _REQUIRED:
        value = environ.get(name)
        status = (
            "missing (not exported to this process)"
            if value is None
            else "empty (or whitespace only)"
            if not value.strip()
            else "set"
        )
        rows.append(f"  {name}: {status}")
    return "\n".join(rows)


@dataclass(frozen=True, kw_only=True)
class ProviderSettings:
    """Keep validated provider identity immutable and credentials out of repr."""

    auth_token: str = field(repr=False)
    model: str
    base_url: str

    @property
    def chat_url(self) -> str:
        """The chat endpoint derived from the configured API root.

        Returns
        -------
        str
            The complete chat-completions request URL.

        """
        return self.base_url + "/chat/completions"

    @property
    def models_url(self) -> str:
        """The model catalog endpoint for the same API root.

        Returns
        -------
        str
            The complete model-discovery request URL.

        """
        return self.base_url + "/models"


def checked_auth_token(value: str, source: str = CREDENTIAL_VARIABLE) -> str:
    """Check a credential that must survive verbatim inside a request header.

    The same rule applies wherever a credential originates, so a stored or
    prompted value can never reach the provider through a weaker check than an
    exported variable. The rejected value is never echoed in the message.

    Returns
    -------
    str
        The credential without surrounding whitespace.

    Raises
    ------
    ValueError
        If the credential is blank or contains characters a header rejects.

    """
    token = value.strip()
    if not token:
        message = f"{source} must not be empty."
        raise ValueError(message)
    if any(not "!" <= character <= "~" for character in token):
        message = f"{source} must contain printable ASCII without whitespace."
        raise ValueError(message)
    return token


def checked_model(value: str, source: str = MODEL_VARIABLE) -> str:
    """Check a model identifier that must form a valid request body field.

    Returns
    -------
    str
        The model identifier without surrounding whitespace.

    Raises
    ------
    ValueError
        If the identifier is blank or carries control characters.

    """
    model = value.strip()
    if not model:
        message = f"{source} must not be empty."
        raise ValueError(message)
    if not model.isprintable():
        message = f"{source} must be a model ID without control characters."
        raise ValueError(message)
    return model


def checked_base_url(value: str, source: str = BASE_URL_VARIABLE) -> str:
    """Normalize an API root and reject anything that could redirect traffic.

    A URL ending in the chat-completions path is accepted and reduced back to
    its root, so both spellings resolve to one identity.

    Returns
    -------
    str
        The normalized API root, without a trailing separator.

    Raises
    ------
    ValueError
        If the value is not an absolute credential-free HTTP(S) URL.

    """
    message = (
        f"{source} must be an absolute HTTP(S) API base URL "
        "with a hostname and no credentials, query, fragment, or whitespace."
    )
    candidate = value.strip()
    if not candidate:
        empty_message = f"{source} must not be empty."
        raise ValueError(empty_message)
    if any(
        character.isspace() or not character.isprintable() for character in candidate
    ):
        raise ValueError(message)
    try:
        parsed = urlsplit(candidate)
        port = parsed.port
        valid = (
            parsed.scheme in {"http", "https"}
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
            and not parsed.query
            and not parsed.fragment
            and (port is None or port > 0)
            and "\\" not in candidate
        )
    except ValueError:
        raise ValueError(message) from None
    if not valid:
        raise ValueError(message)
    path = parsed.path.rstrip("/").removesuffix("/chat/completions").rstrip("/")
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def provider_settings(environ: Mapping[str, str]) -> ProviderSettings:
    """Require one complete environment configuration without provider defaults.

    Returns
    -------
    ProviderSettings
        A validated snapshot shared by chat, delegated work and optimization.

    Raises
    ------
    ValueError
        Required variables are missing, blank, or contain invalid values.

    """
    missing = [name for name in _REQUIRED if not environ.get(name, "").strip()]
    if missing:
        message = "Missing required environment variables: " + ", ".join(missing)
        raise ValueError(message + ".\n" + environment_status(environ) + "\n" + _HELP)
    return ProviderSettings(
        auth_token=checked_auth_token(environ[CREDENTIAL_VARIABLE]),
        model=checked_model(environ[MODEL_VARIABLE]),
        base_url=checked_base_url(environ[BASE_URL_VARIABLE]),
    )
