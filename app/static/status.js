// Campaign status page: stream the AI catch-up and copy its sections into the status form.
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const { md, esc } = window.MD;
  const run = $("ai-run"), out = $("ai-output"), status = $("ai-status");
  let controller = null, lastText = "", lastSources = [];

  // "## Where we are now" ... up to the next "## " heading.
  function section(text, title) {
    const m = text.match(new RegExp(`^##\\s*${title}[^\\n]*\\n([\\s\\S]*?)(?=^##\\s|$(?![\\s\\S]))`, "mi"));
    return m ? m[1].trim() : "";
  }
  const readable = (t) => t.replace(/\[(\d+)\]/g, (m, n) => {
    const s = lastSources[+n - 1]; return s ? `(${s.citation})` : m;
  });

  $("ai-fill").addEventListener("click", () => {
    // "." matches straight or curly apostrophes.
    const where = section(lastText, "Where we are"), next = section(lastText, "What.s coming next")
      || section(lastText, "What.s next");
    if (where) $("where_text").value = readable(where);
    if (next) $("next_text").value = readable(next);
    $("status-form").scrollIntoView({ behavior: "smooth", block: "start" });
    $("where_text").focus();
    status.hidden = false;
    status.textContent = "Copied into the form — review and edit, then Save status.";
  });

  run.addEventListener("click", async () => {
    if (controller) { controller.abort(); return; }
    controller = new AbortController();
    let text = "";
    const t0 = Date.now();
    const timer = setInterval(() => {
      if (!text) status.innerHTML = `<span class="dots">Reading the campaign</span> ${Math.round((Date.now() - t0) / 1000)}s`;
    }, 1000);
    out.hidden = false; out.innerHTML = ""; status.hidden = false; $("ai-use").hidden = true; $("ai-sources").innerHTML = "";
    run.textContent = "Stop";
    try {
      const res = await fetch(`/api/status/catchup${$("ai-careful").checked ? "?careful=true" : ""}`,
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
          if (ev.type === "sources") lastSources = ev.sources;
          else if (ev.type === "thinking") status.innerHTML = `<span class="dots">Thinking it through</span>`;
          else if (ev.type === "token") { text += ev.text; status.hidden = true; out.innerHTML = md(text, true); }
          else if (ev.type === "done") { status.hidden = false; status.textContent = `${ev.model} · ${ev.total_s}s`; }
          else if (ev.type === "error") throw new Error(ev.message);
        }
      }
      lastText = text;
      if (lastSources.length) {
        $("ai-sources").innerHTML = "<p class='small muted'>Adventure passages used:</p><ol class='small'>" +
          lastSources.map((s) => `<li>${esc(s.citation)} · ${esc(s.heading_path)}</li>`).join("") + "</ol>";
      }
      $("ai-use").hidden = !(section(text, "Where we are") || section(text, "What.s coming next"));
    } catch (err) {
      status.hidden = false;
      status.innerHTML = err.name === "AbortError" ? "Stopped." : `<span class="msg-error">${esc(err.message)}</span>`;
    } finally {
      clearInterval(timer);
      controller = null;
      run.textContent = "Catch me up";
    }
  });
})();
