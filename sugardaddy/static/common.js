// Shared helpers for both UIs.
window.SD = {
  // ms epoch -> local HH:MM
  hhmm(ms) {
    const d = new Date(ms);
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  },
  // ms epoch -> local short date+time
  stamp(ms) {
    const d = new Date(ms);
    return d.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  },
  // Insulin dose marker colour, faintly shaded by kind so bolus / correction /
  // basal are distinguishable at a glance without a legend.
  doseColor(kind) {
    return kind === "correction" ? "#6f9dff"
         : kind === "basal" ? "#3f74d6"
         : "#4f8cff"; // bolus (and any unset kind)
  },
  // Chart.js plugin: shade the in-range target band behind the series.
  targetBand(low, high) {
    return {
      id: "targetBand",
      beforeDraw(chart) {
        const { ctx, chartArea, scales } = chart;
        if (!chartArea || !scales.y) return;
        const yLow = scales.y.getPixelForValue(low);
        const yHigh = scales.y.getPixelForValue(high);
        ctx.save();
        ctx.fillStyle = "rgba(62,207,142,0.10)";
        ctx.fillRect(chartArea.left, yHigh, chartArea.right - chartArea.left, yLow - yHigh);
        ctx.restore();
      },
    };
  },
};
