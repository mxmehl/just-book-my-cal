// SPDX-FileCopyrightText: 2026 Max Mehl <https://mehl.mx>
// SPDX-License-Identifier: Apache-2.0

// Show times in the visitor's time zone. Without this script, the page shows the owner's time zone.
(function () {
  "use strict";

  const zone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  const timeFormat = new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit" });
  const dayFormat = new Intl.DateTimeFormat(undefined, { dateStyle: "full" });
  const fullFormat = new Intl.DateTimeFormat(undefined, { dateStyle: "full", timeStyle: "short" });

  // A single meeting time, e.g. on the manage page
  document.querySelectorAll("time[data-full]").forEach(function (el) {
    el.textContent = fullFormat.format(new Date(el.getAttribute("datetime")));
  });

  // Slot pickers: relabel every slot and regroup them by the visitor's days
  document.querySelectorAll("[data-slot-picker]").forEach(function (picker) {
    const container = picker.querySelector("[data-slot-days]");
    const days = new Map();
    picker.querySelectorAll("li").forEach(function (item) {
      const time = item.querySelector("time");
      const date = new Date(time.getAttribute("datetime"));
      time.textContent = timeFormat.format(date);

      const label = dayFormat.format(date);
      if (!days.has(label)) {
        const section = document.createElement("section");
        section.className = "day";
        const heading = document.createElement("h3");
        heading.textContent = label;
        const list = document.createElement("ul");
        list.className = "slots";
        section.append(heading, list);
        days.set(label, section);
      }
      days.get(label).querySelector("ul").append(item);
    });
    container.replaceChildren(...days.values());

    const note = picker.querySelector("[data-tz-note]");
    if (note) {
      note.textContent = "Times are shown in your time zone (" + zone + ").";
    }
  });
})();
