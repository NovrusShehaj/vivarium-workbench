"""Tests for composite_run_views.build_run_artifact's zarr-native Viz/Report
fallback — real xarray/zarr I/O throughout (no mocking of the reader layer),
exercising the actual code path a GovCloud/Ray-dispatched run hits.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from vivarium_workbench.lib.composite_runs import connect, save_metadata
from vivarium_workbench.lib.composite_run_views import build_run_artifact


def make_fake_zarr(store_path, n_steps=4):
    pytest.importorskip("xarray")
    import xarray as xr

    emit = list(range(n_steps))
    part = xr.Dataset({"time_gen=1": ("emitstep_gen=1", [float(s) for s in emit])})
    mass = xr.Dataset({"generation=1": ("emitstep_gen=1", [100.0 + s for s in emit])})
    dt = xr.DataTree.from_dict({
        "experiment_id=e/variant=0/lineage_seed=0": part,
        "experiment_id=e/variant=0/lineage_seed=0/cell_mass": mass,
    })
    dt.to_zarr(str(store_path), mode="w")


def _seed_zarr_run(ws: Path, *, run_id: str, store_path: Path):
    (ws / ".pbg").mkdir(parents=True, exist_ok=True)
    conn = connect(ws / ".pbg" / "composite-runs.db")
    save_metadata(conn, spec_id="pkg.batch_baseline", run_id=run_id,
                  params={"store_path": str(store_path)}, label="",
                  started_at=10.0, n_steps=3, log_path=None)
    conn.close()


@pytest.mark.parametrize("name", ["viz", "report"])
def test_falls_back_to_zarr_render_when_local_file_absent(tmp_path, name):
    """No .pbg/runs/<run_id>/viz.json or report.html was ever written (this
    run never executed locally) — but its store_path resolves to a real zarr
    store, so the artifact renders real data instead of 404ing."""
    ws = tmp_path
    store = ws / "runs.r1.zarr"
    make_fake_zarr(store)
    _seed_zarr_run(ws, run_id="r1", store_path=store)

    content, media_type, download_name, status = build_run_artifact(ws, "r1", name)
    assert status == 200
    assert media_type == "text/html"
    html = content.decode("utf-8")
    assert "cell_mass" in html
    assert "Plotly.newPlot" in html


def test_report_fallback_notes_no_formal_report_card(tmp_path):
    ws = tmp_path
    store = ws / "runs.r2.zarr"
    make_fake_zarr(store)
    _seed_zarr_run(ws, run_id="r2", store_path=store)

    content, _mt, _dl, status = build_run_artifact(ws, "r2", "report")
    assert status == 200
    assert "no formal report card was generated" in content.decode("utf-8")


def test_unknown_run_with_no_local_file_still_404s(tmp_path):
    (tmp_path / ".pbg").mkdir(parents=True)
    content, _mt, _dl, status = build_run_artifact(tmp_path, "no-such-run", "viz")
    assert status == 404
    assert content == b""


def test_local_viz_json_present_is_unaffected_by_the_fallback(tmp_path):
    """A run WITH a real local viz.json (the ordinary local-execution case)
    must render from that file, not fall through to the zarr path at all —
    confirms the fallback only fires when the local file is genuinely absent."""
    ws = tmp_path
    run_dir = ws / ".pbg" / "runs" / "r3"
    run_dir.mkdir(parents=True)
    (run_dir / "viz.json").write_text('{"my_viz": "<p>hand-rendered</p>"}')

    content, _mt, _dl, status = build_run_artifact(ws, "r3", "viz")
    assert status == 200
    assert b"hand-rendered" in content
    assert b"Plotly.newPlot" not in content
