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
  // --- 24-hour time fields -------------------------------------------------
  // <input type="datetime-local"> draws its clock from the browser/OS locale and
  // nothing in the markup overrides it — Chrome ignores the lang attribute for
  // these controls. In a 12-hour locale that means an AM/PM segment where
  // "00 PM" is midday, which silently produced noon-to-noon chart windows and
  // could just as easily put an insulin dose 12 hours out. So the widget is
  // replaced by a native date picker plus explicit 24-hour hour/minute boxes.
  //
  // The original input stays as the canonical field — same id, name and class,
  // still holding "YYYY-MM-DDTHH:MM", only type=hidden — so form posts and every
  // existing reader of .value keep working untouched. Its `value` property is
  // shadowed so programmatic writes (nowInput(), the "Now" buttons) refresh the
  // visible boxes, exactly where the native control used to redraw itself.
  timeFields(root) {
    (root || document)
      .querySelectorAll('input[type="datetime-local"]')
      .forEach((el) => SD.timeField(el));
  },

  timeField(input) {
    // Reach the real value accessor before shadowing it, so the element's own
    // value (the one the browser submits) stays authoritative.
    const proto = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value");
    const rawGet = () => proto.get.call(input);
    const rawSet = (v) => proto.set.call(input, v);

    const pad = (n) => String(n).padStart(2, "0");
    const clamp = (n, hi) => Math.max(0, Math.min(hi, n));

    const wrap = document.createElement("span");
    wrap.className = "tf";
    wrap.setAttribute("role", "group");
    if (input.getAttribute("aria-label")) wrap.setAttribute("aria-label", input.getAttribute("aria-label"));
    wrap.innerHTML =
      '<input type="date" class="tf-date" aria-label="Date">' +
      '<input type="text" class="tf-h" inputmode="numeric" maxlength="2" placeholder="hh" aria-label="Hour, 0 to 23">' +
      '<span class="tf-sep">:</span>' +
      '<input type="text" class="tf-m" inputmode="numeric" maxlength="2" placeholder="mm" aria-label="Minute">';
    const dateEl = wrap.querySelector(".tf-date");
    const boxes = [wrap.querySelector(".tf-h"), wrap.querySelector(".tf-m")];
    const limit = (el) => (el === boxes[0] ? 23 : 59);

    // canonical -> boxes
    function pull() {
      const m = /^(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2})/.exec(rawGet() || "");
      dateEl.value = m ? m[1] : "";
      boxes[0].value = m ? m[2] : "";
      boxes[1].value = m ? m[3] : "";
    }

    // boxes -> canonical. Fires `change` on the canonical field so listeners see
    // an edit the same way they did with the native input.
    function push() {
      if (!dateEl.value) { rawSet(""); return; }
      const h = pad(clamp(parseInt(boxes[0].value, 10) || 0, 23));
      const mi = pad(clamp(parseInt(boxes[1].value, 10) || 0, 59));
      rawSet(`${dateEl.value}T${h}:${mi}`);
      input.dispatchEvent(new Event("change", { bubbles: true }));
    }

    function tidy(el) {
      el.value = el.value.replace(/\D/g, "").slice(0, 2);
      if (el.value !== "") el.value = pad(clamp(parseInt(el.value, 10), limit(el)));
    }

    dateEl.addEventListener("change", push);
    boxes.forEach((el) => {
      // Digits only while typing; pad and clamp once the box is left, so a
      // half-typed "1" isn't rewritten to "01" under the cursor.
      el.addEventListener("input", () => { el.value = el.value.replace(/\D/g, "").slice(0, 2); });
      el.addEventListener("change", () => { tidy(el); push(); });
      el.addEventListener("blur", () => { tidy(el); push(); });
      el.addEventListener("keydown", (e) => {
        const step = e.key === "ArrowUp" ? 1 : e.key === "ArrowDown" ? -1 : 0;
        if (!step) return;
        e.preventDefault(); // wrap around instead of scrolling the page
        const hi = limit(el);
        el.value = pad(((parseInt(el.value, 10) || 0) + step + hi + 1) % (hi + 1));
        push();
      });
    });

    Object.defineProperty(input, "value", {
      configurable: true,
      get: rawGet,
      // Deliberately does not fire `change` — neither did assigning to a native
      // input's value — but it must refresh the boxes.
      set(v) { rawSet(v); pull(); },
    });
    // form.reset() restores the canonical's attribute value without any event.
    const form = input.closest("form");
    if (form) form.addEventListener("reset", () => setTimeout(pull, 0));

    input.type = "hidden";
    input.parentNode.insertBefore(wrap, input);
    pull();
    return wrap;
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
