/**
 * Quick-run panel for the remote Smoldyn backend (Phase 3 of the 2026-09-21
 * SMS-retirement plan, §5.2/§6-Phase-3).
 *
 * What this is — and is not: the viva-smoldyn service exposes one bounded,
 * SYNCHRONOUS operation (POST /smoldyn/v1/simulations). There is no job ID,
 * no polling, no artifact download. This panel is an honest quick-run:
 * submit → wait (bounded by the service's MAX_SECONDS + overhead) → show the
 * per-step molecule counts. It deliberately does NOT emulate the retired SMS
 * campaign dashboard (submit → poll → land → analyze); plan §7 forbids
 * rebuilding durable-job infrastructure client-side.
 *
 * Server contract (lib/smoldyn_run_views.py):
 *   POST /api/smoldyn-run {study, composite, duration[, overrides]} →
 *     200 {samples:[{time, molecule_counts}]}
 *     422 {error, violations:[...]}   — pre-flight, each violation named
 *     502 {error, reachable:false}    — service unreachable
 *     503 {error, smoldyn_configured:false} — backend hidden/unconfigured
 *   GET /api/smoldyn-status → {configured, reachable}
 *
 * Backend hidden when SMOLDYN_API_BASE is unset: the panel checks
 * /api/smoldyn-status first and removes itself — zero cost when off.
 */
(function () {
  "use strict";

  var BP = window.__BASE_PATH__ || "";

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  }

  function renderCountsTable(samples) {
    var table = el("table", "cond-table smoldyn-counts-table");
    var thead = el("thead");
    var headRow = el("tr");
    var species = [];
    Object.keys(samples[samples.length - 1].molecule_counts || {}).forEach(function (s) {
      species.push(s);
    });
    species.sort();
    headRow.appendChild(el("th", null, "time"));
    species.forEach(function (s) { headRow.appendChild(el("th", null, s)); });
    thead.appendChild(headRow);
    table.appendChild(thead);
    var tbody = el("tbody");
    samples.forEach(function (sample) {
      var row = el("tr");
      row.appendChild(el("td", null, String(sample.time)));
      species.forEach(function (s) {
        var counts = sample.molecule_counts || {};
        row.appendChild(el("td", null, counts[s] === undefined ? "—" : String(counts[s])));
      });
      tbody.appendChild(row);
    });
    table.appendChild(tbody);
    return table;
  }

  function showOutcome(out, res, body) {
    out.textContent = "";
    if (res.status === 200 && body && body.samples && body.samples.length) {
      out.appendChild(el("p", "muted", "Remote run complete — " + body.samples.length + " samples."));
      out.appendChild(renderCountsTable(body.samples));
      var dl = el("a", "action-btn", "⬇ Save JSON");
      dl.href = URL.createObjectURL(new Blob(
        [JSON.stringify(body, null, 2)], { type: "application/json" }));
      dl.download = "smoldyn-quick-run.json";
      out.appendChild(dl);
      return;
    }
    if (res.status === 422 && body && body.violations) {
      var p = el("p", null, "This composite is not remotely runnable as configured:");
      out.appendChild(p);
      var ul = el("ul");
      body.violations.forEach(function (v) { ul.appendChild(el("li", null, v)); });
      out.appendChild(ul);
      out.appendChild(el("p", "muted",
        "Local execution is still available for this composite."));
      return;
    }
    if (res.status === 502 && body && body.reachable === false) {
      out.appendChild(el("p", null, "Smoldyn service unreachable: " + (body.error || "")));
      return;
    }
    out.appendChild(el("p", null, (body && body.error) || ("Request failed (" + res.status + ")")));
  }

  function buildPanel(container, study, composite) {
    container.textContent = "";

    var title = el("h3", null, "Remote Smoldyn quick run");
    container.appendChild(title);
    container.appendChild(el("p", "muted",
      "Runs this composite on the remote Smoldyn service (bounded, synchronous — " +
      "returns molecule counts; no campaign tracking)."));

    var row = el("div", "smoldyn-quickrun-row");
    var dur = el("input");
    dur.type = "number"; dur.min = "0"; dur.step = "any";
    dur.value = "10"; dur.id = "smoldyn-quickrun-duration";
    dur.setAttribute("aria-label", "Simulation duration");
    var durLabel = el("label", null, "Duration:");
    durLabel.htmlFor = dur.id;
    row.appendChild(durLabel);
    row.appendChild(dur);
    var runBtn = el("button", "btn-mini", "▶ Run on remote Smoldyn");
    runBtn.type = "button";
    row.appendChild(runBtn);
    container.appendChild(row);

    var out = el("div", "smoldyn-quickrun-out");
    container.appendChild(out);

    runBtn.addEventListener("click", function () {
      var duration = parseFloat(dur.value);
      out.textContent = "Submitting…";
      runBtn.disabled = true;
      fetch(BP + "/api/smoldyn-run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ study: study, composite: composite, duration: duration }),
      }).then(function (res) {
        return res.json().then(function (body) { showOutcome(out, res, body); });
      }).catch(function (e) {
        out.textContent = "Request failed: " + e;
      }).finally(function () {
        runBtn.disabled = false;
      });
    });
  }

  function mount(study, composite) {
    var host = document.getElementById("smoldyn-quickrun");
    if (!host) return;
    fetch(BP + "/api/smoldyn-status").then(function (r) { return r.json(); }).then(function (s) {
      if (!s || !s.configured) { host.remove(); return; }  // backend hidden: zero cost
      buildPanel(host, study, composite);
    }).catch(function () { host.remove(); });
  }

  // Wire from the Model tab's composite cards, which carry data-study and
  // render a loom embed per composite carrying data-id (composite-card.js).
  function initFromCards() {
    var cards = document.getElementById("model-composite-cards");
    if (!cards) return;
    var study = cards.getAttribute("data-study") || "";

    function firstCard() { return cards.querySelector("[data-id]"); }

    // Cards may already be on the page: this script loads after study-detail.js,
    // and on a ?tab=compose deep link study-detail's DOMContentLoaded handler
    // renders the cards before this observer can attach — so check first,
    // and only fall back to watching for the lazy (Overview-landing) render.
    var existing = firstCard();
    if (existing) {
      mount(study, existing.getAttribute("data-id"));
      return;
    }
    var observer = new MutationObserver(function () {
      var card = firstCard();
      if (!card) return;
      observer.disconnect();
      mount(study, card.getAttribute("data-id"));
    });
    observer.observe(cards, { childList: true, subtree: true });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initFromCards);
  } else {
    initFromCards();
  }
})();
