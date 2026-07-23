// Desktop UI: range control, main chart with dose/meal markers, editable tables
// for insulin, composite meals (plates of foods), the food library, and saved
// meal templates. Live auto-refresh, paused while anything is mid-edit.
(function () {
  const KINDS = (document.getElementById("kinds-data").textContent || "bolus").split(",");
  let chart = null;
  let FOODS = []; // cached food library for item pickers/datalist

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
      const label = SD.stamp(scales.x.getValueForPixel(x));
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
      fetch("/api/foods").then((r) => r.json()),
      fetch("/api/meal-templates").then((r) => r.json()),
    ]).then(([timeline, entries, stats, foods, templates]) => {
      FOODS = foods;
      populateFoodsDatalist();
      renderChart(timeline);
      renderTables(entries);
      renderStats(stats);
      renderFoods(foods);
      renderTemplates(templates);
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
      label: m.label + (m.total_carbs != null ? ` (${m.total_carbs}g)` : ""),
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
      plugins: [SD.targetBand(data.target_low, data.target_high), crosshair],
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

  // ---- meals (composite: plate of items) ----
  function mealRow(m) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${m.local}</td><td>${esc(m.label)}</td><td>${m.total_carbs ?? ""}</td>
      <td>${m.total_calories ?? ""}</td><td>${esc(m.note)}</td>
      <td class="row-actions"><button class="icon-btn" data-act="edit">Edit</button>
      <button class="icon-btn danger" data-act="del">✕</button></td>`;
    tr.querySelector('[data-act="del"]').onclick = () => del("meal", m.id);
    tr.querySelector('[data-act="edit"]').onclick = () => openMealEditor(tr, m);
    return tr;
  }

  function openMealEditor(tr, m) {
    tr.innerHTML = `<td colspan="6"><div class="meal-editor">
      <div class="me-head">
        <label>Time <input type="datetime-local" class="me-ts" value="${m ? m.input : nowInput(0)}"></label>
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
    const tbody = root.querySelector(".me-items tbody");
    (m ? m.items : []).forEach((it) => tbody.appendChild(itemRow(it)));
    if (!m) tbody.appendChild(itemRow({}));
    root.querySelector(".me-additem").onclick = () => tbody.appendChild(itemRow({}));
    root.querySelector(".me-cancel").onclick = load;
    root.querySelector(".me-save").onclick = () => {
      const body = {
        ts: root.querySelector(".me-ts").value,
        name: root.querySelector(".me-name").value,
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
      <td><button type="button" class="icon-btn danger it-del">✕</button></td>`;
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
    tr.querySelector(".it-del").onclick = () => tr.remove();
    return tr;
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
      return;
    }
    if (type === "insulin") {
      const tbody = document.querySelector("#insulin-table tbody");
      const tr = document.createElement("tr");
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
    const preset = document.querySelector(".range-presets button.active");
    if (preset) {
      fromEl.value = nowInput(parseInt(preset.dataset.hours, 10));
      toEl.value = nowInput(0);
    }
    load();
  }

  // ---- init ----
  fromEl.value = nowInput(24);
  toEl.value = nowInput(0);
  document.querySelector('.range-presets button[data-hours="24"]').classList.add("active");
  window.addEventListener("load", load);
  setInterval(autoRefresh, REFRESH_MS);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) autoRefresh(); });
})();
