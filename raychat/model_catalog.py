"""Record which models a stored provider identity actually offers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from raychat.configuration import SETTINGS

from .provider_settings import checked_base_url, checked_model
from .user_info import (
    SCHEMA_VERSION,
    checked_nickname,
    home_directory,
    profile_slug,
    read_document,
    replace_document,
)
from .validation import (
    ConfigurationError,
    array_field,
    integer_field,
    settings_fields,
    text_field,
)

if TYPE_CHECKING:
    from pathlib import Path

_CATALOG_FIELDS = ("schema_version", "nickname", "base_url", "models", "fetched_at")
_MAX_MODELS = 4096


@dataclass(frozen=True, kw_only=True)
class ModelCatalog:
    """The catalog one identity returned, and when it was collected.

    A catalog is meaningful only next to the endpoint that produced it, so the
    base URL it was fetched from is stored with it. A stored catalog whose URL
    no longer matches the profile describes a different provider and should be
    refetched rather than shown.
    """

    nickname: str
    base_url: str
    models: tuple[str, ...]
    fetched_at: str

    @property
    def slug(self) -> str:
        """The file name stem this catalog is stored under.

        Returns
        -------
        str
            A portable name derived from the nickname.

        """
        return profile_slug(self.nickname)


def catalogs_directory() -> Path:
    """Locate the directory holding one catalog per stored identity.

    Returns
    -------
    Path
        The configured directory, which need not exist yet.

    """
    return home_directory() / SETTINGS.storage.catalogs_directory


def catalog_path(nickname: str) -> Path:
    """Locate the catalog file for one named identity.

    Returns
    -------
    Path
        The file for this nickname, which need not exist yet.

    """
    return catalogs_directory() / (profile_slug(nickname) + ".json")


def collected_now() -> str:
    """Timestamp a catalog collection in UTC, to the second.

    Returns
    -------
    str
        An ISO 8601 timestamp with an explicit UTC offset.

    """
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _checked_models(value: object, label: str) -> tuple[str, ...]:
    entries = array_field(value, label)
    if len(entries) > _MAX_MODELS:
        message = f"{label} must list at most {_MAX_MODELS} models."
        raise ValueError(message)
    seen: dict[str, None] = {}
    for index, entry in enumerate(entries):
        identifier = checked_model(
            text_field(entry, f"{label}[{index}]"),
            f"{label}[{index}]",
        )
        seen.setdefault(identifier, None)
    return tuple(seen)


def _checked_timestamp(value: object, label: str) -> str:
    stamp = text_field(value, label)
    try:
        datetime.fromisoformat(stamp)
    except ValueError as exc:
        message = f"{label} must be an ISO 8601 timestamp."
        raise ValueError(message) from exc
    return stamp


def load_catalog(path: Path) -> ModelCatalog | None:
    """Read one stored catalog, treating an absent file as nothing collected.

    Returns
    -------
    ModelCatalog | None
        The stored catalog, or None when the file does not exist.

    Raises
    ------
    ValueError
        If the file is a symlink, oversized, not UTF-8 JSON, or malformed.

    """
    try:
        document = read_document(path)
    except FileNotFoundError:
        return None
    filename = path.name
    try:
        fields = settings_fields(document, filename, required=_CATALOG_FIELDS)
        version = integer_field(
            fields.get("schema_version"),
            f"{filename} schema_version",
            minimum=0,
        )
        if version != SCHEMA_VERSION:
            message = (
                f"{filename} schema_version must be {SCHEMA_VERSION}; found {version}."
            )
            raise ValueError(message)
        return ModelCatalog(
            nickname=checked_nickname(
                text_field(fields.get("nickname"), f"{filename} nickname"),
                f"{filename} nickname",
            ),
            base_url=checked_base_url(
                text_field(fields.get("base_url"), f"{filename} base_url"),
                f"{filename} base_url",
            ),
            models=_checked_models(fields.get("models"), f"{filename} models"),
            fetched_at=_checked_timestamp(
                fields.get("fetched_at"),
                f"{filename} fetched_at",
            ),
        )
    except ConfigurationError as exc:
        raise ValueError(str(exc)) from exc


def save_catalog(catalog: ModelCatalog, path: Path | None = None) -> Path:
    """Replace one stored catalog atomically, without touching any credential.

    Refreshing a catalog rewrites only this file, so the credential stored for
    the same identity is never rewritten to record a routine discovery. Values
    are revalidated here, and duplicate identifiers a provider returned are
    collapsed while their order is preserved.

    Returns
    -------
    Path
        The file that now holds the catalog.

    """
    nickname = checked_nickname(catalog.nickname)
    target = catalog_path(nickname) if path is None else path
    payload: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "nickname": nickname,
        "base_url": checked_base_url(catalog.base_url, "base_url"),
        "models": list(_checked_models(list(catalog.models), "models")),
        "fetched_at": _checked_timestamp(catalog.fetched_at, "fetched_at"),
    }
    return replace_document(target, payload)


def discovered_catalog(
    nickname: str,
    base_url: str,
    models: tuple[str, ...],
) -> ModelCatalog:
    """Build a catalog record for identifiers just fetched from a provider.

    Returns
    -------
    ModelCatalog
        A validated record stamped with the current UTC time.

    """
    return ModelCatalog(
        nickname=checked_nickname(nickname),
        base_url=checked_base_url(base_url, "base_url"),
        models=_checked_models(list(models), "models"),
        fetched_at=collected_now(),
    )


def catalog_matches(catalog: ModelCatalog, base_url: str) -> bool:
    """Report whether a stored catalog describes the endpoint now configured.

    Returns
    -------
    bool
        True when the catalog was collected from this same API root.

    """
    return catalog.base_url == checked_base_url(base_url, "base_url")
