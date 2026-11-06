// Ask page: streams NDJSON from /api/ask, renders markdown with clickable citations.
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const thread = $("thread"), form = $("ask-form"), input = $("question"), sendBtn = $("send");
  const scopeSel = $("scope"), careful = $("careful");
  const panel = $("passage");

  let history = [];          // [{role, content}] sent for follow-up questions
  let controller = null;     // AbortController while an answer is streaming

  // ---------------------------------------------------------------- preferences
  const store = {
    get(k) { try { return localStorage.getItem(k); } catch { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode */ } },
  };
  if (scopeSel && store.get("scope")) scopeSel.value = store.get("scope");
  if (careful) careful.checked = store.get("careful") === "1";
  scopeSel?.addEventListener("change", () => store.set("scope", scopeSel.value));
  careful?.addEventListener("change", () => store.set("careful", careful.checked ? "1" : "0"));

  const { md, esc } = window.MD;

  // ---------------------------------------------------------------- passage panel
  function openPassage(src, btn) {
    $("passage-cite").textContent = `[${src.n}] ${src.citation}`;
    $("passage-path").textContent = src.heading_path;
    $("passage-body").innerHTML = md(src.text) +
      (src.link ? `<p><a class="btn small" href="${esc(src.link)}">Open ${src.link.startsWith("/party") ? "character" : "note"} →</a></p>` : "");
    panel.hidden = false;
    panel.dataset.chunk = src.id;
    document.querySelectorAll(".source-item.active").forEach((e) => e.classList.remove("active"));
    btn?.closest(".msg")?.querySelector(`.source-item[data-n="${src.n}"]`)?.classList.add("active");
    $("passage-body").scrollTop = 0;
  }
  $("passage-close").addEventListener("click", () => { panel.hidden = true; });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") panel.hidden = true; });

  thread.addEventListener("click", (e) => {
    const btn = e.target.closest(".cite, .source-item");
    if (!btn) return;
    const msg = btn.closest(".msg");
    const src = msg?._sources?.find((s) => String(s.n) === btn.dataset.n);
    if (src) openPassage(src, btn);
  });

  // ---------------------------------------------------------------- rendering
  function addUser(text) {
    const el = document.createElement("div");
    el.className = "msg user";
    el.innerHTML = `<div class="bubble"></div>`;
    el.querySelector(".bubble").textContent = text;
    thread.appendChild(el);
  }

  function addBot() {
    const el = document.createElement("div");
    el.className = "msg bot";
    el.innerHTML = `<div class="bubble"><div class="status"><span class="dots">Searching the books</span></div>
      <div class="md answer"></div><div class="sources-slot"></div><div class="meta"></div></div>`;
    thread.appendChild(el);
    return el;
  }

  function renderSources(el, sources) {
    el._sources = sources;
    if (!sources.length) return;
    const items = sources.map((s) => `
      <li><button type="button" class="source-item" data-n="${s.n}">
        <span class="num">${s.n}</span>
        <span><strong>${esc(s.citation)}</strong>${s.dm_only && APP.role === "dm" ? '<span class="tag-dm">DM</span>' : ""}
        <span class="where"> · ${esc(s.heading_path)}</span></span>
      </button></li>`).join("");
    el.querySelector(".sources-slot").innerHTML =
      `<details class="sources"><summary>Sources (${sources.length})</summary><ul class="source-list">${items}</ul></details>`;
  }

  const atBottom = () => thread.scrollHeight - thread.scrollTop - thread.clientHeight < 80;
  const scrollDown = () => { thread.scrollTop = thread.scrollHeight; };

  // ---------------------------------------------------------------- ask
  async function ask(question) {
    $("empty")?.remove();
    addUser(question);
    const el = addBot();
    scrollDown();
    const status = el.querySelector(".status"), out = el.querySelector(".answer"), meta = el.querySelector(".meta");
    let text = "", thinkTimer = null, lastEvent = Date.now(), thinking = false;
    // If the model goes quiet mid-answer, say so instead of looking frozen.
    const stallNote = document.createElement("div");
    stallNote.className = "status stall";
    stallNote.hidden = true;
    out.after(stallNote);
    const stallTimer = setInterval(() => {
      const quiet = Math.round((Date.now() - lastEvent) / 1000);
      stallNote.hidden = !(text && !thinking && quiet >= 8);
      if (!stallNote.hidden) {
        stallNote.innerHTML = `<span class="dots">Waiting for the model</span> <span>${quiet}s — the graphics card may be busy</span>`;
      }
    }, 1000);

    controller = new AbortController();
    setBusy(true);
    try {
      const res = await fetch("/api/ask", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          question, history: history.slice(-6),
          scope: scopeSel ? scopeSel.value : "all", careful: !!careful?.checked,
        }),
        signal: controller.signal,
      });
      if (res.status === 401) { location.href = "/login"; return; }
      if (!res.ok) throw new Error(`Server error ${res.status}`);

      const reader = res.body.getReader();
      const dec = new TextDecoder();
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
          const stick = atBottom();
          lastEvent = Date.now();
          thinking = ev.type === "thinking" || (thinking && ev.type !== "token");
          stallNote.hidden = true;
          if (ev.type === "sources") {
            renderSources(el, ev.sources);
            status.innerHTML = ev.sources.length
              ? `<span class="dots">Reading ${ev.sources.length} passages</span>`
              : `<span class="dots">Nothing found, answering anyway</span>`;
          } else if (ev.type === "thinking") {
            const t0 = Date.now();
            const tick = () => { status.innerHTML = `<span class="dots">Thinking carefully</span> <span>${Math.round((Date.now() - t0) / 1000)}s</span>`; };
            tick(); thinkTimer = setInterval(tick, 1000);
          } else if (ev.type === "token") {
            if (thinkTimer) { clearInterval(thinkTimer); thinkTimer = null; }
            status.hidden = true;
            text += ev.text;
            out.innerHTML = md(text, true);
          } else if (ev.type === "done") {
            meta.textContent = `${ev.model} · ${ev.total_s}s`;
          } else if (ev.type === "error") {
            throw new Error(ev.message);
          }
          if (stick) scrollDown();
        }
      }
      history.push({ role: "user", content: question }, { role: "assistant", content: text });
    } catch (err) {
      status.hidden = false;
      status.innerHTML = err.name === "AbortError"
        ? `<span class="muted">Stopped.</span>`
        : `<span class="msg-error">Something went wrong: ${esc(err.message)}</span>`;
      if (text) history.push({ role: "user", content: question }, { role: "assistant", content: text });
    } finally {
      if (thinkTimer) clearInterval(thinkTimer);
      clearInterval(stallTimer);
      stallNote.remove();
      controller = null;
      setBusy(false);
    }
  }

  function setBusy(busy) {
    sendBtn.textContent = busy ? "Stop" : "Ask";
    sendBtn.classList.toggle("primary", !busy);
    input.disabled = busy;
    if (!busy) input.focus();
  }

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    if (controller) { controller.abort(); return; }
    const q = input.value.trim();
    if (!q) return;
    input.value = ""; autosize();
    ask(q);
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); form.requestSubmit(); }
  });
  const autosize = () => { input.style.height = "auto"; input.style.height = Math.min(input.scrollHeight, window.innerHeight * 0.4) + "px"; };
  input.addEventListener("input", autosize);

  document.querySelectorAll(".example").forEach((b) => b.addEventListener("click", () => {
    if (b.dataset.scope && scopeSel) scopeSel.value = b.dataset.scope;
    ask(b.textContent.trim());
  }));

  $("new-chat").addEventListener("click", () => {
    if (controller) controller.abort();
    history = [];
    location.reload();
  });

  input.focus();
})();
