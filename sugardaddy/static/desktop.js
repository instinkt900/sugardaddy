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
      ctx.font = "600 13px -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif";
      const pad = 7, w = ctx.measureText(label).width + pad * 2, h = 20;
      let bx = pt.x + 9;
      if (bx + w > a.right) bx = pt.x - 9 - w; // flip inward near the right edge
      let by = Math.max(a.top, Math.min(pt.y - h / 2, a.bottom - h));
      ctx.beginPath();
      ctx.arc(pt.x, pt.y, 3.5, 0, Math.PI * 2);
      ctx.fillStyle = "#4f8cff";
      ctx.fill();
      roundRect(ctx, bx, by, w, h, 6);
      ctx.fillStyle = "#4f8cff";
      ctx.fill();
      ctx.fillStyle = "#fff";
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
      ctx.strokeStyle = "rgba(232,234,240,0.4)";
      ctx.setLineDash([4, 3]);
      ctx.stroke();
      ctx.setLineDash([]);
      // Time label, kept inside the plot area.
      const label = SD.tableStamp(scales.x.getValueForPixel(x));
      ctx.font = "12px -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif";
      const pad = 5, w = ctx.measureText(label).width + pad * 2, h = 18;
      let bx = x + 6;
      if (bx + w > a.right) bx = x - 6 - w;
      const by = a.top + 2;
      ctx.fillStyle = "rgba(28,31,40,0.95)";
      ctx.strokeStyle = "rgba(232,234,240,0.2)";
      ctx.fillRect(bx, by, w, h);
      ctx.strokeRect(bx, by, w, h);
      ctx.fillStyle = "#e8eaf0";
      ctx.textBaseline = "top";
      ctx.fillText(label, bx + pad, by + 3);
      ctx.restore();
    },
  };

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

  document.querySelectorAll(".range-picker .range-presets button").forEach((b) => {
    b.addEventListener("click", () => {
      setPreset(b);
      fromEl.value = nowInput(parseInt(b.dataset.hours, 10));
      toEl.value = nowInput(0);
      load();
    });
  });
  // Typing a range detaches from the presets. Without this the last-clicked
  // preset stayed active and autoRefresh() wrote the window straight back.
  const manualRange = () => { setPreset(null); load(); };
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
    ]).then(([timeline, entries, stats, foods, templates]) => {
      if (seq !== loadSeq) return; // superseded by a newer load
      FOODS = foods;
      populateFoodsDatalist();
      renderChart(timeline);
      renderTables(entries);
      renderStats(stats);
      renderFoods(foods);
      renderTemplates(templates);
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

  // Dose/meal markers sit just above the bottom of the glucose range, so their
  // y positions move with the data and have to be recomputed each refresh.
  function chartSeries(data) {
    const glucose = data.glucose.map((p) => ({ x: p.t, y: p.v }));
    const ys = glucose.map((p) => p.y);
    const yMin = ys.length ? Math.min(...ys) : 0;
    const yMax = ys.length ? Math.max(...ys) : 10;
    const doseY = yMin;
    const mealY = yMin + (yMax - yMin) * 0.06;
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
    };
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
      [s.glucose, s.smoothed, s.doses, s.meals, s.iob, s.activity].forEach((d, i) => { sets[i].data = d; });
      chart.options.scales.x.min = data.from;
      chart.options.scales.x.max = data.to;
      chart.update();
      return;
    }

    chart = new Chart(ctx, {
      data: {
        datasets: [
          { type: "line", label: "Glucose", data: s.glucose, borderColor: "#4f8cff",
            borderWidth: 2, pointRadius: 0, tension: 0.3, parsing: false },
          // Same data with the sensor's ~30-minute ringing filtered out (see
          // analysis.smooth_glucose). Drawn over the raw line in a lighter tint of
          // the same blue, so it reads as "this line, calmed down" rather than as a
          // second measurement. Toggle it off from the legend like any other series.
          { type: "line", label: "Glucose (smoothed)", data: s.smoothed,
            borderColor: "#bcd3ff", borderWidth: 2, borderDash: [6, 3],
            pointRadius: 0, tension: 0.3, fill: false, parsing: false },
          { type: "scatter", label: "Insulin", data: s.doses,
            borderColor: (c) => SD.doseColor(c.raw && c.raw.kind),
            backgroundColor: (c) => SD.doseColor(c.raw && c.raw.kind),
            pointStyle: "triangle", radius: 7, parsing: false },
          { type: "scatter", label: "Meal", data: s.meals, borderColor: "#ffb020",
            backgroundColor: "#ffb020", pointStyle: "rectRot", radius: 7, parsing: false },
          // Active-insulin (IOB) curve on its own right-hand axis; drawn behind
          // the glucose line (order:1) and translucent so it never hides it.
          { type: "line", label: "Insulin active (u)", data: s.iob, yAxisID: "y1",
            borderColor: "#a78bfa", backgroundColor: "rgba(167,139,250,0.15)",
            borderWidth: 1.5, pointRadius: 0, tension: 0.3, fill: true, parsing: false, order: 1 },
          // Insulin action *rate* (derivative of IOB) on a hidden auto-scaled
          // axis — dashed line, no fill, so it reads as the "how hard it's working
          // now" companion to the filled on-board area. Shape/timing is the point.
          { type: "line", label: "Insulin activity (u/hr)", data: s.activity, yAxisID: "y2",
            borderColor: "#2dd4bf", borderWidth: 1.5, borderDash: [5, 4],
            pointRadius: 0, tension: 0.3, fill: false, parsing: false, order: 1 },
        ],
      },
      options: {
        animation: false,
        interaction: { mode: "nearest", intersect: true },
        plugins: {
          legend: { labels: { color: "#e8eaf0" } },
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
              return `${c.parsed.y} ${chartPayload.units}`;
            },
          } },
        },
        scales: {
          // Pinned to the requested window: left to itself the axis would
          // autoscale to the data, so a stale last reading or an ingest gap made
          // the plotted lines shrink away from the (fixed) chart area edges.
          x: { type: "linear", min: data.from, max: data.to,
               ticks: { color: "#8b90a0", maxTicksLimit: 10,
                        callback: (v) => SD.axisStamp(v, chartPayload.to - chartPayload.from) },
               grid: { color: "#2c303c" } },
          y: { ticks: { color: "#8b90a0" }, grid: { color: "#2c303c" },
               title: { display: true, text: data.units, color: "#8b90a0" } },
          y1: { position: "right", beginAtZero: true, ticks: { color: "#8b90a0" },
                grid: { display: false },
                title: { display: true, text: "insulin (u)", color: "#8b90a0" } },
          // hidden: auto-scales the activity curve for shape comparison only
          y2: { position: "right", beginAtZero: true, display: false },
        },
      },
      plugins: [SD.targetBand(data.target_low, data.target_high), lastValueTag, crosshair],
    });
  }

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
    const note = document.getElementById("ref-caveat");
    if (note) note.hidden = !showRef;
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
      const partial = miss.length > 0;
      const why = partial ? `incomplete — no ${miss.join(", ")} for this meal` : (p.ref_note || "");
      const ref = r.suggested_units == null
        ? `<td class="ref-col muted" title="${esc(why)}">—</td>
           <td class="ref-col"></td>`
        : `<td class="ref-col ${partial ? "ref-partial" : ""}" title="${esc(why)}">
             ${r.suggested_units}u${partial ? "*" : ""}</td>
           <td class="ref-col ${partial ? "ref-partial" : p.ref_delta_units > 0 ? "ref-over" : p.ref_delta_units < 0 ? "ref-under" : ""}"
               title="${esc(why)}">
             ${p.ref_delta_units > 0 ? "+" : ""}${p.ref_delta_units}${partial ? "*" : ""}</td>`;
      tr.innerHTML = `<td>${p.local}</td><td>${esc(p.description) || "(meal)"}</td>
        <td>${p.carbs_g ?? ""}</td><td>${dose(p.bolus_units)}</td><td>${dose(p.iob_start_units)}</td>${ref}
        <td>${p.start_display}</td><td>${p.peak_display}</td>
        <td>${p.peak_delta_display}</td><td>${p.minutes_to_peak}m</td><td>${p.end_display}</td>`;
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

  function setDailyDays(n) {
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
    loadDaily();
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

  // ---- tables: insulin + meals ----
  function renderTables(entries) {
    const iTable = document.querySelector("#insulin-table tbody");
    const mTable = document.querySelector("#meal-table tbody");
    iTable.innerHTML = "";
    mTable.innerHTML = "";
    entries.doses.forEach((d) => iTable.appendChild(doseRow(d)));
    entries.meals.forEach((m) => mTable.appendChild(mealRow(m)));
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
    root.querySelector(".me-additem").onclick = () => tbody.appendChild(itemRow({}));
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

  // ---- foods (library) ----
  function renderFoods(list) {
    const tb = document.querySelector("#foods-table tbody");
    tb.innerHTML = "";
    list.forEach((f) => tb.appendChild(foodRow(f)));
  }

  function foodRow(f) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${esc(f.name)}</td><td>${esc(f.description)}</td><td>${f.carbs_g ?? ""}</td>
      <td>${f.calories ?? ""}</td>
      <td class="row-actions"><button class="icon-btn" data-act="edit">Edit</button>
      <button class="icon-btn danger" data-act="del">✕</button></td>`;
    tr.querySelector('[data-act="del"]').onclick = () => del("foods", f.id);
    tr.querySelector('[data-act="edit"]').onclick = () => editFood(tr, f);
    return tr;
  }

  function foodEditCells(f) {
    f = f || {};
    return `
      <td><input type="text" class="f-name" value="${attr(f.name || "")}" placeholder="name"></td>
      <td><input type="text" class="f-desc" value="${attr(f.description || "")}" placeholder="description"></td>
      <td><input type="number" step="1" min="0" class="f-carbs" value="${f.carbs_g ?? ""}" placeholder="carbs" style="width:66px"></td>
      <td><input type="number" step="1" min="0" class="f-cal" value="${f.calories ?? ""}" placeholder="cal" style="width:66px"></td>
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
    tr.querySelector(".save").onclick = () => patch("foods", f.id, readFood(tr));
    tr.querySelector('[data-act="cancel"]').onclick = load;
  }

  // ---- meal templates (saved meals) ----
  function renderTemplates(list) {
    const wrap = document.getElementById("templates-list");
    wrap.innerHTML = "";
    if (!list.length) { wrap.innerHTML = `<p class="muted">No saved meals yet.</p>`; return; }
    list.forEach((t) => wrap.appendChild(templateBlock(t)));
  }

  function templateBlock(t) {
    const div = document.createElement("div");
    div.className = "tmpl";
    const summary = (t.items || []).map((i) => `${+i.count || 1}× ${i.name}`).join(", ") || "(empty)";
    div.innerHTML = `<div class="tmpl-head">
      <span class="tmpl-name">${esc(t.name)}</span>
      <span class="tmpl-summary muted">${esc(summary)}</span>
      <span style="flex:1"></span>
      <button class="icon-btn t-edit">Edit</button>
      <button class="icon-btn danger t-del">✕</button></div>`;
    div.querySelector(".t-del").onclick = () => del("meal-templates", t.id);
    div.querySelector(".t-edit").onclick = () => openTemplateEditor(div, t);
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
    // Typing detaches from a library food; matching a food name re-links + fills.
    nameI.addEventListener("input", () => { nameI.dataset.foodId = ""; });
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

  // ---- add buttons ----
  document.querySelectorAll(".add-btn[data-add]").forEach((btn) => {
    btn.addEventListener("click", () => addRow(btn.dataset.add));
  });
  document.getElementById("add-template").addEventListener("click", () => {
    const wrap = document.getElementById("templates-list");
    if (wrap.querySelector(".tmpl-editor")) return; // one new editor at a time
    const div = document.createElement("div");
    div.className = "tmpl";
    wrap.prepend(div);
    openTemplateEditor(div, null);
  });

  function addRow(type) {
    if (type === "meal") {
      const tbody = document.querySelector("#meal-table tbody");
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
    fetch(`/api/${type}`, { method: "POST", body: fd }).then(load);
  }
  function createJSON(type, body) {
    fetch(`/api/${type}`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    }).then(load);
  }
  function patch(type, id, body) {
    fetch(`/api/${type}/${id}`, {
      method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    }).then(async (r) => {
      if (!r.ok) {
        const e = await r.json().catch(() => ({}));
        alert(e.error || "Update failed.");
      }
      load();
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
    return !!document.querySelector(
      "#insulin-table input, #insulin-table select, #meal-table input, #meal-table select, " +
      "#foods-table input, #templates-list input, #templates-list select"
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
  fromEl.value = nowInput(24);
  toEl.value = nowInput(0);
  setPreset(document.querySelector('.range-picker .range-presets button[data-hours="24"]'));
  window.addEventListener("load", load);
  setInterval(autoRefresh, REFRESH_MS);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) autoRefresh(); });
})();
