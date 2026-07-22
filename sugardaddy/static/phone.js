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

  window.addEventListener("load", draw);
})();
