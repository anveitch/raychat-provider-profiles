"""Complete an incomplete provider environment from the stored profile it names."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .provider_settings import (
    BASE_URL_VARIABLE,
    CREDENTIAL_VARIABLE,
    MODEL_VARIABLE,
)
from .user_info import load_profile, load_user_info, profiles_directory

if TYPE_CHECKING:
    from collections.abc import MutableMapping
    from pathlib import Path

    from .user_info import Profile

IGNORE_PROFILES_VARIABLE = "RAYCHAT_IGNORE_PROFILES"
_REQUIRED = (CREDENTIAL_VARIABLE, MODEL_VARIABLE, BASE_URL_VARIABLE)


def active_profile(
    pointer: Path | None = None,
    directory: Path | None = None,
) -> Profile | None:
    """Load the stored identity the pointer names.

    Returns
    -------
    Profile | None
        The active identity, or None when no profile has been selected.

    Raises
    ------
    ValueError
        If the pointer names a profile that is not stored.

    """
    selected = load_user_info(pointer).active_profile
    if selected is None:
        return None
    folder = profiles_directory() if directory is None else directory
    path = folder / (selected + ".json")
    profile = load_profile(path)
    if profile is None:
        message = (
            f"The selected profile {selected!r} is not stored at {path}. "
            "Choose another profile or export the provider variables directly."
        )
        raise ValueError(message)
    return profile


def apply_stored_identity(
    environ: MutableMapping[str, str],
    pointer: Path | None = None,
    directory: Path | None = None,
) -> Profile | None:
    """Fill only the provider variables the environment leaves blank.

    Exported variables always win, so a shell, a CI job or a wrapper script
    keeps full control and nothing stored on disk can redirect a session that
    already states where it is going. The values collapse into the environment
    before anything else starts, so the one immutable identity that plugins,
    workers and subagents inherit is unchanged in kind: this only decides where
    its values came from. A pointer naming a profile that is not stored
    propagates the ValueError raised while loading it.

    Returns
    -------
    Profile | None
        The profile that supplied values, or None when none was consulted.

    """
    if environ.get(IGNORE_PROFILES_VARIABLE, "").strip():
        return None
    missing = [name for name in _REQUIRED if not environ.get(name, "").strip()]
    if not missing:
        return None
    profile = active_profile(pointer, directory)
    if profile is None:
        return None
    stored = {
        CREDENTIAL_VARIABLE: profile.auth_token,
        MODEL_VARIABLE: profile.model,
        BASE_URL_VARIABLE: profile.base_url,
    }
    for name in missing:
        value = stored[name]
        if value is not None:
            environ[name] = value
    return profile
