// Phone UI: tab switching + a compact 24h glucose chart.
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

  // --- mini chart ---
  function draw() {
    const ctx = document.getElementById("mini-chart");
    if (!ctx || typeof Chart === "undefined") return;
    fetch("/api/timeline")
      .then((r) => r.json())
      .then((data) => {
        const pts = data.glucose.map((p) => ({ x: p.t, y: p.v }));
        new Chart(ctx, {
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

  // --- meal suggestions: custom combobox (saved shortcuts + recent meals) ---
  // A native <datalist> proved unreliable on mobile (won't open, autocomplete
  // quirks), so this is a self-contained dropdown we fully control.
  let suggestions = [];
  let activeIdx = -1;
  let filteredNow = [];
  const nameEl = document.getElementById("meal-name");
  const carbsEl = document.getElementById("meal-carbs");
  const tagsEl = document.getElementById("meal-tags");
  const idEl = document.getElementById("known-id");
  const updateBtn = document.getElementById("km-update");
  const saveNewBtn = document.getElementById("km-savenew");
  const listEl = document.getElementById("meal-suggest");
  const statusEl = document.getElementById("km-status");
  const mealForm = document.getElementById("meal-form");

  function loadSuggestions() {
    return fetch("/api/meal-suggestions")
      .then((r) => r.json())
      .then((data) => { suggestions = data; syncSelection(); })
      .catch(() => {});
  }

  function carbLabel(s) { return s.carbs_g != null ? `${s.carbs_g}g` : ""; }
  function esc(s) {
    return (s || "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  }

  function matchByName(name) {
    const n = (name || "").trim().toLowerCase();
    return suggestions.find((s) => s.name.trim().toLowerCase() === n) || null;
  }

  // Reflect an exact-name match in the hidden id + Update button (no prefill).
  function syncSelection() {
    const m = matchByName(nameEl.value);
    idEl.value = m && m.known_id ? m.known_id : "";
    updateBtn.disabled = !(m && m.known_id);
  }

  function currentFilter() {
    const q = nameEl.value.trim().toLowerCase();
    return q ? suggestions.filter((s) => s.name.toLowerCase().includes(q)) : suggestions;
  }

  function openList() {
    filteredNow = currentFilter();
    activeIdx = -1;
    if (!suggestions.length) return; // nothing saved/logged yet
    if (!filteredNow.length) {
      listEl.innerHTML = `<li class="empty" aria-disabled="true">No matching meals</li>`;
    } else {
      listEl.innerHTML = filteredNow
        .map((s, i) => `<li role="option" data-i="${i}"><span>${esc(s.name)}</span><span class="s-carb">${carbLabel(s)}</span></li>`)
        .join("");
      listEl.querySelectorAll("li[data-i]").forEach((li) => {
        // pointerdown + preventDefault keeps the input focused through the tap.
        li.addEventListener("pointerdown", (e) => { e.preventDefault(); pick(filteredNow[+li.dataset.i]); });
      });
    }
    listEl.hidden = false;
    nameEl.setAttribute("aria-expanded", "true");
  }

  function closeList() {
    listEl.hidden = true;
    activeIdx = -1;
    nameEl.setAttribute("aria-expanded", "false");
  }

  function highlight() {
    listEl.querySelectorAll("li[data-i]").forEach((li, i) => li.classList.toggle("active", i === activeIdx));
  }

  function pick(s) {
    if (!s) return;
    nameEl.value = s.name;
    carbsEl.value = s.carbs_g != null ? s.carbs_g : "";
    tagsEl.value = s.tags || "";
    idEl.value = s.known_id || "";
    updateBtn.disabled = !s.known_id;
    closeList();
  }

  function flash(msg) {
    if (!statusEl) return;
    statusEl.textContent = msg;
    setTimeout(() => { if (statusEl.textContent === msg) statusEl.textContent = ""; }, 2500);
  }

  function currentFields() {
    const fd = new FormData();
    fd.append("name", nameEl.value.trim());
    fd.append("carbs_g", carbsEl.value);
    fd.append("tags", tagsEl.value);
    return fd;
  }

  if (nameEl) {
    nameEl.addEventListener("focus", openList);
    nameEl.addEventListener("click", openList);
    nameEl.addEventListener("input", () => { syncSelection(); openList(); });
    nameEl.addEventListener("blur", () => setTimeout(closeList, 120));

    nameEl.addEventListener("keydown", (e) => {
      if (listEl.hidden && (e.key === "ArrowDown" || e.key === "ArrowUp")) { openList(); return; }
      const n = listEl.querySelectorAll("li[data-i]").length;
      if (e.key === "ArrowDown") { e.preventDefault(); activeIdx = Math.min(activeIdx + 1, n - 1); highlight(); }
      else if (e.key === "ArrowUp") { e.preventDefault(); activeIdx = Math.max(activeIdx - 1, 0); highlight(); }
      else if (e.key === "Enter") {
        if (!listEl.hidden && activeIdx >= 0) { e.preventDefault(); pick(filteredNow[activeIdx]); }
      } else if (e.key === "Escape") { closeList(); }
    });

    updateBtn.addEventListener("click", () => {
      const id = idEl.value;
      if (!id) return;
      fetch(`/api/known-meals/${id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: nameEl.value.trim(), carbs_g: carbsEl.value, tags: tagsEl.value }),
      }).then(() => loadSuggestions()).then(() => flash("Saved meal updated."));
    });

    saveNewBtn.addEventListener("click", () => {
      if (!nameEl.value.trim()) { flash("Enter a name first."); return; }
      fetch("/api/known-meals", { method: "POST", body: currentFields() })
        .then((r) => r.json())
        .then(() => loadSuggestions())
        .then(() => flash("Saved as a new meal."));
    });

    // Reset combo state after a successful log, and refresh suggestions so the
    // just-logged meal becomes available for next time.
    mealForm.addEventListener("reset", () => {
      idEl.value = "";
      updateBtn.disabled = true;
      if (statusEl) statusEl.textContent = "";
      closeList();
      loadSuggestions();
    });

    loadSuggestions();
  }

  window.addEventListener("load", draw);
})();
