"""Discover provider models without changing the environment-configured identity."""

from __future__ import annotations

import os
from dataclasses import replace
from typing import TYPE_CHECKING

from raychat.model_catalog import (
    catalog_path,
    discovered_catalog,
    load_catalog,
    save_catalog,
)
from raychat.provider_resolution import active_profile, supplied_by_profile
from raychat.provider_settings import MODEL_VARIABLE, checked_base_url
from raychat.sdk import CommandDefinition, Menu, WorkerDescriptor
from raychat.transport import run_chat_profile
from raychat.user_info import Profile, save_profile
from raychat.validation import json_object, string_list_field

_NAMED_IN_REPORT = 3

if TYPE_CHECKING:
    from raychat.sdk import PluginAPI, PluginContext

    from .client import ChatAPI


def _selected_profile() -> Profile | None:
    """Load the identity this session resolved, tolerating an unreadable store.

    Returns
    -------
    Profile | None
        The active identity, or None when none is stored or it cannot be read.

    """
    try:
        return active_profile()
    except (ValueError, OSError, RuntimeError):
        return None


def _named(names: list[str], label: str) -> str:
    shown = ", ".join(names[:_NAMED_IN_REPORT])
    more = ", …" if len(names) > _NAMED_IN_REPORT else ""
    return f"{len(names)} {label} ({shown}{more})"


def _difference(before: tuple[str, ...], after: tuple[str, ...]) -> str:
    """Describe how a freshly read catalog differs from the stored one.

    Returns
    -------
    str
        A short report, empty when the catalog is unchanged or newly stored.

    """
    if not before or before == after:
        return ""
    known, current = set(before), set(after)
    added = [name for name in after if name not in known]
    removed = [name for name in before if name not in current]
    parts = [
        _named(names, label)
        for names, label in ((added, "added"), (removed, "removed"))
        if names
    ]
    return "; ".join(parts)


class ModelMenu:
    """Keep discovery, menu callbacks and provider state in one plugin generation."""

    def __init__(self, api: PluginAPI) -> None:
        """Register discovery as a cancellable application command."""
        self.client: ChatAPI | None = None
        api.register_menu("models", self.menu)
        api.register_command(
            CommandDefinition(
                "models",
                self.command,
                while_running=True,
                scope="application",
                description="Discover provider models and view configuration guidance",
                usage="/models [filter]",
            ),
        )

    def bind(self, client: ChatAPI) -> None:
        """Retain the configured primary client for model discovery."""
        if self.client is None:
            self.client = client

    def command(self, arguments: str, ctx: PluginContext) -> str:
        """Fetch the catalog in an isolated worker, then open the cached menu.

        Returns
        -------
        str
            The discovery result and selection instructions.

        Raises
        ------
        ValueError
            This provider is not configured or returned no models.

        """
        if self.client is None:
            message = "The chat-completions provider is not active."
            raise ValueError(message)
        payload = self.client.private_payload()
        descriptor = WorkerDescriptor(
            payload["plugin"],
            "models",
            payload["source"],
            payload["options"],
            tuple(payload["secrets"]),
        )
        cancel = ctx.cancel_check or (lambda: None)
        ctx.notify("Loading provider models…")
        models = string_list_field(
            json_object(run_chat_profile(descriptor, [], cancel)),
            "models",
            allow_empty=True,
        )
        cancel()
        if not models:
            message = (
                "The provider returned no models. Your selected model is unchanged."
            )
            raise ValueError(message)
        ctx.state["models"] = models
        ctx.checkpoint()
        ctx.emit("ui", {"menu": "models", "filter": arguments.strip()})
        report = self._record(tuple(models))
        return (
            f"Loaded {len(models)} models{report}. Type to filter; "
            "Enter or click to select one."
        )

    def _record(self, models: tuple[str, ...]) -> str:
        """Store the catalog beside the active identity and describe any drift.

        Returns
        -------
        str
            A clause naming what changed, empty when nothing did.

        """
        profile = _selected_profile()
        if profile is None or self.client is None:
            return ""
        try:
            base_url = checked_base_url(self.client.url)
            path = catalog_path(profile.nickname)
            previous = load_catalog(path)
            save_catalog(discovered_catalog(profile.nickname, base_url, models))
        except (ValueError, OSError, RuntimeError):
            # Discovery succeeded; failing to cache it must not fail the command.
            return ""
        known = previous.models if previous is not None else ()
        changed = _difference(known, models)
        return f" ({changed})" if changed else ""

    def menu(self, ctx: PluginContext) -> Menu:
        """Build the menu without performing network requests during rendering.

        Returns
        -------
        Menu
            Searchable choices with the current model selected.

        """
        selected = self.client.model if self.client is not None else None
        models = string_list_field(
            ctx.state.get("models", []),
            "models",
            allow_empty=True,
        )
        return Menu(
            "Models · type to filter",
            tuple(
                (name, name + (" [current]" if name == selected else ""))
                for name in models
            ),
            self.select,
            selected=selected,
            searchable=True,
        )

    @staticmethod
    def select(identifier: str, ctx: PluginContext) -> None:
        """Store the chosen model on the active identity for the next launch."""
        profile = _selected_profile()
        if profile is None:
            ctx.notify(
                f"No stored profile to update. Set {MODEL_VARIABLE} to "
                f"{identifier} and restart RayChat to use it.",
            )
            return
        if not supplied_by_profile(os.environ, MODEL_VARIABLE):
            ctx.notify(
                f"{MODEL_VARIABLE} is set in this environment and overrides the "
                f"stored profile, so {identifier} would not take effect. Unset it "
                f"to choose the model in RayChat.",
            )
            return
        try:
            save_profile(replace(profile, model=identifier))
        except (ValueError, OSError, RuntimeError) as exc:
            ctx.notify(f"Could not update {profile.nickname!r}: {exc}")
            return
        ctx.notify(
            f"Saved {identifier} to {profile.nickname!r}. Restart RayChat to use it.",
        )
