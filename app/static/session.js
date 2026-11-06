// Session page (DM): quick-add shortcuts and the AI prep / recap drafts.
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const { md, esc } = window.MD;

  // Ctrl/Cmd+Enter submits the log form.
  const logForm = $("log-form");
  logForm?.querySelector("textarea").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); logForm.requestSubmit(); }
  });
  if (location.hash === "#log") logForm?.querySelector("textarea").focus();

  // ---------------------------------------------------------------- AI panel
  let mode = "prep", controller = null;
  const run = $("ai-run"), out = $("ai-output"), status = $("ai-status"), save = $("ai-save");
  const LABELS = { prep: ["Draft session plan", "Save to DM notes", "dm_text"],
                   recap: ["Draft recap", "Save as player recap", "player_text"] };

  function setMode(m) {
    mode = m;
    document.querySelectorAll(".ai-tab").forEach((t) => {
      const on = t.dataset.mode === m;
      t.classList.toggle("on", on); t.setAttribute("aria-selected", on);
    });
    document.querySelectorAll("[data-for]").forEach((el) => { el.hidden = el.dataset.for !== m; });
    run.textContent = LABELS[m][0];
    $("ai-save-btn").textContent = LABELS[m][1];
    $("ai-target").value = LABELS[m][2];
    out.hidden = true; save.hidden = true; status.hidden = true; $("ai-sources").innerHTML = "";
  }
  document.querySelectorAll(".ai-tab").forEach((t) => t.addEventListener("click", () => setMode(t.dataset.mode)));
  if (location.hash === "#recap") setMode("recap");

  run.addEventListener("click", async () => {
    if (controller) { controller.abort(); return; }
    controller = new AbortController();
    let text = "", sources = [], timer = null;
    const t0 = Date.now();
    const tick = (label) => { status.innerHTML = `<span class="dots">${label}</span> ${Math.round((Date.now() - t0) / 1000)}s`; };
    out.hidden = false; out.innerHTML = ""; save.hidden = true; status.hidden = false; $("ai-sources").innerHTML = "";
    run.textContent = "Stop";
    timer = setInterval(() => { if (!text) tick(mode === "prep" ? "Reading the story so far" : "Writing the recap"); }, 1000);
    try {
      const careful = mode === "prep" && $("ai-careful").checked;
      const res = await fetch(`/api/sessions/${window.SESSION_ID}/${mode}${careful ? "?careful=true" : ""}`,
                              { method: "POST", signal: controller.signal });
      if (res.status === 401) { location.href = "/login"; return; }
      if (!res.ok) throw new Error(`Server error ${res.status}`);
      const reader = res.body.getReader(), dec = new TextDecoder();
      let buf = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let nl;
        while ((nl = buf.indexOf("\n")) >= 0) {
          const line = buf.slice(0, nl); buf = buf.slice(nl + 1);
          if (!line.trim()) continue;
          const ev = JSON.parse(line);
          if (ev.type === "sources") sources = ev.sources;
          else if (ev.type === "thinking") { tick("Thinking it through"); }
          else if (ev.type === "token") { text += ev.text; status.hidden = true; out.innerHTML = md(text, true); }
          else if (ev.type === "done") { status.hidden = false; status.textContent = `${ev.model} · ${ev.total_s}s — edit after saving if needed.`; }
          else if (ev.type === "error") throw new Error(ev.message);
        }
      }
      if (sources.length) {
        $("ai-sources").innerHTML = "<p class='small muted'>Adventure passages used:</p><ol class='small'>" +
          sources.map((s) => `<li>${esc(s.citation)} · ${esc(s.heading_path)}</li>`).join("") + "</ol>";
      }
      if (text.trim()) {
        // Keep [n] citations readable once saved: turn them into "(Strixhaven p.X)".
        $("ai-text").value = text.replace(/\[(\d+)\]/g, (m, n) => {
          const s = sources[+n - 1]; return s ? `(${s.citation})` : m;
        });
        save.hidden = false;
      }
    } catch (err) {
      status.hidden = false;
      status.innerHTML = err.name === "AbortError" ? "Stopped." : `<span class="msg-error">${esc(err.message)}</span>`;
    } finally {
      clearInterval(timer);
      controller = null;
      run.textContent = LABELS[mode][0];
    }
  });
})();
