// Small, safe markdown renderer shared by every page (escapes HTML first).
// MD.md(text, withCites) -> HTML. Elements with [data-md] are rendered on load.
window.MD = (() => {
  "use strict";
  const esc = (s) => s.replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  function inline(s, withCites) {
    s = esc(s)
      .replace(/\*\*\*(.+?)\*\*\*/g, "<strong><em>$1</em></strong>")
      .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|[\s(>])\*(?!\s)(.+?)\*(?=[\s).,;:!?<]|$)/g, "$1<em>$2</em>")
      .replace(/(^|[\s(>])_(?!\s)(.+?)_(?=[\s).,;:!?<]|$)/g, "$1<em>$2</em>")
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/&lt;br&gt;/g, "<br>");
    if (withCites) {
      s = s.replace(/\[(\d+(?:\s*,\s*\d+)*)\]/g, (_, nums) =>
        nums.split(/\s*,\s*/).map((n) =>
          `<button type="button" class="cite" data-n="${n}" aria-label="Show source ${n}">${n}</button>`).join(""));
    }
    return s;
  }

  function md(text, withCites = false) {
    const lines = text.replace(/\r/g, "").split("\n");
    let html = "", list = null, para = [], table = [];
    const flushPara = () => { if (para.length) { html += `<p>${inline(para.join(" "), withCites)}</p>`; para = []; } };
    const flushList = () => { if (list) { html += `</${list}>`; list = null; } };
    const flushTable = () => {
      if (!table.length) return;
      const cellsOf = (r) => r.trim().replace(/^\||\|$/g, "").split("|");
      const isSeparator = (r) => cellsOf(r).every((c) => /^\s*:?-+:?\s*$/.test(c));
      const rows = table.filter((r) => !isSeparator(r));
      html += "<table>" + rows.map((r, i) => {
        const cells = cellsOf(r);
        const tag = i === 0 ? "th" : "td";
        return "<tr>" + cells.map((c) => `<${tag}>${inline(c.trim(), withCites)}</${tag}>`).join("") + "</tr>";
      }).join("") + "</table>";
      table = [];
    };
    for (const raw of lines) {
      const line = raw.trimEnd();
      if (/^\s*\|.*\|\s*$/.test(line)) { flushPara(); flushList(); table.push(line); continue; }
      flushTable();
      let m;
      if (!line.trim()) { flushPara(); flushList(); continue; }
      if ((m = line.match(/^(#{1,6})\s+(.*)$/))) {
        flushPara(); flushList();
        const lvl = Math.min(Math.max(m[1].length, 2), 5);
        html += `<h${lvl}>${inline(m[2], withCites)}</h${lvl}>`;
        continue;
      }
      if ((m = line.match(/^\s*[-*•]\s+(.*)$/)) || (m = line.match(/^\s*\d+[.)]\s+(.*)$/))) {
        flushPara();
        const kind = /^\s*\d/.test(line) ? "ol" : "ul";
        if (list !== kind) { flushList(); html += `<${kind}>`; list = kind; }
        html += `<li>${inline(m[1], withCites)}</li>`;
        continue;
      }
      if ((m = line.match(/^>\s?(.*)$/))) { flushPara(); flushList(); html += `<blockquote>${inline(m[1], withCites)}</blockquote>`; continue; }
      flushList();
      para.push(line.trim());
    }
    flushPara(); flushList(); flushTable();
    return html;
  }

  const renderAll = (root = document) => root.querySelectorAll("[data-md]").forEach((el) => {
    el.innerHTML = md(el.textContent.trim());
    el.removeAttribute("data-md");
    el.classList.add("md");
  });
  document.addEventListener("DOMContentLoaded", () => renderAll());
  return { md, esc, renderAll };
})();
