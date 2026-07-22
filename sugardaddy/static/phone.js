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

  // --- known meals (input shortcuts) ---
  let known = [];
  const nameEl = document.getElementById("meal-name");
  const carbsEl = document.getElementById("meal-carbs");
  const tagsEl = document.getElementById("meal-tags");
  const idEl = document.getElementById("known-id");
  const updateBtn = document.getElementById("km-update");
  const saveNewBtn = document.getElementById("km-savenew");
  const listEl = document.getElementById("known-meals-list");
  const statusEl = document.getElementById("km-status");
  const mealForm = document.getElementById("meal-form");

  function loadKnown() {
    return fetch("/api/known-meals")
      .then((r) => r.json())
      .then((data) => {
        known = data;
        listEl.innerHTML = known
          .map((k) => `<option value="${attr(k.name)}">${carbLabel(k)}</option>`)
          .join("");
        syncSelection();
      })
      .catch(() => {});
  }

  function carbLabel(k) {
    return k.carbs_g != null ? `${k.carbs_g}g` : "";
  }
  function attr(s) { return (s || "").replace(/"/g, "&quot;"); }

  // Match the typed name (case-insensitive) to a known meal; prefill + toggle buttons.
  function matchByName(name) {
    const n = (name || "").trim().toLowerCase();
    return known.find((k) => k.name.trim().toLowerCase() === n) || null;
  }

  function syncSelection(prefill) {
    const m = matchByName(nameEl.value);
    if (m) {
      idEl.value = m.id;
      updateBtn.disabled = false;
      if (prefill) {
        carbsEl.value = m.carbs_g != null ? m.carbs_g : "";
        tagsEl.value = m.tags || "";
      }
    } else {
      idEl.value = "";
      updateBtn.disabled = true;
    }
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
    // 'input' fires both on typing and on picking a datalist option.
    nameEl.addEventListener("input", () => syncSelection(true));

    updateBtn.addEventListener("click", () => {
      const id = idEl.value;
      if (!id) return;
      fetch(`/api/known-meals/${id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: nameEl.value.trim(), carbs_g: carbsEl.value, tags: tagsEl.value }),
      }).then(() => loadKnown()).then(() => flash("Saved meal updated."));
    });

    saveNewBtn.addEventListener("click", () => {
      if (!nameEl.value.trim()) { flash("Enter a name first."); return; }
      fetch("/api/known-meals", { method: "POST", body: currentFields() })
        .then((r) => r.json())
        .then(() => loadKnown())
        .then(() => flash("Saved as a new meal."));
    });

    // Reset combo state after a successful log (form.reset clears fields).
    mealForm.addEventListener("reset", () => {
      idEl.value = "";
      updateBtn.disabled = true;
      if (statusEl) statusEl.textContent = "";
    });

    loadKnown();
  }

  window.addEventListener("load", draw);
})();
