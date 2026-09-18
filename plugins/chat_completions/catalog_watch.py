"""Notice in the background when the selected model stops being offered."""

from __future__ import annotations

import os
import threading
from typing import TYPE_CHECKING

from raychat.first_run import fetch_models
from raychat.model_catalog import catalog_path, discovered_catalog, save_catalog
from raychat.provider_resolution import active_profile
from raychat.provider_settings import provider_settings
from raychat.sdk import StatusItem

if TYPE_CHECKING:
    from raychat.sdk import PluginAPI, PluginContext


class CatalogWatch:
    """Refresh the stored catalog once a session is running, without blocking it.

    The refresh is deliberately not part of startup. RayChat opens without any
    network request, and a provider that is slow or unreachable would otherwise
    delay every launch by its timeout. Running afterwards on its own thread
    keeps that property.

    Only one difference is worth reporting: the model this session is using is
    no longer offered. Additions and unrelated removals are recorded silently,
    because a provider that gains models routinely would otherwise train the
    operator to ignore the one report that matters.
    """

    def __init__(self, api: PluginAPI) -> None:
        """Begin a refresh once this plugin generation is configured."""
        self.checked = threading.Event()
        api.configure(self.begin)

    def begin(self, ctx: PluginContext) -> None:
        """Start the refresh on its own thread and return immediately.

        Configuration runs after registration. A handler subscribed to the
        session's own start would already have missed it in a supervised
        launch, where the session exists before plugins subscribe.
        """
        if self.checked.is_set():
            return
        self.checked.set()
        threading.Thread(
            target=self._refresh,
            args=(ctx,),
            name="raychat-catalog-refresh",
            daemon=True,
        ).start()

    @staticmethod
    def collect() -> tuple[str, str, tuple[str, ...]] | None:
        """Read the catalog for the running identity and store it.

        Returns
        -------
        tuple[str, str, tuple[str, ...]] | None
            The configuration name, the model in use and the advertised models,
            or None when nothing could be established.

        """
        settings = provider_settings(os.environ)
        profile = active_profile()
        if profile is None:
            return None
        models = fetch_models(settings.base_url, settings.auth_token)
        if not models:
            return None
        save_catalog(
            discovered_catalog(profile.nickname, settings.base_url, models),
            catalog_path(profile.nickname),
        )
        return profile.nickname, settings.model, models

    @classmethod
    def _refresh(cls, ctx: PluginContext) -> None:
        try:
            collected = cls.collect()
        except (ValueError, OSError, RuntimeError):
            # A session must never be disturbed by a background refresh. An
            # unreachable provider, a catalog this endpoint does not publish and
            # an unwritable home are all ordinary, and the chat itself reports
            # anything that actually prevents a request.
            return
        if collected is None:
            return
        _nickname, model, models = collected
        if model in models:
            return
        # A status label rather than a notification: the refresh finishes while
        # the session is still being configured, before any sink exists to show
        # a transient message, and this is worth stating for as long as it
        # remains true rather than once.
        ctx.set_status(
            "model_unavailable",
            StatusItem(
                f"{model} no longer offered · /models",
                level="warning",
                priority=95,
            ),
        )
