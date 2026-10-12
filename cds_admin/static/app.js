// While a sync is running, poll the status and reload the page when it finishes.
(function () {
  const status = document.getElementById("status");
  if (!status) return;
  let running = status.dataset.running === "true";
  async function poll() {
    try {
      const res = await fetch("/api/status", { credentials: "same-origin", cache: "no-store" });
      if (res.status === 401) { location.href = "/login"; return; }
      const data = await res.json();
      if (running && !data.running) { location.reload(); return; }
      if (!running && data.running) { location.reload(); return; }
    } catch (e) { /* retry on the next tick */ }
    setTimeout(poll, running ? 3000 : 15000);
  }
  setTimeout(poll, running ? 3000 : 15000);
})();

// Target add/edit/delete pages open in a separate window so the list keeps its scroll position.
// Without JavaScript (or if the popup is blocked) the link opens normally in the same tab.
(function () {
  document.querySelectorAll("a[data-popup]").forEach(function (link) {
    link.addEventListener("click", function (event) {
      const url = new URL(link.href, location.href);
      url.searchParams.set("popup", "1");
      const win = window.open(url.toString(), "cds-target", "popup,width=760,height=880");
      if (win) { event.preventDefault(); win.focus(); }
    });
  });

  document.querySelectorAll("[data-close]").forEach(function (button) {
    button.addEventListener("click", function () {
      window.close();
    });
  });

  // Result page in the separate window: refresh the list in the original window, then close.
  const done = document.getElementById("done");
  if (done) {
    try {
      if (window.opener && !window.opener.closed) window.opener.location.reload();
    } catch (e) { /* the original window was navigated elsewhere */ }
    if (done.dataset.autoClose === "true") setTimeout(function () { window.close(); }, 1500);
  }

  // New target: put the group name at the head of the output path while the user has not edited it.
  const group = document.getElementById("group");
  const output = document.querySelector("input[data-group-prefix]");
  if (group && output) {
    let auto = output.value === "" || output.value === group.value.trim() + "/";
    output.addEventListener("input", function () { auto = false; });
    group.addEventListener("input", function () {
      if (!auto) return;
      const name = group.value.trim();
      output.value = name ? name + "/" : "";
    });
  }
})();
