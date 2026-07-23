// Phone UI: tab switching, a compact 24h glucose chart with live refresh, and
// the meal "plate builder" (foods + counts, saved-meal templates).
(function () {
  // --- tabs ---
  const tabs = document.querySelectorAll(".tab");
  tabs.forEach((tab) => {
    tab.addEventListener("click", () => {
      tabs.forEach((t) => t.classList.remove("active"));
      tab.classList.add("active");
      const which = tab.dataset.tab;
      document.getElementById("tab-insulin").classList.toggle("hidden", which !== "insulin");
      document.getElementById("tab-meal").classList.toggle("hidden", which !== "meal");
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

  // ================= live refresh (current reading + mini chart) =============
  const REFRESH_MS = 60000;

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
  function updateCurrent() {
    return fetch("/api/current").then((r) => r.json()).then(renderCurrent).catch(() => {});
  }

  let miniChart = null;
  function draw() {
    const ctx = document.getElementById("mini-chart");
    if (!ctx || typeof Chart === "undefined") return;
    fetch("/api/timeline")
      .then((r) => r.json())
      .then((data) => {
        const pts = data.glucose.map((p) => ({ x: p.t, y: p.v }));
        if (miniChart) {
          miniChart.data.datasets[0].data = pts;
          miniChart.update("none");
          return;
        }
        miniChart = new Chart(ctx, {
          type: "line",
          data: { datasets: [{
            data: pts, borderColor: "#4f8cff", borderWidth: 2,
            pointRadius: 0, tension: 0.3, fill: false,
          }]},
          options: {
            animation: false,
            parsing: false,
            plugins: { legend: { display: false } },
            scales: {
              x: { type: "linear", ticks: { color: "#8b90a0", maxTicksLimit: 6,
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

  function refresh() { updateCurrent(); draw(); }

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

    function renderTotals() {
      let c = 0, cal = 0, hasC = false, hasCal = false;
      plate.forEach((it) => {
        if (it.carbs_g != null) { c += it.carbs_g * it.count; hasC = true; }
        if (it.calories != null) { cal += it.calories * it.count; hasCal = true; }
      });
      const bits = [];
      if (hasC) bits.push(`${Math.round(c * 10) / 10} g carbs`);
      if (hasCal) bits.push(`${Math.round(cal)} cal`);
      totalsEl.textContent = plate.length ? bits.join(" · ") : "";
    }

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
        .then(() => { resetBuilder(); refreshRecent(); status(named ? "Meal logged & saved." : "Meal logged."); })
        .catch(() => status("Could not log meal."));
    });

    loadFoods();
    loadTemplates();
    renderPlate();
  }

  // ================= init ====================================================
  window.addEventListener("load", refresh);
  setInterval(() => { if (!document.hidden) refresh(); }, REFRESH_MS);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
})();
