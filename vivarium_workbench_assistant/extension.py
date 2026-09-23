"""The ``assistant`` workbench extension (entry point ``vivarium_workbench.extensions``).

Loaded by the core seam (``vivarium_workbench.lib.extensions``) only when the
operator enables it::

    pip install 'vivarium-workbench[assistant]'
    vivarium-workbench serve --workspace . --enable-extension assistant

Contributes the ``/api/ext/assistant/*`` routes, the right-side panel, the
Settings → AI Assistant section and their static assets.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter

from vivarium_workbench.lib.extensions import ExtensionAssets, ExtensionContext
from vivarium_workbench_assistant import EXTENSION_ID, policy, routes
from vivarium_workbench_assistant.chat_service import ChatService
from vivarium_workbench_assistant.secrets import install_log_redaction
from vivarium_workbench_assistant.services import AssistantServices

STATIC_DIR = Path(__file__).resolve().parent / "static"

SCRIPTS = (
    "assistant-markdown.js",
    "assistant-stream.js",
    "assistant-diff.js",
    "assistant-settings.js",
    "assistant.js",
)


class AssistantExtension:
    id = EXTENSION_ID
    title = "AI Assistant"

    def __init__(self) -> None:
        install_log_redaction()
        self.services = AssistantServices()
        self.chat = ChatService(self.services)

    def assets(self) -> ExtensionAssets:
        return ExtensionAssets(
            static_dir=STATIC_DIR,
            scripts=SCRIPTS,
            styles=("assistant.css",),
            panel=True,
            panel_label="Assistant",
            settings_section="assistant",
        )

    def register(self, router: APIRouter, ctx: ExtensionContext) -> None:
        routes.register(router, ctx, self.services, self.chat)

    def availability(self) -> tuple[bool, str]:
        return policy.availability()
