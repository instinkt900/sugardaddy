// Desktop UI: range control, main chart with dose/meal markers, editable tables
// for insulin, composite meals (plates of foods), the food library, and saved
// meal templates. Live auto-refresh, paused while anything is mid-edit.
(function () {
  // Swap the range picker's datetime-local inputs for 24-hour ones before any
  // code reads or assigns their .value. Rows built later upgrade their own.
  SD.timeFields();

  // Read a comma-joined list stashed in a <template> element. A <template>'s
  // text lives in its .content fragment, so el.textContent is empty — use
  // .content.textContent (falling back to textContent for non-template hosts).
  function dataList(id) {
    const el = document.getElementById(id);
    if (!el) return "";
    return (el.content ? el.content.textContent : el.textContent) || "";
  }
  const KINDS = (dataList("kinds-data") || "bolus").split(",");
  const MEAL_TYPES = dataList("mealtypes-data").split(",").filter(Boolean);
  const cap = (s) => (s ? s.charAt(0).toUpperCase() + s.slice(1) : "");
  let chart = null;
  let FOODS = []; // cached food library for item pickers/datalist

  function roundRect(ctx, x, y, w, h, r) {
    if (ctx.roundRect) { ctx.beginPath(); ctx.roundRect(x, y, w, h, r); return; }
    ctx.beginPath();
    ctx.moveTo(x + r, y);
    ctx.arcTo(x + w, y, x + w, y + h, r);
    ctx.arcTo(x + w, y + h, x, y + h, r);
    ctx.arcTo(x, y + h, x, y, r);
    ctx.arcTo(x, y, x + w, y, r);
    ctx.closePath();
  }

  // A pill at the tip of the glucose line showing the latest reading.
  const lastValueTag = {
    id: "lastValueTag",
    afterDatasetsDraw(chart) {
      const meta = chart.getDatasetMeta(0); // dataset 0 = glucose line
      const pts = chart.data.datasets[0] && chart.data.datasets[0].data;
      if (!meta || !meta.data.length || !pts || !pts.length) return;
      const pt = meta.data[meta.data.length - 1];
      const val = pts[pts.length - 1].y;
      if (pt == null || val == null) return;
      const { ctx, chartArea: a } = chart;
      const label = `${val}`;
      ctx.save();
      ctx.font = `700 13px ${SD.tok("--font")}`;
      const pad = 7, w = ctx.measureText(label).width + pad * 2, h = 20;
      let bx = pt.x + 9;
      if (bx + w > a.right) bx = pt.x - 9 - w; // flip inward near the right edge
      let by = Math.max(a.top, Math.min(pt.y - h / 2, a.bottom - h));
      ctx.beginPath();
      ctx.arc(pt.x, pt.y, 3.5, 0, Math.PI * 2);
      ctx.fillStyle = SD.tok("--accent");
      ctx.fill();
      roundRect(ctx, bx, by, w, h, 6);
      ctx.fillStyle = SD.tok("--accent");
      ctx.fill();
      ctx.fillStyle = SD.tok("--accent-ink");
      ctx.textBaseline = "middle";
      ctx.textAlign = "left";
      ctx.fillText(label, bx + pad, by + h / 2 + 0.5);
      ctx.restore();
    },
  };

  // Vertical crosshair that follows the mouse and labels the time under it.
  const crosshair = {
    id: "crosshair",
    afterEvent(chart, args) {
      const e = args.event;
      const a = chart.chartArea;
      let x = null;
      if (e.type !== "mouseout" && e.x != null &&
          e.x >= a.left && e.x <= a.right && e.y >= a.top && e.y <= a.bottom) {
        x = e.x;
      }
      if (chart._crosshairX !== x) { chart._crosshairX = x; args.changed = true; }
    },
    afterDraw(chart) {
      const x = chart._crosshairX;
      if (x == null) return;
      const { ctx, chartArea: a, scales } = chart;
      ctx.save();
      ctx.beginPath();
      ctx.moveTo(x, a.top);
      ctx.lineTo(x, a.bottom);
      ctx.lineWidth = 1;
      ctx.strokeStyle = SD.alpha(SD.tok("--text-2"), 0.6);
      ctx.setLineDash([4, 3]);
      ctx.stroke();
      ctx.setLineDash([]);
      // Time label, kept inside the plot area.
      const label = SD.tableStamp(scales.x.getValueForPixel(x));
      ctx.font = `12px ${SD.tok("--font")}`;
      const pad = 5, w = ctx.measureText(label).width + pad * 2, h = 18;
      let bx = x + 6;
      if (bx + w > a.right) bx = x - 6 - w;
      const by = a.top + 2;
      ctx.fillStyle = SD.tok("--surface");
      ctx.strokeStyle = SD.tok("--line");
      ctx.fillRect(bx, by, w, h);
      ctx.strokeRect(bx, by, w, h);
      ctx.fillStyle = SD.tok("--text");
      ctx.textBaseline = "top";
      ctx.fillText(label, bx + pad, by + 3);
      ctx.restore();
    },
  };

  // ---- persisted view settings ----
  // Which window you are looking at and which series you have switched off are
  // *view* state, not data — but they were re-chosen on every reload, so coming
  // back to the dashboard meant rebuilding the same view by hand each time.
  // localStorage rather than a cookie: it never rides along on a request, and
  // this is per-device state by nature (the big screen and the tablet want
  // different answers). Losing it costs nothing, so every read and write is
  // best-effort — storage can be disabled or full, and neither is worth an error.
  const PREFS_KEY = "sd.desktop.view";
  let prefs = {};
  try { prefs = JSON.parse(localStorage.getItem(PREFS_KEY)) || {}; } catch (e) { prefs = {}; }
  function savePrefs(patch) {
    prefs = { ...prefs, ...patch };
    try { localStorage.setItem(PREFS_KEY, JSON.stringify(prefs)); } catch (e) { /* not worth it */ }
  }

  // ---- range ----
  function nowInput(offsetHours = 0) {
    const d = new Date(Date.now() - offsetHours * 3600e3);
    d.setSeconds(0, 0);
    const p = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
  }
  const fromEl = document.getElementById("range-from");
  const toEl = document.getElementById("range-to");

  function rangeEpochs() {
    const from = Math.floor(new Date(fromEl.value).getTime() / 1000);
    const to = Math.floor(new Date(toEl.value).getTime() / 1000);
    return { from, to };
  }

  const liveEl = document.getElementById("range-live");

  // Which preset is active doubles as the live/frozen flag: with one selected the
  // window tracks "now" and auto-refreshes, with a hand-typed range it holds
  // still. Passing null means "custom range" and freezes refresh.
  // Scoped to .range-picker throughout: the daily-intake panel reuses the
  // .range-presets look for its own day picker, and an unscoped selector would
  // wire the header's handler onto those buttons too — clicking "3d" there set the
  // chart window from a non-existent data-hours and blanked this picker's
  // highlight.
  function setPreset(btn) {
    document.querySelectorAll(".range-picker .range-presets button")
      .forEach((x) => x.classList.toggle("active", x === btn));
    if (liveEl) {
      liveEl.textContent = btn ? "live" : "frozen";
      liveEl.classList.toggle("frozen", !btn);
    }
  }
  function activePreset() {
    return document.querySelector(".range-picker .range-presets button.active");
  }

  // Point the window at the last N hours. Shared by the click handlers and the
  // restore-on-load path so a remembered preset lands in exactly the state
  // clicking it would have.
  function selectPreset(btn) {
    setPreset(btn);
    fromEl.value = nowInput(parseInt(btn.dataset.hours, 10));
    toEl.value = nowInput(0);
  }

  document.querySelectorAll(".range-picker .range-presets button").forEach((b) => {
    b.addEventListener("click", () => {
      selectPreset(b);
      // Remember the preset, not the window it produced: it means "the last 12
      // hours", which is a different span tomorrow.
      savePrefs({ preset: parseInt(b.dataset.hours, 10), from: null, to: null });
      load();
    });
  });
  // Typing a range detaches from the presets. Without this the last-clicked
  // preset stayed active and autoRefresh() wrote the window straight back.
  const manualRange = () => {
    setPreset(null);
    // A hand-typed window is a fixed pair of instants, so it is stored as one.
    savePrefs({ preset: null, from: fromEl.value, to: toEl.value });
    load();
  };
  fromEl.addEventListener("change", manualRange);
  toEl.addEventListener("change", manualRange);

  // ---- load everything ----
  // Editing `from` then `to` fires two loads back to back, and their responses
  // can land out of order — leaving the chart on the earlier request's window.
  // Only the newest load is allowed to render; stale ones are dropped.
  let loadSeq = 0;

  function load() {
    const seq = ++loadSeq;
    const { from, to } = rangeEpochs();
    const qs = `?from=${from}&to=${to}`;
    Promise.all([
      fetch("/api/timeline" + qs).then((r) => r.json()),
      fetch("/api/entries" + qs).then((r) => r.json()),
      fetch("/api/stats" + qs).then((r) => r.json()),
      fetch("/api/foods").then((r) => r.json()),
      fetch("/api/meal-templates").then((r) => r.json()),
      fetch("/api/missing-carbs").then((r) => r.json()),
    ]).then(([timeline, entries, stats, foods, templates, todo]) => {
      if (seq !== loadSeq) return; // superseded by a newer load
      FOODS = foods;
      populateFoodsDatalist();
      renderChart(timeline);
      renderTables(entries);
      renderStats(stats);
      renderFoods(foods);
      renderTemplates(templates);
      renderTodo(todo);
      loadPhone();
      // Its own window, but still refreshed here so an edit or a new entry shows
      // up in the rollup without needing the day picker touched.
      loadDaily();
    });
  }

  // ---- chart ----
  // The most recent timeline payload. The chart's own callbacks read from this
  // rather than closing over an argument, so they stay correct across the
  // in-place updates below instead of being frozen at build time.
  let chartPayload = null;

  // Collapse a note to one tooltip-sized line. Chart.js draws a tooltip label as
  // a single unwrapped run, so a multi-line note would spill off the chart.
  function oneLine(s, max) {
    const t = (s || "").replace(/\s+/g, " ").trim();
    return t.length > max ? t.slice(0, max - 1) + "…" : t;
  }

  // Dose/meal/note markers sit in a row along the foot of the chart. Only a
  // reading above the fixed top moves them, so they hold still across refreshes.
  function chartSeries(data) {
    const glucose = data.glucose.map((p) => ({ x: p.t, y: p.v }));
    const ys = glucose.map((p) => p.y);
    const yMax = ys.length ? Math.max(...ys) : 0;
    const { dose: doseY, meal: mealY, note: noteY } = SD.markerRow(data.units, yMax);
    return {
      glucose,
      smoothed: (data.smoothed || []).map((p) => ({ x: p.t, y: p.v })),
      iob: (data.iob || []).map((p) => ({ x: p.t, y: p.v })),
      activity: (data.activity || []).map((p) => ({ x: p.t, y: p.v })),
      doses: data.doses.map((d) => ({ x: d.t, y: doseY, kind: d.kind, label: `${d.units}u ${d.kind}` })),
      meals: data.meals.map((m) => ({
        x: m.t, y: mealY,
        label: m.label + (m.total_carbs != null ? ` (${m.total_carbs}g)` : ""),
      })),
      // A note has no value of its own — it is an annotation on whatever the
      // glucose was doing around it, so it only ever gets a marker and a stamp.
      notes: (data.notes || []).map((n) => ({ x: n.t, y: noteY, label: oneLine(n.text, 70) })),
    };
  }

  // Legend order, stated outright rather than fallen out of dataset order or the
  // `order` values (which exist for draw stacking and shouldn't have to serve two
  // masters). Lines first, then markers: the continuous series are what you read
  // the chart *along*, the markers are events dropped onto it, and grouping them
  // that way means the legend stops interleaving the two kinds.
  const LEGEND_ORDER = [
    "Glucose (smoothed)",
    "Sensor readings",
    "Insulin active (u)",
    "Insulin activity (u/hr)",
    "Insulin",
    "Meal",
    "Note",
  ];
  // Anything unlisted sorts to the end rather than to the front, so adding a
  // dataset and forgetting this list degrades to "appended" instead of "hijacks
  // the first slot".
  const legendRank = (item) => {
    const i = LEGEND_ORDER.indexOf(item.text);
    return i === -1 ? LEGEND_ORDER.length : i;
  };
  const legendOrder = (a, b) => legendRank(a) - legendRank(b);

  // Which series are switched off, keyed by legend label. By label and not by
  // index on purpose: an index would quietly re-map onto a different series the
  // next time a dataset is added or reordered, so yesterday's hidden IOB curve
  // would come back as a hidden glucose line. An unrecognised label is simply
  // ignored, which is also what makes renaming a dataset safe.
  function saveHidden() {
    if (!chart) return;
    savePrefs({
      hidden: chart.data.datasets
        .filter((ds, i) => !chart.isDatasetVisible(i))
        .map((ds) => ds.label),
    });
  }

  function renderChart(data) {
    const ctx = document.getElementById("main-chart");
    if (typeof Chart === "undefined") return;
    chartPayload = data;
    const s = chartSeries(data);

    // Once built, refresh the existing chart rather than replacing it. Chart.js
    // keeps per-dataset legend visibility on the instance, so the old
    // destroy()/new Chart() pair silently switched every hidden series back on
    // at each auto-refresh — which made a chart impossible to read for long.
    if (chart) {
      const sets = chart.data.datasets;
      [s.glucose, s.smoothed, s.doses, s.meals, s.notes, s.iob, s.activity]
        .forEach((d, i) => { sets[i].data = d; });
      chart.options.scales.x.min = data.from;
      chart.options.scales.x.max = data.to;
      chart.update();
      return;
    }

    const config = {
      data: {
        datasets: [
          // The raw readings, kept but pushed back: thin, grey and translucent.
          // They are what the sensor actually said, so they stay on the chart and
          // stay legible — but roughly ±0.8 mmol/L of what they show is the
          // sensor's own ringing (see analysis.smooth_glucose), and at full
          // strength that noise is what the eye follows. Dataset 0 by design:
          // lastValueTag reads it for the "now" badge, which must be a real
          // reading rather than a derived one.
          { type: "line", label: "Sensor readings", data: s.glucose,
            borderColor: SD.tok("--chart-raw"), borderWidth: 1,
            pointRadius: 0, tension: 0.3, parsing: false, order: 1 },
          // The trend, with that ringing filtered out. This is the line to read,
          // so it gets the accent colour and the weight. Labelled "smoothed"
          // rather than plain "Glucose" on purpose: it is derived, and the legend
          // should not imply it is the measurement.
          { type: "line", label: "Glucose (smoothed)", data: s.smoothed,
            borderColor: SD.tok("--accent"), borderWidth: 2.5,
            pointRadius: 0, tension: 0.3, fill: false, parsing: false, order: 0 },
          // order:1 alongside the raw readings — draw stacking only. Where these
          // land in the legend is LEGEND_ORDER's business now, not `order`'s.
          { type: "scatter", label: "Insulin", data: s.doses,
            borderColor: (c) => SD.doseColor(c.raw && c.raw.kind),
            backgroundColor: (c) => SD.doseColor(c.raw && c.raw.kind),
            pointStyle: "triangle", radius: 7, parsing: false, order: 1 },
          { type: "scatter", label: "Meal", data: s.meals, borderColor: SD.tok("--food"),
            backgroundColor: SD.tok("--food"), pointStyle: "rectRot", radius: 7,
            parsing: false, order: 1 },
          // Rose, and the only round marker: a note is a different *kind* of
          // thing from a dose or a meal — not a quantity that happened at a time,
          // but a reason the numbers around it may not compare to other days.
          // Distinct enough from the orange meals to pick out at a glance.
          { type: "scatter", label: "Note", data: s.notes, borderColor: SD.tok("--note"),
            backgroundColor: SD.tok("--note"), pointStyle: "circle", radius: 6,
            parsing: false, order: 1 },
          // Active-insulin (IOB) curve on its own right-hand axis, translucent so
          // it never hides anything. order:2 puts it at the back of the stack:
          // smoothed trend (0) in front of the raw readings (1) in front of these.
          // Held back on purpose: it is a large filled area, so at full strength
          // it carries more visual weight than the glucose line it is meant to
          // give context to. Faint enough to read the shape from, quiet enough
          // that the eye doesn't land on it first.
          { type: "line", label: "Insulin active (u)", data: s.iob, yAxisID: "y1",
            borderColor: SD.alpha(SD.tok("--insulin"), 0.45), backgroundColor: SD.alpha(SD.tok("--insulin"), 0.07),
            borderWidth: 1.5, pointRadius: 0, tension: 0.3, fill: true, parsing: false, order: 2 },
          // Insulin action *rate* (derivative of IOB) on a hidden auto-scaled
          // axis — dashed line, no fill, so it reads as the "how hard it's working
          // now" companion to the filled on-board area. Shape/timing is the point.
          { type: "line", label: "Insulin activity (u/hr)", data: s.activity, yAxisID: "y2",
            borderColor: SD.alpha(SD.tok("--insulin"), 0.8), borderWidth: 1.5, borderDash: [5, 4],
            pointRadius: 0, tension: 0.3, fill: false, parsing: false, order: 2 },
        ],
      },
      options: {
        animation: false,
        interaction: { mode: "nearest", intersect: true },
        plugins: {
          legend: {
            labels: { color: SD.tok("--text"), sort: legendOrder },
            // Keep Chart.js's own show/hide behaviour and record the result —
            // wrapping the default rather than reimplementing it means the
            // toggle keeps working if the library's version of it changes.
            onClick(e, item, legend) {
              Chart.defaults.plugins.legend.onClick.call(this, e, item, legend);
              saveHidden();
            },
          },
          tooltip: { callbacks: {
            // On a linear axis Chart.js titles the tooltip with the raw x value,
            // so hovering a line read "1,785,182,204,000"; the dose/meal scatters
            // got no title at all, leaving no way to see when a marker was. Both
            // want the same stamp the crosshair uses.
            title: (items) => {
              const pt = items.length ? items[0] : null;
              const x = pt ? (pt.parsed ? pt.parsed.x : null) ?? (pt.raw ? pt.raw.x : null) : null;
              return x == null ? "" : SD.tableStamp(x);
            },
            label: (c) => {
              if (c.raw.label) return c.raw.label;
              if (c.dataset.yAxisID === "y1") return `${c.parsed.y} u active`;
              if (c.dataset.yAxisID === "y2") return `${c.parsed.y} u/hr acting`;
              // The smoothed series carries an extra decimal so the line renders
              // as a curve rather than a staircase, but a glucose value is quoted
              // to 0.1 mmol/L (or a whole mg/dL) — round it back for the text.
              const dp = chartPayload.units === "mmol/L" ? 1 : 0;
              return `${c.parsed.y.toFixed(dp)} ${chartPayload.units}`;
            },
          } },
        },
        scales: {
          // Pinned to the requested window: left to itself the axis would
          // autoscale to the data, so a stale last reading or an ingest gap made
          // the plotted lines shrink away from the (fixed) chart area edges.
          x: { type: "linear", min: data.from, max: data.to,
               ticks: { color: SD.tok("--text-2"), maxTicksLimit: 10,
                        callback: (v) => SD.axisStamp(v, chartPayload.to - chartPayload.from) },
               grid: { color: SD.tok("--chart-grid") } },
          // Glucose on the RIGHT, insulin on the left — the opposite of the
          // conventional primary-axis-left layout, and deliberate. The chart ends
          // at now, so the reading being read off it is always the rightmost
          // point; putting its scale on the far side of the chart from where the
          // eye already is invites reading the value against the insulin axis.
          // The scale that matters most sits next to the value that matters most.
          // Based at zero, always. Autoscaling the bottom means the vertical
          // distance to a low depends on whatever else is in the window, so the
          // same 3.2 reading can look alarming on one day's chart and ordinary
          // on another. A fixed floor makes the height of the trace comparable
          // across every window you load.
          y: { position: "right", min: 0, suggestedMax: SD.chartTop(data.units),
               ticks: { color: SD.tok("--text-2") }, grid: { color: SD.tok("--chart-grid") },
               title: { display: true, text: data.units, color: SD.tok("--text-2") } },
          y1: { position: "left", beginAtZero: true, ticks: { color: SD.tok("--text-2") },
                grid: { display: false },
                title: { display: true, text: "insulin (u)", color: SD.tok("--text-2") } },
          // hidden: auto-scales the activity curve for shape comparison only
          y2: { position: "left", beginAtZero: true, display: false },
        },
      },
      plugins: [SD.targetBand(data.target_low, data.target_high), lastValueTag, crosshair],
    };

    // Restore the series that were switched off. Set on the dataset before
    // construction rather than toggled afterwards, so a hidden series is never
    // painted once and then pulled away on first render.
    // Array-checked, not just defaulted: `new Set` throws on a non-iterable, and
    // an exception here would take the whole chart down over a stale preference.
    const off = new Set(Array.isArray(prefs.hidden) ? prefs.hidden : []);
    config.data.datasets.forEach((ds) => { if (off.has(ds.label)) ds.hidden = true; });

    chart = new Chart(ctx, config);
  }
  // The palette is read once when the chart is built, so a theme flip rebuilds
  // it. renderChart() keeps hidden series via prefs, so nothing is lost.
  SD.onTheme(() => {
    if (!chart || !chartPayload) return;
    chart.destroy();
    chart = null;
    renderChart(chartPayload);
  });

  // ---- stats ----
  function renderStats(s) {
    const sm = s.summary;
    const u = sm.units;
    const pct = (v) => (v == null ? "—" : v + "%");
    document.getElementById("stat-tir").textContent = pct(sm.tir_percent);
    document.getElementById("stat-avg").textContent = sm.avg_display == null ? "—" : `${sm.avg_display} ${u}`;
    document.getElementById("stat-gmi").textContent = sm.gmi_percent == null ? "—" : `${sm.gmi_percent}%`;
    document.getElementById("stat-lowhigh").textContent = `${sm.low_count} / ${sm.high_count}`;
    document.getElementById("stat-count").textContent = sm.reading_count;

    const tb = document.querySelector("#postmeal-table tbody");
    tb.innerHTML = "";
    // The experimental bolus reference is opt-in: it only exists once an ISF is
    // configured, so show its columns (and the caveat) only when data carries it.
    const showRef = (s.post_meal || []).some((p) => p.ref);
    document.getElementById("postmeal-table").classList.toggle("show-ref", showRef);
    (s.post_meal || []).forEach((p) => {
      const tr = document.createElement("tr");
      // faint "·" placeholder where there was no bolus / no active IOB
      const dose = (u) => (u ? `${u}u` : `<span class="muted">·</span>`);
      // Experimental reference columns. Always emitted so header and body stay in
      // step; CSS hides them unless the table carries .show-ref. The title holds
      // the component breakdown, so a gap against the dose given is diagnosable.
      const r = p.ref || {};
      // A figure built from only *some* of its inputs (e.g. carbs never logged)
      // is still shown — the correction half is real — but must be marked "*",
      // matching the report, so a small number can't read as "barely dose here"
      // when the carb half is simply absent.
      const miss = r.missing || [];
      // Two ways to be incomplete: an input the server had none of at all, and a
      // plate where the carbs it did have came from only some of the items. The
      // second still produces a figure (a floor), so it is marked, not withheld.
      const partial = miss.length > 0 || !!p.ref_carbs_partial;
      const gaps = miss.slice();
      if (p.ref_carbs_partial) gaps.push("a carb count on every item");
      const why = partial ? `incomplete — no ${gaps.join(", ")} for this meal` : (p.ref_note || "");
      const ref = r.suggested_units == null
        ? `<td class="ref-col muted" title="${esc(why)}">—</td>
           <td class="ref-col"></td>`
        : `<td class="ref-col ${partial ? "ref-partial" : ""}" title="${esc(why)}">
             ${r.suggested_units}u${partial ? "*" : ""}</td>
           <td class="ref-col ${partial ? "ref-partial" : p.ref_delta_units > 0 ? "ref-over" : p.ref_delta_units < 0 ? "ref-under" : ""}"
               title="${esc(why)}">
             ${p.ref_delta_units > 0 ? "+" : ""}${p.ref_delta_units}${partial ? "*" : ""}</td>`;
      // A meal logged minutes ago is in the table straight away, with its carbs,
      // bolus, IOB and reference — the glucose columns fill in as the sensor
      // catches up. Blanks here mean "not yet", so they must not render as 0.
      const g = (v) => (v == null ? `<span class="muted">·</span>` : v);
      tr.classList.toggle("pending", !!p.pending);
      tr.innerHTML = `<td>${p.local}</td><td>${esc(p.description) || "(meal)"}</td>
        <td>${p.carbs_g ?? ""}</td><td>${dose(p.bolus_units)}</td><td>${dose(p.iob_start_units)}</td>${ref}
        <td>${g(p.start_display)}</td><td>${g(p.peak_display)}</td>
        <td>${p.peak_delta_display == null ? `<span class="muted" title="response still to come">awaiting</span>` : p.peak_delta_display}</td>
        <td>${p.minutes_to_peak == null ? `<span class="muted">·</span>` : p.minutes_to_peak + "m"}</td>
        <td>${g(p.end_display)}</td>`;
      tb.appendChild(tr);
    });
  }

  // ---- daily intake rollup ----
  // Deliberately plain: what went in each day and how much mealtime insulin met
  // it. Basal is excluded upstream (see analysis.daily_intake) because one nightly
  // long-acting dose would dwarf the boluses this sits beside.
  //
  // This panel keeps its OWN range, independent of the chart's picker. Sharing it
  // meant a 24h window sliced the oldest day in half and the table showed a
  // fraction of a day's intake beside whole ones — indistinguishable from a light
  // day. /api/daily always starts at local midnight instead.
  let dailyDays = 7;

  let dailyUnits = "";

  function loadDaily() {
    fetch(`/api/daily?days=${dailyDays}`)
      .then((r) => r.json())
      .then((d) => { dailyUnits = d.units || ""; renderDaily(d.rows); })
      .catch(() => {});
  }

  // `reload` is false only on the restore-at-startup path: load() fetches the
  // rollup on its own a moment later, and firing both raced two responses into
  // the same table for no gain.
  function setDailyDays(n, reload = true) {
    // Clamped to the same bounds the endpoint enforces, and written back to the
    // field: typing 999 and getting 365 days of rows while the box still reads 999
    // is the kind of small lie that makes you distrust the whole table.
    // `|| 7` would swallow a typed 0 as "no value"; only a genuinely unparseable
    // field (cleared) falls back, and 0 clamps to 1 the way the endpoint does.
    const parsed = parseInt(n, 10);
    dailyDays = Number.isNaN(parsed) ? 7 : Math.max(1, Math.min(parsed, 365));
    document.querySelectorAll(".day-presets button").forEach((b) =>
      b.classList.toggle("active", parseInt(b.dataset.days, 10) === dailyDays)
    );
    const input = document.getElementById("daily-days");
    if (input) input.value = dailyDays;
    savePrefs({ days: dailyDays });
    if (reload) loadDaily();
  }

  document.querySelectorAll(".day-presets button").forEach((b) => {
    b.addEventListener("click", () => setDailyDays(b.dataset.days));
  });
  const dailyInput = document.getElementById("daily-days");
  if (dailyInput) {
    // "change", not "input": re-fetching per keystroke would request 3 days on the
    // way to typing 30.
    dailyInput.addEventListener("change", () => setDailyDays(dailyInput.value));
  }

  function renderDaily(rows) {
    const tb = document.querySelector("#daily-table tbody");
    if (!tb) return;
    const list = (rows || []).slice().reverse(); // newest day first, like the entry tables
    tb.innerHTML = "";

    // A figure built from only some of its inputs is still shown — what was
    // recorded is real — but it has to say so, or a light day and a badly-logged
    // one look identical. Same "*" convention as the post-meal reference. `why` is
    // the whole explanation because the reasons differ: unlogged carbs make a
    // total a floor, while a half-covered sensor makes an average unrepresentative.
    const mark = (complete, why) =>
      complete ? "" : `<span class="partial" title="${why}">*</span>`;
    const missing = (what) =>
      `not every meal that day has ${what}, so this is a floor, not a total`;
    const thin = "the sensor did not cover the whole day, so this average only spans the hours it was on";
    const cell = (n, unit, complete, what) =>
      n ? `${n}${unit}${mark(complete, missing(what))}` : `<span class="muted">·</span>`;
    // mmol/L is conventionally shown to 1 dp, mg/dL as a whole number.
    const gDp = dailyUnits === "mg/dL" ? 0 : 1;

    list.forEach((d) => {
      const tr = document.createElement("tr");
      // Today is a day in progress, not a short day. Say so on the row and keep it
      // out of the average, or every mean reads low until bedtime.
      const today = d.in_progress ? ` <span class="tag">so far</span>` : "";
      // Average glucose for the day. The title carries the sample size, since an
      // average over 40 readings and one over 288 are not the same claim.
      const g = d.glucose_avg == null
        ? `<span class="muted">·</span>`
        : `<span title="mean of ${d.reading_count} readings${dailyUnits ? " " + dailyUnits : ""}` +
          `, covering ${Math.round((d.glucose_coverage || 0) * 100)}% of the day">${d.glucose_avg.toFixed(gDp)}</span>` +
          mark(d.glucose_complete, thin);
      tr.innerHTML =
        `<td>${esc(d.label || d.day)}${today}</td>` +
        `<td>${g}</td>` +
        `<td>${cell(d.carbs_g, " g", d.carbs_complete, "a carb count")}</td>` +
        `<td>${cell(d.calories, " kcal", d.calories_complete, "a calorie count")}</td>` +
        `<td>${cell(d.insulin_units, "u", true, "")}</td>` +
        `<td class="muted">${d.meal_count}</td>` +
        `<td class="muted">${d.dose_count}</td>`;
      tb.appendChild(tr);
    });

    // Mean over *complete logged* days only. Two exclusions, for the same reason:
    // today is still running, and a day nothing was logged on is a gap in the
    // record — averaging either in would read as a fast that never happened.
    const foot = document.getElementById("daily-avg");
    if (!foot) return;
    if (!list.length) {
      foot.innerHTML = `<th colspan="7" class="muted">Nothing logged in this range.</th>`;
      return;
    }
    const full = list.filter((d) => !d.in_progress);
    if (!full.length) {
      foot.innerHTML = `<th colspan="7" class="muted">Today is still in progress — no complete day to average yet.</th>`;
      return;
    }
    const mean = (key) => full.reduce((a, d) => a + (d[key] || 0), 0) / full.length;
    const every = (key) => full.every((d) => d[key]);
    // Glucose averages over the days that have one, so a day the sensor missed
    // entirely doesn't count as a zero. It is a mean of daily means — the "typical
    // day" figure this row is asking for, not the overall mean of every reading,
    // which would let a long day outvote a short one.
    const gDays = full.filter((d) => d.glucose_avg != null);
    const gAvg = gDays.length
      ? `${(gDays.reduce((a, d) => a + d.glucose_avg, 0) / gDays.length).toFixed(gDp)}` +
        mark(gDays.every((d) => d.glucose_complete), thin)
      : `<span class="muted">·</span>`;
    foot.innerHTML =
      `<th>Average / day <span class="muted">(${full.length} full day${full.length === 1 ? "" : "s"})</span></th>` +
      `<th>${gAvg}</th>` +
      `<th>${mean("carbs_g").toFixed(0)} g${mark(every("carbs_complete"), missing("a carb count"))}</th>` +
      `<th>${mean("calories").toFixed(0)} kcal${mark(every("calories_complete"), missing("a calorie count"))}</th>` +
      `<th>${mean("insulin_units").toFixed(1)}u</th>` +
      `<th class="muted">${mean("meal_count").toFixed(1)}</th>` +
      `<th class="muted">${mean("dose_count").toFixed(1)}</th>`;
  }

  // ---- tables: insulin + meals + notes ----
  // The meal log appears twice, on the Overview and in the Logbook, so it is
  // addressed by class and each copy gets its own rows (and its own editors).
  function renderTables(entries) {
    const iTable = document.querySelector("#insulin-table tbody");
    const nTable = document.querySelector("#note-table tbody");
    iTable.innerHTML = "";
    nTable.innerHTML = "";
    entries.doses.forEach((d) => iTable.appendChild(doseRow(d)));
    document.querySelectorAll(".meal-table tbody").forEach((mTable) => {
      mTable.innerHTML = "";
      entries.meals.forEach((m) => mTable.appendChild(mealRow(m)));
    });
    (entries.notes || []).forEach((n) => nTable.appendChild(noteRow(n)));
    if (!(entries.notes || []).length) {
      nTable.innerHTML = `<tr><td colspan="3" class="muted">Nothing noted in this range.</td></tr>`;
    }
  }

  function doseRow(d) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${d.local}</td><td>${d.units}u</td><td>${d.kind}</td><td>${esc(d.note)}</td>
      <td class="row-actions"><button class="icon-btn" data-act="edit">Edit</button>
      <button class="icon-btn danger" data-act="del">✕</button></td>`;
    tr.querySelector('[data-act="del"]').onclick = () => del("insulin", d.id);
    tr.querySelector('[data-act="edit"]').onclick = () => editDose(tr, d);
    return tr;
  }

  // Cells are addressed by class rather than by position: the 24-hour time field
  // expands into several inputs, so querySelectorAll("input,select") order is no
  // longer a reliable way to find them.
  function doseEditCells(d) {
    d = d || {};
    return `
      <td><input type="datetime-local" class="d-ts" value="${d.input || nowInput(0)}"></td>
      <td><input type="number" class="d-units" step="0.5" min="0" value="${d.units ?? ""}"
                 placeholder="units" style="width:70px"></td>
      <td><select class="d-kind">${KINDS.map((k) => `<option ${k === d.kind ? "selected" : ""}>${k}</option>`).join("")}</select></td>
      <td><input type="text" class="d-note" value="${attr(d.note)}" placeholder="note"></td>
      <td class="row-actions"><button class="icon-btn save">Save</button>
      <button class="icon-btn" data-act="cancel">Cancel</button></td>`;
  }
  function readDose(tr) {
    return {
      ts: tr.querySelector(".d-ts").value,
      units: tr.querySelector(".d-units").value,
      kind: tr.querySelector(".d-kind").value,
      note: tr.querySelector(".d-note").value,
    };
  }

  function editDose(tr, d) {
    tr.innerHTML = doseEditCells(d);
    SD.timeFields(tr);
    tr.querySelector(".save").onclick = () => {
      const body = readDose(tr);
      patch("insulin", d.id, { ...body, units: parseFloat(body.units) });
    };
    tr.querySelector('[data-act="cancel"]').onclick = load;
  }

  // ---- notes (free-text context events) ----
  // No structure to edit beyond a stamp and the words, so the row is its own
  // editor rather than borrowing the meal editor's panel.
  function noteRow(n) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${n.local}</td><td class="note-text">${esc(n.text)}</td>
      <td class="row-actions"><button class="icon-btn" data-act="edit">Edit</button>
      <button class="icon-btn danger" data-act="del">✕</button></td>`;
    tr.querySelector('[data-act="del"]').onclick = () => del("note", n.id);
    tr.querySelector('[data-act="edit"]').onclick = () => editNote(tr, n);
    return tr;
  }

  function noteEditCells(n) {
    n = n || {};
    return `
      <td><input type="datetime-local" class="n-ts" value="${n.input || nowInput(0)}"></td>
      <td><textarea class="n-text" rows="2" placeholder="what happened">${esc(n.text || "")}</textarea></td>
      <td class="row-actions"><button class="icon-btn save">Save</button>
      <button class="icon-btn" data-act="cancel">Cancel</button></td>`;
  }
  function readNote(tr) {
    return { ts: tr.querySelector(".n-ts").value, text: tr.querySelector(".n-text").value };
  }

  function editNote(tr, n) {
    tr.innerHTML = noteEditCells(n);
    SD.timeFields(tr);
    tr.querySelector(".save").onclick = () => patch("note", n.id, readNote(tr));
    tr.querySelector('[data-act="cancel"]').onclick = load;
  }

  // ---- meals (composite: plate of items) ----
  function mealRow(m) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${m.local}</td><td>${cap(esc(m.meal_type))}</td><td>${esc(m.label)}</td>
      <td>${m.total_carbs ?? ""}</td><td>${m.total_calories ?? ""}</td><td>${esc(m.note)}</td>
      <td class="row-actions"><button class="icon-btn" data-act="edit">Edit</button>
      <button class="icon-btn danger" data-act="del">✕</button></td>`;
    tr.querySelector('[data-act="del"]').onclick = () => del("meal", m.id);
    tr.querySelector('[data-act="edit"]').onclick = () => openMealEditor(tr, m);
    return tr;
  }

  function typeOptions(selected) {
    const opts = ['<option value="">—</option>'];
    MEAL_TYPES.forEach((t) => opts.push(`<option value="${t}" ${t === selected ? "selected" : ""}>${cap(t)}</option>`));
    return opts.join("");
  }

  function openMealEditor(tr, m) {
    tr.innerHTML = `<td colspan="7"><div class="meal-editor">
      <div class="me-head">
        <label>Time <input type="datetime-local" class="me-ts" value="${m ? m.input : nowInput(0)}"></label>
        <label>Type <select class="me-type">${typeOptions(m ? m.meal_type : "")}</select></label>
        <label>Name <input type="text" class="me-name" value="${m ? attr(m.name) : ""}" placeholder="optional"></label>
        <label>Note <input type="text" class="me-note" value="${m ? attr(m.note) : ""}" placeholder="optional"></label>
      </div>
      <table class="data-table me-items">
        <thead><tr><th>Food</th><th>Carbs</th><th>Cal</th><th>Count</th><th></th></tr></thead>
        <tbody></tbody>
      </table>
      ${SD.refMarkup("me-ref")}
      <div class="me-actions">
        <button type="button" class="icon-btn me-additem">+ Item</button>
        <span style="flex:1"></span>
        <button type="button" class="icon-btn save me-save">Save</button>
        <button type="button" class="icon-btn me-cancel">Cancel</button>
      </div></div></td>`;
    const root = tr.querySelector(".meal-editor");
    SD.timeFields(root);
    const tbody = root.querySelector(".me-items tbody");
    (m ? m.items : []).forEach((it) => tbody.appendChild(itemRow(it)));
    if (!m) tbody.appendChild(itemRow({}));
    // A new plate is judged against now; an existing one against its own
    // moment — the glucose and active insulin when it was eaten — so fixing
    // Tuesday's chips shows what Tuesday's corrected plate asked for.
    const refresh = attachMealRef(root, tbody, m ? m.id : null);
    root.querySelector(".me-additem").onclick = () => { tbody.appendChild(itemRow({})); refresh(); };
    // input covers typing; change catches a library food filling the macros in;
    // click catches a row being removed. All three land on the same debounce.
    ["input", "change", "click"].forEach((e) => tbody.addEventListener(e, refresh));
    root.querySelector(".me-cancel").onclick = load;
    root.querySelector(".me-save").onclick = () => {
      const body = {
        ts: root.querySelector(".me-ts").value,
        name: root.querySelector(".me-name").value,
        meal_type: root.querySelector(".me-type").value,
        note: root.querySelector(".me-note").value,
        items: readItems(tbody),
      };
      if (m) patch("meal", m.id, body);
      else createJSON("meal", body);
    };
  }

  // ---- experimental bolus reference (see sugardaddy/bolus.py) -------------
  // The shared panel (SD.refMarkup / SD.renderRef — the phone uses the same
  // one), fed from the plate as typed. A figure to reconcile an intended dose
  // against, never an amount to give.

  // Carb total for the plate as typed, plus whether *every* named row fed it. A
  // plate where half the rows have no carb count still totals to a number, and
  // that number is a floor — the panel has to say so rather than imply a dose.
  function plateCarbs(tbody) {
    let total = 0, counted = 0, rows = 0;
    readItems(tbody).forEach((it) => {
      rows += 1;
      const c = parseFloat(it.carbs_g);
      if (it.carbs_g !== "" && !isNaN(c)) { total += c * (parseFloat(it.count) || 1); counted += 1; }
    });
    return { grams: counted ? Math.round(total * 10) / 10 : null, complete: rows > 0 && counted === rows };
  }

  function attachMealRef(root, tbody, mealId) {
    const el = root.querySelector(".me-ref");
    let off = false;   // no ISF configured: the panel doesn't exist at all
    let timer = null;
    function fetchRef() {
      if (!root.isConnected) return;   // editor closed under a pending timer
      const carbs = plateCarbs(tbody);
      const qs = new URLSearchParams();
      if (carbs.grams != null) qs.set("carbs", carbs.grams);
      if (mealId != null) qs.set("meal_id", mealId);
      fetch(`/api/bolus-reference?${qs}`)
        .then((r) => r.json())
        .then((d) => { if (!SD.renderRef(el, d, carbs)) off = !d.enabled; })
        .catch(() => {});
    }
    // Debounced: typing a carb count fires per keystroke, and a reference that
    // flickers through three values on the way to one is harder to trust.
    function refresh() {
      if (off || !el) return;
      clearTimeout(timer);
      timer = setTimeout(fetchRef, 250);
    }
    refresh();
    return refresh;
  }

  // ---- foods (library) ----
  // Foods with no carb count show a dash; chasing them up happens in the
  // "Still need carbs" panel above, which works by name across every logged
  // line and fills the library food too, so the table no longer keeps a to-do
  // group of its own.
  // Most eaten first: the regulars are the rows worth keeping tidy, and the
  // tail of never- or once-used foods is what clutters the phone's search.
  const byUse = (a, b) => (b.uses - a.uses) || a.name.localeCompare(b.name);
  function renderFoods(list) {
    const tb = document.querySelector("#foods-table tbody");
    tb.innerHTML = "";
    const q = (document.getElementById("food-filter").value || "").trim().toLowerCase();
    list = list.filter((f) => !q || f.name.toLowerCase().includes(q)).slice().sort(byUse);
    list.forEach((f) => tb.appendChild(foodRow(f)));
  }

  function foodRow(f) {
    const tr = document.createElement("tr");
    if (f.pending) tr.className = "food-pending";
    tr.innerHTML = `${starCell(f.favourite)}<td>${esc(f.name)}</td><td>${esc(f.description)}</td>
      <td>${f.carbs_g ?? `<span class="muted">—</span>`}</td>
      <td>${f.calories ?? ""}</td>
      <td class="${f.uses > 1 ? "" : "muted"}">${f.uses ? f.uses + "×" : "never"}</td>
      <td class="muted">${f.last_used ? SD.tableStamp(f.last_used).split(" ")[0] : ""}</td>
      <td class="row-actions"><button class="icon-btn" data-act="edit">Edit</button>
      <button class="icon-btn danger" data-act="del">✕</button></td>`;
    tr.querySelector('[data-act="del"]').onclick = () => del("foods", f.id);
    tr.querySelector('[data-act="edit"]').onclick = () => editFood(tr, f);
    tr.querySelector(".star").onclick = () => patch("foods", f.id, { favourite: !f.favourite });
    return tr;
  }

  // A favourite toggle. It edits nothing but the shortcut, so it saves on
  // click rather than going through an Edit/Save row.
  function starCell(on) {
    return `<td class="star-col"><button type="button" class="star${on ? " on" : ""}"
      title="${on ? "On the phone's shortcuts — click to remove" : "Add to the phone's shortcuts"}">${on ? "★" : "☆"}</button></td>`;
  }

  function foodEditCells(f) {
    f = f || {};
    return `
      <td class="star-col"></td>
      <td><input type="text" class="f-name" value="${attr(f.name || "")}" placeholder="name"></td>
      <td><input type="text" class="f-desc" value="${attr(f.description || "")}" placeholder="description"></td>
      <td><input type="number" step="1" min="0" class="f-carbs" value="${f.carbs_g ?? ""}" placeholder="carbs" style="width:66px"></td>
      <td><input type="number" step="1" min="0" class="f-cal" value="${f.calories ?? ""}" placeholder="cal" style="width:66px"></td>
      <td></td><td></td>
      <td class="row-actions"><button class="icon-btn save">Save</button>
      <button class="icon-btn" data-act="cancel">Cancel</button></td>`;
  }
  function readFood(tr) {
    return {
      name: tr.querySelector(".f-name").value,
      description: tr.querySelector(".f-desc").value,
      carbs_g: tr.querySelector(".f-carbs").value,
      calories: tr.querySelector(".f-cal").value,
    };
  }
  function editFood(tr, f) {
    tr.innerHTML = foodEditCells(f);
    tr.querySelector(".save").onclick = () =>
      patch("foods", f.id, readFood(tr)).then((res) => {
        // Details for a food that had none also complete the blanks it left on
        // past plates, which is invisible from this table — so say so.
        if (res && res.filled_items) {
          alert(`Saved. ${res.filled_items} logged item${res.filled_items === 1 ? "" : "s"} filled in from this food.`);
        }
      });
    tr.querySelector('[data-act="cancel"]').onclick = load;
  }

  // ---- meal templates (saved meals) ----
  function renderTemplates(list) {
    const wrap = document.getElementById("templates-list");
    wrap.innerHTML = "";
    if (!list.length) { wrap.innerHTML = `<p class="muted">No saved meals yet.</p>`; return; }
    list.slice().sort(byUse).forEach((t) => wrap.appendChild(templateBlock(t)));
  }

  function templateBlock(t) {
    const div = document.createElement("div");
    div.className = "tmpl";
    const summary = (t.items || []).map((i) => `${+i.count || 1}× ${i.name}`).join(", ") || "(empty)";
    div.innerHTML = `<div class="tmpl-head">
      <button type="button" class="star${t.favourite ? " on" : ""}" title="Phone tile">${t.favourite ? "★" : "☆"}</button>
      <span class="tmpl-name">${esc(t.name)}</span>
      <span class="tmpl-summary muted">${esc(summary)}</span>
      <span class="tmpl-uses muted">${t.uses ? `logged ${t.uses}×` : "never logged"}</span>
      <span style="flex:1"></span>
      <button class="icon-btn t-edit">Edit</button>
      <button class="icon-btn danger t-del">✕</button></div>`;
    div.querySelector(".t-del").onclick = () => del("meal-templates", t.id);
    div.querySelector(".t-edit").onclick = () => openTemplateEditor(div, t);
    div.querySelector(".star").onclick = () => patch("meal-templates", t.id, { favourite: !t.favourite });
    return div;
  }

  function openTemplateEditor(div, t) {
    div.innerHTML = `<div class="tmpl-editor">
      <label class="t-namewrap">Name <input type="text" class="t-name" value="${t ? attr(t.name) : ""}" placeholder="meal name"></label>
      <table class="data-table me-items">
        <thead><tr><th>Food</th><th>Carbs</th><th>Cal</th><th>Count</th><th></th></tr></thead>
        <tbody></tbody>
      </table>
      <div class="me-actions">
        <button type="button" class="icon-btn t-additem">+ Item</button>
        <span style="flex:1"></span>
        <button type="button" class="icon-btn save t-save">Save</button>
        <button type="button" class="icon-btn t-cancel">Cancel</button>
      </div></div>`;
    const tbody = div.querySelector(".me-items tbody");
    (t ? t.items : []).forEach((it) => tbody.appendChild(itemRow(it)));
    if (!t) tbody.appendChild(itemRow({}));
    div.querySelector(".t-additem").onclick = () => tbody.appendChild(itemRow({}));
    div.querySelector(".t-cancel").onclick = load;
    div.querySelector(".t-save").onclick = () => {
      const body = { name: div.querySelector(".t-name").value.trim(), items: readItems(tbody) };
      if (!body.name) { alert("Enter a meal name."); return; }
      if (t) patch("meal-templates", t.id, body);
      else createJSON("meal-templates", body);
    };
  }

  // ---- shared item editor (used by meals + templates) ----
  function populateFoodsDatalist() {
    const dl = document.getElementById("foods-datalist");
    dl.innerHTML = FOODS.map((f) => `<option value="${attr(f.name)}"></option>`).join("");
  }

  function itemRow(it) {
    it = it || {};
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td><input type="text" class="it-name" list="foods-datalist" value="${attr(it.name || "")}" placeholder="food"></td>
      <td><input type="number" class="it-carbs" step="1" min="0" value="${it.carbs_g ?? ""}" style="width:66px"></td>
      <td><input type="number" class="it-cal" step="1" min="0" value="${it.calories ?? ""}" style="width:66px"></td>
      <td><input type="number" class="it-count" step="0.5" min="0" value="${it.count ?? 1}" style="width:60px"></td>
      <td class="row-actions"><button type="button" class="icon-btn save it-save" title="Save to food library">+</button>
      <button type="button" class="icon-btn danger it-del">✕</button></td>`;
    const nameI = tr.querySelector(".it-name");
    const carbsI = tr.querySelector(".it-carbs");
    const calI = tr.querySelector(".it-cal");
    nameI.dataset.foodId = it.food_id ?? "";
    // A native <datalist> drops every option on mere focus, which the phone
    // combobox doesn't do — so detach the list while the field is empty and
    // re-attach it on the first keystroke, keeping the two UIs consistent.
    nameI.addEventListener("focus", () => { if (!nameI.value.trim()) nameI.removeAttribute("list"); });
    // Typing detaches from a library food; matching a food name re-links + fills.
    nameI.addEventListener("input", () => { nameI.setAttribute("list", "foods-datalist"); nameI.dataset.foodId = ""; });
    nameI.addEventListener("change", () => {
      const f = FOODS.find((x) => x.name.toLowerCase() === nameI.value.trim().toLowerCase());
      if (f) {
        nameI.dataset.foodId = f.id;
        if (carbsI.value === "") carbsI.value = f.carbs_g ?? "";
        if (calI.value === "") calI.value = f.calories ?? "";
      }
    });
    tr.querySelector(".it-save").onclick = () => saveItemAsFood(tr, nameI);
    tr.querySelector(".it-del").onclick = () => tr.remove();
    return tr;
  }

  // Push this item into the food library. POST /api/foods upserts by name, so
  // this saves a new food or overwrites the matching one; the plate itself is
  // untouched (items stay snapshots) until its own Save.
  function saveItemAsFood(tr, nameI) {
    const name = nameI.value.trim();
    if (!name) { alert("Name the food first."); return; }
    const btn = tr.querySelector(".it-save");
    btn.disabled = true;
    fetch("/api/foods", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name,
        carbs_g: tr.querySelector(".it-carbs").value,
        calories: tr.querySelector(".it-cal").value,
      }),
    })
      .then(async (r) => {
        if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || "Save failed.");
        return r.json();
      })
      .then((food) => {
        nameI.dataset.foodId = food.id; // the item is library-backed from now on
        btn.textContent = "✓";
        setTimeout(() => { btn.textContent = "+"; }, 1200);
        refreshFoods();
      })
      .catch((e) => alert(e.message))
      .finally(() => { btn.disabled = false; });
  }

  // Re-pull the library after an item is saved into it. A full load() would tear
  // down the open meal/template editor, so only the datalist and (when nothing
  // there is mid-edit) the foods table are refreshed.
  function refreshFoods() {
    return fetch("/api/foods")
      .then((r) => r.json())
      .then((foods) => {
        FOODS = foods;
        populateFoodsDatalist();
        if (!document.querySelector("#foods-table input")) renderFoods(foods);
      });
  }

  function readItems(tbody) {
    return [...tbody.querySelectorAll("tr")]
      .map((r) => ({
        food_id: r.querySelector(".it-name").dataset.foodId || null,
        name: r.querySelector(".it-name").value.trim(),
        carbs_g: r.querySelector(".it-carbs").value,
        calories: r.querySelector(".it-cal").value,
        count: r.querySelector(".it-count").value,
      }))
      .filter((i) => i.name);
  }

  // ---- missing carbs (the to-do) ----
  // Plate lines with no carb count, by name. Filling one is completing those
  // entries, not rewriting them — the server only writes blanks.
  function renderTodo(rows) {
    const tb = document.querySelector("#todo-table tbody");
    const badge = document.getElementById("lib-badge");
    badge.hidden = !rows.length;
    badge.textContent = rows.length;
    tb.innerHTML = "";
    if (!rows.length) {
      tb.innerHTML = `<tr><td colspan="7" class="muted">Nothing waiting — every logged item has a carb count.</td></tr>`;
      return;
    }
    rows.forEach((t) => {
      const tr = document.createElement("tr");
      tr.innerHTML = `<td>${esc(t.name)}</td><td>${t.count}×</td><td class="muted">${esc(t.local.split(" ")[0])}</td>
        <td><input type="number" class="t-carbs" min="0" step="1" placeholder="g" style="width:70px"></td>
        <td><input type="number" class="t-cal" min="0" step="1" placeholder="cal" style="width:70px" value="${t.calories ?? ""}"></td>
        <td><input type="checkbox" class="t-keep" title="Also add to your foods"></td>
        <td class="row-actions"><button class="icon-btn save">Fill</button></td>`;
      tr.querySelector(".save").onclick = () => {
        const carbs = tr.querySelector(".t-carbs").value;
        if (carbs === "") { alert("Enter the carbs first."); return; }
        fetch("/api/missing-carbs", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name: t.name, carbs_g: carbs, calories: tr.querySelector(".t-cal").value,
                                 keep: tr.querySelector(".t-keep").checked }),
        }).then(async (r) => {
          const d = await r.json().catch(() => ({}));
          if (!r.ok) { alert(d.error || "Save failed."); return; }
          tr.querySelector(".t-carbs").value = "";
          load();
        });
      };
      tb.appendChild(tr);
    });
  }
  document.getElementById("food-filter").addEventListener("input", () => renderFoods(FOODS));

  // ---- phone setup ----
  // The dose chips are stored server-side (they're chosen here and read on the
  // phone, so neither device's own storage will do). Until someone edits them
  // they are the user's own most-used amounts, which the server works out.
  const KIND_NAMES = { bolus: "Bolus", correction: "Correction", basal: "Basal" };
  let presets = null;

  function loadPhone() {
    Promise.all([
      fetch("/api/settings/dose-presets").then((r) => r.json()),
      fetch("/api/shortcuts").then((r) => r.json()),
    ]).then(([p, sc]) => {
      if (!document.querySelector("#presets-editor input:focus")) renderPresets(p);
      document.getElementById("preview-meals").innerHTML =
        sc.meals.map((t) => `<span class="pchip${t.favourite ? " fav" : ""}">${t.favourite ? "★ " : ""}${esc(t.name)}</span>`).join("") || `<span class="muted">none yet</span>`;
      document.getElementById("preview-foods").innerHTML =
        sc.foods.map((f) => `<span class="pchip${f.favourite ? " fav" : ""}">${f.favourite ? "★ " : ""}${esc(f.name)} · ${f.carbs_g}g</span>`).join("") || `<span class="muted">none yet</span>`;
    }).catch(() => {});
  }

  function renderPresets(p) {
    presets = p;
    document.getElementById("presets-status").textContent = p.custom
      ? "Set by hand. The phone shows these under the insulin stepper."
      : "Learned from your last 90 days — the amounts you log most. Edit them to set your own.";
    document.getElementById("presets-reset").hidden = !p.custom;
    const box = document.getElementById("presets-editor");
    box.innerHTML = ["bolus", "correction", "basal"].map((k) => `
      <div class="preset-row" data-kind="${k}">
        <span class="preset-kind">${KIND_NAMES[k]}</span>
        <span class="preset-chips">${(p.presets[k] || []).map((u) =>
          `<span class="pchip">${u} u<button type="button" class="pchip-x" data-u="${u}" title="Remove">×</button></span>`).join("")}</span>
        <input type="number" class="preset-add" min="0" step="0.5" placeholder="+ u" aria-label="Add a ${KIND_NAMES[k]} amount">
      </div>`).join("");
    box.querySelectorAll(".preset-row").forEach((row) => {
      const k = row.dataset.kind;
      row.querySelectorAll(".pchip-x").forEach((x) => x.onclick = () =>
        savePresets(k, (presets.presets[k] || []).filter((u) => u !== parseFloat(x.dataset.u))));
      const add = row.querySelector(".preset-add");
      add.addEventListener("keydown", (e) => {
        if (e.key !== "Enter") return;
        const u = parseFloat(add.value);
        if (u > 0) savePresets(k, [...(presets.presets[k] || []), u]);
      });
      add.addEventListener("change", () => {
        const u = parseFloat(add.value);
        if (u > 0) savePresets(k, [...(presets.presets[k] || []), u]);
      });
    });
  }
  function savePresets(kind, list) {
    const next = { ...presets.presets, [kind]: list };
    putPresets(next);
  }
  function putPresets(value) {
    fetch("/api/settings/dose-presets", {
      method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ presets: value }),
    }).then((r) => r.json()).then(renderPresets);
  }
  document.getElementById("presets-reset").onclick = () => putPresets(null);

  // ---- settings ----
  // Rendered from /api/settings, which describes every editable field (type,
  // unit, help, where its value comes from). Loaded when the view opens and
  // after each save — never by the auto-refresh, which would wipe a half-typed
  // value.
  const SECTION_ACTIONS = {
    notify: `<button type="button" class="icon-btn" data-act="push-test">Send a test notification</button>`,
    victoriametrics: `<button type="button" class="icon-btn" data-act="vm-test">Test connection</button>
      <button type="button" class="icon-btn" data-act="vm-sync">Sync now</button>
      <button type="button" class="icon-btn danger" data-act="vm-resend">Resend everything</button>`,
    backfill: `<span class="inline-form"><label class="inline">Days <input type="number" class="ha-days" value="90" min="1" max="3650" style="width:80px"></label>
      <input type="password" class="ha-token" placeholder="long-lived token (used once, not saved)" autocomplete="off" style="min-width:260px">
      <button type="button" class="icon-btn save" data-act="ha-import">Import glucose history</button></span>`,
  };

  function fieldInput(f) {
    const v = f.value;
    const base = f.kind.replace(/^opt/, "");
    if (base === "bool") return `<input type="checkbox" data-key="${f.key}" ${v ? "checked" : ""}>`;
    if (base === "choice") {
      return `<select data-key="${f.key}">${f.choices.map((c) => `<option ${c === v ? "selected" : ""}>${esc(c)}</option>`).join("")}</select>`;
    }
    const type = base === "float" || base === "int" ? "number" : base === "date" ? "date" : "text";
    const step = base === "int" ? "1" : "any";
    return `<input type="${type}" ${type === "number" ? `step="${step}"` : ""} data-key="${f.key}" value="${attr(v == null ? "" : String(v))}"
      placeholder="${f.kind.startsWith("opt") ? "not set" : ""}">`;
  }
  const show = (v) => (v == null || v === "" ? "not set" : v === true ? "on" : v === false ? "off" : String(v));

  function renderSettings(d) {
    document.getElementById("outbound-banner").hidden = !d.no_outbound;
    const box = document.getElementById("settings-sections");
    box.innerHTML = d.sections.map((sec) => `
      <section class="panel settings-panel" data-section="${sec.id}">
        <h2>${esc(sec.title)}</h2>
        <p class="panel-note muted">${esc(sec.note)}</p>
        <div class="field-list">${sec.fields.map((f) => `
          <div class="set-field">
            <label for="f-${f.key}">${esc(f.label)}${f.unit ? ` <span class="muted">(${esc(f.unit)})</span>` : ""}</label>
            <div class="set-input">${fieldInput(f).replace("data-key", `id="f-${f.key}" data-key`)}</div>
            <div class="set-src">${f.source === "db"
              ? `<span class="src-db" title="config.toml says: ${attr(show(f.file_value))}">changed here</span>
                 <button type="button" class="link-btn" data-reset="${f.key}" title="Back to the config.toml value (${attr(show(f.file_value))})">↺ file</button>`
              : `<span class="muted">from file</span>`}
              ${f.applies !== "now" ? `<span class="muted"> · applies on ${esc(f.applies)}</span>` : ""}</div>
            ${f.help ? `<div class="set-help muted">${esc(f.help)}</div>` : ""}
          </div>`).join("")}</div>
        <div class="me-actions">
          <button type="button" class="icon-btn save" data-act="save">Save</button>
          <span class="set-status muted"></span>
          <span style="flex:1"></span>
          ${SECTION_ACTIONS[sec.id] || ""}
        </div>
        <div class="action-result muted"></div>
      </section>`).join("");

    document.getElementById("bootstrap-list").innerHTML =
      d.bootstrap.map((b) => `<dt>${esc(b.label)}</dt><dd><code>${esc(b.value)}</code></dd>`).join("");
    document.getElementById("secrets-list").innerHTML = d.secrets.map((x) =>
      `<li><span class="${x.set ? "dot-ok" : "dot-off"}"></span><code>${esc(x.name)}</code> <span class="muted">— ${esc(x.what)}: ${x.set ? "set" : "not set"}</span></li>`).join("");
    document.getElementById("settings-history").innerHTML = d.history.length
      ? d.history.map((h) => `<li><span class="muted">${SD.tableStamp(h.ts)}</span> <code>${esc(h.key)}</code>
          ${esc(show(h.old))} → <b>${esc(show(h.new))}</b>${h.reset ? ` <span class="muted">(back to file)</span>` : ""}</li>`).join("")
      : `<li class="muted">No changes made here yet.</li>`;

    box.querySelectorAll(".settings-panel").forEach((panel) => {
      const sec = panel.dataset.section;
      const say = (msg, bad) => {
        const el = panel.querySelector(".set-status");
        el.textContent = msg;
        el.classList.toggle("bad-text", !!bad);
      };
      panel.querySelector('[data-act="save"]').onclick = () => {
        const changes = {};
        panel.querySelectorAll("[data-key]").forEach((el) => {
          const name = el.dataset.key.split(".")[1];
          changes[name] = el.type === "checkbox" ? el.checked : el.value;
        });
        sendSettings("PUT", "/api/settings", { changes: { [sec]: changes } }, say);
      };
      panel.querySelectorAll("[data-reset]").forEach((b) => b.onclick = () =>
        sendSettings("POST", "/api/settings/reset", { keys: [b.dataset.reset] }, say));
      panel.querySelectorAll("[data-act]").forEach((b) => {
        const act = b.dataset.act;
        if (act === "save") return;
        b.onclick = () => runAction(act, panel, b);
      });
    });
  }

  function sendSettings(method, url, body, say) {
    say("Saving…");
    fetch(url, { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })
      .then(async (r) => {
        const d = await r.json().catch(() => ({}));
        if (!r.ok) { say(d.error || "Save failed.", true); return; }
        renderSettings(d);
        // Units, targets and the insulin maths change what every view shows.
        load();
      });
  }

  function runAction(act, panel, btn) {
    const out = panel.querySelector(".action-result");
    const post = (url, body) => fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) })
      .then(async (r) => ({ ok: r.ok, d: await r.json().catch(() => ({})) }));
    const report = (p, fmt) => p.then(({ ok, d }) => {
      out.classList.toggle("bad-text", !ok || d.ok === false);
      out.textContent = ok ? fmt(d) : (d.error || "Failed.");
    }).finally(() => { btn.disabled = false; });
    if (act === "vm-resend" && !confirm("Delete everything this app exported to VictoriaMetrics and send the whole history again?")) return;
    btn.disabled = true;
    out.textContent = "Working…";
    if (act === "push-test") report(post("/api/push/test"), (d) => `Sent to ${d.sent ?? 0} of ${d.subscriptions ?? 0} device(s).`);
    else if (act === "vm-test") report(post("/api/actions/victoriametrics", { action: "test" }), (d) => d.ok ? "Reached the server." : `Server answered HTTP ${d.status}.`);
    else if (act === "vm-sync" || act === "vm-resend") {
      report(post("/api/actions/victoriametrics", { action: act === "vm-sync" ? "sync" : "resend" }),
        (d) => `Sent ${d.glucose} readings; ${d.events_written} doses/meals written, ${d.events_deleted} removed.`);
    } else if (act === "ha-import") {
      out.textContent = "Importing — this can take a minute…";
      report(post("/api/actions/ha-import", { days: panel.querySelector(".ha-days").value, token: panel.querySelector(".ha-token").value }),
        (d) => `Fetched ${d.fetched} readings, ${d.added} new (now ${d.total} stored).`);
      panel.querySelector(".ha-token").value = "";
    }
  }

  function loadSettings() {
    fetch("/api/settings").then((r) => r.json()).then(renderSettings).catch(() => {});
  }

  // ---- CSV ----
  document.querySelectorAll("#export-buttons [data-kind]").forEach((b) => b.addEventListener("click", () => {
    const span = document.querySelector('input[name="export-span"]:checked').value;
    let qs = "";
    if (span === "range") { const { from, to } = rangeEpochs(); qs = `?from=${from}&to=${to}`; }
    window.location.href = `/api/export/${b.dataset.kind}.csv${qs}`;
  }));

  function importFile(dryRun) {
    const file = document.getElementById("import-file").files[0];
    const kind = document.getElementById("import-kind").value;
    const out = document.getElementById("import-result");
    if (!file) { alert("Choose a CSV file first."); return; }
    const fd = new FormData();
    fd.append("file", file);
    out.hidden = false;
    out.innerHTML = "Reading…";
    fetch(`/api/import/${kind}?dry_run=${dryRun}`, { method: "POST", body: fd })
      .then(async (r) => {
        const d = await r.json().catch(() => ({}));
        if (!r.ok) { out.innerHTML = `<span class="bad-text">${esc(d.error || "Import failed.")}</span>`; return; }
        const errs = d.errors.map((e) => `<li>line ${e.line}: ${esc(e.error)}</li>`).join("");
        const more = d.error_count > d.errors.length ? `<li>…and ${d.error_count - d.errors.length} more</li>` : "";
        const what = kind === "meals" ? "meal" : kind === "glucose" ? "reading" : kind === "notes" ? "note" : "dose";
        out.innerHTML = dryRun
          ? `<p><b>${d.added}</b> new ${what}${d.added === 1 ? "" : "s"} to add from ${d.rows} row${d.rows === 1 ? "" : "s"};
               ${d.duplicates} already here (skipped)${d.error_count ? `; <span class="bad-text">${d.error_count} row${d.error_count === 1 ? "" : "s"} can't be read</span>` : ""}.</p>
             ${errs || more ? `<ul class="err-list">${errs}${more}</ul>` : ""}
             ${d.added ? `<button type="button" class="icon-btn save" id="import-go">Import ${d.added} ${what}${d.added === 1 ? "" : "s"}</button>` : ""}`
          : `<p>Imported <b>${d.added}</b> ${what}${d.added === 1 ? "" : "s"}${d.duplicates ? `, skipped ${d.duplicates} already here` : ""}.</p>`;
        const go = document.getElementById("import-go");
        if (go) go.onclick = () => importFile(false);
        if (!dryRun) load();
      });
  }
  document.getElementById("import-check").addEventListener("click", () => importFile(true));
  ["import-file", "import-kind"].forEach((id) => document.getElementById(id).addEventListener("change", () => {
    document.getElementById("import-result").hidden = true;
  }));

  // ---- views ----
  // One page, five views. The current one is remembered with the rest of the
  // view state, and is in the URL hash so a link (or a reload) lands on it.
  const VIEWS = ["overview", "logbook", "meals", "library", "phone", "settings"];
  function showView(v) {
    if (!VIEWS.includes(v)) v = "overview";
    document.querySelectorAll(".desk-nav button").forEach((b) => b.classList.toggle("active", b.dataset.view === v));
    document.querySelectorAll(".view").forEach((el) => { el.hidden = el.dataset.view !== v; });
    // The range only means something to the time-based views.
    document.getElementById("range-bar").classList.toggle("dim", v === "library" || v === "phone");
    if (v === "settings") loadSettings();
    if (location.hash.slice(1) !== v) history.replaceState(null, "", "#" + v);
    savePrefs({ view: v });
    // A chart built while its view was hidden measured itself at zero.
    if (v === "overview" && chart) chart.resize();
  }
  document.querySelectorAll(".desk-nav button").forEach((b) => b.addEventListener("click", () => showView(b.dataset.view)));
  window.addEventListener("hashchange", () => showView(location.hash.slice(1)));

  // ---- add buttons ----
  document.querySelectorAll(".add-btn[data-add]").forEach((btn) => {
    btn.addEventListener("click", () => addRow(btn.dataset.add, btn.closest(".panel")));
  });
  document.getElementById("add-template").addEventListener("click", () => {
    const wrap = document.getElementById("templates-list");
    if (wrap.querySelector(".tmpl-editor")) return; // one new editor at a time
    const div = document.createElement("div");
    div.className = "tmpl";
    wrap.prepend(div);
    openTemplateEditor(div, null);
  });

  // `panel` is the clicked button's panel, which picks the meal table to add to.
  function addRow(type, panel) {
    if (type === "meal") {
      const tbody = panel.querySelector(".meal-table tbody");
      const tr = document.createElement("tr");
      tbody.prepend(tr);
      openMealEditor(tr, null);
      return;
    }
    if (type === "food") {
      const tbody = document.querySelector("#foods-table tbody");
      const tr = document.createElement("tr");
      tr.innerHTML = foodEditCells(null);
      tr.querySelector(".save").onclick = () => create("foods", readFood(tr));
      tr.querySelector('[data-act="cancel"]').onclick = load;
      tbody.prepend(tr);
      // the library scrolls, so the new top row can be off-screen; go to it
      tbody.closest(".table-scroll").scrollTop = 0;
      return;
    }
    if (type === "note") {
      const tbody = document.querySelector("#note-table tbody");
      const tr = document.createElement("tr");
      tr.innerHTML = noteEditCells(null);
      SD.timeFields(tr);
      tr.querySelector(".save").onclick = () => create("note", readNote(tr));
      tr.querySelector('[data-act="cancel"]').onclick = load;
      // The empty-state row would otherwise sit under the new editor.
      if (!tbody.querySelector(".row-actions")) tbody.innerHTML = "";
      tbody.prepend(tr);
      return;
    }
    if (type === "insulin") {
      const tbody = document.querySelector("#insulin-table tbody");
      const tr = document.createElement("tr");
      tr.innerHTML = doseEditCells(null);
      SD.timeFields(tr);
      tr.querySelector(".save").onclick = () => create("insulin", readDose(tr));
      tr.querySelector('[data-act="cancel"]').onclick = load;
      tbody.prepend(tr);
    }
  }

  // ---- API calls ----
  function create(type, fields) {
    const fd = new FormData();
    Object.entries(fields).forEach(([k, v]) => fd.append(k, v ?? ""));
    fetch(`/api/${type}`, { method: "POST", body: fd }).then(async (r) => {
      // A rejected create used to reload regardless, so the row simply vanished
      // and took the typed values with it. Say what was wrong and leave the
      // editor standing, the way patch() already does.
      if (!r.ok) {
        const e = await r.json().catch(() => ({}));
        alert(e.error || "Save failed.");
        return;
      }
      load();
    });
  }
  function createJSON(type, body) {
    fetch(`/api/${type}`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    }).then(load);
  }
  // Resolves with the parsed response so a caller can react to what the server
  // did (the foods table reports how many logged items a fill-in reached).
  function patch(type, id, body) {
    return fetch(`/api/${type}/${id}`, {
      method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    }).then(async (r) => {
      const res = await r.json().catch(() => ({}));
      if (!r.ok) alert(res.error || "Update failed.");
      load();
      return r.ok ? res : null;
    });
  }
  function del(type, id) {
    if (!confirm("Delete this entry?")) return;
    fetch(`/api/${type}/${id}`, { method: "DELETE" }).then(load);
  }

  // ---- utils ----
  function esc(s) { return (s ?? "").replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c])); }
  function attr(s) { return (s ?? "").replace(/"/g, "&quot;"); }

  // ---- live auto-refresh ----
  const REFRESH_MS = 60000;
  function isEditing() {
    // The to-do's carb boxes are always on the page, so only a filled-in one
    // (or a focused one) counts as an edit in progress.
    const todoBusy = [...document.querySelectorAll("#todo-table input[type=number]")]
      .some((i) => i.value !== "" || i === document.activeElement);
    return todoBusy || !!document.querySelector(
      "#insulin-table input, #insulin-table select, .meal-table input, .meal-table select, " +
      "#note-table input, #note-table textarea, " +
      "#foods-table input, #templates-list input, #templates-list select, #presets-editor input:focus"
    );
  }
  function autoRefresh() {
    if (document.hidden || isEditing()) return;
    const preset = activePreset();
    // A hand-picked window is frozen. Nothing new can land inside a fixed
    // historical range, and repainting under the user is the whole problem —
    // clicking a preset re-arms live mode. Explicit edits still call load().
    if (!preset) return;
    fromEl.value = nowInput(parseInt(preset.dataset.hours, 10));
    toEl.value = nowInput(0);
    load();
  }

  // ---- init ----
  // Restore the last window. A preset is re-anchored to *now* — a remembered
  // "12h" means the last twelve hours today, not the twelve that ended when the
  // tab was closed — while a hand-typed range comes back verbatim and stays
  // frozen, which is the whole point of having typed it. parseInt before the
  // selector so a corrupt stored value can't reach querySelector and throw.
  const savedHours = parseInt(prefs.preset, 10);
  const savedPreset = Number.isFinite(savedHours)
    ? document.querySelector(`.range-picker .range-presets button[data-hours="${savedHours}"]`)
    : null;
  if (savedPreset) {
    selectPreset(savedPreset);
  } else if (typeof prefs.from === "string" && typeof prefs.to === "string" && prefs.from && prefs.to) {
    fromEl.value = prefs.from;
    toEl.value = prefs.to;
    setPreset(null);
  } else {
    selectPreset(document.querySelector('.range-picker .range-presets button[data-hours="24"]'));
  }
  if (prefs.days) setDailyDays(prefs.days, false);
  showView(location.hash.slice(1) || prefs.view || "overview");
  window.addEventListener("load", load);
  setInterval(autoRefresh, REFRESH_MS);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) autoRefresh(); });
})();
