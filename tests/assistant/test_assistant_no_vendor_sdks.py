"""The assistant extension talks to providers over httpx — no vendor SDKs.

Mirror of ``tests/test_no_ai_deps.py`` (which guards the AI-free core and stays
unchanged) for the ``vivarium_workbench_assistant`` package: keeping SDKs out
keeps the extension co-installable in scientific workspace environments and
makes every provider request go through the outbound policy.
"""
from __future__ import annotations

import ast
from pathlib import Path

_FORBIDDEN_ROOTS = {
    "anthropic", "openai", "cohere", "mistralai", "ollama", "litellm", "llama_index", "replicate",
    "vertexai", "transformers", "google_generativeai", "requests", "aiohttp",
}
_FORBIDDEN_PREFIXES = ("langchain",)
_FORBIDDEN_DOTTED = ("google.generativeai", "google.genai", "google.cloud.aiplatform")
#: google-auth's own transport is allowed (it is how ADC tokens are refreshed).
_ALLOWED_DOTTED = ("google.auth", "google.oauth2")

PKG = Path(__file__).resolve().parents[2] / "vivarium_workbench_assistant"


def _imports(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module)
    return names


def _violation(dotted: str) -> bool:
    if any(dotted == a or dotted.startswith(a + ".") for a in _ALLOWED_DOTTED):
        return False
    root = dotted.split(".", 1)[0]
    if root in _FORBIDDEN_ROOTS:
        return True
    if any(root == p or root.startswith(p + "_") for p in _FORBIDDEN_PREFIXES):
        return True
    return any(dotted == d or dotted.startswith(d + ".") for d in _FORBIDDEN_DOTTED)


def test_extension_imports_no_vendor_sdk():
    offenders = []
    for path in sorted(PKG.rglob("*.py")):
        for dotted in _imports(ast.parse(path.read_text(), filename=str(path))):
            if _violation(dotted):
                offenders.append(f"{path.relative_to(PKG.parent)} imports {dotted!r}")
    assert not offenders, "\n".join(offenders)


def test_detector_fires():
    assert _violation("openai") and _violation("anthropic.types") and _violation("google.genai")
    assert not _violation("google.auth.transport.requests") and not _violation("httpx")


def test_no_exec_or_eval_in_the_extension():
    bad = []
    for path in sorted(PKG.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("exec", "eval"):
                bad.append(f"{path.name}:{node.lineno}")
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and \
                    node.func.attr in ("system", "popen") and getattr(node.func.value, "id", "") == "os":
                bad.append(f"{path.name}:{node.lineno}")
            for kw in getattr(node, "keywords", []) or []:
                if kw.arg == "shell" and isinstance(kw.value, ast.Constant) and kw.value.value is True:
                    bad.append(f"{path.name}:{node.lineno} shell=True")
    assert not bad, bad


def test_frontend_never_assigns_model_text_to_html():
    static = PKG / "static"
    for js in static.glob("*.js"):
        # Code only: drop // line comments (the renderer documents WHY it avoids these sinks).
        code = "\n".join(line for line in js.read_text().splitlines() if not line.lstrip().startswith("//"))
        for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
            assert sink not in code, f"{js.name} uses {sink}"
