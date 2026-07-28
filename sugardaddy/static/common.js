// Shared helpers for both UIs.
window.SD = {
  // ms epoch -> local HH:MM
  hhmm(ms) {
    const d = new Date(ms);
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  },
  // ms epoch -> dd/mm/yy HH:MM, day-first to match the server's local_str().
  // Padded by hand rather than via toLocaleString so the format is the same
  // whatever locale the browser reports.
  tableStamp(ms) {
    const d = new Date(ms);
    const p = (n) => String(n).padStart(2, "0");
    return `${p(d.getDate())}/${p(d.getMonth() + 1)}/${p(d.getFullYear() % 100)} ` +
           `${p(d.getHours())}:${p(d.getMinutes())}`;
  },
  // ms epoch -> the shortest axis tick that still disambiguates, given the
  // window width in ms. A full stamp on ~10 ticks collides at any real width,
  // so drop the parts the window makes redundant: within a day the date is
  // implied, and past a week the time is noise.
  axisStamp(ms, spanMs) {
    const d = new Date(ms);
    const p = (n) => String(n).padStart(2, "0");
    const date = `${p(d.getDate())}/${p(d.getMonth() + 1)}`;
    const time = `${p(d.getHours())}:${p(d.getMinutes())}`;
    if (spanMs <= 36 * 3600e3) return time;
    if (spanMs <= 7 * 86400e3) return `${date} ${time}`;
    return date;
  },
  // Insulin dose marker colour, shaded + hue-shifted by kind so bolus /
  // correction / basal are distinguishable at a glance without a legend. A small
  // cool-colour arc (sky -> blue -> indigo), kept distinct from the orange meals.
  doseColor(kind) {
    return kind === "correction" ? "#38bdf8"  // sky, lighter
         : kind === "basal" ? "#7c6cf0"       // indigo, deeper
         : "#4f8cff";                         // bolus (and any unset kind)
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
