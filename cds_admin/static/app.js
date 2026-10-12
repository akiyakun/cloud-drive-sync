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
