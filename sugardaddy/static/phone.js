// Phone UI: tab switching, a compact 12h glucose chart with live refresh, and
// the meal "plate builder" (foods + counts, saved-meal templates).
(function () {
  // Swap the datetime-local inputs for 24-hour ones before anything reads them.
  // The markup keeps type="datetime-local" so the form still works if JS fails.
  SD.timeFields();

  // --- tabs ---
  const tabs = document.querySelectorAll(".tab");
  tabs.forEach((tab) => {
    tab.addEventListener("click", () => {
      tabs.forEach((t) => t.classList.remove("active"));
      tab.classList.add("active");
      const which = tab.dataset.tab;
      document.getElementById("tab-insulin").classList.toggle("hidden", which !== "insulin");
      document.getElementById("tab-meal").classList.toggle("hidden", which !== "meal");
      // The reference goes stale while the tab is closed (it's only refreshed
      // when visible), so bring it up to date on the way in.
      if (which === "meal") updateMealRef();
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

  // Insulin uses an HTMX form; confirm on a successful post.
  const insulinForm = document.querySelector("#tab-insulin form");
  if (insulinForm) {
    insulinForm.addEventListener("htmx:afterRequest", (e) => {
      if (e.detail && e.detail.successful) toast("Dose logged");
    });
  }

  // ================= live refresh (current reading + mini chart) =============
  const REFRESH_MS = 60000;
  // The mini chart is ~128px tall on a phone. A full day of readings, doses and
  // meal markers packed into that is a smear you can't read a shape off; half a
  // day is still more than enough context for "where am I heading". The desktop
  // keeps the wider windows.
  const CHART_HOURS = 12;

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
        // Sit the dose/meal markers along the low end of the glucose range.
        const ys = g.map((p) => p.y);
        const yMin = ys.length ? Math.min(...ys) : 0;
        const yMax = ys.length ? Math.max(...ys) : 10;
        const doses = data.doses.map((d) => ({ x: d.t, y: yMin, kind: d.kind, label: `${d.units}u ${d.kind}` }));
        const meals = data.meals.map((m) => ({
          x: m.t, y: yMin + (yMax - yMin) * 0.06,
          label: m.label + (m.total_carbs != null ? ` (${m.total_carbs}g)` : ""),
        }));
        if (miniChart) {
          miniChart.data.datasets[0].data = g;
          miniChart.data.datasets[1].data = doses;
          miniChart.data.datasets[2].data = meals;
          // The window slides forward with every refresh, so re-pin it too.
          miniChart.options.scales.x.min = data.from;
          miniChart.options.scales.x.max = data.to;
          miniChart.update("none");
          return;
        }
        miniChart = new Chart(ctx, {
          data: { datasets: [
            { type: "line", data: g, borderColor: "#4f8cff", borderWidth: 2,
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
            plugins: {
              legend: { display: false },
              tooltip: { callbacks: { label: (c) => c.raw.label || `${c.parsed.y} ${data.units}` } },
            },
            scales: {
              // Pinned to the window the API resolved — see desktop.js.
              x: { type: "linear", min: data.from, max: data.to,
                   ticks: { color: "#8b90a0", maxTicksLimit: 6,
                     callback: (v) => SD.hhmm(v) }, grid: { display: false } },
              y: { ticks: { color: "#8b90a0" }, grid: { color: "#2c303c" },
                   suggestedMin: 0, title: { display: true, text: data.units, color: "#8b90a0" } },
            },
          },
          plugins: [SD.targetBand(data.target_low, data.target_high)],
        });
      })
      .catch(() => {});
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
    input.addEventListener("focus", open);
    input.addEventListener("click", open);
    input.addEventListener("input", open); // re-filter the list as you type
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
      return fetch("/api/foods").then((r) => r.json()).then((d) => { foods = d; }).catch(() => {});
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
    const refVal = refEl && refEl.querySelector(".mr-val");
    const refParts = document.getElementById("mr-parts");
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

    function renderRef(d, carbsComplete) {
      if (!refEl) return;
      if (!d.enabled) { refOff = true; refEl.hidden = true; return; }
      const r = d.ref || {};
      // A "*" marks a figure built from only some of its inputs — the same
      // convention as the desktop table and the report, so an incomplete 1.2u
      // can't be read as "barely dose here" when the carbs simply aren't in yet.
      const why = refReasons(r.missing || [], d, carbsComplete);
      refEl.hidden = false;
      refEl.classList.toggle("mr-partial", why.length > 0);
      refVal.textContent =
        r.suggested_units == null ? "—" : `≈${uStr(r.suggested_units)}${why.length ? "*" : ""}`;

      const bits = [];
      if (r.carb_units != null) bits.push(`${uStr(r.carb_units)} carbs`);
      if (r.correction_units != null) bits.push(`${signedU(r.correction_units)} correction`);
      if (r.iob_units) bits.push(`−${uStr(r.iob_units)} active`);
      if (d.glucose != null && !d.glucose_stale) {
        // Both to the precision the unit is conventionally quoted at — JSON
        // hands back 7.0 as 7, and "at 5.1, target 7" reads like two scales.
        const g = (n) => n.toFixed(d.units === "mg/dL" ? 0 : 1);
        bits.push(`at ${g(d.glucose)}, target ${g(d.target)} ${d.units}`);
      }
      refParts.textContent = bits.join(" · ");
      refWhy.textContent = why.length ? `* ${why.join("; ")}` : "";
    }

    function fetchRef() {
      const carbs = plateCarbs();
      const q = carbs.grams != null ? `?carbs=${encodeURIComponent(carbs.grams)}` : "";
      fetch(`/api/bolus-reference${q}`)
        .then((r) => r.json())
        .then((d) => renderRef(d, carbs.complete))
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

    function refreshRecent() {
      fetch("/api/recent").then((r) => r.text()).then((html) => {
        const el = document.getElementById("recent");
        if (el) el.outerHTML = html;
      }).catch(() => {});
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

    loadFoods();
    loadTemplates();
    renderPlate();
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
