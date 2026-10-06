"use strict";
(() => {
  const label = document.querySelector("#source-label");
  const count = document.querySelector("#source-star-count");
  if (!label || !count) return;
  const sync = () => {
    const english = window.ExtorePreferences?.resolved.language === "en";
    label.textContent = english ? "Source" : "源码";
    document.querySelector("#source-link").setAttribute("aria-label", english ? "View Extore source on GitHub" : "查看 Extore 的 GitHub 源码");
  };
  sync();
  window.ExtorePreferences?.subscribe(sync);
  fetch("/api/source", { headers: { Accept: "application/json" } })
    .then((response) => response.ok ? response.json() : null)
    .then((data) => {
      if (Number.isSafeInteger(data?.stars) && data.stars >= 0)
        count.textContent = String(data.stars);
    })
    .catch(() => {});
})();
