"""Store the provider identities a user configures in the application, not a shell."""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from dataclasses import dataclass, field
from hashlib import sha256
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
MAX_NICKNAME_CHARS = 64
_MAX_SLUG_CHARS = 64
_SLUG_EXTRA = "-_"
_POINTER_FIELDS = ("schema_version",)
_POINTER_OPTIONAL = ("active_profile",)
_PROFILE_FIELDS = ("schema_version", "nickname")
_PROFILE_OPTIONAL = ("auth_token", "model", "base_url", "instruction_role")
# Windows refuses these names with any extension, on every drive.
_RESERVED_SLUGS = frozenset({
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{digit}" for digit in "123456789"),
    *(f"lpt{digit}" for digit in "123456789"),
})


@dataclass(frozen=True, kw_only=True)
class Profile:
    """One named provider identity, with the credential kept out of repr.

    The nickname is what the user typed and is stored verbatim; the file name
    is derived from it separately. Every other field is optional, so a profile
    that records only a chosen model stays valid and a value supplied by the
    environment never needs a copy here.
    """

    nickname: str
    auth_token: str | None = field(default=None, repr=False)
    model: str | None = None
    base_url: str | None = None
    instruction_role: str | None = None

    @property
    def slug(self) -> str:
        """The file name stem this profile is stored under.

        Returns
        -------
        str
            A portable name derived from the nickname.

        """
        return profile_slug(self.nickname)


@dataclass(frozen=True, kw_only=True)
class UserInfo:
    """Which stored profile the next launch should use."""

    active_profile: str | None = None


def checked_nickname(value: str, source: str = "nickname") -> str:
    """Check a name the user chose for one provider identity.

    Returns
    -------
    str
        The nickname without surrounding whitespace.

    Raises
    ------
    ValueError
        If the nickname is blank, unprintable, or longer than the limit.

    """
    nickname = value.strip()
    if not nickname:
        message = f"{source} must not be empty."
        raise ValueError(message)
    if not nickname.isprintable():
        message = f"{source} must not contain control characters."
        raise ValueError(message)
    if len(nickname) > MAX_NICKNAME_CHARS:
        message = f"{source} must be at most {MAX_NICKNAME_CHARS} characters."
        raise ValueError(message)
    return nickname


def checked_instruction_role(value: str, source: str = "instruction_role") -> str:
    """Check the message role this provider expects its instructions to carry.

    Some providers reject a system role outright and require the same text as a
    user message, so this is a property of the provider rather than a taste.

    Returns
    -------
    str
        The role, without surrounding whitespace.

    Raises
    ------
    ValueError
        If the role is not one the configuration permits.

    """
    role = value.strip().casefold()
    allowed = SETTINGS.chat.instruction_roles
    if role not in allowed:
        message = f"{source} must be one of: {', '.join(allowed)}."
        raise ValueError(message)
    return role


def profile_slug(nickname: str) -> str:
    """Derive a portable file name stem from a nickname.

    The result is lowercase ASCII because Windows and macOS compare file names
    case-insensitively, so names differing only in case would otherwise collide
    unpredictably. A nickname that survives none of this, such as one written
    entirely in another script, falls back to a stable digest of it. A nickname
    the shared check rejects propagates its ValueError.

    Returns
    -------
    str
        A name safe to use on every supported platform.

    """
    name = checked_nickname(nickname)
    kept = "".join(
        character
        if character.isascii() and (character.isalnum() or character in _SLUG_EXTRA)
        else "-"
        for character in name.casefold()
    )
    slug = "-".join(part for part in kept.split("-") if part)
    slug = slug[:_MAX_SLUG_CHARS].strip(_SLUG_EXTRA)
    if not slug or slug in _RESERVED_SLUGS:
        return "profile-" + sha256(name.encode("utf-8")).hexdigest()[:8]
    return slug


def home_directory() -> Path:
    """Locate the operator's RayChat home, as the supervisor resolves it.

    Returns
    -------
    Path
        The directory holding stored configuration, which need not exist yet.

    """
    return Path.home() / SETTINGS.storage.home_directory


def user_info_path() -> Path:
    """Locate the pointer naming the profile the next launch should use.

    Returns
    -------
    Path
        The configured file, which need not exist yet.

    """
    return home_directory() / SETTINGS.storage.user_info_filename


def profiles_directory() -> Path:
    """Locate the directory holding one file per stored identity.

    Returns
    -------
    Path
        The configured directory, which need not exist yet.

    """
    return home_directory() / SETTINGS.storage.profiles_directory


def profile_path(nickname: str) -> Path:
    """Locate the file storing one named identity.

    Returns
    -------
    Path
        The file for this nickname, which need not exist yet.

    """
    return profiles_directory() / (profile_slug(nickname) + ".json")


def stored_profile_slugs(directory: Path | None = None) -> tuple[str, ...]:
    """List the stored profiles by file name, without parsing any of them.

    Returns
    -------
    tuple[str, ...]
        Sorted slugs, empty when nothing has been stored yet.

    """
    target = profiles_directory() if directory is None else directory
    try:
        entries = sorted(
            item.name.removesuffix(".json")
            for item in target.iterdir()
            if item.is_file() and item.name.endswith(".json")
        )
    except (FileNotFoundError, NotADirectoryError):
        return ()
    return tuple(entries)


def _field_label(filename: str, name: str) -> str:
    return f"{filename} {name}"


def _optional_value(
    fields: Mapping[str, object],
    filename: str,
    name: str,
    check: Callable[[str, str], str],
) -> str | None:
    label = _field_label(filename, name)
    value = text_field(fields.get(name), label, nullable=True)
    if value is None:
        return None
    return check(value, label)


def _checked_version(fields: Mapping[str, object], filename: str) -> None:
    version = integer_field(
        fields.get("schema_version"),
        _field_label(filename, "schema_version"),
        minimum=0,
    )
    if version != SCHEMA_VERSION:
        message = (
            f"{_field_label(filename, 'schema_version')} must be "
            f"{SCHEMA_VERSION}; found {version}."
        )
        raise ValueError(message)


def read_document(path: Path) -> object:
    """Read one bounded JSON configuration file, refusing anything substituted.

    An absent file propagates FileNotFoundError rather than decoding to a
    value, so a file holding the JSON literal null stays distinguishable from
    no file at all.

    Returns
    -------
    object
        The decoded document, whatever its JSON type.

    Raises
    ------
    ValueError
        If the path is a symlink, exceeds the configured size, or does not
        decode as UTF-8 JSON.

    """
    if path.is_symlink():
        message = f"{path} must be a regular file, not a symlink."
        raise ValueError(message)
    limit = SETTINGS.limits.max_config_bytes
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        message = f"{path} exceeds {limit} bytes."
        raise ValueError(message)
    try:
        return json_object(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        message = f"{path} is not valid UTF-8 JSON."
        raise ValueError(message) from exc


def replace_document(path: Path, payload: Mapping[str, object]) -> Path:
    """Write one configuration file atomically, readable only by its owner.

    The temporary file is created in the same directory and renamed over the
    target, so an interrupted write leaves the previous file intact.

    Returns
    -------
    Path
        The file that was replaced.

    """
    raw = (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    path.parent.mkdir(
        parents=True,
        exist_ok=True,
        mode=SETTINGS.storage.directory_mode,
    )
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        if os.name == "posix":
            temporary.chmod(SETTINGS.storage.file_mode)
        temporary.replace(path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()
    return path


def load_user_info(path: Path | None = None) -> UserInfo:
    """Read which profile is active, treating an absent pointer as none.

    Returns
    -------
    UserInfo
        The stored selection, or an empty selection when nothing is stored.

    Raises
    ------
    ValueError
        If the pointer is a symlink, oversized, not UTF-8 JSON, or malformed.

    """
    target = user_info_path() if path is None else path
    try:
        document = read_document(target)
    except FileNotFoundError:
        return UserInfo()
    filename = target.name
    try:
        fields = settings_fields(
            document,
            filename,
            required=_POINTER_FIELDS,
            optional=_POINTER_OPTIONAL,
        )
        _checked_version(fields, filename)
        active = text_field(
            fields.get("active_profile"),
            _field_label(filename, "active_profile"),
            nullable=True,
        )
    except ConfigurationError as exc:
        raise ValueError(str(exc)) from exc
    if active is not None:
        active = profile_slug(active)
    return UserInfo(active_profile=active)


def save_user_info(info: UserInfo, path: Path | None = None) -> Path:
    """Record which profile the next launch should use, replacing it atomically.

    Returns
    -------
    Path
        The file that now holds the pointer.

    """
    target = user_info_path() if path is None else path
    payload: dict[str, object] = {"schema_version": SCHEMA_VERSION}
    if info.active_profile is not None:
        payload["active_profile"] = profile_slug(info.active_profile)
    return replace_document(target, payload)


def load_profile(path: Path) -> Profile | None:
    """Read one stored identity, checking it exactly as the environment is checked.

    Returns
    -------
    Profile | None
        The stored identity, or None when the file does not exist.

    Raises
    ------
    ValueError
        If the file is a symlink, oversized, not UTF-8 JSON, or holds a value
        the provider rules reject.

    """
    try:
        document = read_document(path)
    except FileNotFoundError:
        return None
    filename = path.name
    try:
        fields = settings_fields(
            document,
            filename,
            required=_PROFILE_FIELDS,
            optional=_PROFILE_OPTIONAL,
        )
        _checked_version(fields, filename)
        nickname = checked_nickname(
            text_field(fields.get("nickname"), _field_label(filename, "nickname")),
            _field_label(filename, "nickname"),
        )
        return Profile(
            nickname=nickname,
            auth_token=_optional_value(
                fields,
                filename,
                "auth_token",
                checked_auth_token,
            ),
            model=_optional_value(fields, filename, "model", checked_model),
            base_url=_optional_value(fields, filename, "base_url", checked_base_url),
            instruction_role=_optional_value(
                fields,
                filename,
                "instruction_role",
                checked_instruction_role,
            ),
        )
    except ConfigurationError as exc:
        raise ValueError(str(exc)) from exc


def save_profile(profile: Profile, path: Path | None = None) -> Path:
    """Replace one stored identity atomically, readable only by its owner.

    Values are revalidated here so an invalid identity cannot reach the disk,
    and the URL is normalized exactly as the environment path normalizes it.
    An interrupted write leaves the previous file intact because the temporary
    file is created in the same directory and then renamed over the target. A
    value the shared rules reject propagates their ValueError before anything
    is written; a directory or file that cannot be replaced propagates OSError.

    Nicknames that reduce to the same file name address the same profile, so a
    caller offering to create one should check for an existing file first.

    Returns
    -------
    Path
        The file that now holds the identity.

    """
    nickname = checked_nickname(profile.nickname)
    target = profile_path(nickname) if path is None else path
    payload: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "nickname": nickname,
    }
    if profile.auth_token is not None:
        payload["auth_token"] = checked_auth_token(profile.auth_token, "auth_token")
    if profile.model is not None:
        payload["model"] = checked_model(profile.model, "model")
    if profile.base_url is not None:
        payload["base_url"] = checked_base_url(profile.base_url, "base_url")
    if profile.instruction_role is not None:
        payload["instruction_role"] = checked_instruction_role(
            profile.instruction_role,
            "instruction_role",
        )
    return replace_document(target, payload)
