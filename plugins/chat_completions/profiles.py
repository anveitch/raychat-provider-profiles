"""Choose which stored provider configuration later sessions should use."""

from __future__ import annotations

from typing import TYPE_CHECKING

from raychat.first_run import profile_entries
from raychat.provider_resolution import active_profile
from raychat.sdk import CommandDefinition, Menu
from raychat.user_info import (
    UserInfo,
    profile_slug,
    profiles_directory,
    save_user_info,
    stored_profile_slugs,
)

if TYPE_CHECKING:
    from raychat.sdk import PluginAPI, PluginContext
    from raychat.user_info import Profile


def _selected() -> Profile | None:
    try:
        return active_profile()
    except (ValueError, OSError, RuntimeError):
        return None


def _entries() -> list[tuple[str, str]]:
    directory = profiles_directory()
    return profile_entries(stored_profile_slugs(directory), directory)


class ProfileMenu:
    """Present the stored configurations and record which one to use next."""

    def __init__(self, api: PluginAPI) -> None:
        """Register the configuration menu as an application command."""
        api.register_menu("profiles", self.menu)
        api.register_command(
            CommandDefinition(
                "profile",
                self.command,
                while_running=True,
                scope="application",
                description="Choose the stored provider configuration to use next",
                usage="/profile [filter]",
            ),
        )

    @staticmethod
    def command(arguments: str, ctx: PluginContext) -> str:
        """Open the menu of stored configurations.

        Returns
        -------
        str
            A summary naming the active configuration.

        Raises
        ------
        ValueError
            No configuration is stored for this operator.

        """
        entries = _entries()
        if not entries:
            message = (
                "No stored configurations exist. Start RayChat without provider "
                "variables exported to create one."
            )
            raise ValueError(message)
        ctx.emit("ui", {"menu": "profiles", "filter": arguments.strip()})
        current = _selected()
        active = f"Active: {current.nickname}." if current is not None else ""
        return f"{len(entries)} stored configuration(s). {active}".strip()

    def menu(self, ctx: PluginContext) -> Menu:
        """Build the menu without reading anything the command did not already read.

        Returns
        -------
        Menu
            Searchable configurations with the active one selected.

        """
        del ctx
        current = _selected()
        selected = None if current is None else profile_slug(current.nickname)
        return Menu(
            "Configurations · type to filter",
            tuple(
                (slug, label + (" [current]" if slug == selected else ""))
                for slug, label in _entries()
            ),
            self.select,
            selected=selected,
            searchable=True,
        )

    @staticmethod
    def select(identifier: str, ctx: PluginContext) -> None:
        """Record the chosen configuration for the next launch."""
        current = _selected()
        if current is not None and profile_slug(current.nickname) == identifier:
            ctx.notify(f"{identifier} is already the active configuration.")
            return
        try:
            save_user_info(UserInfo(active_profile=identifier))
        except (ValueError, OSError, RuntimeError) as exc:
            ctx.notify(f"Could not select {identifier}: {exc}")
            return
        ctx.notify(
            f"Selected {identifier}. Restart RayChat to use it; the running "
            "session keeps the identity it started with.",
        )
