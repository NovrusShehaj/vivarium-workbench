# Custom composites

A composite is a process-bigraph document: a `name` and a `state` tree, plus
optional `description`, `tags`, `parameters`, `requires`, `author`, and
`version`. `schemaVersion` is optional. When it is omitted, the document is
schema version 1. The JSON Schema is
[`docs/schemas/composite-1.schema.json`](schemas/composite-1.schema.json).
An example is
[`docs/examples/custom-composite.composite.json`](examples/custom-composite.composite.json).

## Where the file lives

```text
<workspace>/<package_path>/composites/<stem>.composite.json
```

`package_path` comes from `workspace.yaml`. One file is one composite. The
catalog id is `<package_path>.composites.<stem>`, taken from the filename, not
from `name`. The stem must start with a letter and contain only letters,
digits, underscores, or hyphens (at most 64 characters). Nested directories are
not scanned.

You can place the file there yourself, or use **Import composite** on the
Composites tab. Both paths use the same validator. Import copies the document
into that directory; it does not keep a live link to the original path.

## Reload, replace, remove

Discovery runs when the Composites tab loads. **Reload** bypasses the short
discovery cache. Editing the file on disk shows up on the next reload.

Importing the same stem again is rejected until you confirm **Replace
existing**. Replace rewrites `<stem>.composite.json` and, if a YAML twin of
the same stem exists, removes that twin after the JSON write succeeds.

**Remove** deletes only a workspace file with that stem. Installed, federated,
and generator composites are not deleted. A broken file is listed above the
cards and does not remove the valid composites.

`$schema` is for editors. The workbench does not fetch it.
