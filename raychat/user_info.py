"""Store the provider identity a user configures in the application, not a shell."""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from raychat.configuration import SETTINGS

from .provider_settings import checked_auth_token, checked_base_url, checked_model
from .validation import (
    ConfigurationError,
    integer_field,
    json_object,
    settings_fields,
    text_field,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

SCHEMA_VERSION = 1
_REQUIRED_FIELDS = ("schema_version",)
_OPTIONAL_FIELDS = ("auth_token", "model", "base_url")


@dataclass(frozen=True, kw_only=True)
class UserInfo:
    """Provider identity held on disk, with the credential kept out of repr.

    Every field is optional so that a partially configured file stays valid:
    a value supplied by the environment never needs a copy here, and a menu
    that records only the chosen model must not invent the other two.
    """

    auth_token: str | None = field(default=None, repr=False)
    model: str | None = None
    base_url: str | None = None


def user_info_path() -> Path:
    """Locate the stored identity inside the operator's RayChat home directory.

    Returns
    -------
    Path
        The configured file, which need not exist yet.

    """
    home = Path.home() / SETTINGS.storage.home_directory
    return home / SETTINGS.storage.user_info_filename


def _field_label(name: str) -> str:
    return f"{SETTINGS.storage.user_info_filename} {name}"


def _optional_value(
    fields: Mapping[str, object],
    name: str,
    check: Callable[[str, str], str],
) -> str | None:
    label = _field_label(name)
    value = text_field(fields.get(name), label, nullable=True)
    if value is None:
        return None
    return check(value, label)


def _validated(data: object) -> UserInfo:
    try:
        fields = settings_fields(
            data,
            SETTINGS.storage.user_info_filename,
            required=_REQUIRED_FIELDS,
            optional=_OPTIONAL_FIELDS,
        )
        version = integer_field(
            fields.get("schema_version"),
            _field_label("schema_version"),
            minimum=0,
        )
        if version != SCHEMA_VERSION:
            message = (
                f"{_field_label('schema_version')} must be {SCHEMA_VERSION}; "
                f"found {version}."
            )
            raise ValueError(message)
        return UserInfo(
            auth_token=_optional_value(fields, "auth_token", checked_auth_token),
            model=_optional_value(fields, "model", checked_model),
            base_url=_optional_value(fields, "base_url", checked_base_url),
        )
    except ConfigurationError as exc:
        raise ValueError(str(exc)) from exc


def load_user_info(path: Path | None = None) -> UserInfo:
    """Read the stored identity, treating an absent file as no configuration.

    Values are checked with the same rules the environment uses, so a file can
    never introduce an identity the exported variables would have rejected. A
    file that exists but cannot be read propagates the underlying OSError.

    Returns
    -------
    UserInfo
        Validated values, each None where the file omits it. Every field is
        None when the file does not exist.

    Raises
    ------
    ValueError
        If the file is a symlink, exceeds the configured size, is not UTF-8
        JSON, or holds a value the provider rules reject.

    """
    target = user_info_path() if path is None else path
    if target.is_symlink():
        message = f"{target} must be a regular file, not a symlink."
        raise ValueError(message)
    limit = SETTINGS.limits.max_config_bytes
    try:
        with target.open("rb") as stream:
            raw = stream.read(limit + 1)
    except FileNotFoundError:
        return UserInfo()
    if len(raw) > limit:
        message = f"{target} exceeds {limit} bytes."
        raise ValueError(message)
    try:
        decoded = json_object(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        message = f"{target} is not valid UTF-8 JSON."
        raise ValueError(message) from exc
    return _validated(decoded)


def save_user_info(info: UserInfo, path: Path | None = None) -> Path:
    """Replace the stored identity atomically, readable only by its owner.

    Values are revalidated here so an invalid identity cannot reach the disk,
    and the URL is normalized exactly as the environment path normalizes it.
    An interrupted write leaves the previous file intact because the temporary
    file is created in the same directory and then renamed over the target. A
    value the shared rules reject propagates their ValueError before anything
    is written; a directory or file that cannot be replaced propagates OSError.

    Returns
    -------
    Path
        The file that now holds the configuration.

    """
    target = user_info_path() if path is None else path
    payload: dict[str, object] = {"schema_version": SCHEMA_VERSION}
    if info.auth_token is not None:
        payload["auth_token"] = checked_auth_token(
            info.auth_token,
            _field_label("auth_token"),
        )
    if info.model is not None:
        payload["model"] = checked_model(info.model, _field_label("model"))
    if info.base_url is not None:
        payload["base_url"] = checked_base_url(
            info.base_url,
            _field_label("base_url"),
        )
    raw = (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    target.parent.mkdir(
        parents=True,
        exist_ok=True,
        mode=SETTINGS.storage.directory_mode,
    )
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=str(target.parent),
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        if os.name == "posix":
            temporary.chmod(SETTINGS.storage.file_mode)
        temporary.replace(target)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()
    return target
