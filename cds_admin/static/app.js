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
      // Center the window on the screen the browser is on
      const width = Math.min(760, screen.availWidth), height = Math.min(880, screen.availHeight);
      const left = Math.round((screen.availLeft || 0) + (screen.availWidth - width) / 2);
      const top = Math.round((screen.availTop || 0) + (screen.availHeight - height) / 2);
      const win = window.open(url.toString(), "cds-target",
        "popup,width=" + width + ",height=" + height + ",left=" + left + ",top=" + top);
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

  // "Use the Drive name": look up the Drive folder/file name from the URL field and put it in the name field.
  const useName = document.getElementById("use-drive-name");
  if (useName) {
    const result = document.getElementById("drive-name-result");
    useName.addEventListener("click", async function () {
      const drive = document.getElementById("drive").value.trim();
      if (!drive) { result.textContent = "先に Drive の URL か ID を入れてください。"; return; }
      useName.disabled = true;
      result.textContent = "Drive に問い合わせています…";
      try {
        const query = new URLSearchParams({ drive: drive, kind: useName.dataset.kind });
        const res = await fetch("/api/drive-name?" + query.toString(), { credentials: "same-origin", cache: "no-store" });
        if (res.status === 401) { result.textContent = "ログインが切れました。ログインし直してください。"; return; }
        const data = await res.json();
        if (!res.ok) { result.textContent = data.error; return; }
        document.getElementById("name").value = data.name;
        result.textContent = data.name === data.drive_name ? "Drive の名前を入れました。"
          : "Drive の名前「" + data.drive_name + "」から、使えない文字などを直して入れました。";
      } catch (e) {
        result.textContent = "Drive の名前を取得できませんでした。";
      } finally {
        useName.disabled = false;
      }
    });
  }

  // "Keep originals" only applies when converting for AI
  const ai = document.getElementById("ai_markdown");
  const keepOriginals = document.getElementById("keep_originals");
  if (ai && keepOriginals) {
    ai.addEventListener("change", function () {
      keepOriginals.disabled = !ai.checked;
      if (!ai.checked) keepOriginals.checked = false;
    });
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
