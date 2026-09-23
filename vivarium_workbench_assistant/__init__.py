"""Vivarium Workbench AI coding assistant — an optional, opt-in extension.

The workbench core (``vivarium_workbench``) stays AI-free: it never imports
this package (import-linter contract) and never loads it unless the operator
enables it (``VIVARIUM_WORKBENCH_EXTENSIONS=assistant`` or
``vivarium-workbench serve --enable-extension assistant``) and the
``vivarium-workbench[assistant]`` extra is installed.

What it adds, all behind the core's extension seam (``lib/extensions.py``):

* a right-side assistant panel (streamed chat over the workspace);
* Settings → AI Assistant (bring-your-own-key provider instances for
  Anthropic, OpenAI, Google Vertex AI, Google AI Studio, OpenRouter and local
  OpenAI-compatible servers such as Ollama or LM Studio);
* explicit, previewed project context; reviewed, stale-safe file proposals;
  and a bounded, approval-gated tool loop.

Provider calls always go through the workbench server (never the browser),
use ``httpx`` directly (no vendor SDKs), and pass an outbound SSRF policy.
Credentials live in the OS keychain, an environment variable, Google ADC, or
server memory — never in the browser, the workspace, URLs, logs or
transcripts. On shared (hosted), read-only and snapshot deployments the
assistant is disabled unless an operator explicitly enables it.
"""
from __future__ import annotations

__all__ = ["EXTENSION_ID"]

EXTENSION_ID = "assistant"
