// In-page "click again to confirm" for destructive buttons.
// Native confirm() popups are silently blocked in some embedded browsers (and count as
// "Cancel"), so any <button data-confirm="message"> needs a second click within 5 s.
(() => {
  "use strict";
  const ARM_MS = 5000;

  function disarm(btn) {
    clearTimeout(btn._confirmTimer);
    btn.classList.remove("armed");
    if (btn._confirmText != null) btn.textContent = btn._confirmText;
    btn._confirmHint?.remove();
    btn._confirmHint = null;
    delete btn.dataset.armed;
  }

  document.addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-confirm]");
    if (!btn) return;
    if (btn.dataset.armed) { btn._confirmText != null && (btn.textContent = btn._confirmText); return; } // 2nd click: go
    e.preventDefault();
    document.querySelectorAll("button[data-armed]").forEach(disarm);
    btn.dataset.armed = "1";
    btn._confirmText = btn.textContent;
    btn.textContent = btn.dataset.confirmLabel || "Click again to confirm";
    btn.classList.add("armed");
    // Floating hint under the button: inserting it inline would shift the button away
    // from the cursor, and the second click would miss.
    const hint = document.createElement("div");
    hint.className = "confirm-hint";
    hint.setAttribute("role", "alert");
    hint.textContent = btn.dataset.confirm;
    document.body.appendChild(hint);
    const r = btn.getBoundingClientRect();
    const left = Math.min(Math.max(8, r.right - hint.offsetWidth), window.innerWidth - hint.offsetWidth - 8);
    hint.style.left = `${left + window.scrollX}px`;
    hint.style.top = `${r.bottom + window.scrollY + 6}px`;
    btn._confirmHint = hint;
    btn._confirmTimer = setTimeout(() => disarm(btn), ARM_MS);
  }, true);

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") document.querySelectorAll("button[data-armed]").forEach(disarm);
  });
})();
