// Phone UI: a status screen (glucose, depot, today's entries) and three big
// buttons, each opening a full-screen sheet that is finished with taps. See
// docs/plans/redesign.md for why it is shaped this way.
//
// Touch rules, everywhere in this file: act on `click` (a completed tap — the
// browser withholds it from a gesture that scrolled), never on pointerdown; and
// every sheet goes through openSheet(), which keeps the press that opened it
// from landing on it (SD.guardOpen) and makes the phone's back gesture close
// the sheet instead of leaving the app.
(function () {
  const BOOT = JSON.parse(document.getElementById("boot").textContent || "{}");
  const UNITS = BOOT.units || "mmol/L";
  const KIND_LABEL = { bolus: "Bolus", correction: "Correction", basal: "Basal" };
  // Rapid-acting first, in the order they come up in a day.
  const KIND_ORDER = ["bolus", "correction", "basal"].filter((k) => (BOOT.kinds || []).includes(k));
  const REFRESH_MS = 60000;
  const CHART_HOURS = 12;

  // ================= small helpers ===========================================
  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => [...(root || document).querySelectorAll(sel)];
  function esc(s) {
    return String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  }
  function num(v) {
    if (v == null || String(v).trim() === "") return null;
    const n = parseFloat(v);
    return isNaN(n) ? null : n;
  }
  const r1 = (n) => Math.round(n * 10) / 10;
  const fmtU = (n) => `${r1(n)} u`;
  const fmtG = (n) => `${Math.round(n)} g`;
  const pad = (n) => String(n).padStart(2, "0");
  // A local "YYYY-MM-DDTHH:MM" — the shape the server's parse_local() takes.
  function inputStamp(d) {
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
  }
  const hhmm = (ms) => { const d = new Date(ms); return `${pad(d.getHours())}:${pad(d.getMinutes())}`; };
  function dayWord(ms) {
    const d = new Date(ms), t = new Date();
    if (d.toDateString() === t.toDateString()) return "Today";
    t.setDate(t.getDate() - 1);
    if (d.toDateString() === t.toDateString()) return "Yesterday";
    return d.toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" });
  }
  const cap = (s) => (s ? s[0].toUpperCase() + s.slice(1) : "");

  function getJSON(url) {
    return fetch(url).then((r) => { if (!r.ok) throw new Error(r.status); return r.json(); });
  }
  function sendJSON(method, url, body) {
    return fetch(url, {
      method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    }).then(async (r) => {
      const d = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(d.error || `HTTP ${r.status}`);
      return d;
    });
  }
  // /api/insulin and /api/note are form posts (they also serve HTMX); without
  // an HX-Request header they answer JSON.
  function sendForm(url, fields) {
    const fd = new FormData();
    Object.entries(fields).forEach(([k, v]) => fd.append(k, v ?? ""));
    return fetch(url, { method: "POST", body: fd }).then(async (r) => {
      const d = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(d.error || `HTTP ${r.status}`);
      return d;
    });
  }

  let toastTimer = null;
  function toast(msg, bad) {
    const el = $("#toast");
    el.textContent = msg;
    el.classList.toggle("bad", !!bad);
    el.hidden = false;
    void el.offsetWidth; // reflow so the transition re-fires on rapid repeats
    el.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => {
      el.classList.remove("show");
      setTimeout(() => { if (!el.classList.contains("show")) el.hidden = true; }, 250);
    }, 1800);
  }

  // ================= sheets ==================================================
  // Sheets stack (search opens over the food sheet, an item over search). Each
  // open pushes a history entry, so the phone's back gesture — the most natural
  // "no, go back" there is — closes the top sheet rather than the app. Closing
  // by button goes through history.back() for the same reason, so the two
  // paths can't leave history and the stack disagreeing.
  const stack = [];

  // `replace` swaps the top sheet for this one in place (detail → its editor)
  // without touching history twice: a back() followed straight away by a
  // push races the browser's asynchronous history traversal.
  function openSheet(id, build, { replace } = {}) {
    const el = $("#sheet-" + id);
    const panel = el.firstElementChild;
    panel.className = "sheet-panel";
    const swapped = !!(replace && stack.length);
    if (swapped) hideTop();
    if (build) build(panel);
    el.style.zIndex = 200 + stack.length * 2;
    el.hidden = false;
    stack.push(el);
    if (swapped) history.replaceState({ depth: stack.length }, "");
    else history.pushState({ depth: stack.length }, "");
    SD.guardOpen(el);
    requestAnimationFrame(() => el.classList.add("open"));
    return panel;
  }
  function hideTop() {
    const el = stack.pop();
    if (!el) return;
    el.classList.remove("open");
    el.hidden = true;
    if (el._onClose) { const f = el._onClose; el._onClose = null; f(); }
  }
  function closeSheet(n) {
    n = Math.min(n || 1, stack.length);
    if (n > 0) history.go(-n);
  }
  const closeAll = () => closeSheet(stack.length);
  window.addEventListener("popstate", (e) => {
    const depth = (e.state && e.state.depth) || 0;
    while (stack.length > depth) hideTop();
  });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && stack.length) closeSheet(); });
  $$(".sheet").forEach((el) => SD.backdropClose(el, () => { if (stack[stack.length - 1] === el) closeSheet(); }));

  // Standard sheet chrome: a header with Cancel/back on the left and the title,
  // a scrolling body, and a footer for the one primary action.
  function sheetFrame(panel, { title, back, body, foot }) {
    panel.innerHTML =
      `<header class="sheet-head">
         <button type="button" class="head-btn" data-close>${back ? "‹ Back" : "Cancel"}</button>
         <h3>${esc(title)}</h3><span class="head-spacer"></span>
       </header>
       <div class="sheet-body">${body}</div>
       ${foot ? `<footer class="sheet-foot">${foot}</footer>` : ""}`;
    $("[data-close]", panel).addEventListener("click", () => closeSheet());
  }

  // ================= reusable controls =======================================
  // Amount: a big number between − and +, with one-tap chips under it. The
  // number is an input, so the odd 7.5 is one tap on it plus the keypad — but
  // nothing ever opens the keyboard by itself.
  function amountMarkup(cls, unit) {
    return `<div class="amount ${cls}">
        <button type="button" class="amt-btn" data-d="-1" aria-label="Less">−</button>
        <label class="amt-display"><input class="amt-input" type="number" inputmode="decimal" min="0" step="any" placeholder="0"><span class="amt-unit">${unit}</span></label>
        <button type="button" class="amt-btn" data-d="1" aria-label="More">+</button>
      </div>
      <div class="chips amt-chips ${cls}-chips"></div>`;
  }
  function amountField(root, cls, { step = 1, chips = [], fmt = (v) => `${v}`, onChange = () => {} } = {}) {
    const box = $("." + cls, root);
    const input = $(".amt-input", box);
    const chipBox = $(`.${cls}-chips`, root);
    const get = () => num(input.value);
    // Chips and listeners follow the value; the input itself is only written
    // by set(), never while someone is typing "7." on the way to "7.5".
    function sync() {
      $$(".chip", chipBox).forEach((c) => c.classList.toggle("on", num(c.dataset.v) === get()));
      onChange(get());
    }
    function set(v) {
      input.value = v == null ? "" : r1(v);
      sync();
    }
    function setChips(list) {
      chipBox.innerHTML = (list || []).map((v) => `<button type="button" class="chip" data-v="${v}">${esc(fmt(v))}</button>`).join("");
      chipBox.hidden = !(list || []).length;
      sync();
    }
    $$(".amt-btn", box).forEach((b) => b.addEventListener("click", () => {
      const v = (get() || 0) + step * +b.dataset.d;
      set(Math.max(0, v));
    }));
    chipBox.addEventListener("click", (e) => {
      const c = e.target.closest(".chip");
      if (c) set(num(c.dataset.v) === get() ? null : num(c.dataset.v));
    });
    input.addEventListener("input", sync);
    setChips(chips);
    return { get, set, setChips, input };
  }

  // When: "Now" unless changed. The offsets are resolved at save time, so a
  // sheet left open for ten minutes still logs "now" as now.
  function whenMarkup() {
    return `<div class="when">
        <div class="chips when-chips">
          <button type="button" class="chip on" data-off="0">Now</button>
          <button type="button" class="chip" data-off="15">15 min ago</button>
          <button type="button" class="chip" data-off="30">30 min ago</button>
          <button type="button" class="chip" data-off="60">1 h ago</button>
          <button type="button" class="chip" data-off="other">Other…</button>
        </div>
        <div class="when-other" hidden><input type="datetime-local" class="when-input" aria-label="Time"></div>
      </div>`;
  }
  function whenField(root, initial) {
    const box = $(".when", root);
    const other = $(".when-other", box);
    const input = $(".when-input", box);
    let off = "0";
    function pick(v) {
      off = v;
      $$(".chip", box).forEach((c) => c.classList.toggle("on", c.dataset.off === v));
      other.hidden = v !== "other";
    }
    $(".when-chips", box).addEventListener("click", (e) => {
      const c = e.target.closest(".chip");
      if (!c) return;
      if (c.dataset.off === "other" && !input.value) input.value = inputStamp(new Date());
      pick(c.dataset.off);
    });
    SD.timeField(input);
    if (initial) { input.value = initial; pick("other"); }
    return {
      // "" = now, which the server resolves on its own clock.
      value() {
        if (off === "other") return input.value;
        if (off === "0") return "";
        return inputStamp(new Date(Date.now() - +off * 60000));
      },
    };
  }

  // A note behind a link: optional detail costs one line until it's wanted.
  function noteMarkup(value) {
    return `<button type="button" class="link add-note" ${value ? "hidden" : ""}>+ Add a note</button>
      <input type="text" class="note-input" placeholder="note" value="${esc(value || "")}" ${value ? "" : "hidden"}>`;
  }
  function noteField(root) {
    const btn = $(".add-note", root), input = $(".note-input", root);
    btn.addEventListener("click", () => { btn.hidden = true; input.hidden = false; input.focus(); });
    return { value: () => input.value.trim() };
  }

  // ================= data ====================================================
  let current = {};
  let entries = { doses: [], meals: [], notes: [] };
  let shortcuts = { doses: {}, foods: [], meals: [] };
  let foods = [];
  let templates = [];

  function loadShortcuts() {
    return getJSON("/api/shortcuts").then((d) => { shortcuts = d; }).catch(() => {});
  }
  function loadLibrary() {
    return Promise.all([getJSON("/api/foods"), getJSON("/api/meal-templates")])
      .then(([f, t]) => { foods = f; templates = t; })
      .catch(() => {});
  }

  // ================= home ====================================================
  function renderHero(c) {
    const el = $("#hero");
    if (!c.has_reading) {
      el.className = "hero st-none";
      el.innerHTML = `<div class="hero-row"><span class="hero-val">—</span></div><div class="hero-meta">No glucose reading yet</div>`;
      return;
    }
    // Past a quarter of an hour the number is history, not "now": it stays,
    // but greyed, and says how old it is first.
    const stale = c.minutes_ago > 15;
    const st = c.is_low ? "low" : c.is_high ? "high" : "in";
    const word = c.is_low ? "Low" : c.is_high ? "High" : "In range";
    el.className = `hero st-${st}${stale ? " stale" : ""}`;
    el.innerHTML =
      `<div class="hero-row"><span class="hero-val">${c.value}</span><span class="hero-trend">${c.trend || ""}</span></div>` +
      `<div class="hero-meta">${stale ? `Last reading ${c.minutes_ago} min ago` : `<span class="hero-state">${word}</span> · ${c.minutes_ago} min ago`} · ${c.units}</div>`;
  }
  function renderPills(c) {
    $("#iob-pill .pill-val").textContent = c.iob ? fmtU(c.iob) : "None";
    $("#iob-pill").classList.toggle("quiet", !c.iob);
    const b = c.basal_hours_ago;
    $("#basal-pill .pill-val").textContent = b == null ? "—" : b < 1 ? "just now" : `${Math.round(b)} h ago`;
    $("#basal-pill .pill-lab").textContent = `last basal${c.basal_units ? ` · ${r1(c.basal_units)} u` : ""}`;
  }
  function updateCurrent() {
    return getJSON("/api/current").then((c) => { current = c; renderHero(c); renderPills(c); }).catch(() => {});
  }

  let miniChart = null, chartData = null;
  function drawChart(data) {
    const ctx = $("#mini-chart");
    if (!ctx || typeof Chart === "undefined") return;
    chartData = data;
    const g = data.glucose.map((p) => ({ x: p.t, y: p.v }));
    const sm = (data.smoothed || []).map((p) => ({ x: p.t, y: p.v }));
    const yMax = g.length ? Math.max(...g.map((p) => p.y)) : 0;
    const row = SD.markerRow(data.units, yMax);
    const doses = data.doses.map((d) => ({ x: d.t, y: row.dose, kind: d.kind }));
    const meals = data.meals.map((m) => ({ x: m.t, y: row.meal }));
    if (miniChart) {
      [g, sm, doses, meals].forEach((d, i) => { miniChart.data.datasets[i].data = d; });
      miniChart.options.scales.x.min = data.from;
      miniChart.options.scales.x.max = data.to;
      miniChart.update("none");
      return;
    }
    miniChart = new Chart(ctx, {
      data: { datasets: [
        // The readings, faint; the smoothed trend is the line to read (the
        // sensor rings by ~±0.8 mmol/L, which on a short chart is all wobble).
        { type: "line", data: g, borderColor: SD.tok("--chart-raw"), borderWidth: 1,
          pointRadius: 0, tension: 0.3, parsing: false },
        { type: "line", data: sm, borderColor: SD.tok("--accent"), borderWidth: 3,
          pointRadius: 0, tension: 0.3, parsing: false, borderCapStyle: "round" },
        { type: "scatter", data: doses, backgroundColor: (c) => SD.doseColor(c.raw && c.raw.kind),
          borderWidth: 0, pointStyle: "triangle", radius: 5, parsing: false },
        { type: "scatter", data: meals, backgroundColor: SD.tok("--food"), borderWidth: 0,
          pointStyle: "rectRot", radius: 5, parsing: false },
      ]},
      options: {
        animation: false, parsing: false, maintainAspectRatio: false,
        events: [], // display only: a tap on the chart must not do anything
        plugins: { legend: { display: false }, tooltip: { enabled: false } },
        scales: {
          x: { type: "linear", min: data.from, max: data.to, ticks: { display: false }, grid: { display: false }, border: { display: false } },
          // Hard zero base and a fixed top: the same 3.2 sits at the same
          // height on every refresh (see SD.chartTop).
          y: { min: 0, suggestedMax: SD.chartTop(data.units), ticks: { display: false },
               grid: { display: false }, border: { display: false } },
        },
      },
      plugins: [SD.targetBand(data.target_low, data.target_high)],
    });
  }
  function updateChart() {
    return getJSON(`/api/timeline?hours=${CHART_HOURS}`).then(drawChart).catch(() => {});
  }
  SD.onTheme(() => { if (miniChart) { miniChart.destroy(); miniChart = null; } if (chartData) drawChart(chartData); });

  // Today: one timeline, newest first. Grouping by kind (the old layout) made
  // "what happened around lunch" a matter of reading three lists side by side.
  function timelineRows() {
    const rows = [];
    entries.doses.forEach((d) => rows.push({ kind: "insulin", id: d.id, t: d.t,
      main: `<b>${r1(d.units)} u</b> ${esc(KIND_LABEL[d.kind] || d.kind).toLowerCase()}`, sub: d.note }));
    entries.meals.forEach((m) => {
      const g = m.total_carbs != null ? `<span class="tl-tag">${fmtG(m.total_carbs)}</span>` : `<span class="tl-tag warn">no carbs</span>`;
      rows.push({ kind: "meal", id: m.id, t: m.t, main: `${esc(m.label)} ${g}`, sub: m.note });
    });
    entries.notes.forEach((n) => rows.push({ kind: "note", id: n.id, t: n.t, main: esc(n.text), sub: "" }));
    return rows.sort((a, b) => b.t - a.t);
  }
  function renderTimeline() {
    const ul = $("#timeline");
    const rows = timelineRows();
    if (!rows.length) {
      ul.innerHTML = `<li class="tl-empty">Nothing logged in the last 24 hours.</li>`;
      return;
    }
    let lastDay = "Today";
    ul.innerHTML = rows.map((r) => {
      const day = dayWord(r.t);
      const sep = day !== lastDay ? `<li class="tl-sep">${esc(day)}</li>` : "";
      lastDay = day;
      return sep + `<li class="tl tl-${r.kind}" data-kind="${r.kind}" data-id="${r.id}">
          <span class="tl-time">${hhmm(r.t)}</span><span class="tl-dot"></span>
          <span class="tl-text"><span class="tl-main">${r.main}</span>${r.sub ? `<span class="tl-sub">${esc(r.sub)}</span>` : ""}</span>
          <span class="tl-chev" aria-hidden="true">›</span></li>`;
    }).join("");
  }
  function updateEntries() {
    return getJSON("/api/entries?hours=24").then((d) => { entries = d; renderTimeline(); }).catch(() => {});
  }
  $("#timeline").addEventListener("click", (e) => {
    const li = e.target.closest("li[data-id]");
    if (li) openDetail(li.dataset.kind, +li.dataset.id);
  });

  // The missing-carbs to-do: a chip on Home, absent when there's nothing owed.
  let todo = [];
  function updateTodo() {
    return getJSON("/api/missing-carbs?days=14").then((d) => {
      todo = d;
      const chip = $("#todo-chip");
      chip.hidden = !d.length;
      // "recent" because the phone asks for the last 14 days only; the full
      // backlog is on the desktop's Library view.
      chip.textContent = d.length === 1 ? "1 recent food still needs carbs" : `${d.length} recent foods still need carbs`;
    }).catch(() => {});
  }
  $("#todo-chip").addEventListener("click", openTodo);

  function refreshHome() {
    updateCurrent();
    updateChart();
    updateEntries();
    updateTodo();
  }

  // ================= insulin flow ============================================
  // `dose` given = editing that entry.
  function openInsulin(dose, preset, swap) {
    openSheet("insulin", (panel) => {
      sheetFrame(panel, {
        title: dose ? "Edit insulin" : "Insulin",
        body: `
          <div class="seg kind-seg" role="radiogroup">
            ${KIND_ORDER.map((k) => `<button type="button" class="seg-btn" data-kind="${k}">${KIND_LABEL[k] || k}</button>`).join("")}
          </div>
          ${amountMarkup("ins-amt", "u")}
          <p class="ctx-line"></p>
          <div class="corr-ref" hidden></div>
          <h4 class="field-title">When</h4>
          ${whenMarkup()}
          ${noteMarkup(dose && dose.note)}
          ${dose ? `<button type="button" class="link danger del-btn">Delete this entry</button>` : ""}`,
        foot: `<button type="button" class="primary big log-btn" disabled>Log</button>`,
      });
      let kind = (dose && dose.kind) || (preset && preset.kind) || "bolus";
      const logBtn = $(".log-btn", panel);
      const amt = amountField(panel, "ins-amt", {
        step: 1, fmt: (v) => `${v} u`,
        onChange: (v) => {
          logBtn.disabled = !(v > 0);
          logBtn.textContent = v > 0 ? `${dose ? "Save" : "Log"} ${r1(v)} u ${KIND_LABEL[kind].toLowerCase()}` : (dose ? "Save" : "Log");
        },
      });
      const when = whenField(panel, dose && dose.input);
      const note = noteField(panel);

      function setKind(k) {
        kind = k;
        $$(".kind-seg .seg-btn", panel).forEach((b) => b.classList.toggle("on", b.dataset.kind === k));
        amt.setChips((shortcuts.doses || {})[k] || []);
        renderCorrection();
      }
      $(".kind-seg", panel).addEventListener("click", (e) => {
        const b = e.target.closest(".seg-btn");
        if (b) setKind(b.dataset.kind);
      });

      // What the dose is going into, in one line — the two numbers people
      // check before dosing anyway.
      const bits = [];
      if (current.has_reading) bits.push(`Glucose ${current.value} ${current.trend || ""}`.trim());
      bits.push(current.iob ? `${fmtU(current.iob)} active` : "no active insulin");
      $(".ctx-line", panel).textContent = bits.join(" · ");

      // Correction mode carries the correction half of the reference: the same
      // experimental, non-directive figure the meal flow stacks, alone.
      function renderCorrection() {
        const box = $(".corr-ref", panel);
        if (kind !== "correction" || dose) { box.hidden = true; return; }
        getJSON("/api/bolus-reference").then((d) => {
          if (!d.enabled || !d.ref || d.ref.correction_units == null) { box.hidden = true; return; }
          const c = d.ref.correction_units;
          box.hidden = false;
          box.innerHTML = `<span class="cr-num">${c > 0 ? "+" : c < 0 ? "−" : ""}${r1(Math.abs(c))} u</span>
            <span class="cr-text">correction at your ISF (at ${d.glucose.toFixed(1)}, target ${d.target.toFixed(1)})${d.ref.iob_units ? ` · ${r1(d.ref.iob_units)} u already active, not deducted` : ""}</span>
            <span class="cr-cav">Experimental — a figure to check against, not advice.</span>`;
        }).catch(() => { box.hidden = true; });
      }

      setKind(kind);
      if (dose) amt.set(dose.units);
      else if (preset && preset.units) amt.set(preset.units);

      logBtn.addEventListener("click", () => {
        const units = amt.get();
        if (!(units > 0)) return;
        logBtn.disabled = true;
        const body = { units, kind, ts: when.value(), note: note.value() };
        const req = dose
          ? sendJSON("PATCH", `/api/insulin/${dose.id}`, { ...body, ts: body.ts || dose.input })
          : sendForm("/api/insulin", body);
        req.then(() => {
          closeAll();
          toast(dose ? "Saved" : `${r1(units)} u ${KIND_LABEL[kind].toLowerCase()} logged`);
          refreshHome();
        }).catch((err) => { logBtn.disabled = false; toast(`Couldn't save: ${err.message}`, true); });
      });
      if (dose) $(".del-btn", panel).addEventListener("click", () => deleteEntry("insulin", dose.id));
    }, { replace: !!swap });
  }

  // ================= food flow ===============================================
  // One meal being built (or edited) at a time.
  //   plate: [{name, carbs_g, calories, count, food_id, tags}]
  //   from:  the saved meal it was loaded from, with a fingerprint of its plate
  //          so "changed since loading" is a comparison, not a guess.
  let meal = null;

  function newMeal(edit) {
    meal = {
      edit: edit || null,
      plate: edit ? edit.items.map((i) => ({ ...i })) : [],
      name: edit ? edit.name : "",
      from: null,
      type: edit ? edit.meal_type : "",
      note: edit ? edit.note : "",
    };
  }
  const fingerprint = (items) => JSON.stringify(items.map((i) => [i.name.toLowerCase(), i.count, i.carbs_g]));
  function plateCarbs() {
    let total = 0, counted = 0;
    meal.plate.forEach((it) => { if (it.carbs_g != null) { total += it.carbs_g * it.count; counted += 1; } });
    return { grams: counted ? r1(total) : null, complete: meal.plate.length > 0 && counted === meal.plate.length };
  }
  function plateChanged() {
    return meal.from && fingerprint(meal.plate) !== meal.from.print;
  }
  function addToPlate(item) {
    const same = meal.plate.find((p) => p.name.toLowerCase() === item.name.toLowerCase() && p.carbs_g === item.carbs_g);
    if (same) same.count += item.count || 1;
    else meal.plate.push({ count: 1, calories: null, food_id: null, tags: "", ...item });
  }
  function addTemplate(t) {
    const wasEmpty = !meal.plate.length;
    t.items.forEach((i) => addToPlate({ name: i.name, carbs_g: i.carbs_g, calories: i.calories, count: i.count, food_id: i.food_id }));
    // Only an otherwise-empty plate *is* the saved meal; a saved meal added to
    // a plate that already had things on it is just more food.
    if (wasEmpty) {
      meal.name = t.name;
      meal.from = { id: t.id, name: t.name, print: fingerprint(meal.plate) };
    }
  }
  // A plate-line for a food from the library.
  const foodItem = (f) => ({ name: f.name, carbs_g: f.carbs_g, calories: f.calories, count: 1, food_id: f.id });

  // Breakfast / lunch / dinner / snack from the clock, so it is one tap to
  // correct rather than one field to fill.
  function guessMealType(d) {
    const h = d.getHours() + d.getMinutes() / 60;
    if (h >= 5 && h < 10.5) return "breakfast";
    if (h >= 11.5 && h < 14.5) return "lunch";
    if (h >= 17 && h < 21) return "dinner";
    return "snack";
  }

  function openFood(edit, swap) {
    newMeal(edit);
    // Shortcuts may be a beat behind the sheet; redraw when they land, but
    // only if the plate step is still the one showing.
    Promise.all([loadShortcuts(), loadLibrary()]).then(() => { if (meal && meal.step === 1) renderPlateStep(); });
    openSheet("food", (panel) => {
      panel.classList.add("tall");
      renderPlateStep(panel);
    }, { replace: !!swap });
    $("#sheet-food")._onClose = () => { meal = null; };
  }

  function plateRowsMarkup() {
    if (!meal.plate.length) {
      return `<p class="plate-empty">Tap a meal or food below, or search.</p>`;
    }
    return `<ul class="plate">${meal.plate.map((it, i) => `
        <li class="plate-row" data-i="${i}">
          <span class="pr-count">${it.count}×</span>
          <span class="pr-name">${esc(it.name)}${it.tags && it.tags.includes("estimate") ? ` <span class="tl-tag">estimate</span>` : ""}</span>
          <span class="pr-carbs">${it.carbs_g != null ? fmtG(it.carbs_g * it.count) : `<span class="warn">? g</span>`}</span>
          <span class="tl-chev" aria-hidden="true">›</span>
        </li>`).join("")}</ul>`;
  }

  function renderPlateStep(panel) {
    panel = panel || $("#sheet-food .sheet-panel");
    if (!meal) return;
    meal.step = 1;
    const carbs = plateCarbs();
    const n = meal.plate.length;
    const tiles = meal.edit ? [] : shortcuts.meals || [];
    const chips = shortcuts.foods || [];
    const scroll = $(".sheet-body", panel) ? $(".sheet-body", panel).scrollTop : 0;
    sheetFrame(panel, {
      title: meal.edit ? "Edit meal" : "What are you eating?",
      body: `
        ${meal.from ? `<div class="from-badge">From <b>${esc(meal.from.name)}</b>${plateChanged() ? " · changed" : ""}</div>` : ""}
        ${plateRowsMarkup()}
        <button type="button" class="searchbox">Search foods…</button>
        ${tiles.length ? `<h4 class="field-title">Saved meals</h4>
          <div class="tiles">${tiles.map((t) => {
            const g = t.items.reduce((a, i) => a + (i.carbs_g || 0) * i.count, 0);
            return `<button type="button" class="tile" data-tmpl="${t.id}"><span class="tile-name">${esc(t.name)}</span><span class="tile-sub">${fmtG(g)}</span></button>`;
          }).join("")}</div>` : ""}
        ${chips.length ? `<h4 class="field-title">Your foods</h4>
          <div class="chips food-chips">${chips.map((f) =>
            `<button type="button" class="chip food-chip" data-food="${f.id}">${esc(f.name)} <span class="chip-sub">${fmtG(f.carbs_g)}</span></button>`).join("")}</div>` : ""}
        <button type="button" class="tile tile-quick"><span class="tile-name">Quick carbs</span><span class="tile-sub">Not sure what it was? Just a carb estimate.</span></button>`,
      foot: `<div class="foot-sum">${n ? `<b>${carbs.grams != null ? fmtG(carbs.grams) : "? g"}</b>${carbs.complete || carbs.grams == null ? "" : "+"} carbs · ${n} item${n === 1 ? "" : "s"}` : "<span class=muted>Plate is empty</span>"}</div>
             <button type="button" class="primary next-btn" ${n ? "" : "disabled"}>Next</button>`,
    });
    $(".sheet-body", panel).scrollTop = scroll;

    $(".searchbox", panel).addEventListener("click", openSearch);
    $(".tile-quick", panel).addEventListener("click", () => openItem({ mode: "estimate" }));
    $$("[data-tmpl]", panel).forEach((b) => b.addEventListener("click", () => {
      const t = (shortcuts.meals || []).find((x) => x.id === +b.dataset.tmpl);
      if (t) { addTemplate(t); renderPlateStep(); }
    }));
    $$("[data-food]", panel).forEach((b) => b.addEventListener("click", () => {
      const f = chips.find((x) => x.id === +b.dataset.food);
      if (!f) return;
      addToPlate(foodItem(f));
      renderPlateStep();
      b.classList.add("pulse");
    }));
    $$(".plate-row", panel).forEach((li) => li.addEventListener("click", () => openItem({ mode: "edit", index: +li.dataset.i })));
    $(".next-btn", panel).addEventListener("click", renderDoseStep);
  }

  // Step 2: check and log. The reference is shown, the dose is the user's own
  // figure — it is never prefilled from the reference (insulin-awareness.md:
  // a prefilled number turns a cross-check into a directive).
  function renderDoseStep() {
    const panel = $("#sheet-food .sheet-panel");
    meal.step = 2;
    const carbs = plateCarbs();
    const edit = meal.edit;
    const type = meal.type || guessMealType(edit ? new Date(edit.t) : new Date());
    meal.type = type;
    const given = edit ? (edit._given || null) : null;
    sheetFrame(panel, {
      title: edit ? "Check & save" : "Check & log",
      back: true,
      body: `
        <div class="summary">
          <div class="sum-name">${esc(meal.name || meal.plate.map((i) => i.name).join(", "))}</div>
          <div class="sum-carbs">${carbs.grams != null ? fmtG(carbs.grams) : "No"} carbs${carbs.grams != null && !carbs.complete ? " (some items unknown)" : ""}</div>
        </div>
        <div class="seg type-seg">${BOOT.meal_types.map((t) => `<button type="button" class="seg-btn${t === type ? " on" : ""}" data-type="${t}">${cap(t)}</button>`).join("")}</div>
        <h4 class="field-title">When</h4>
        ${whenMarkup()}
        ${SD.refMarkup("food-ref")}
        ${edit
          ? `<p class="given-line">${given ? `Insulin logged with it: <b>${given}</b>` : "No insulin logged within 20 min of it."} Edit doses from the Today list.</p>`
          : `<h4 class="field-title">Insulin with this meal <span class="muted">(optional)</span></h4>${amountMarkup("meal-amt", "u")}`}
        ${meal.from && plateChanged() ? `<label class="switch-row"><input type="checkbox" class="upd-tmpl"><span>Also update the saved meal “${esc(meal.from.name)}”</span></label>` : ""}
        ${noteMarkup(meal.note)}`,
      foot: `<button type="button" class="primary big log-btn">${edit ? "Save changes" : "Log meal"}</button>`,
    });
    $("[data-close]", panel).replaceWith(Object.assign(document.createElement("button"), {
      type: "button", className: "head-btn", textContent: "‹ Back", onclick: () => renderPlateStep(),
    }));
    const when = whenField(panel, edit && edit.input);
    const note = noteField(panel);
    $(".type-seg", panel).addEventListener("click", (e) => {
      const b = e.target.closest(".seg-btn");
      if (!b) return;
      meal.type = b.dataset.type;
      $$(".type-seg .seg-btn", panel).forEach((x) => x.classList.toggle("on", x === b));
    });
    const logBtn = $(".log-btn", panel);
    let amt = null;
    if (!edit) {
      amt = amountField(panel, "meal-amt", {
        step: 1, chips: (shortcuts.doses || {}).bolus || [], fmt: (v) => `${v} u`,
        onChange: (v) => { logBtn.textContent = v > 0 ? `Log meal + ${r1(v)} u` : "Log meal"; },
      });
    }

    // Live for a new plate (against now), at the meal's own moment for an
    // edit — the corrected chips judged against the glucose they were eaten at.
    const qs = new URLSearchParams();
    if (carbs.grams != null) qs.set("carbs", carbs.grams);
    if (edit) qs.set("meal_id", edit.id);
    getJSON(`/api/bolus-reference?${qs}`).then((d) => SD.renderRef($(".food-ref", panel), d, carbs)).catch(() => {});

    logBtn.addEventListener("click", () => {
      logBtn.disabled = true;
      const ts = when.value();
      const items = meal.plate.map((i) => ({ ...i }));
      const body = {
        ts: ts || (edit ? edit.input : ""),
        name: meal.name, meal_type: meal.type, note: note.value(), items,
        save_template: !!($(".upd-tmpl", panel) && $(".upd-tmpl", panel).checked),
      };
      const units = amt ? amt.get() : null;
      // One timestamp for both, resolved here, so the dose pairs with the
      // plate in every analysis that matches the two by time.
      if (!body.ts && units > 0) body.ts = inputStamp(new Date());
      const save = edit ? sendJSON("PATCH", `/api/meal/${edit.id}`, body) : sendJSON("POST", "/api/meal", body);
      save
        .then(() => (units > 0 ? sendForm("/api/insulin", { units, kind: "bolus", ts: body.ts, note: "" }) : null))
        .then(() => {
          closeAll();
          toast(edit ? "Meal saved" : units > 0 ? `Meal + ${r1(units)} u logged` : "Meal logged");
          refreshHome();
          loadShortcuts();
        })
        .catch((err) => { logBtn.disabled = false; toast(`Couldn't save: ${err.message}`, true); });
    });
  }

  // ---- search: the whole library, as a full-screen list --------------------
  function openSearch() {
    openSheet("search", (panel) => {
      panel.classList.add("tall");
      panel.innerHTML = `
        <header class="sheet-head search-head">
          <input type="search" class="search-input" placeholder="Search foods" autocomplete="off" enterkeyhint="search">
          <button type="button" class="head-btn" data-close>Done</button>
        </header>
        <div class="sheet-body"><ul class="results"></ul></div>`;
      $("[data-close]", panel).addEventListener("click", () => closeSheet());
      const input = $(".search-input", panel);
      const list = $(".results", panel);
      function render() {
        const q = input.value.trim().toLowerCase();
        const match = (s) => !q || s.toLowerCase().includes(q);
        // Most-eaten first: the library is long, and the food you want is
        // nearly always one you have had before.
        const fs = foods.filter((f) => match(f.name)).sort((a, b) => b.uses - a.uses || a.name.localeCompare(b.name)).slice(0, 60);
        const ts = q ? templates.filter((t) => match(t.name)) : [];
        const exact = foods.some((f) => f.name.toLowerCase() === q);
        list.innerHTML =
          ts.map((t) => `<li class="res" data-tmpl="${t.id}"><span class="res-name">${esc(t.name)}</span><span class="res-sub">saved meal</span></li>`).join("") +
          fs.map((f) => `<li class="res" data-food="${f.id}"><span class="res-name">${esc(f.name)}</span><span class="res-sub">${f.carbs_g != null ? fmtG(f.carbs_g) : "? g"}</span></li>`).join("") +
          (q && !exact ? `<li class="res res-new" data-new="1"><span class="res-name">Add “${esc(input.value.trim())}”</span><span class="res-sub">just for this meal</span></li>` : "") +
          (!fs.length && !ts.length && !q ? `<li class="res-empty">No foods yet.</li>` : "");
      }
      list.addEventListener("click", (e) => {
        const li = e.target.closest(".res");
        if (!li) return;
        if (li.dataset.new) { openItem({ mode: "new", name: input.value.trim() }); return; }
        if (li.dataset.tmpl) {
          const t = templates.find((x) => x.id === +li.dataset.tmpl);
          if (t) { addTemplate(t); toast(`Added ${t.name}`); }
        } else {
          const f = foods.find((x) => x.id === +li.dataset.food);
          if (!f) return;
          addToPlate(foodItem(f));
          toast(`Added ${f.name}`);
        }
        renderPlateStep();
        closeSheet();
      });
      input.addEventListener("input", render);
      render();
      // Focus after the slide-in, so the keyboard comes up with the sheet
      // rather than shoving the page around mid-animation.
      setTimeout(() => input.focus(), 120);
    });
  }

  // ---- one plate line: edit it, add a typed one-off, or a quick estimate ----
  //   {mode: "edit", index} | {mode: "new", name} | {mode: "estimate"}
  function openItem(opts) {
    const it = opts.mode === "edit" ? meal.plate[opts.index] : null;
    const est = opts.mode === "estimate" || (it && (it.tags || "").includes("estimate"));
    openSheet("item", (panel) => {
      sheetFrame(panel, {
        title: opts.mode === "edit" ? "Edit item" : est ? "Quick carbs" : "New food",
        body: `
          <label class="field">${est ? "What was it? <span class=muted>(optional)</span>" : "Name"}
            <input type="text" class="it-name" value="${esc(it ? it.name : opts.name || "")}" placeholder="${est ? "e.g. cake at the party" : "food name"}"></label>
          <h4 class="field-title">${est ? "About how many carbs?" : "Carbs per serve"}</h4>
          ${amountMarkup("it-carbs", "g")}
          ${est ? "" : `<h4 class="field-title">How many</h4>${amountMarkup("it-count", "×")}`}
          ${opts.mode === "new" ? `<label class="switch-row"><input type="checkbox" class="it-keep"><span>Keep in my foods for next time</span></label>
             <p class="hint">Leave carbs empty if you don't know — it goes on the “still need carbs” list.</p>` : ""}
          ${it ? `<button type="button" class="link danger it-remove">Remove from plate</button>` : ""}`,
        foot: `<button type="button" class="primary big it-done">${it ? "Done" : "Add to plate"}</button>`,
      });
      const carbs = amountField(panel, "it-carbs", {
        step: est ? 5 : 1, chips: est ? [10, 20, 30, 45, 60, 90] : [], fmt: (v) => `${v} g`,
      });
      const count = est ? null : amountField(panel, "it-count", { step: 1, chips: [0.5, 1, 2, 3], fmt: (v) => `${v}×` });
      if (it) { carbs.set(it.carbs_g); if (count) count.set(it.count); }
      else if (count) count.set(1);
      $(".it-done", panel).addEventListener("click", () => {
        const name = $(".it-name", panel).value.trim() || (est ? "Estimated carbs" : "");
        if (!name) { toast("Give it a name", true); return; }
        const c = carbs.get();
        if (est && c == null) { toast("Enter a carb estimate", true); return; }
        const n = count ? count.get() || 1 : 1;
        if (it) {
          // A renamed line is a different food: drop its library link.
          if (name.toLowerCase() !== it.name.toLowerCase()) it.food_id = null;
          Object.assign(it, { name, carbs_g: c, count: n });
        } else {
          addToPlate({ name, carbs_g: c, count: n, tags: est ? "estimate" : "",
            keep: !!($(".it-keep", panel) && $(".it-keep", panel).checked) });
        }
        renderPlateStep();
        // An item added from search closes search too: back to the plate.
        closeSheet(opts.mode === "new" ? 2 : 1);
      });
      if (it) $(".it-remove", panel).addEventListener("click", () => {
        meal.plate.splice(opts.index, 1);
        renderPlateStep();
        closeSheet();
      });
    });
  }

  // ================= note flow ===============================================
  function openNote(n) {
    openSheet("note", (panel) => {
      sheetFrame(panel, {
        title: n ? "Edit note" : "Note",
        body: `<textarea class="note-text" rows="4" placeholder="What happened — sick, exercise, a bad night…">${esc(n ? n.text : "")}</textarea>
          <h4 class="field-title">When</h4>${whenMarkup()}
          ${n ? `<button type="button" class="link danger del-btn">Delete this note</button>` : ""}`,
        foot: `<button type="button" class="primary big log-btn">${n ? "Save" : "Log note"}</button>`,
      });
      const when = whenField(panel, n && n.input);
      $(".log-btn", panel).addEventListener("click", () => {
        const text = $(".note-text", panel).value.trim();
        if (!text) { toast("Write a few words first", true); return; }
        const ts = when.value();
        const req = n ? sendJSON("PATCH", `/api/note/${n.id}`, { text, ts: ts || n.input }) : sendForm("/api/note", { text, ts });
        req.then(() => { closeAll(); toast(n ? "Saved" : "Note logged"); refreshHome(); })
          .catch((err) => toast(`Couldn't save: ${err.message}`, true));
      });
      if (n) $(".del-btn", panel).addEventListener("click", () => deleteEntry("note", n.id));
    });
  }

  // ================= entry detail ============================================
  // A tap opens a read view first — what it was, and for a meal what it asked
  // for and what happened after — with Edit and Delete in it. Reading is the
  // common case; a tap must never drop you straight into an editor.
  function findEntry(kind, id) {
    const bucket = { insulin: "doses", meal: "meals", note: "notes" }[kind];
    return (entries[bucket] || []).find((x) => x.id === id);
  }
  function openDetail(kind, id) {
    const e = findEntry(kind, id);
    if (!e) { refreshHome(); return; }
    if (kind === "note") { openNote(e); return; }
    if (kind === "insulin") {
      openSheet("detail", (panel) => {
        sheetFrame(panel, {
          title: "Insulin",
          body: `<div class="detail-hero"><span class="dh-val">${r1(e.units)} u</span><span class="dh-sub">${KIND_LABEL[e.kind] || e.kind} · ${dayWord(e.t)} ${hhmm(e.t)}</span></div>
            ${e.note ? `<p class="detail-note">${esc(e.note)}</p>` : ""}
            <button type="button" class="link danger del-btn">Delete this entry</button>`,
          foot: `<button type="button" class="secondary big edit-btn">Edit</button>`,
        });
        $(".edit-btn", panel).addEventListener("click", () => openInsulin(e, null, true));
        $(".del-btn", panel).addEventListener("click", () => deleteEntry("insulin", e.id));
      });
      return;
    }
    openSheet("detail", (panel) => {
      panel.classList.add("tall");
      sheetFrame(panel, {
        title: e.meal_type ? cap(e.meal_type) : "Meal",
        body: `<div class="detail-hero"><span class="dh-val">${e.total_carbs != null ? fmtG(e.total_carbs) : "? g"}</span>
               <span class="dh-sub">${esc(e.name || "")}${e.name ? " · " : ""}${dayWord(e.t)} ${hhmm(e.t)}</span></div>
          <ul class="plate readonly">${e.items.map((i) => `<li class="plate-row"><span class="pr-count">${i.count}×</span>
            <span class="pr-name">${esc(i.name)}</span><span class="pr-carbs">${i.carbs_g != null ? fmtG(i.carbs_g * i.count) : `<span class="warn">? g</span>`}</span></li>`).join("")}</ul>
          ${e.note ? `<p class="detail-note">${esc(e.note)}</p>` : ""}
          <div class="insight"><p class="muted">Loading…</p></div>
          ${SD.refMarkup("detail-ref")}
          <button type="button" class="link danger del-btn">Delete this meal</button>`,
        foot: `<button type="button" class="secondary big edit-btn">Edit meal</button>`,
      });
      $(".del-btn", panel).addEventListener("click", () => deleteEntry("meal", e.id));
      $(".edit-btn", panel).addEventListener("click", () => openFood(e, true));
      getJSON(`/api/meal/${e.id}/insight`).then((d) => {
        const given = d.doses.length ? d.doses.map((x) => `${r1(x.units)} u ${x.kind}`).join(" + ") : null;
        e._given = given;
        const r = d.response;
        let resp = "";
        if (r && !r.pending && r.start_display != null) {
          const dl = r.peak_delta_display;
          resp = `<div class="resp"><div><span class="rv">${r.start_display}</span><span class="rl">at the meal</span></div>
              <div><span class="rv">${r.peak_display}</span><span class="rl">${Date.now() - e.t < 2 * 3600e3 ? "highest so far" : `peak${r.minutes_to_peak != null ? ` after ${r.minutes_to_peak} min` : ""}`}</span></div>
              <div><span class="rv ${dl > 3 ? "warn" : ""}">${dl > 0 ? "+" : ""}${dl}</span><span class="rl">rise</span></div></div>`;
        } else {
          resp = `<p class="muted">${r && r.pending ? "The glucose response is still coming in." : "No glucose readings around this meal."}</p>`;
        }
        $(".insight", panel).innerHTML = `<p class="given-line">${given ? `Insulin with it: <b>${given}</b>` : "No insulin logged within 20 min of it."}</p>${resp}`;
        const items = e.items;
        const counted = items.filter((i) => i.carbs_g != null).length;
        SD.renderRef($(".detail-ref", panel), d.reference,
          { grams: e.total_carbs, complete: items.length > 0 && counted === items.length });
      }).catch(() => { $(".insight", panel).innerHTML = `<p class="muted">Couldn't load the details.</p>`; });
    });
  }

  function deleteEntry(kind, id) {
    if (!confirm(kind === "meal" ? "Delete this meal?" : kind === "note" ? "Delete this note?" : "Delete this dose?")) return;
    fetch(`/api/${kind}/${id}`, { method: "DELETE" })
      .then((r) => { if (!r.ok) throw new Error(); closeAll(); toast("Deleted"); refreshHome(); })
      .catch(() => toast("Couldn't delete that", true));
  }

  // ================= missing carbs ===========================================
  function openTodo() {
    openSheet("todo", (panel) => {
      panel.classList.add("tall");
      sheetFrame(panel, {
        title: "Still need carbs",
        body: `<p class="hint">Fill one in and every meal it was logged on is completed too.</p>
          <ul class="todo-list">${todo.map((t, i) => `<li class="todo-row" data-i="${i}">
            <div class="todo-text"><b>${esc(t.name)}</b><span class="muted">${t.count}× · last ${esc(t.local.split(" ")[0])}</span></div>
            <input type="number" inputmode="decimal" min="0" class="todo-carbs" placeholder="g" aria-label="Carbs for ${esc(t.name)}">
            <button type="button" class="secondary todo-save">Save</button></li>`).join("")}</ul>`,
      });
      $$(".todo-row", panel).forEach((li) => {
        $(".todo-save", li).addEventListener("click", () => {
          const t = todo[+li.dataset.i];
          const carbs = num($(".todo-carbs", li).value);
          if (carbs == null) { toast("Enter the carbs first", true); return; }
          sendJSON("POST", "/api/missing-carbs", { name: t.name, carbs_g: carbs })
            .then((d) => {
              li.remove();
              toast(`Filled ${d.filled_items} entr${d.filled_items === 1 ? "y" : "ies"}`);
              updateTodo(); updateEntries();
              if (!$(".todo-row", panel)) closeSheet();
            })
            .catch((err) => toast(`Couldn't save: ${err.message}`, true));
        });
      });
    });
  }

  // ================= settings ================================================
  function openSettings() {
    openSheet("settings", (panel) => {
      sheetFrame(panel, {
        title: "Settings",
        body: `<div class="set-row"><div><b>Basal reminder</b><p class="hint notify-hint">Checking…</p></div>
            <button type="button" class="secondary notify-toggle" disabled>…</button></div>
          <a class="set-row set-link" href="/desktop"><div><b>Desktop view</b><p class="hint">Review, library, phone shortcuts</p></div><span class="tl-chev">›</span></a>
          <p class="hint center">Sugar Daddy v${esc(BOOT.version)} · not a medical device</p>`,
      });
      initPush(panel);
    });
  }

  // ---- push notifications (the basal reminder, see sugardaddy/notify.py) ----
  // Hand-written rather than HTMX: it must talk to the browser's PushManager
  // before it has anything to send the server.
  function keyToBytes(b64) {
    const padded = (b64 + "=".repeat((4 - (b64.length % 4)) % 4)).replace(/-/g, "+").replace(/_/g, "/");
    return Uint8Array.from(atob(padded), (c) => c.charCodeAt(0));
  }
  async function initPush(panel) {
    const btn = $(".notify-toggle", panel), hint = $(".notify-hint", panel);
    const set = (label, msg, disabled) => { btn.textContent = label; btn.disabled = !!disabled; if (msg) hint.textContent = msg; };
    if (!("serviceWorker" in navigator) || !("PushManager" in window) || !("Notification" in window)) {
      set("Not supported", "This browser can't do push notifications.", true);
      return;
    }
    let key;
    try {
      const res = await fetch("/api/push/key");
      if (!res.ok) { set("Unavailable", "Reminders aren't switched on for this server.", true); return; }
      key = (await res.json()).key;
    } catch (_) { set("Unavailable", "Couldn't reach the server.", true); return; }
    const reg = await navigator.serviceWorker.ready;
    let sub = await reg.pushManager.getSubscription();
    const on = () => set("Turn off", "This phone is told when no basal has been logged.", false);
    const off = (msg) => set("Turn on", msg || "Get told when no basal dose has been logged.", false);
    if (sub) on(); else off();
    btn.addEventListener("click", async () => {
      set("Working…", null, true);
      try {
        sub = await reg.pushManager.getSubscription();
        if (sub) {
          // Tell the server first: if the browser subscription goes and then the
          // server can't be reached, it keeps pushing to a dead endpoint.
          await fetch("/api/push/unsubscribe", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ endpoint: sub.endpoint }) });
          await sub.unsubscribe();
          off("Reminders are off for this phone.");
        } else {
          // Must stay inside the click's task: Android only shows the prompt
          // for a request it can attribute to a user gesture.
          if ((await Notification.requestPermission()) !== "granted") { off("Permission denied — enable it in site settings."); return; }
          sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: keyToBytes(key) });
          const res = await fetch("/api/push/subscribe", { method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify(Object.assign(sub.toJSON(), { label: navigator.platform || "" })) });
          if (!res.ok) throw new Error("subscribe failed: " + res.status);
          on();
        }
      } catch (err) { off("Something went wrong: " + err.message); }
    });
  }

  // ================= wiring ==================================================
  $$(".actionbar .act").forEach((b) => b.addEventListener("click", () => {
    const f = b.dataset.flow;
    if (f === "insulin") openInsulin();
    else if (f === "food") openFood();
    else openNote();
  }));
  $("#open-settings").addEventListener("click", openSettings);

  // The timeline is rendered from the API, so a fresh page starts with an
  // empty history state the sheets can stack on top of.
  history.replaceState({ depth: 0 }, "");
  loadShortcuts();
  window.addEventListener("load", refreshHome);
  setInterval(() => { if (!document.hidden) { updateCurrent(); updateChart(); if (!stack.length) updateEntries(); } }, REFRESH_MS);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refreshHome(); });
})();
