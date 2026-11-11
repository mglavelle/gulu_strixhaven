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

  // 👍 / 👎 on your own answers - shown to the admin on the Usage page.
  function addFeedback(el, id, current) {
    const box = document.createElement("span");
    box.className = "feedback";
    box.innerHTML = `<span class="muted">Helpful?</span>
      <button type="button" class="fb" data-v="1" aria-label="Helpful" title="Helpful">👍</button>
      <button type="button" class="fb" data-v="-1" aria-label="Not helpful" title="Not helpful — wrong or unclear">👎</button>`;
    const paint = (v) => box.querySelectorAll(".fb").forEach((b) => {
      b.classList.toggle("on", +b.dataset.v === v); b.setAttribute("aria-pressed", +b.dataset.v === v);
    });
    paint(current);
    box.addEventListener("click", async (e) => {
      const b = e.target.closest(".fb");
      if (!b) return;
      const v = b.classList.contains("on") ? 0 : +b.dataset.v;   // click again to clear
      paint(v || null);
      try {
        await fetch(`/api/history/${id}/feedback`, {
          method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ value: v }) });
      } catch { /* best effort */ }
    });
    el.querySelector(".meta").appendChild(box);
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
          } else if (ev.type === "saved") {
            addFeedback(el, ev.id, null);
          } else if (ev.type === "error") {
            throw new Error(ev.message);
          }
          if (stick) scrollDown();
        }
      }
      history.push({ role: "user", content: question }, { role: "assistant", content: text });
      refreshRecent();                       // the server saved it; show it under Recent
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

  // ---------------------------------------------------------------- recent questions
  const recent = $("recent"), recentList = $("recent-list"), recentFilter = $("recent-filter");
  const lists = { mine: [], group: [] };
  let view = "mine";

  const when = (iso) => {
    const d = new Date(iso);
    if (isNaN(d)) return "";
    const days = Math.floor((Date.now() - d) / 86400000);
    return days < 1 ? d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })
      : days < 7 ? d.toLocaleDateString([], { weekday: "short" }) : d.toLocaleDateString();
  };

  function renderRecent() {
    const words = recentFilter.value.toLowerCase().split(/\s+/).filter(Boolean);
    const items = lists[view].filter((h) => words.every((w) => (h.question + " " + h.snippet).toLowerCase().includes(w)));
    recentList.innerHTML = items.length ? items.map((h) => `
      <li class="recent-item" data-id="${h.id}">
        <button type="button" class="recent-open" data-id="${h.id}">
          <span class="recent-q">${esc(h.question)}</span>
          <span class="recent-meta">${view === "group" ? esc(h.mine ? "You" : h.asker) + " · " : ""}${when(h.created_at)}</span>
        </button>
        ${h.can_delete ? `<button type="button" class="icon-btn recent-del" data-id="${h.id}" aria-label="Delete this saved answer"
             data-confirm="Delete this saved answer?" data-confirm-label="Delete?">✕</button>` : ""}
      </li>`).join("")
      : `<li class="muted small recent-empty">${recentFilter.value ? "No matches." :
          view === "mine" ? "Your questions will appear here." : "Nothing shared yet."}</li>`;
  }

  async function refreshRecent() {
    try {
      const [mine, group] = await Promise.all(["mine", "group"].map((v) =>
        fetch(`/api/history?view=${v}`).then((r) => (r.ok ? r.json() : []))));
      lists.mine = mine; lists.group = group;
      renderRecent();
    } catch { /* offline: keep what we have */ }
  }

  document.querySelectorAll(".recent-tab").forEach((t) => t.addEventListener("click", () => {
    view = t.dataset.view;
    document.querySelectorAll(".recent-tab").forEach((b) => {
      b.classList.toggle("on", b === t); b.setAttribute("aria-selected", b === t);
    });
    renderRecent();
  }));
  recentFilter.addEventListener("input", renderRecent);

  // Show a saved answer in the conversation, exactly as it was answered.
  async function openSaved(id) {
    const res = await fetch(`/api/history/${id}`);
    if (!res.ok) { refreshRecent(); return; }
    const h = await res.json();
    $("empty")?.remove();
    addUser(h.question);
    const el = addBot();
    el.classList.add("saved");
    el.querySelector(".status").hidden = true;
    el.querySelector(".answer").innerHTML = md(h.answer, true);
    renderSources(el, h.sources);
    const meta = el.querySelector(".meta");
    meta.innerHTML = `Saved answer · ${h.mine ? "you" : esc(h.asker)} asked ${esc(when(h.created_at))}
      <button type="button" class="linklike ask-again">Ask again for a fresh answer</button>`;
    meta.querySelector(".ask-again").addEventListener("click", () => { if (!controller) ask(h.question); });
    if (h.mine) addFeedback(el, h.id, h.feedback);
    history.push({ role: "user", content: h.question }, { role: "assistant", content: h.answer });
    scrollDown();
    if (window.innerWidth < 1100) setRecentOpen(false);
  }

  recentList.addEventListener("click", async (e) => {
    const del = e.target.closest(".recent-del");
    if (del) {
      if (e.defaultPrevented) return;          // first click: ui.js only armed it ("Delete?")
      await fetch(`/api/history/${del.dataset.id}`, { method: "DELETE" });
      refreshRecent();
      return;
    }
    const open = e.target.closest(".recent-open");
    if (open) openSaved(open.dataset.id);
  });

  // Panel open/closed: open by default on wide screens, remembered per browser.
  function setRecentOpen(open) {
    recent.classList.toggle("open", open);
    $("recent-toggle").setAttribute("aria-expanded", open);
    store.set("recentOpen", open ? "1" : "0");
  }
  const saved = store.get("recentOpen");
  setRecentOpen(saved ? saved === "1" : window.innerWidth >= 1100);
  $("recent-toggle").addEventListener("click", () => setRecentOpen(!recent.classList.contains("open")));
  $("recent-close").addEventListener("click", () => setRecentOpen(false));

  // ---------------------------------------------------------------- "asked before" suggestions
  const STOP = new Set("what when where which while with from that this does have your about there their they them then than into make many much some such will would could should can how who why the and for are was were been being does did not but you our out get got".split(" "));
  const keywords = (s) => new Set(s.toLowerCase().match(/[a-z0-9']+/g)?.filter((w) => w.length > 2 && !STOP.has(w)) || []);
  const suggestBox = $("suggest");
  let suggestTimer = null;

  function suggest() {
    const typed = keywords(input.value);
    if (typed.size < 2 || controller) { suggestBox.hidden = true; return; }
    const seen = new Set(), scored = [];
    for (const h of [...lists.mine, ...lists.group]) {
      const k = h.question.toLowerCase().trim();
      if (seen.has(k)) continue;
      seen.add(k);
      const qw = keywords(h.question);
      let hits = 0; typed.forEach((w) => { if (qw.has(w)) hits++; });
      const score = hits / Math.max(typed.size, qw.size);
      if (hits >= 2 && score >= 0.4) scored.push({ h, score });
    }
    scored.sort((a, b) => b.score - a.score);
    const top = scored.slice(0, 3);
    suggestBox.hidden = !top.length;
    suggestBox.innerHTML = top.length ? `<span class="muted small">Asked before:</span>` + top.map(({ h }) =>
      `<button type="button" class="suggest-item" data-id="${h.id}" title="Open the saved answer">${esc(h.question)}</button>`).join("") : "";
  }
  input.addEventListener("input", () => { clearTimeout(suggestTimer); suggestTimer = setTimeout(suggest, 250); });
  suggestBox.addEventListener("click", (e) => {
    const b = e.target.closest(".suggest-item");
    if (!b) return;
    input.value = ""; autosize(); suggestBox.hidden = true;
    openSaved(b.dataset.id);
  });
  form.addEventListener("submit", () => { suggestBox.hidden = true; });

  refreshRecent();
  input.focus();
})();
