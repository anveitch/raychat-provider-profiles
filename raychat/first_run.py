"""Configure a provider identity interactively when nothing is stored yet."""

from __future__ import annotations

import json
from contextlib import AbstractContextManager
from typing import TYPE_CHECKING
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request

from raychat.configuration import SETTINGS

from .http_debug import build_http_opener
from .provider_settings import (
    checked_auth_token,
    checked_base_url,
    checked_model,
)
from .user_info import (
    Profile,
    UserInfo,
    checked_instruction_role,
    checked_nickname,
    load_profile,
    profile_path,
    save_profile,
    save_user_info,
)
from .validation import (
    ConfigurationError,
    array_field,
    configuration_fields,
    json_object,
    text_field,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

_USER_AGENT = "raychat-setup/1"
_MAX_CATALOG_BYTES = 1 << 20
_DISCOVERY_TIMEOUT_SECONDS = 15.0
_MAX_LISTED = 40
_REFUSED_CREDENTIAL = frozenset({401, 403})
_NO_CATALOG = frozenset({404, 405, 501})


def _read_binary(opened: object) -> bytes:
    if not isinstance(opened, AbstractContextManager):
        message = "The catalog response does not expose a managed stream."
        raise TypeError(message)
    with opened as response:
        content: object = response.read(_MAX_CATALOG_BYTES + 1)
    if not isinstance(content, bytes):
        message = "The catalog response stream must return bytes."
        raise TypeError(message)
    return content


def fetch_models(base_url: str, auth_token: str) -> tuple[str, ...]:
    """Ask the configured endpoint which models the credential may use.

    A proxy answers this with the models the supplied key is entitled to, so
    the catalog is a property of the credential and endpoint together rather
    than of the product. A server that does not publish a catalog answers with
    an error, which the caller treats as an empty list rather than a failure.
    An unreachable or refusing endpoint propagates the underlying OSError.

    Returns
    -------
    tuple[str, ...]
        Every advertised identifier, in the order the endpoint returned it.

    Raises
    ------
    HTTPError
        If the endpoint refuses the request, including an invalid credential
        or an endpoint that publishes no catalog.
    ValueError
        If the endpoint answers with something other than a model catalog.

    """
    endpoint = urlsplit(checked_base_url(base_url) + "/models")
    headers = {
        "Accept": "application/json",
        "Authorization": "Bearer " + checked_auth_token(auth_token),
        "User-Agent": _USER_AGENT,
    }
    # The scheme is written literally so only HTTP(S) can ever be opened; the
    # checked base URL has already excluded credentials, queries and fragments.
    location = endpoint.netloc + endpoint.path
    request = (
        Request(f"https://{location}", headers=headers, method="GET")
        if endpoint.scheme == "https"
        else Request(f"http://{location}", headers=headers, method="GET")
    )
    try:
        opened: object = build_http_opener().open(
            request,
            timeout=_DISCOVERY_TIMEOUT_SECONDS,
        )
    except HTTPError as exc:
        # The error carries an open response; release it before it propagates.
        exc.close()
        raise
    raw = _read_binary(opened)
    if len(raw) > _MAX_CATALOG_BYTES:
        message = "The model catalog is larger than this client accepts."
        raise ValueError(message)
    try:
        document = configuration_fields(json_object(raw.decode("utf-8")), "catalog")
        entries = array_field(document.get("data"), "catalog.data")
        found = [
            checked_model(
                text_field(
                    configuration_fields(entry, "catalog.data[]").get("id"),
                    "catalog.data[].id",
                ),
                "catalog.data[].id",
            )
            for entry in entries
        ]
    except (ConfigurationError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        message = "The endpoint did not answer with a model catalog."
        raise ValueError(message) from exc
    unique: dict[str, None] = {}
    for name in found:
        unique.setdefault(name, None)
    return tuple(unique)


def _role_prompt() -> str:
    roles = ", ".join(SETTINGS.chat.instruction_roles)
    default = SETTINGS.chat.instruction_role
    return f"Context role for instructions ({roles}) [{default}]: "


def _asked(
    reader: Callable[[str], str],
    writer: Callable[[str], None],
    prompt: str,
    check: Callable[[str], str],
    default: str | None = None,
) -> str | None:
    """Ask until the answer passes its own rule, or the user gives up.

    Returns
    -------
    str | None
        The checked answer, or None when the user cancelled or answered blank.

    """
    while True:
        try:
            answer = reader(prompt)
        except (EOFError, KeyboardInterrupt):
            return None
        if not answer.strip():
            # A prompt that shows a default treats Enter as accepting it;
            # every other prompt treats a blank answer as giving up.
            return None if default is None else check(default)
        try:
            return check(answer)
        except ValueError as exc:
            writer(f"  {exc}")


def _discovered(
    writer: Callable[[str], None],
    base_url: str,
    auth_token: str,
    discover: Callable[[str, str], tuple[str, ...]],
) -> tuple[tuple[str, ...], bool]:
    """Read the catalog, distinguishing a refused credential from a bare server.

    Conflating the two would be actively misleading: a refused credential and a
    server without a catalog look identical at the transport, but one means the
    token is wrong and the other means nothing is wrong at all.

    Returns
    -------
    tuple[tuple[str, ...], bool]
        The advertised models, and whether the credential itself was refused.

    """
    writer("  Asking the endpoint which models this credential can use...")
    try:
        return discover(base_url, auth_token), False
    except HTTPError as exc:
        if exc.code in _REFUSED_CREDENTIAL:
            writer(f"  The endpoint refused this credential (HTTP {exc.code}).")
            return (), True
        if exc.code in _NO_CATALOG:
            writer(f"  This endpoint publishes no model catalog (HTTP {exc.code}).")
            return (), False
        writer(f"  The catalog could not be read (HTTP {exc.code}).")
        return (), False
    except (OSError, ValueError) as exc:
        writer(f"  The catalog could not be read ({exc}).")
        return (), False


def _initial_model(
    writer: Callable[[str], None],
    reader: Callable[[str], str],
    models: Sequence[str],
) -> str | None:
    """Adopt a model to start with, asking only when nothing was advertised.

    The model is not asked for here on purpose: the catalog is only known after
    the endpoint answers, and choosing from it belongs in the application where
    the whole list is visible and switching is one keystroke. The last entry the
    endpoint advertised is adopted to start from. A provider that publishes no
    catalog leaves nothing to adopt, so that case still asks.

    Returns
    -------
    str | None
        The model to start with, or None when the user cancelled.

    """
    if models:
        # The last entry the endpoint advertised. Which one is arbitrary: this
        # is a value to start from, not a recommendation, and /models replaces
        # it once the whole list is visible.
        writer(f"  Starting with {models[-1]}. Use /models to change it.")
        return checked_model(models[-1])
    writer("  This endpoint advertises no models, so name one it accepts.")
    return _asked(reader, writer, "Model: ", checked_model)


NEW_PROFILE = "+"


def profile_entries(slugs: Sequence[str], directory: Path) -> list[tuple[str, str]]:
    """Describe each stored profile for the startup list.

    A profile that cannot be read is listed with its reason rather than hidden,
    because silently omitting one the operator knows they created is worse than
    showing that it needs attention.

    Returns
    -------
    list[tuple[str, str]]
        The slug and a human label for each stored profile, in stored order.

    """
    entries: list[tuple[str, str]] = []
    for slug in slugs:
        try:
            profile = load_profile(directory / (slug + ".json"))
        except (ValueError, OSError) as exc:
            entries.append((slug, f"{slug}  (unreadable: {exc})"))
            continue
        if profile is None:
            entries.append((slug, f"{slug}  (missing)"))
            continue
        model = profile.model or "no model recorded"
        entries.append((slug, f"{profile.nickname}  [{model}]"))
    return entries


def choose_profile(
    reader: Callable[[str], str],
    writer: Callable[[str], None],
    entries: Sequence[tuple[str, str]],
    active: str | None = None,
) -> str | None:
    """Ask which stored profile to launch, or to add another.

    The list is shown even when only one profile exists, so the identity a
    session runs under is always stated rather than assumed.

    Returns
    -------
    str | None
        The chosen slug, NEW_PROFILE to add one, or None when cancelled.

    """
    writer("Which configuration should this session use?")
    default = 1
    for index, (slug, label) in enumerate(entries, start=1):
        marker = " (current)" if slug == active else ""
        if slug == active:
            default = index
        writer(f"  {index:>2}. {label}{marker}")
    writer(f"  {len(entries) + 1:>2}. Add a new configuration")
    while True:
        try:
            answer = reader(f"Choose 1-{len(entries) + 1} [{default}]: ").strip()
        except (EOFError, KeyboardInterrupt):
            return None
        if not answer:
            return entries[default - 1][0]
        if answer.isdigit():
            chosen = int(answer)
            if chosen == len(entries) + 1:
                return NEW_PROFILE
            if 1 <= chosen <= len(entries):
                return entries[chosen - 1][0]
        writer(f"  Enter a number between 1 and {len(entries) + 1}.")


def configure_interactively(
    reader: Callable[[str], str],
    secret_reader: Callable[[str], str],
    writer: Callable[[str], None],
    discover: Callable[[str, str], tuple[str, ...]] = fetch_models,
    heading: str = "RayChat is not configured yet.",
) -> Profile | None:
    """Ask for one provider identity, verify it, and store it as the active one.

    Each answer is checked with the same rule its exported variable obeys, so a
    typo is refused at the prompt rather than at the first request. The token is
    read without echoing it.

    Returns
    -------
    Profile | None
        The stored identity, or None when the user declined to finish.

    """
    writer(heading)
    writer("Answer a few questions, or press Enter alone to cancel.")
    base_url = _asked(
        reader,
        writer,
        "Server URL (the API root, ending in /v1): ",
        checked_base_url,
    )
    if base_url is None:
        return None
    while True:
        auth_token = _asked(secret_reader, writer, "API token: ", checked_auth_token)
        if auth_token is None:
            return None
        models, refused = _discovered(writer, base_url, auth_token, discover)
        if not refused:
            break
        writer("  Enter it again, or press Enter alone to cancel.")
    model = _initial_model(writer, reader, models)
    if model is None:
        return None
    role = _asked(
        reader,
        writer,
        _role_prompt(),
        checked_instruction_role,
        default=SETTINGS.chat.instruction_role,
    )
    if role is None:
        return None
    nickname = _asked(reader, writer, "Name for this configuration: ", checked_nickname)
    if nickname is None:
        return None
    profile = Profile(
        nickname=nickname,
        auth_token=auth_token,
        model=model,
        base_url=base_url,
        instruction_role=role,
    )
    save_profile(profile)
    save_user_info(UserInfo(active_profile=nickname))
    writer(f"  Saved {nickname!r} to {profile_path(nickname)}.")
    writer("  Exported variables will still override it when present.")
    return profile
