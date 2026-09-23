"""The assistant's generated TypeScript declarations stay in sync with its models."""
from vivarium_workbench_assistant.generate_ts import OUTPUT_PATH, generate


def test_assistant_generated_ts_is_current():
    assert OUTPUT_PATH.read_text(encoding="utf-8") == generate(), (
        "assistant.generated.d.ts is stale — run `python -m vivarium_workbench_assistant.generate_ts`")


def test_contract_shapes():
    ts = generate()
    assert "export interface RunRequest {" in ts
    assert "action: 'send' | 'retry' | 'regenerate';" in ts
    assert "api_key: string | null;" in ts            # SecretStr is a string on the wire (write-only)
    assert "export interface StreamEventRunStart {" in ts
    assert "context_manifest: ContextManifestEntry[];" in ts
