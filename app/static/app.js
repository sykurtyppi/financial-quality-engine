// The workbench's only script (r36): no library, no network but this UI's own
// pages. Everything works without it; it saves a reload or two.
(function () {
  "use strict";

  // A run in flight: fetch its status fragment every 2 s and swap it in.
  // When the run is done the page reloads to show the new card; a failure,
  // or a run gone "stalled" (it ran past the server's limit), is swapped in
  // and polling stops. "idle" while a run was in flight means the server no
  // longer knows the run (it restarted): said, not left as a blank box.
  var POLL_MS = 2000;
  function watch(box) {
    var state = box.getAttribute("data-state");
    if (state !== "queued" && state !== "running") return;
    var url = box.getAttribute("data-status-url");
    setTimeout(function poll() {
      fetch(url, { credentials: "same-origin", headers: { "HX-Request": "true" } })
        .then(function (r) { if (!r.ok) throw new Error(r.status); return r.text(); })
        .then(function (html) {
          var holder = document.createElement("div");
          holder.innerHTML = html.trim();
          var next = holder.querySelector("[data-job-status]");
          if (!next) throw new Error("no status in the response");
          var now = next.getAttribute("data-state");
          if (now === "done") { window.location.reload(); return; }
          if (now === "idle") {
            var said = document.createElement("div");
            said.className = "err";
            said.textContent = "The server restarted while this run was in flight, so it no " +
              "longer knows how the run ended. Reload the page to see the latest run.";
            next.appendChild(said);
          }
          box.replaceWith(next);
          box = next;
          if (now === "queued" || now === "running") setTimeout(poll, POLL_MS);
        })
        .catch(function () { setTimeout(poll, POLL_MS * 3); });
    }, POLL_MS);
  }
  document.querySelectorAll("[data-job-status]").forEach(watch);

  // A link into the folded appendix (the price box's "#valuation") opens it
  // where the browser does not already.
  function reveal() {
    var id = decodeURIComponent(window.location.hash.slice(1));
    var el = id && document.getElementById(id);
    if (!el) return;
    var folded = el.closest("details");
    if (folded && !folded.open) folded.open = true;
    el.scrollIntoView();
  }
  window.addEventListener("hashchange", reveal);
  reveal();

  // The price box's time: now, in this browser's own zone, with its offset
  // (the observation refuses a time without one, and never assumes one).
  function pad(n) { return (n < 10 ? "0" : "") + n; }
  function localNow() {
    var d = new Date(), off = -d.getTimezoneOffset(), sign = off >= 0 ? "+" : "-";
    off = Math.abs(off);
    return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()) + "T" +
      pad(d.getHours()) + ":" + pad(d.getMinutes()) + sign + pad(Math.floor(off / 60)) + ":" + pad(off % 60);
  }
  document.querySelectorAll("input[data-local-now]").forEach(function (input) {
    if (!input.dataset.kept) input.value = localNow();
    var button = document.querySelector("[data-now-for='" + input.id + "']");
    if (button) button.addEventListener("click", function () { input.value = localNow(); });
  });

  // Slow actions (a build, an SEC fetch): relabel on click so a wait does
  // not read as "nothing happened" and invite a second click.
  document.addEventListener("click", function (e) {
    var a = e.target.closest(".js-gen");
    if (!a || a.dataset.loading) return;
    a.dataset.loading = "1";
    a.textContent = a.dataset.loadingText || "Generating…";
    a.classList.add("is-loading");
  });
  document.addEventListener("submit", function (e) {
    var b = e.target.querySelector("button.js-busy");
    if (!b) return;
    b.textContent = b.dataset.loadingText || "Working…";
    b.classList.add("is-loading");
  });
})();
