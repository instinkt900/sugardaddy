// Desktop UI: range control, main chart with dose/meal markers, editable tables.
(function () {
  const KINDS = (document.getElementById("kinds-data").textContent || "bolus").split(",");
  let chart = null;

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

  document.querySelectorAll(".range-presets button").forEach((b) => {
    b.addEventListener("click", () => {
      document.querySelectorAll(".range-presets button").forEach((x) => x.classList.remove("active"));
      b.classList.add("active");
      fromEl.value = nowInput(parseInt(b.dataset.hours, 10));
      toEl.value = nowInput(0);
      load();
    });
  });
  fromEl.addEventListener("change", load);
  toEl.addEventListener("change", load);

  // ---- load everything ----
  function load() {
    const { from, to } = rangeEpochs();
    const qs = `?from=${from}&to=${to}`;
    Promise.all([
      fetch("/api/timeline" + qs).then((r) => r.json()),
      fetch("/api/entries" + qs).then((r) => r.json()),
      fetch("/api/stats" + qs).then((r) => r.json()),
      fetch("/api/known-meals").then((r) => r.json()),
    ]).then(([timeline, entries, stats, known]) => {
      renderChart(timeline);
      renderTables(entries);
      renderStats(stats);
      renderKnown(known);
    });
  }

  // ---- chart ----
  function renderChart(data) {
    const ctx = document.getElementById("main-chart");
    if (typeof Chart === "undefined") return;
    const g = data.glucose.map((p) => ({ x: p.t, y: p.v }));
    const ys = g.map((p) => p.y);
    const yMin = ys.length ? Math.min(...ys) : 0;
    const yMax = ys.length ? Math.max(...ys) : 10;
    const doseY = yMin;
    const mealY = yMin + (yMax - yMin) * 0.06;

    const doses = data.doses.map((d) => ({ x: d.t, y: doseY, label: `${d.units}u ${d.kind}` }));
    const meals = data.meals.map((m) => ({
      x: m.t, y: mealY,
      label: (m.description || "meal") + (m.carbs_g != null ? ` (${m.carbs_g}g)` : ""),
    }));

    if (chart) chart.destroy();
    chart = new Chart(ctx, {
      data: {
        datasets: [
          { type: "line", label: "Glucose", data: g, borderColor: "#4f8cff",
            borderWidth: 2, pointRadius: 0, tension: 0.3, parsing: false },
          { type: "scatter", label: "Insulin", data: doses, borderColor: "#4f8cff",
            backgroundColor: "#4f8cff", pointStyle: "triangle", radius: 7, parsing: false },
          { type: "scatter", label: "Meal", data: meals, borderColor: "#ffb020",
            backgroundColor: "#ffb020", pointStyle: "rectRot", radius: 7, parsing: false },
        ],
      },
      options: {
        animation: false,
        interaction: { mode: "nearest", intersect: true },
        plugins: {
          legend: { labels: { color: "#e8eaf0" } },
          tooltip: { callbacks: { label: (c) => c.raw.label || `${c.parsed.y} ${data.units}` } },
        },
        scales: {
          x: { type: "linear", ticks: { color: "#8b90a0", maxTicksLimit: 10, callback: (v) => SD.stamp(v) },
               grid: { color: "#2c303c" } },
          y: { ticks: { color: "#8b90a0" }, grid: { color: "#2c303c" },
               title: { display: true, text: data.units, color: "#8b90a0" } },
        },
      },
      plugins: [SD.targetBand(data.target_low, data.target_high)],
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
    (s.post_meal || []).forEach((p) => {
      const tr = document.createElement("tr");
      tr.innerHTML = `<td>${SD.stamp(p.ts_utc * 1000)}</td><td>${esc(p.description) || "(meal)"}</td>
        <td>${p.carbs_g ?? ""}</td><td>${p.start_display}</td><td>${p.peak_display}</td>
        <td>${p.peak_delta_display}</td><td>${p.minutes_to_peak}m</td><td>${p.end_display}</td>`;
      tb.appendChild(tr);
    });
  }

  // ---- tables (inline CRUD) ----
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

  function editDose(tr, d) {
    tr.innerHTML = `
      <td><input type="datetime-local" value="${d.input}"></td>
      <td><input type="number" step="0.5" min="0" value="${d.units}" style="width:70px"></td>
      <td><select>${KINDS.map((k) => `<option ${k === d.kind ? "selected" : ""}>${k}</option>`).join("")}</select></td>
      <td><input type="text" value="${attr(d.note)}"></td>
      <td class="row-actions"><button class="icon-btn save">Save</button>
      <button class="icon-btn" data-act="cancel">Cancel</button></td>`;
    const [ts, units, kind, note] = tr.querySelectorAll("input,select");
    tr.querySelector(".save").onclick = () =>
      patch("insulin", d.id, { ts: ts.value, units: parseFloat(units.value), kind: kind.value, note: note.value });
    tr.querySelector('[data-act="cancel"]').onclick = load;
  }

  function mealRow(m) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${m.local}</td><td>${esc(m.description)}</td><td>${m.carbs_g ?? ""}</td>
      <td>${esc(m.tags)}</td><td>${esc(m.note)}</td>
      <td class="row-actions"><button class="icon-btn" data-act="edit">Edit</button>
      <button class="icon-btn danger" data-act="del">✕</button></td>`;
    tr.querySelector('[data-act="del"]').onclick = () => del("meal", m.id);
    tr.querySelector('[data-act="edit"]').onclick = () => editMeal(tr, m);
    return tr;
  }

  function editMeal(tr, m) {
    tr.innerHTML = `
      <td><input type="datetime-local" value="${m.input}"></td>
      <td><input type="text" value="${attr(m.description)}"></td>
      <td><input type="number" step="1" min="0" value="${m.carbs_g ?? ""}" style="width:70px"></td>
      <td><input type="text" value="${attr(m.tags)}"></td>
      <td><input type="text" value="${attr(m.note)}"></td>
      <td class="row-actions"><button class="icon-btn save">Save</button>
      <button class="icon-btn" data-act="cancel">Cancel</button></td>`;
    const [ts, desc, carbs, tags, note] = tr.querySelectorAll("input");
    tr.querySelector(".save").onclick = () =>
      patch("meal", m.id, {
        ts: ts.value, description: desc.value,
        carbs_g: carbs.value, tags: tags.value, note: note.value,
      });
    tr.querySelector('[data-act="cancel"]').onclick = load;
  }

  // ---- known meals (input shortcuts) ----
  function renderKnown(list) {
    const tb = document.querySelector("#known-table tbody");
    tb.innerHTML = "";
    list.forEach((k) => tb.appendChild(knownRow(k)));
  }

  function knownRow(k) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${esc(k.name)}</td><td>${k.carbs_g ?? ""}</td><td>${esc(k.tags)}</td>
      <td class="row-actions"><button class="icon-btn" data-act="edit">Edit</button>
      <button class="icon-btn danger" data-act="del">✕</button></td>`;
    tr.querySelector('[data-act="del"]').onclick = () => del("known-meals", k.id);
    tr.querySelector('[data-act="edit"]').onclick = () => editKnown(tr, k);
    return tr;
  }

  function editKnown(tr, k) {
    tr.innerHTML = `
      <td><input type="text" value="${attr(k.name)}"></td>
      <td><input type="number" step="1" min="0" value="${k.carbs_g ?? ""}" style="width:70px"></td>
      <td><input type="text" value="${attr(k.tags)}"></td>
      <td class="row-actions"><button class="icon-btn save">Save</button>
      <button class="icon-btn" data-act="cancel">Cancel</button></td>`;
    const [name, carbs, tags] = tr.querySelectorAll("input");
    tr.querySelector(".save").onclick = () =>
      patch("known-meals", k.id, { name: name.value, carbs_g: carbs.value, tags: tags.value });
    tr.querySelector('[data-act="cancel"]').onclick = load;
  }

  // ---- add ----
  document.querySelectorAll(".add-btn").forEach((btn) => {
    btn.addEventListener("click", () => addRow(btn.dataset.add));
  });

  function addRow(type) {
    const tbody = document.querySelector(`#${type}-table tbody`);
    const tr = document.createElement("tr");
    if (type === "insulin") {
      tr.innerHTML = `
        <td><input type="datetime-local" value="${nowInput(0)}"></td>
        <td><input type="number" step="0.5" min="0" placeholder="units" style="width:70px"></td>
        <td><select>${KINDS.map((k) => `<option>${k}</option>`).join("")}</select></td>
        <td><input type="text" placeholder="note"></td>
        <td class="row-actions"><button class="icon-btn save">Save</button>
        <button class="icon-btn" data-act="cancel">Cancel</button></td>`;
      const [ts, units, kind, note] = tr.querySelectorAll("input,select");
      tr.querySelector(".save").onclick = () =>
        create("insulin", { ts: ts.value, units: units.value, kind: kind.value, note: note.value });
    } else if (type === "meal") {
      tr.innerHTML = `
        <td><input type="datetime-local" value="${nowInput(0)}"></td>
        <td><input type="text" placeholder="description"></td>
        <td><input type="number" step="1" min="0" placeholder="carbs" style="width:70px"></td>
        <td><input type="text" placeholder="tags"></td>
        <td><input type="text" placeholder="note"></td>
        <td class="row-actions"><button class="icon-btn save">Save</button>
        <button class="icon-btn" data-act="cancel">Cancel</button></td>`;
      const [ts, desc, carbs, tags, note] = tr.querySelectorAll("input");
      tr.querySelector(".save").onclick = () =>
        create("meal", { ts: ts.value, description: desc.value, carbs_g: carbs.value, tags: tags.value, note: note.value });
    } else if (type === "known") {
      tr.innerHTML = `
        <td><input type="text" placeholder="name"></td>
        <td><input type="number" step="1" min="0" placeholder="carbs" style="width:70px"></td>
        <td><input type="text" placeholder="tags"></td>
        <td class="row-actions"><button class="icon-btn save">Save</button>
        <button class="icon-btn" data-act="cancel">Cancel</button></td>`;
      const [name, carbs, tags] = tr.querySelectorAll("input");
      tr.querySelector(".save").onclick = () =>
        create("known-meals", { name: name.value, carbs_g: carbs.value, tags: tags.value });
    }
    tr.querySelector('[data-act="cancel"]').onclick = load;
    tbody.prepend(tr);
  }

  // ---- API calls ----
  function create(type, fields) {
    const fd = new FormData();
    Object.entries(fields).forEach(([k, v]) => fd.append(k, v ?? ""));
    fetch(`/api/${type}`, { method: "POST", body: fd }).then(load);
  }
  function patch(type, id, body) {
    fetch(`/api/${type}/${id}`, {
      method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    }).then(load);
  }
  function del(type, id) {
    if (!confirm("Delete this entry?")) return;
    fetch(`/api/${type}/${id}`, { method: "DELETE" }).then(load);
  }

  // ---- utils ----
  function esc(s) { return (s ?? "").replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c])); }
  function attr(s) { return (s ?? "").replace(/"/g, "&quot;"); }

  // ---- init ----
  fromEl.value = nowInput(24);
  toEl.value = nowInput(0);
  document.querySelector('.range-presets button[data-hours="24"]').classList.add("active");
  window.addEventListener("load", load);
})();
