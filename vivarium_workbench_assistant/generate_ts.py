"""Generate ``static/assistant.generated.d.ts`` from ``api_models.MODELS``.

Uses the core's reusable emitter (``vivarium_workbench.lib.generate_ts``) —
the extension imports the core, never the other way round.

Run:  python -m vivarium_workbench_assistant.generate_ts
``tests/assistant/test_assistant_generated_types.py`` asserts the committed
file is current.
"""
from __future__ import annotations

from pathlib import Path

from vivarium_workbench.lib.generate_ts import render_declarations
from vivarium_workbench_assistant.api_models import MODELS

OUTPUT_PATH = Path(__file__).resolve().parent / "static" / "assistant.generated.d.ts"


def generate() -> str:
    return render_declarations(
        MODELS,
        header=("// AUTO-GENERATED from vivarium_workbench_assistant/api_models.py — do not edit by hand.\n"
                "// Regenerate: python -m vivarium_workbench_assistant.generate_ts"),
    )


def main() -> None:
    OUTPUT_PATH.write_text(generate(), encoding="utf-8")
    print(f"wrote {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
