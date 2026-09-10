// Phone UI: tab switching, a compact 12h glucose chart with live refresh, and
// the meal "plate builder" (foods + counts, saved-meal templates).
(function () {
  // Swap the datetime-local inputs for 24-hour ones before anything reads them.
  // The markup keeps type="datetime-local" so the form still works if JS fails.
  SD.timeFields();

  // --- tabs ---
  // Each button owns the panel at #tab-<data-tab>, so adding a tab is markup only.
  const tabs = document.querySelectorAll(".tab");
  tabs.forEach((tab) => {
    tab.addEventListener("click", () => {
      tabs.forEach((t) => {
        t.classList.toggle("active", t === tab);
        const panel = document.getElementById("tab-" + t.dataset.tab);
        if (panel) panel.classList.toggle("hidden", t !== tab);
      });
      // The reference goes stale while the tab is closed (it's only refreshed
      // when visible), so bring it up to date on the way in.
      if (tab.dataset.tab === "meal") updateMealRef();
    });
  });

  function esc(s) {
    return (s || "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  }
  function numOrNull(v) {
    if (v == null || String(v).trim() === "") return null;
    const n = parseFloat(v);
    return isNaN(n) ? null : n;
  }
  function nowInput() {
    const d = new Date();
    d.setSeconds(0, 0);
    const p = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
  }

  // "Now" buttons: reset an adjacent datetime input to the current time (the
  // prefilled value goes stale if the app is left open for a while).
  document.querySelectorAll("[data-now]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const el = document.getElementById(btn.dataset.now);
      if (el) el.value = nowInput();
    });
  });

  // Brief success confirmation overlay so a fast tap gives visible feedback.
  let toastTimer = null;
  let toastHide = null;
  function toast(msg) {
    const el = document.getElementById("toast");
    if (!el) return;
    el.textContent = msg;
    el.hidden = false;
    void el.offsetWidth; // reflow so the transition re-fires on rapid repeats
    el.classList.add("show");
    clearTimeout(toastTimer);
    clearTimeout(toastHide);
    toastTimer = setTimeout(() => {
      el.classList.remove("show");
      toastHide = setTimeout(() => { if (!el.classList.contains("show")) el.hidden = true; }, 250);
    }, 1600);
  }

  // Insulin and notes both post via HTMX; confirm on a successful post.
  [["#tab-insulin form", "Dose logged"], ["#tab-note form", "Note logged"]].forEach(([sel, msg]) => {
    const form = document.querySelector(sel);
    if (!form) return;
    form.addEventListener("htmx:afterRequest", (e) => {
      if (e.detail && e.detail.successful) toast(msg);
    });
  });

  // ================= live refresh (current reading + mini chart) =============
  const REFRESH_MS = 60000;
  // The mini chart is ~128px tall on a phone. A full day of readings, doses and
  // meal markers packed into that is a smear you can't read a shape off; half a
  // day is still more than enough context for "where am I heading". The desktop
  // keeps the wider windows.
  const CHART_HOURS = 12;
  // The chart carries no tick labels, so the window it covers is said in words
  // once, under it. Fixed for the life of the page — the span never changes,
  // only which 12 hours it is.
  const spanEl = document.getElementById("chart-span");
  if (spanEl) spanEl.textContent = `last ${CHART_HOURS} hours`;

  function statusClass(c) {
    if (!c.has_reading) return "";
    if (c.is_low) return "is-low";
    if (c.is_high) return "is-high";
    return "in-range";
  }
  function renderCurrent(c) {
    const el = document.getElementById("current");
    if (!el) return;
    el.className = ("current " + statusClass(c)).trim();
    if (c.has_reading) {
      el.innerHTML =
        `<div class="current-value">${c.value}<span class="trend">${c.trend}</span></div>` +
        `<div class="current-meta">${c.units} · ${c.minutes_ago} min ago</div>`;
    } else {
      el.innerHTML =
        `<div class="current-value">—</div>` +
        `<div class="current-meta">no glucose reading yet</div>`;
    }
  }
  // Insulin tiles under the glucose reading: on-board amount + action phase.
  // Descriptive only (Layer 1 of the insulin-awareness plan): they state active
  // insulin and where its action sits on the curve, never a dose to take.
  function setTile(id, val, lab, muted) {
    const tile = document.getElementById(id);
    if (!tile) return;
    tile.classList.toggle("muted", muted);
    tile.querySelector(".io-val").textContent = val;
    tile.querySelector(".io-lab").textContent = lab;
  }
  function renderIo(c) {
    if (c.iob) {
      const n = c.iob_dose_count;
      setTile("iob-tile", `≈${c.iob} u`, n ? `on board · ${n} dose${n === 1 ? "" : "s"}` : "on board", false);
    } else {
      setTile("iob-tile", "—", "no insulin on board", true);
    }
    if (c.activity_pct != null) {
      setTile("act-tile", `${c.activity_pct}%`, `action ${c.activity_dir}`, false);
    } else {
      setTile("act-tile", "—", "no action", true);
    }
  }
  function updateCurrent() {
    return fetch("/api/current")
      .then((r) => r.json())
      .then((c) => { renderCurrent(c); renderIo(c); })
      .catch(() => {});
  }

  let miniChart = null;
  function draw() {
    const ctx = document.getElementById("mini-chart");
    if (!ctx || typeof Chart === "undefined") return;
    fetch(`/api/timeline?hours=${CHART_HOURS}`)
      .then((r) => r.json())
      .then((data) => {
        const g = data.glucose.map((p) => ({ x: p.t, y: p.v }));
        // The trend with the sensor's ~30-minute ringing filtered out, same as
        // the desktop. On a 128px-tall chart the raw trace is mostly wobble, so
        // this is the line to read; the readings stay behind it, faint, because
        // they are what actually happened.
        const sm = (data.smoothed || []).map((p) => ({ x: p.t, y: p.v }));
        // Sit the dose/meal markers in a row along the foot of the chart.
        const ys = g.map((p) => p.y);
        const yMax = ys.length ? Math.max(...ys) : 0;
        const row = SD.markerRow(data.units, yMax);
        const doses = data.doses.map((d) => ({ x: d.t, y: row.dose, kind: d.kind, label: `${d.units}u ${d.kind}` }));
        const meals = data.meals.map((m) => ({
          x: m.t, y: row.meal,
          label: m.label + (m.total_carbs != null ? ` (${m.total_carbs}g)` : ""),
        }));
        if (miniChart) {
          miniChart.data.datasets[0].data = g;
          miniChart.data.datasets[1].data = sm;
          miniChart.data.datasets[2].data = doses;
          miniChart.data.datasets[3].data = meals;
          // The window slides forward with every refresh, so re-pin it too.
          miniChart.options.scales.x.min = data.from;
          miniChart.options.scales.x.max = data.to;
          miniChart.update("none");
          return;
        }
        miniChart = new Chart(ctx, {
          data: { datasets: [
            { type: "line", data: g, borderColor: "rgba(139,144,160,0.45)", borderWidth: 1,
              pointRadius: 0, tension: 0.3, fill: false, parsing: false },
            { type: "line", data: sm, borderColor: "#4f8cff", borderWidth: 2,
              pointRadius: 0, tension: 0.3, fill: false, parsing: false },
            { type: "scatter", label: "Insulin", data: doses,
              borderColor: (c) => SD.doseColor(c.raw && c.raw.kind),
              backgroundColor: (c) => SD.doseColor(c.raw && c.raw.kind),
              pointStyle: "triangle", radius: 5, parsing: false },
            { type: "scatter", label: "Meal", data: meals, borderColor: "#ffb020",
              backgroundColor: "#ffb020", pointStyle: "rectRot", radius: 5, parsing: false },
          ]},
          options: {
            animation: false,
            parsing: false,
            // The .chart-box wrapper sets the height (see style.css); without
            // this Chart.js would derive it from the width instead.
            maintainAspectRatio: false,
            plugins: {
              legend: { display: false },
              tooltip: { callbacks: { label: (c) => c.raw.label || `${c.parsed.y} ${data.units}` } },
            },
            scales: {
              // No tick labels on either axis. The phone chart answers "what
              // shape am I in", not "what was the number at 14:20" — the big
              // reading above it is the number, and on a 128px canvas the ticks
              // cost more width and height than the reading they gave back. The
              // span they used to imply is stated once under the chart instead
              // (#chart-span), and the shaded target band keeps the vertical
              // scale legible without a single label.
              //
              // Pinned to the window the API resolved — see desktop.js.
              x: { type: "linear", min: data.from, max: data.to,
                   ticks: { display: false }, grid: { display: false } },
              // Hard zero base, not suggestedMin: a low is the thing this chart
              // exists to make obvious, and a floating baseline changes how far
              // down "3.2" looks from one refresh to the next. Same fixed
              // reference point every time — see desktop.js.
              y: { min: 0, suggestedMax: SD.chartTop(data.units),
                   ticks: { display: false }, grid: { color: "#2c303c" } },
            },
          },
          plugins: [SD.targetBand(data.target_low, data.target_high)],
        });
      })
      .catch(() => {});
  }

  // Re-render the recent list from the server partial, so an entry logged or
  // edited here shows exactly what was stored rather than what was typed.
  function refreshRecent() {
    fetch("/api/recent").then((r) => r.text()).then((html) => {
      const el = document.getElementById("recent");
      if (el) el.outerHTML = html;
    }).catch(() => {});
  }

  // Assigned by the meal builder below; a no-op when the builder isn't on the
  // page so the refresh cycle doesn't need to know whether it exists.
  let updateMealRef = () => {};
  function mealTabOpen() {
    const t = document.getElementById("tab-meal");
    return t && !t.classList.contains("hidden");
  }

  function refresh() {
    updateCurrent();
    draw();
    // Glucose and IOB both move under a plate that hasn't changed, so the
    // reference has to follow the clock as well as the form.
    if (mealTabOpen()) updateMealRef();
  }

  // ================= combobox factory ========================================
  // A self-contained dropdown (native <datalist> is unreliable on mobile).
  // getItems() returns the current array of {name, ...}; rightLabel(item)
  // gives the secondary text; onPick(item) fires on selection.
  function makeCombo(input, list, getItems, onPick, rightLabel) {
    let filtered = [], active = -1;
    function currentFilter() {
      const q = input.value.trim().toLowerCase();
      const items = getItems();
      return q ? items.filter((s) => s.name.toLowerCase().includes(q)) : items;
    }
    function open() {
      filtered = currentFilter(); active = -1;
      if (!getItems().length) { list.hidden = true; return; }
      if (!filtered.length) {
        list.innerHTML = `<li class="empty" aria-disabled="true">No matches</li>`;
      } else {
        list.innerHTML = filtered
          .map((s, i) => `<li role="option" data-i="${i}"><span>${esc(s.name)}</span><span class="s-carb">${esc(rightLabel(s))}</span></li>`)
          .join("");
        list.querySelectorAll("li[data-i]").forEach((li) => {
          li.addEventListener("pointerdown", (e) => { e.preventDefault(); onPick(filtered[+li.dataset.i]); close(); });
        });
      }
      list.hidden = false; input.setAttribute("aria-expanded", "true");
    }
    function close() { list.hidden = true; active = -1; input.setAttribute("aria-expanded", "false"); }
    function highlight() { list.querySelectorAll("li[data-i]").forEach((li, i) => li.classList.toggle("active", i === active)); }
    // The dropdown is a typing aid, not a browse menu: on a phone a tap to
    // focus brings the keyboard up, and the full list on top of that buries
    // the rest of the form. So focus/click open it only when text is already
    // in the box, and close it otherwise (a stale list from before a blur
    // must not linger); the first keystroke is what brings it up.
    const openIfTyping = () => { if (input.value.trim()) open(); else close(); };
    input.addEventListener("focus", openIfTyping);
    input.addEventListener("click", openIfTyping);
    input.addEventListener("input", open); // drops the list on the first keystroke, re-filters after
    input.addEventListener("blur", () => setTimeout(close, 120));
    input.addEventListener("keydown", (e) => {
      if (list.hidden && (e.key === "ArrowDown" || e.key === "ArrowUp")) { open(); return; }
      const n = list.querySelectorAll("li[data-i]").length;
      if (e.key === "ArrowDown") { e.preventDefault(); active = Math.min(active + 1, n - 1); highlight(); }
      else if (e.key === "ArrowUp") { e.preventDefault(); active = Math.max(active - 1, 0); highlight(); }
      else if (e.key === "Enter") { if (!list.hidden && active >= 0) { e.preventDefault(); onPick(filtered[active]); close(); } }
      else if (e.key === "Escape") { close(); }
    });
    return { open, close };
  }

  // ================= meal plate builder ======================================
  const nameEl = document.getElementById("meal-name");
  if (nameEl) {
    const tmplList = document.getElementById("tmpl-suggest");
    const plateEl = document.getElementById("plate-list");
    const plateEmpty = document.getElementById("plate-empty");
    const totalsEl = document.getElementById("plate-totals");
    const foodEl = document.getElementById("food-name");
    const foodList = document.getElementById("food-suggest");
    const carbsEl = document.getElementById("food-carbs");
    const calEl = document.getElementById("food-cal");
    const countEl = document.getElementById("food-count");
    const addBtn = document.getElementById("add-to-plate");
    const libBtn = document.getElementById("save-to-library");
    const typeEl = document.getElementById("meal-type");
    const tsEl = document.getElementById("meal-ts");
    const noteEl = document.getElementById("meal-note");
    const logBtn = document.getElementById("log-meal");
    const statusEl = document.getElementById("meal-status");

    let foods = [];
    let templates = [];
    let plate = [];           // [{food_id, name, carbs_g, calories, count}]
    let pickedFoodId = null;  // set when a library food is chosen; cleared on manual edit

    function status(msg) {
      statusEl.textContent = msg;
      setTimeout(() => { if (statusEl.textContent === msg) statusEl.textContent = ""; }, 2500);
    }

    function loadFoods() {
      return fetch("/api/foods")
        .then((r) => r.json())
        .then((d) => { foods = d; renderPendingChip(); })
        .catch(() => {});
    }
    function loadTemplates() {
      return fetch("/api/meal-templates").then((r) => r.json()).then((d) => { templates = d; }).catch(() => {});
    }

    function fmtMacros(carbs, cal) {
      const bits = [];
      if (carbs != null) bits.push(`${carbs}g`);
      if (cal != null) bits.push(`${cal}cal`);
      return bits.join(" · ");
    }

    function renderPlate() {
      plateEl.innerHTML = "";
      plate.forEach((it, i) => {
        const li = document.createElement("li");
        li.innerHTML =
          `<span class="pi-name">${esc(it.name)}</span>` +
          `<span class="pi-macros">${esc(fmtMacros(it.carbs_g, it.calories))}</span>` +
          `<input class="pi-count" type="number" min="0" step="0.5" inputmode="decimal" value="${it.count}">` +
          `<button type="button" class="pi-del" title="Remove">✕</button>`;
        li.querySelector(".pi-count").addEventListener("change", (e) => {
          it.count = parseFloat(e.target.value) || 0;
          renderTotals();
        });
        li.querySelector(".pi-del").addEventListener("click", () => { plate.splice(i, 1); renderPlate(); });
        plateEl.appendChild(li);
      });
      plateEmpty.classList.toggle("hidden", plate.length > 0);
      renderTotals();
    }

    // Carb total for the plate, plus whether *every* item contributed one. A
    // plate where half the items have no carb count still totals to a number,
    // and that number is a floor — the reference panel has to say so.
    function plateCarbs() {
      let total = 0, counted = 0;
      plate.forEach((it) => {
        if (it.carbs_g != null) { total += it.carbs_g * it.count; counted += 1; }
      });
      return {
        grams: counted ? Math.round(total * 10) / 10 : null,
        complete: plate.length > 0 && counted === plate.length,
      };
    }

    function renderTotals() {
      let cal = 0, hasCal = false;
      plate.forEach((it) => {
        if (it.calories != null) { cal += it.calories * it.count; hasCal = true; }
      });
      const carbs = plateCarbs();
      const bits = [];
      if (carbs.grams != null) bits.push(`${carbs.grams} g carbs`);
      if (hasCal) bits.push(`${Math.round(cal)} cal`);
      totalsEl.textContent = plate.length ? bits.join(" · ") : "";
      refreshRef();
    }

    // ---- experimental bolus reference (see sugardaddy/bolus.py) -------------
    // The same formula the desktop replays against past meals, run against the
    // plate being built: a figure to reconcile the intended dose against, never
    // an amount to give. It shows its components so a gap against the user's own
    // judgement is diagnosable, and it names the inputs it went without rather
    // than quietly treating them as zero.
    const refEl = document.getElementById("meal-ref");
    const refCorr = document.getElementById("mr-corr");
    const refCorrNote = document.getElementById("mr-corr-note");
    const refMeal = document.getElementById("mr-meal");
    const refMealNote = document.getElementById("mr-meal-note");
    const refTotal = document.getElementById("mr-total");
    const refIob = document.getElementById("mr-iob-val");
    const refWhy = document.getElementById("mr-why");
    let refOff = false;   // no ISF configured: the panel doesn't exist at all
    let refTimer = null;

    const uStr = (n) => `${Math.round(n * 10) / 10}u`;
    const signedU = (n) => `${n > 0 ? "+" : n < 0 ? "−" : ""}${uStr(Math.abs(n))}`;

    // Everything the figure had to do without, worst first. The plate's own carb
    // gaps are only visible here (the server sees a total, not which items fed
    // it), so they're folded in alongside the server's `missing` list.
    function refReasons(missing, d, carbsComplete) {
      const why = [];
      if (missing.includes("glucose")) {
        // "stale" and "none at all" are both a dropped correction, but they are
        // different problems — one waits, the other needs the sensor looked at.
        why.push(d.glucose == null ? "there's no glucose reading yet"
                                   : "the last glucose reading is too old to correct against");
      }
      if (missing.includes("isf")) why.push("no ISF is configured");
      if (missing.includes("icr")) why.push("no carb ratio is configured");
      if (missing.includes("carbs")) why.push("no carbs are entered yet");
      else if (!carbsComplete) why.push("not every item on the plate has a carb count");
      return why;
    }

    function renderRef(d, carbs) {
      if (!refEl) return;
      if (!d.enabled) { refOff = true; refEl.hidden = true; return; }
      const r = d.ref || {};
      // A "*" marks a figure built from only some of its inputs — the same
      // convention as the desktop table and the report, so an incomplete 1.2u
      // can't be read as "barely dose here" when the carbs simply aren't in yet.
      const why = refReasons(r.missing || [], d, carbs.complete);
      const star = why.length ? "*" : "";
      refEl.hidden = false;
      refEl.classList.toggle("mr-partial", why.length > 0);

      // Correction, with what it is correcting from and to. Signed: below target
      // it is negative, and it eats into the carb cover rather than vanishing.
      refCorr.textContent = r.correction_units == null ? "—" : signedU(r.correction_units);
      if (d.glucose != null && !d.glucose_stale) {
        // Both to the precision the unit is conventionally quoted at — JSON
        // hands back 7.0 as 7, and "at 5.1, target 7" reads like two scales.
        const g = (n) => n.toFixed(d.units === "mg/dL" ? 0 : 1);
        refCorrNote.textContent = `at ${g(d.glucose)}, target ${g(d.target)} ${d.units}`;
      } else {
        refCorrNote.textContent = d.glucose == null ? "no reading" : "reading too old";
      }

      refMeal.textContent = r.carb_units == null ? "—" : uStr(r.carb_units);
      refMealNote.textContent = carbs.grams == null ? "no carbs entered" : `${carbs.grams} g carbs`;

      refTotal.textContent = r.suggested_units == null ? "—" : `≈${uStr(r.suggested_units)}${star}`;
      // Deliberately NOT deducted from the figure above: a plate fully covered
      // an hour ago would otherwise report 0 u for the carbs going in now.
      refIob.textContent = r.iob_units ? uStr(r.iob_units) : "none";

      refWhy.textContent = why.length ? `* ${why.join("; ")}` : "";
    }

    function fetchRef() {
      const carbs = plateCarbs();
      const q = carbs.grams != null ? `?carbs=${encodeURIComponent(carbs.grams)}` : "";
      fetch(`/api/bolus-reference${q}`)
        .then((r) => r.json())
        .then((d) => renderRef(d, carbs))
        .catch(() => {});
    }

    // Debounced: editing a count fires per keystroke, and a reference that
    // flickers through three values on the way to one is harder to trust.
    function refreshRef() {
      if (refOff || !refEl) return;
      clearTimeout(refTimer);
      refTimer = setTimeout(fetchRef, 250);
    }
    updateMealRef = refreshRef;

    function addToPlate() {
      const name = foodEl.value.trim();
      if (!name) { status("Enter a food name."); return; }
      plate.push({
        food_id: pickedFoodId,
        name,
        carbs_g: numOrNull(carbsEl.value),
        calories: numOrNull(calEl.value),
        count: parseFloat(countEl.value) || 1,
      });
      foodEl.value = ""; carbsEl.value = ""; calEl.value = ""; countEl.value = "1";
      pickedFoodId = null;
      renderPlate();
      foodEl.focus();
    }

    function resetBuilder() {
      plate = [];
      nameEl.value = ""; noteEl.value = ""; typeEl.value = "";
      tsEl.value = nowInput();
      renderPlate();
    }

    // -- food combobox: pick prefills macros; typing marks the item ad-hoc --
    makeCombo(
      foodEl, foodList, () => foods,
      (f) => {
        foodEl.value = f.name;
        carbsEl.value = f.carbs_g != null ? f.carbs_g : "";
        calEl.value = f.calories != null ? f.calories : "";
        pickedFoodId = f.id;
      },
      (f) => fmtMacros(f.carbs_g, f.calories),
    );
    foodEl.addEventListener("input", () => { pickedFoodId = null; });

    // -- saved-meal (template) combobox: load its plate --
    makeCombo(
      nameEl, tmplList, () => templates,
      (t) => {
        nameEl.value = t.name;
        plate = (t.items || []).map((i) => ({
          food_id: i.food_id, name: i.name, carbs_g: i.carbs_g, calories: i.calories, count: i.count,
        }));
        renderPlate();
      },
      (t) => `${(t.items || []).length} item${(t.items || []).length === 1 ? "" : "s"}`,
    );

    addBtn.addEventListener("click", addToPlate);

    libBtn.addEventListener("click", () => {
      const name = foodEl.value.trim();
      if (!name) { status("Enter a food name first."); return; }
      fetch("/api/foods", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, carbs_g: carbsEl.value, calories: calEl.value }),
      })
        .then((r) => r.json())
        .then((f) => { pickedFoodId = f.id; return loadFoods(); })
        .then(() => status(`Saved "${name}" to the library.`));
    });

    logBtn.addEventListener("click", () => {
      if (!plate.length) { status("Add at least one food."); return; }
      const named = !!nameEl.value.trim();
      fetch("/api/meal", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          ts: tsEl.value, name: nameEl.value.trim(), meal_type: typeEl.value,
          note: noteEl.value.trim(), items: plate,
        }),
      })
        .then((r) => r.json())
        // A named meal is also saved to the library (created or updated by name).
        .then(() => loadTemplates())
        .then(() => { resetBuilder(); refreshRecent(); toast(named ? "Meal logged & saved" : "Meal logged"); })
        .catch(() => status("Could not log meal."));
    });

    // ---- pending foods: the carb counts still owed ------------------------
    // A food logged without carbs is registered in the library as pending (see
    // web.register_pending_foods) so it can be chased up later. Chasing it up
    // used to mean finding a desktop, which is not where the meal was logged
    // and not where the answer is known — usually you are still holding the
    // packet. The chip below the food entry is the whole footprint: no chip
    // when the queue is empty, so the tab is no longer for the trouble.
    const chip = document.getElementById("pending-chip");
    const pendSheet = document.getElementById("pending-sheet");
    const pendList = document.getElementById("pending-list");
    const pendStatus = document.getElementById("pending-status");

    function pendingFoods() {
      return foods.filter((f) => f.pending);
    }

    // Fill blanks on the current plate from the library, by name. Same rule as
    // the server's backfill: only an item that recorded *nothing* is touched.
    function adoptKnownCarbs() {
      const byName = new Map(foods.map((f) => [f.name.trim().toLowerCase(), f]));
      plate.forEach((it) => {
        if (it.carbs_g != null) return;
        const f = byName.get((it.name || "").trim().toLowerCase());
        if (!f || f.carbs_g == null) return;
        it.carbs_g = f.carbs_g;
        if (it.calories == null) it.calories = f.calories;
        if (it.food_id == null) it.food_id = f.id;
      });
      renderPlate();
    }

    function renderPendingChip() {
      if (!chip) return;
      const n = pendingFoods().length;
      chip.hidden = n === 0;
      chip.textContent = `${n} food${n === 1 ? "" : "s"} awaiting carbs`;
    }

    function pendingRow(f) {
      const li = document.createElement("li");
      li.className = "e-item";
      li.dataset.id = f.id;
      li.innerHTML =
        `<span class="pf-name">${esc(f.name)}</span>` +
        `<span class="ei-nums">` +
        `<input type="number" class="pf-carbs" step="1" min="0" inputmode="decimal" placeholder="carbs (g)" aria-label="Carbs for ${esc(f.name)}">` +
        `<input type="number" class="pf-cal" step="1" min="0" inputmode="decimal" placeholder="cal" value="${f.calories ?? ""}" aria-label="Calories for ${esc(f.name)}">` +
        `<button type="button" class="ei-del" title="Delete this food">✕</button></span>`;
      // A pending food is sometimes just a typo that made it onto a plate. It
      // can go — deleting the library row leaves the logged meal untouched,
      // since items are snapshots.
      li.querySelector(".ei-del").addEventListener("click", () => {
        if (!confirm(`Delete "${f.name}" from the food library?`)) return;
        fetch(`/api/foods/${f.id}`, { method: "DELETE" })
          .then(() => loadFoods())
          .then(() => { li.remove(); if (!pendingFoods().length) closePending(); })
          .catch(() => { pendStatus.textContent = "Could not delete that."; });
      });
      return li;
    }

    function openPending() {
      pendList.innerHTML = "";
      pendingFoods().forEach((f) => pendList.appendChild(pendingRow(f)));
      pendStatus.textContent = "";
      pendSheet.hidden = false;
    }

    function closePending() {
      pendSheet.hidden = true;
      pendList.innerHTML = "";
    }

    function savePending() {
      // Only rows that were actually filled in are sent — the rest stay pending,
      // which is the honest answer for a food whose carbs are still unknown.
      const edits = [...pendList.querySelectorAll("li[data-id]")]
        .map((li) => ({
          id: +li.dataset.id,
          carbs_g: numOrNull(li.querySelector(".pf-carbs").value),
          calories: numOrNull(li.querySelector(".pf-cal").value),
        }))
        .filter((e) => e.carbs_g != null);
      if (!edits.length) { pendStatus.textContent = "Enter a carb count on at least one."; return; }
      pendStatus.textContent = "Saving…";
      Promise.all(
        edits.map((e) =>
          fetch(`/api/foods/${e.id}`, {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ carbs_g: e.carbs_g, calories: e.calories }),
          }).then((r) => (r.ok ? r.json() : {})),
        ),
      )
        .then((results) => {
          // How much history that just completed is the point of the exercise,
          // so it is reported rather than left as a silent side effect.
          const filled = results.reduce((n, r) => n + (r.filled_items || 0), 0);
          return loadFoods().then(() => {
            closePending();
            // The plate being built can be holding one of these right now — the
            // usual case, in fact, since that is what put it on the queue. Take
            // the new figures into it so the totals and the bolus reference
            // stop reading it as a carb-less item.
            adoptKnownCarbs();
            toast(filled ? `Saved · ${filled} logged item${filled === 1 ? "" : "s"} filled` : "Saved");
          });
        })
        .catch(() => { pendStatus.textContent = "Could not save those."; });
    }

    if (chip) {
      chip.addEventListener("click", openPending);
      document.getElementById("pending-save").addEventListener("click", savePending);
      document.getElementById("pending-cancel").addEventListener("click", closePending);
      pendSheet.addEventListener("click", (e) => { if (e.target === pendSheet) closePending(); });
      document.addEventListener("keydown", (e) => {
        if (e.key === "Escape" && !pendSheet.hidden) closePending();
      });
    }

    loadFoods();
    loadTemplates();
    renderPlate();
  }

  // ================= edit / delete a logged entry ============================
  // Everything in the recent list is editable in place: long-press an entry and
  // the sheet opens on it, with Save and Delete. Long-press rather than a row of
  // pencil icons because this list is read far more often than it is corrected,
  // and three buttons per row on a phone is a list you can't scan — and rather
  // than tap, because a tap on a mis-hit row must not open a dialog over the
  // number you were reading.
  //
  // One sheet, built per entry type at open time. Meals get their whole plate:
  // a wrong carb count is the correction actually worth making, and sending the
  // user to the desktop for it is how it doesn't get made.
  const LONG_PRESS_MS = 450;
  const PRESS_SLOP_PX = 10;  // past this the gesture was a scroll, not a press

  const sheet = document.getElementById("entry-sheet");
  const sheetTitle = document.getElementById("sheet-title");
  const sheetBody = document.getElementById("sheet-body");
  const sheetStatus = document.getElementById("sheet-status");
  let editing = null;  // {kind, id, items?} while the sheet is open

  // The kinds/meal types the server rendered into the log forms — read off those
  // selects rather than re-templated, so there is one list per concept.
  function optionsFrom(sel, selected) {
    const src = document.querySelector(sel);
    if (!src) return "";
    return [...src.options]
      .map((o) => `<option value="${esc(o.value)}"${o.value === (selected || "") ? " selected" : ""}>${esc(o.textContent)}</option>`)
      .join("");
  }

  function sheetSay(msg) {
    sheetStatus.textContent = msg;
  }

  function closeSheet() {
    editing = null;
    sheet.hidden = true;
    sheetBody.innerHTML = "";
    sheetSay("");
  }

  function tsField(input) {
    return `<label class="sf">Time
      <input type="datetime-local" class="e-ts" value="${esc(input)}" aria-label="Time"></label>`;
  }

  // -- meal plate rows (the same shape as the builder's, editable) --
  function mealItemRow(it) {
    const li = document.createElement("li");
    li.className = "e-item";
    li.innerHTML =
      `<input type="text" class="ei-name" value="${esc(it.name || "")}" placeholder="food" aria-label="Food">` +
      `<span class="ei-nums">` +
      `<input type="number" class="ei-carbs" step="1" min="0" inputmode="decimal" value="${it.carbs_g ?? ""}" placeholder="carbs" aria-label="Carbs (g)">` +
      `<input type="number" class="ei-cal" step="1" min="0" inputmode="decimal" value="${it.calories ?? ""}" placeholder="cal" aria-label="Calories">` +
      `<input type="number" class="ei-count" step="0.5" min="0" inputmode="decimal" value="${it.count ?? 1}" aria-label="Count">` +
      `<button type="button" class="ei-del" title="Remove">✕</button></span>`;
    li.querySelector(".ei-del").addEventListener("click", () => li.remove());
    return li;
  }

  function readMealItems() {
    return [...sheetBody.querySelectorAll(".e-item")]
      .map((li) => ({
        // food_id is dropped on purpose: an item renamed here is a different
        // food, and stale provenance is worse than none (see models.MealItem).
        name: li.querySelector(".ei-name").value.trim(),
        carbs_g: numOrNull(li.querySelector(".ei-carbs").value),
        calories: numOrNull(li.querySelector(".ei-cal").value),
        count: parseFloat(li.querySelector(".ei-count").value) || 1,
      }))
      .filter((it) => it.name);
  }

  function buildSheet(kind, e) {
    if (kind === "insulin") {
      sheetTitle.textContent = "Edit dose";
      sheetBody.innerHTML =
        `<label class="sf">Units<input type="number" class="e-units" step="0.5" min="0" inputmode="decimal" value="${e.units}"></label>` +
        `<label class="sf">Type<select class="e-kind">${optionsFrom("#tab-insulin select[name=kind]", e.kind)}</select></label>` +
        tsField(e.input) +
        `<label class="sf">Note<input type="text" class="e-note" value="${esc(e.note || "")}" placeholder="optional note"></label>`;
    } else if (kind === "note") {
      sheetTitle.textContent = "Edit note";
      sheetBody.innerHTML =
        `<label class="sf">Note<textarea class="e-text" rows="3">${esc(e.text || "")}</textarea></label>` +
        tsField(e.input);
    } else {
      sheetTitle.textContent = "Edit meal";
      sheetBody.innerHTML =
        `<ul class="e-items"></ul>` +
        `<button type="button" class="combo-btn e-additem">+ Item</button>` +
        `<label class="sf">Name<input type="text" class="e-name" value="${esc(e.name || "")}" placeholder="optional name"></label>` +
        `<label class="sf">Type<select class="e-type">${optionsFrom("#meal-type", e.meal_type)}</select></label>` +
        tsField(e.input) +
        `<label class="sf">Note<input type="text" class="e-mnote" value="${esc(e.note || "")}" placeholder="optional note"></label>`;
      const list = sheetBody.querySelector(".e-items");
      (e.items || []).forEach((it) => list.appendChild(mealItemRow(it)));
      sheetBody.querySelector(".e-additem").addEventListener("click", () => {
        list.appendChild(mealItemRow({}));
      });
    }
    // The canonical datetime field is swapped for 24-hour boxes here as well —
    // a 12-hour "00 PM" would move a dose half a day (see SD.timeField).
    SD.timeFields(sheetBody);
  }

  function openEditor(kind, id) {
    // The list is server-rendered HTML, so the values behind it come from the
    // API rather than being scraped back out of the markup.
    fetch("/api/entries?hours=24")
      .then((r) => r.json())
      .then((d) => {
        const bucket = { insulin: "doses", meal: "meals", note: "notes" }[kind];
        const entry = (d[bucket] || []).find((x) => x.id === id);
        if (!entry) { toast("That entry is gone."); refreshRecent(); return; }
        editing = { kind, id };
        buildSheet(kind, entry);
        sheet.hidden = false;
      })
      .catch(() => toast("Couldn't open that entry."));
  }

  function editPayload() {
    const ts = sheetBody.querySelector(".e-ts");
    const q = (sel) => sheetBody.querySelector(sel);
    if (editing.kind === "insulin") {
      return {
        ts: ts.value, units: numOrNull(q(".e-units").value),
        kind: q(".e-kind").value, note: q(".e-note").value.trim(),
      };
    }
    if (editing.kind === "note") {
      return { ts: ts.value, text: q(".e-text").value.trim() };
    }
    return {
      ts: ts.value, name: q(".e-name").value.trim(), meal_type: q(".e-type").value,
      note: q(".e-mnote").value.trim(), items: readMealItems(),
    };
  }

  function saveEdit() {
    const body = editPayload();
    if (editing.kind === "insulin" && !(body.units > 0)) { sheetSay("Enter the units."); return; }
    if (editing.kind === "note" && !body.text) { sheetSay("A note needs some words."); return; }
    if (editing.kind === "meal" && !body.items.length) { sheetSay("A meal needs at least one item."); return; }
    const { kind, id } = editing;
    fetch(`/api/${kind}/${id}`, {
      method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    })
      .then((r) => { if (!r.ok) throw new Error(); closeSheet(); refreshRecent(); refresh(); toast("Saved"); })
      .catch(() => sheetSay("Could not save that."));
  }

  function deleteEdit() {
    if (!confirm("Delete this entry?")) return;
    const { kind, id } = editing;
    fetch(`/api/${kind}/${id}`, { method: "DELETE" })
      .then((r) => { if (!r.ok) throw new Error(); closeSheet(); refreshRecent(); refresh(); toast("Deleted"); })
      .catch(() => sheetSay("Could not delete that."));
  }

  if (sheet) {
    document.getElementById("sheet-save").addEventListener("click", saveEdit);
    document.getElementById("sheet-delete").addEventListener("click", deleteEdit);
    document.getElementById("sheet-cancel").addEventListener("click", closeSheet);
    // Backdrop tap closes; a tap inside the panel must not.
    sheet.addEventListener("click", (e) => { if (e.target === sheet) closeSheet(); });
    document.addEventListener("keydown", (e) => { if (e.key === "Escape" && editing) closeSheet(); });

    // Delegated from the document: the recent list is replaced wholesale by every
    // HTMX swap and refresh, so a listener bound to it would last one post.
    let pressTimer = null, pressAt = null;
    const entryAt = (t) => t && t.closest && t.closest("#recent li[data-id]");
    const cancelPress = () => { clearTimeout(pressTimer); pressTimer = null; pressAt = null; };

    document.addEventListener("pointerdown", (e) => {
      const li = entryAt(e.target);
      if (!li) return;
      pressAt = { x: e.clientX, y: e.clientY };
      pressTimer = setTimeout(() => {
        pressTimer = null;
        // Haptic where it exists: the press has no visible progress, so without
        // it a long-press feels like nothing happened until the sheet appears.
        if (navigator.vibrate) navigator.vibrate(15);
        openEditor(li.dataset.kind, +li.dataset.id);
      }, LONG_PRESS_MS);
    });
    document.addEventListener("pointermove", (e) => {
      // A press that travels is a scroll. Cancel rather than fight the page.
      if (!pressAt) return;
      if (Math.abs(e.clientX - pressAt.x) > PRESS_SLOP_PX ||
          Math.abs(e.clientY - pressAt.y) > PRESS_SLOP_PX) cancelPress();
    });
    ["pointerup", "pointercancel", "scroll"].forEach((ev) =>
      document.addEventListener(ev, cancelPress, true));
    // Android fires its own text-selection/context menu on a long press, which
    // would land on top of the sheet.
    document.addEventListener("contextmenu", (e) => { if (entryAt(e.target)) e.preventDefault(); });
  }

  // ================= push notifications ======================================
  // The one notification is the basal reminder (see sugardaddy/notify.py). This
  // has to be hand-written rather than HTMX: it must talk to the browser's
  // PushManager before it has anything to send the server.

  // The applicationServerKey has to be raw bytes, but the server sends the key as
  // base64url — the only form the Web Push spec puts on the wire.
  function keyToBytes(b64) {
    const padded = (b64 + "=".repeat((4 - (b64.length % 4)) % 4))
      .replace(/-/g, "+")
      .replace(/_/g, "/");
    return Uint8Array.from(atob(padded), (c) => c.charCodeAt(0));
  }

  const notifyBtn = () => document.getElementById("notify-toggle");
  const notifyHint = () => document.getElementById("notify-hint");

  function setNotifyState(label, message, disabled) {
    const b = notifyBtn();
    const h = notifyHint();
    if (b) {
      b.textContent = label;
      b.disabled = !!disabled;
    }
    if (h && message) h.textContent = message;
  }

  async function pushSubscribe(reg, key) {
    // Must stay inside the click handler's task: Android only shows the
    // permission prompt for a request it can attribute to a user gesture.
    const permission = await Notification.requestPermission();
    if (permission !== "granted") {
      setNotifyState("Enable reminders", "Permission denied — enable it in site settings.", false);
      return;
    }
    const sub = await reg.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: keyToBytes(key),
    });
    const res = await fetch("/api/push/subscribe", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(Object.assign(sub.toJSON(), { label: navigator.platform || "" })),
    });
    if (!res.ok) throw new Error("subscribe failed: " + res.status);
    setNotifyState("Disable reminders", "This device will be told when no basal has been logged.", false);
  }

  async function pushUnsubscribe(sub) {
    // Tell the server first: if the browser subscription goes and then the server
    // can't be reached, it keeps pushing to an endpoint nothing listens on.
    await fetch("/api/push/unsubscribe", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ endpoint: sub.endpoint }),
    });
    await sub.unsubscribe();
    setNotifyState("Enable reminders", "Reminders are off for this device.", false);
  }

  async function initPush() {
    if (!notifyBtn()) return;
    if (!("serviceWorker" in navigator) || !("PushManager" in window) || !("Notification" in window)) {
      setNotifyState("Not supported", "This browser can't do push notifications.", true);
      return;
    }

    let key;
    try {
      const res = await fetch("/api/push/key");
      if (!res.ok) {
        setNotifyState("Unavailable", "Reminders aren't switched on for this server.", true);
        return;
      }
      key = (await res.json()).key;
    } catch (_) {
      setNotifyState("Unavailable", "Couldn't reach the server.", true);
      return;
    }

    const reg = await navigator.serviceWorker.ready;
    let sub = await reg.pushManager.getSubscription();
    if (sub) {
      setNotifyState("Disable reminders", "This device will be told when no basal has been logged.", false);
    } else {
      setNotifyState("Enable reminders", "Get a reminder when no basal dose has been logged.", false);
    }

    notifyBtn().addEventListener("click", async () => {
      setNotifyState("Working…", null, true);
      try {
        sub = await reg.pushManager.getSubscription();
        if (sub) await pushUnsubscribe(sub);
        else await pushSubscribe(reg, key);
      } catch (err) {
        setNotifyState("Enable reminders", "Something went wrong: " + err.message, false);
      }
    });
  }

  // ================= init ====================================================
  window.addEventListener("load", initPush);
  window.addEventListener("load", refresh);
  setInterval(() => { if (!document.hidden) refresh(); }, REFRESH_MS);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
})();
