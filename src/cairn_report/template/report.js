let figures = [], active = 0, zoom = 1, panX = 0, panY = 0, pendingSelection = null, comments = [], saveTimer = null;
const $ = selector => document.querySelector(selector);
const el = (tag, className, text) => { const node = document.createElement(tag); if (className) node.className = className; if (text !== undefined) node.textContent = text; return node; };
const reportRoute = location.pathname.match(/^\/reports\/([a-z0-9][a-z0-9-]*)\//);
const reportSlug = reportRoute?.[1] || "overview";  // the overview super-report is served at /
const commentsApi = `/api/reports/${reportSlug}/comments`;
const commentsStorageKey = `standalone-report-comments:${reportSlug}`;
const COMMENT_ICON = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 4.5h14a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2h-8l-5 3v-3H5a2 2 0 0 1-2-2v-8a2 2 0 0 1 2-2Z"/></svg>';
const isFigureTarget = target => target.startsWith("figure:");
function commentFor(target) { return [...comments].reverse().find(row => row.target === target); }
function commentIcon(target, line = "") { const button = el("button", "image-comment"); button.type = "button"; button.title = "Comment on image"; button.setAttribute("aria-label", "Comment on image"); button.innerHTML = COMMENT_ICON; button.onclick = event => { event.preventDefault(); event.stopPropagation(); openComment(target, line, "", button.getBoundingClientRect(), commentFor(target)); }; return button; }
async function plotSpec(figure) { if (!figure._spec) { const response = await fetch(figure.src); if (!response.ok) throw new Error(`Could not load ${figure.src}`); figure._spec = await response.json(); } return figure._spec; }
async function renderPlot(node, figure, full = false) { const spec = await plotSpec(figure); const layout = {...(spec.layout || {}), autosize: true}; const config = {responsive: true, displaylogo: false, ...(spec.config || {})};
  if (full) delete layout.height;
  else { layout.margin = {...(layout.margin || {}), t: Math.max(layout.margin?.t ?? 0, 48)}; Object.assign(config, {displayModeBar: true, scrollZoom: false, modeBarButtonsToRemove: ["sendDataToCloud", "select2d", "lasso2d", "autoScale2d", "hoverClosestCartesian", "hoverCompareCartesian", "toggleSpikelines"]}); } // Card: compact tool bar in the reserved top margin; scroll zoom stays off so page scrolling works; full tools live in the lightbox.
  await Plotly.react(node, spec.data || [], layout, config); }

// The builder renders every card from report.md; the page only adds behavior to them.
function hydrateFigure(card) {
  const item = {trace: JSON.parse(card.dataset.trace || "{}"), id: card.dataset.figureId, type: card.dataset.figureType, src: card.dataset.src, title: card.querySelector("h3")?.textContent || "", caption: card.querySelector(".figure-copy p")?.textContent || "", line: card.dataset.line || ""};
  const index = figures.push(item) - 1, target = `figure:${item.id}`;
  card.classList.add("comment-anchor");
  if (item.type === "plotly") {
    const expand = el("button", "figure-expand", "⛶"); expand.type = "button"; expand.title = "Open interactive figure"; expand.setAttribute("aria-label", "Open interactive figure"); expand.onclick = event => { event.stopPropagation(); openFigure(index); };
    card.append(expand, commentIcon(target, item.line)); renderPlot(card.querySelector(".plotly-preview"), item);
  } else {
    card.tabIndex = 0; card.setAttribute("role", "button"); card.onclick = () => openFigure(index); card.onkeydown = event => { if (event.key === "Enter" || event.key === " ") openFigure(index); };
    card.append(commentIcon(target, item.line));
  }
}
Promise.all([fetch("report-data.json").then(r => r.json()), fetch("narrative.html").then(r => r.text())]).then(([data, narrative]) => {
  document.title = data.title; $("#title").textContent = data.title; $("#subtitle").textContent = data.subtitle;
  $("#status").textContent = data.status; $("#status").hidden = !data.status; $("#report-summary").textContent = data.summary || ""; $("#report-summary").hidden = !data.summary; if (data.overview) { const back = $("#overview-link"); back.textContent = `← ${data.overview.title}`; back.href = data.overview.href; back.title = "Back to this direction in the project overview"; back.hidden = false; } $("#updated").textContent = `Updated · ${data.updated}`;
  $("#report-prose").innerHTML = narrative;
  document.querySelectorAll("#report-prose h2[id]").forEach(heading => { const link = el("a", null, heading.textContent); link.href = `#${heading.id}`; $("#report-nav").append(link); });
  loadReportList(data);
  bindFiguresInDocumentOrder(); bindSelectionComments(); loadComments(); revealHashTarget();
});
// Content arrives after load, so the browser cannot scroll to #anchor links (backlinks, report: links) by itself.
function revealHashTarget() {
  const id = decodeURIComponent(location.hash.slice(1)); const target = id && document.getElementById(id);
  if (!target) return;
  target.scrollIntoView({block: "center"}); target.classList.add("is-link-target");
  setTimeout(() => target.classList.remove("is-link-target"), 2200);
}
window.addEventListener("hashchange", revealHashTarget);

// The sidebar lists every report in the project so related branches are one click apart.
async function loadReportList(data) {
  let reports = [];
  try { const response = await fetch("/api/reports"); if (response.ok) reports = await response.json(); } catch (_) {}
  // Without the server's list (a static copy), a direction still links back to the overview.
  if (!reports.length && data.overview) reports = [{slug: "overview", title: data.overview.title, href: "/"}, {slug: reportSlug, title: data.title, href: location.pathname}];
  if (reports.length < 2) return;
  const list = $("#report-list");
  reports.forEach(report => {
    const overview = report.slug === "overview";
    const link = el("a", overview ? "report-list-overview" : "report-list-direction");
    if (overview) link.append(el("span", "report-list-kind", "Overview"));
    link.append(el("span", null, report.title));
    link.href = overview && data.overview ? data.overview.href : report.href;
    link.title = overview ? "Project overview" : (report.subtitle || report.title);
    if (report.slug === reportSlug) link.setAttribute("aria-current", "page");
    list.append(link);
  });
  $("#report-list-section").hidden = false;
}

const lightbox = $("#lightbox"), lightboxImage = $("#lightbox-image"), lightboxPlot = $("#lightbox-plot");
async function showFigure(index) { active = (index + figures.length) % figures.length; const figure = figures[active]; const interactive = figure.type === "plotly"; lightboxImage.hidden = interactive; lightboxPlot.hidden = !interactive; $(".zoom").hidden = interactive; if (interactive) { lightboxImage.src = ""; await renderPlot(lightboxPlot, figure, true); } else { if (globalThis.Plotly) Plotly.purge(lightboxPlot); lightboxImage.src = figure.src; lightboxImage.alt = figure.title; resetView(); } syncLightboxComment(); $("#lightbox-title").textContent = figure.title; $("#lightbox-caption").textContent = figure.caption || ""; renderTrace(figure.trace || {}); }
// Every enlarged figure says where it came from: file, run, producing code, commit.
const codeRef = ref => ref ? `${ref.file || "?"}${ref.line ? `:${ref.line}` : ""}${ref.function ? ` · ${ref.function}()` : ""}` : "";
let traceOpen = false;
try { traceOpen = localStorage.getItem("report-trace-open") === "1"; } catch (_) {}
function applyTraceState() {
  const list = $("#lightbox-trace"), button = $("#lightbox-info"), available = list.childElementCount > 0;
  button.hidden = !available; list.hidden = !available || !traceOpen;
  button.setAttribute("aria-expanded", String(traceOpen && available));
  button.setAttribute("aria-label", traceOpen ? "Hide figure source" : "Show figure source");
}
function toggleTrace() { traceOpen = !traceOpen; try { localStorage.setItem("report-trace-open", traceOpen ? "1" : "0"); } catch (_) {} applyTraceState(); }
$("#lightbox-info").onclick = toggleTrace;
function renderTrace(trace) {
  const rows = [
    ["File", trace.path],
    ["Run", trace.run],
    ["Drawn by", codeRef(trace.producer)],
    ["Logged by", codeRef(trace.origin)],
    ["Generated", trace.generated],
    ["Read in block", (trace.inputs || []).join(", ")],
    ["Commit", trace.revision ? trace.revision.slice(0, 12) + (trace.uncommitted ? ` + ${trace.uncommitted}` : "") : ""],
    ["Report", trace.report],
  ].filter(([, value]) => value);
  const list = $("#lightbox-trace"); list.replaceChildren();
  rows.forEach(([label, value]) => { const row = el("div"); row.append(el("dt", null, label), el("dd", null, value)); list.append(row); });
  applyTraceState();
}
function syncLightboxComment() { const button = $("#lightbox-comment"), figure = figures[active]; const has = Boolean(figure && commentFor(`figure:${figure.id}`)); button.classList.toggle("has-comment", has); button.title = has ? "Edit figure comment" : "Comment on figure"; }
function openFigure(index) { showFigure(index); lightbox.showModal(); }
function applyView() { lightboxImage.style.setProperty("--zoom", zoom); lightboxImage.style.setProperty("--pan-x", `${panX}px`); lightboxImage.style.setProperty("--pan-y", `${panY}px`); $("#zoom-label").textContent = `${Math.round(zoom * 100)}%`; }
let hoverPoint = null;
function imageCenter() { const parent = lightboxImage.offsetParent; if (!parent) { const rect = lightboxImage.getBoundingClientRect(); return {x: rect.left + rect.width / 2 - panX, y: rect.top + rect.height / 2 - panY}; } const box = parent.getBoundingClientRect(); return {x: box.left + lightboxImage.offsetLeft + lightboxImage.offsetWidth / 2, y: box.top + lightboxImage.offsetTop + lightboxImage.offsetHeight / 2}; }
// Zoom about `point` (client coordinates) so the pixel under it stays fixed; without a point, zoom about the current view.
function setZoom(value, point = null) { const next = Math.max(.5, Math.min(4, value)); if (next <= 1) { panX = 0; panY = 0; } else if (point) { const center = imageCenter(), ratio = next / zoom; panX = point.x - center.x - ratio * (point.x - center.x - panX); panY = point.y - center.y - ratio * (point.y - center.y - panY); } else { const ratio = next / zoom; panX *= ratio; panY *= ratio; } zoom = next; applyView(); }
function resetView() { zoom = 1; panX = 0; panY = 0; applyView(); }
lightbox.querySelector(".close").onclick = () => lightbox.close(); lightbox.querySelector(".previous").onclick = () => showFigure(active - 1); lightbox.querySelector(".next").onclick = () => showFigure(active + 1);
$("#lightbox-comment").onclick = event => { const figure = figures[active]; const anchor = event.currentTarget.getBoundingClientRect(); lightbox.close(); openComment(`figure:${figure.id}`, figure.line || "", "", anchor, commentFor(`figure:${figure.id}`)); };
lightbox.querySelector(".zoom").onclick = event => { const action = event.target.dataset.zoom; if (action === "in") setZoom(zoom + .25); if (action === "out") setZoom(zoom - .25); if (action === "reset") resetView(); };
lightboxImage.addEventListener("wheel", event => { event.preventDefault(); setZoom(zoom + (event.deltaY < 0 ? .15 : -.15), {x: event.clientX, y: event.clientY}); }, {passive: false});
lightboxImage.addEventListener("pointermove", event => { hoverPoint = {x: event.clientX, y: event.clientY}; });
lightboxImage.addEventListener("pointerleave", () => { hoverPoint = null; });
lightboxImage.draggable = false; lightboxImage.addEventListener("dragstart", event => event.preventDefault());
lightboxImage.addEventListener("pointerdown", event => { if (zoom <= 1) return; event.preventDefault(); const startX = event.clientX - panX, startY = event.clientY - panY; lightboxImage.setPointerCapture(event.pointerId); lightboxImage.classList.add("is-panning"); const move = next => { panX = next.clientX - startX; panY = next.clientY - startY; applyView(); }; const done = () => { lightboxImage.classList.remove("is-panning"); lightboxImage.removeEventListener("pointermove", move); lightboxImage.removeEventListener("pointerup", done); lightboxImage.removeEventListener("pointercancel", done); }; lightboxImage.addEventListener("pointermove", move); lightboxImage.addEventListener("pointerup", done); lightboxImage.addEventListener("pointercancel", done); });
document.addEventListener("keydown", event => { if (!lightbox.open) return; if (event.key === "ArrowLeft") showFigure(active - 1); if (event.key === "ArrowRight") showFigure(active + 1); if (event.key === "+" || event.key === "=") setZoom(zoom + .25, hoverPoint); if (event.key === "-") setZoom(zoom - .25, hoverPoint); if (event.key === "i" && !event.metaKey && !event.ctrlKey) toggleTrace(); if (event.key === "Escape") lightbox.close(); });

function bindFiguresInDocumentOrder() {
  let offset = 0;
  document.querySelectorAll("#report-prose .figure-card, #report-prose img").forEach(node => {
    if (node.matches(".figure-card")) { hydrateFigure(node); return; }
    if (node.closest(".figure-card")) return;
    const image = node, line = image.closest("[data-source-line]")?.dataset.sourceLine || "unknown";
    const id = `markdown-line-${line}-${offset++}`;
    const index = figures.push({id, line, src: image.src, title: image.alt || `Figure from Markdown line ${line}`, caption: `Generated from report.md line ${line}`}) - 1;
    image.tabIndex = 0; image.setAttribute("role", "button"); image.onclick = () => openFigure(index); image.onkeydown = event => { if (event.key === "Enter" || event.key === " ") openFigure(index); }; const anchor = image.closest("[data-source-line]") || image.parentElement; anchor.classList.add("comment-anchor"); anchor.dataset.commentTarget = `figure:${id}`; anchor.append(commentIcon(`figure:${id}`, line));
  });
}

const commentPopover = $("#comment-popover"), commentForm = $("#comment-form");
function placeCommentPopover(rect) { const width = 360, gap = 8; const left = Math.max(12, Math.min(window.innerWidth - width - 12, rect?.left ?? (window.innerWidth - width) / 2)); const top = Math.max(12, Math.min(window.innerHeight - 230, (rect?.bottom ?? window.innerHeight / 2) + gap)); commentPopover.style.left = `${left}px`; commentPopover.style.top = `${top}px`; }
function closeComment() { if (saveTimer) { clearTimeout(saveTimer); saveTimer = null; queueSave(); } commentPopover.hidden = true; }
function openComment(target, line = "", quote = "", anchor = null, existing = null) { clearTimeout(saveTimer); if (!existing && isFigureTarget(target)) existing = commentFor(target); if (existing) { line = existing.line || line; quote = existing.quote || quote; } currentSession = {id: existing?.id || ""}; commentForm.elements.id.value = currentSession.id; commentForm.elements.target.value = target; commentForm.elements.line.value = line; commentForm.elements.quote.value = quote; commentForm.elements.text.value = existing?.text || ""; $("#comment-target").textContent = line ? `report.md line ${line}` : target; $("#comment-quote").textContent = quote; $("#comment-quote").hidden = !quote; $("#comment-message").textContent = existing ? "Saved" : "Autosaves while you type"; placeCommentPopover(anchor); commentPopover.hidden = false; commentForm.elements.text.focus(); }
function bindSelectionComments() {
  const report = $("main"), button = $("#selection-comment");
  const inspect = () => { const selection = window.getSelection(); if (!selection?.rangeCount || !selection.toString().trim() || commentPopover.contains(selection.anchorNode)) { button.hidden = true; return; } const range = selection.getRangeAt(0); const start = range.startContainer; const node = start.nodeType === Node.TEXT_NODE ? start.parentElement : start; if (!node || !report.contains(node)) { button.hidden = true; return; } const card = node.closest(".figure-card"); const block = node.closest("[data-source-line]"); const context = block || node.closest("[data-comment-target]") || node.closest("section") || node.closest("header, footer") || report; const line = card?.dataset.line || block?.dataset.sourceLine || ""; const target = card ? card.dataset.commentTarget : line ? `markdown:line:${line}` : context.dataset.commentTarget || (context.id ? `section:${context.id}` : "section:report"); const rect = range.getBoundingClientRect(); pendingSelection = {line, target, quote: selectionQuote(range).slice(0, 1000), rect}; button.style.left = `${Math.max(8, Math.min(window.innerWidth - 40, rect.right + 6))}px`; button.style.top = `${Math.max(8, Math.min(window.innerHeight - 40, rect.bottom + 6))}px`; button.hidden = false; };
  report.addEventListener("mouseup", () => setTimeout(inspect));
  report.addEventListener("touchend", () => setTimeout(inspect));
  report.addEventListener("keyup", event => { if (event.shiftKey) setTimeout(inspect); });
  button.onclick = () => { button.hidden = true; openComment(pendingSelection.target, pendingSelection.line, pendingSelection.quote, pendingSelection.rect); };
}

function localComments() { try { return JSON.parse(localStorage.getItem(commentsStorageKey) || "[]"); } catch (_) { return []; } }
function upsert(rows, comment) { const index = rows.findIndex(row => row.id === comment.id); if (index < 0) rows.push(comment); else rows[index] = comment; return rows; }
function storeLocal(comment) { localStorage.setItem(commentsStorageKey, JSON.stringify(upsert(localComments(), comment))); }
function removeLocal(ids) { const remove = new Set(ids.filter(Boolean)); if (!remove.size) return; localStorage.setItem(commentsStorageKey, JSON.stringify(localComments().filter(row => !remove.has(row.id)))); }
function anchorFor(target) { if (target.startsWith("section:") && document.getElementById(target.slice(8))) return document.getElementById(target.slice(8)); if (target.startsWith("finding:")) return document.getElementById(target.slice(8)); if (target.startsWith("markdown:line:")) return document.querySelector(`[data-source-line="${target.slice(14)}"]`); return [...document.querySelectorAll("[data-comment-target]")].find(node => node.dataset.commentTarget === target); }
function clearCommentMarks() {
  document.querySelectorAll(".comment-bubble").forEach(node => node.remove());
  document.querySelectorAll("mark.comment-highlight").forEach(mark => { const parent = mark.parentNode; mark.replaceWith(...mark.childNodes); parent.normalize(); });
  document.querySelectorAll(".has-comment").forEach(node => node.classList.remove("has-comment"));
  document.querySelectorAll(".math-highlight").forEach(node => { node.classList.remove("math-highlight"); node.onclick = null; node.removeAttribute("title"); });
}
// Formulas are quoted by their LaTeX source: rendered MathML text is not a usable quote.
const formulaOf = node => (node?.nodeType === Node.TEXT_NODE ? node.parentElement : node)?.closest?.(".math-tex");
function selectionQuote(range) {
  const first = formulaOf(range.startContainer);
  if (first && first === formulaOf(range.endContainer)) return `$${first.dataset.latex}$`;
  const fragment = range.cloneContents();
  fragment.querySelectorAll(".math-tex").forEach(node => node.replaceWith(` $${node.dataset.latex}$ `));
  fragment.querySelectorAll("button").forEach(node => node.remove());
  fragment.querySelectorAll("p, li, div, tr, th, td, h1, h2, h3, h4, pre, blockquote").forEach(node => node.append(" "));
  return fragment.textContent.replace(/\s+/g, " ").trim();
}
// Match ignoring whitespace: selections and the DOM disagree about tabs and newlines (tables, blocks).
function highlightQuote(root, quote) {
  const needle = quote.replace(/\s+/g, ""); if (!needle) return [];
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT, {acceptNode: node => {
    if (node.nodeType === Node.ELEMENT_NODE) return node.matches("button, .comment-popover") ? NodeFilter.FILTER_REJECT : node.classList.contains("math-tex") ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_SKIP;
    return node.parentElement.closest("button, math") ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT; }});
  let text = ""; const map = []; let node;
  while ((node = walker.nextNode())) {
    if (node.nodeType === Node.ELEMENT_NODE) { for (const char of `$${node.dataset.latex}$`.replace(/\s+/g, "")) { text += char; map.push([node, -1]); } continue; }
    const value = node.nodeValue; for (let i = 0; i < value.length; i++) { if (/\s/.test(value[i])) continue; text += value[i]; map.push([node, i]); }
  }
  const start = text.indexOf(needle); if (start < 0) return [];
  const spans = new Map(), formulas = new Set();
  for (let k = start; k < start + needle.length; k++) { const [target, offset] = map[k]; if (offset < 0) { formulas.add(target); continue; } const span = spans.get(target); if (span) span[1] = offset + 1; else spans.set(target, [offset, offset + 1]); }
  const marks = [...spans].map(([textNode, [from, to]]) => { const range = document.createRange(); range.setStart(textNode, from); range.setEnd(textNode, to); const mark = el("mark", "comment-highlight"); range.surroundContents(mark); return mark; });
  formulas.forEach(formula => formula.classList.add("math-highlight"));
  return [...marks, ...formulas];
}
function renderCommentBubbles() {
  clearCommentMarks();
  const blockLevel = new Map(), unanchored = [];
  comments.forEach(row => {
    const anchor = anchorFor(row.target); if (!anchor) { unanchored.push(row); return; }
    const open = event => { if (window.getSelection()?.toString().trim()) return; event.preventDefault(); event.stopPropagation(); openComment(row.target, row.line || "", row.quote || "", event.currentTarget.getBoundingClientRect(), row); };
    if (isFigureTarget(row.target)) { anchor.classList.add("has-comment"); anchor.querySelectorAll(".image-comment").forEach(button => { button.classList.add("has-comment"); button.title = "Edit figure comment"; }); return; }
    const marks = row.quote ? highlightQuote(anchor, row.quote) : [];
    if (marks.length) { marks.forEach(mark => { mark.title = row.text; mark.onclick = open; }); const bubble = el("button", "comment-bubble inline"); bubble.type = "button"; bubble.title = row.text; bubble.setAttribute("aria-label", "Open comment"); bubble.innerHTML = COMMENT_ICON; bubble.onclick = open; marks.at(-1).after(bubble); return; }
    if (!blockLevel.has(anchor)) blockLevel.set(anchor, []); blockLevel.get(anchor).push({row, open, stale: Boolean(row.quote)});
  });
  blockLevel.forEach((entries, anchor) => { const {open} = entries.at(-1); const stale = entries.some(entry => entry.stale); anchor.classList.add("comment-anchor", "has-comment"); const bubble = el("button", `comment-bubble${stale ? " is-stale" : ""}`, entries.length > 1 ? String(entries.length) : ""); bubble.type = "button"; if (entries.length === 1) bubble.innerHTML = COMMENT_ICON; bubble.title = stale ? "Quoted text no longer found; open comment" : entries.length > 1 ? `${entries.length} comments` : "Open comment"; bubble.onclick = open; anchor.append(bubble); });
  renderUnanchored(unanchored);
  syncLightboxComment();
}
// A comment whose target was removed or renamed is never hidden: it is listed with its quote.
function renderUnanchored(rows) {
  const section = $("#unanchored-comments"), list = $("#unanchored-list");
  list.replaceChildren(); section.hidden = !rows.length;
  rows.forEach(row => {
    const item = el("li"), button = el("button", "unanchored-comment"); button.type = "button";
    button.append(el("span", "unanchored-target", row.quote ? `“${row.quote.slice(0, 120)}”` : row.target), el("span", "unanchored-text", row.text));
    button.onclick = event => openComment(row.target, row.line || "", row.quote || "", event.currentTarget.getBoundingClientRect(), row);
    item.append(button); list.append(item);
  });
}
async function loadComments() {
  let hosted = [], reachable = false;
  const browser = localComments();
  try { const response = await fetch(commentsApi); if (response.ok) { hosted = await response.json(); reachable = true; } } catch (_) {}
  if (reachable && browser.length) {
    const migrated = [];
    for (const row of browser) {
      try {
        const response = await fetch(commentsApi, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(row)});
        if (!response.ok) continue;
        const saved = await response.json(); upsert(hosted, saved); migrated.push(row.id);
      } catch (_) {}
    }
    removeLocal(migrated);
  }
  comments = [...localComments()]; hosted.forEach(row => upsert(comments, row));
  comments = comments.filter(row => row.event !== "resolve" && row.event !== "delete" && row.target); renderCommentBubbles();
}
// Saves and deletes run one at a time, in order. Each open comment is a session whose id
// the first save fills in, so a quick second keystroke updates it instead of creating a
// duplicate, and a slow response can never overwrite a newer edit.
let currentSession = {id: ""}, saveQueue = Promise.resolve();
const enqueue = task => (saveQueue = saveQueue.then(task).catch(() => {}));
const showStatus = (session, text) => { if (session === currentSession) $("#comment-message").textContent = text; };
function queueSave() { const session = currentSession, payload = Object.fromEntries(new FormData(commentForm)); return enqueue(() => saveComment(session, payload)); }
async function saveComment(session, payload) {
  payload.id = session.id; payload.text = payload.text.trim();
  if (!payload.text) { if (session.id) await deleteComment(session, {close: false}); showStatus(session, "Autosaves while you type"); return; }
  showStatus(session, "Saving…");
  let saved;
  try { const response = await fetch(commentsApi, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload)}); if (!response.ok) throw new Error("server rejected comment"); saved = await response.json(); removeLocal([payload.id, saved.id]); }
  catch (_) { const id = payload.id || globalThis.crypto?.randomUUID?.() || `local-${Date.now()}-${Math.random().toString(16).slice(2)}`; saved = {...payload, id, timestamp: new Date().toISOString(), storage: "browser"}; storeLocal(saved); }
  session.id = saved.id; if (session === currentSession) commentForm.elements.id.value = saved.id;
  upsert(comments, saved); showStatus(session, "Saved"); renderCommentBubbles();
}
commentForm.addEventListener("submit", event => event.preventDefault());
// Deleting appends a delete event; earlier revisions stay in the history.
async function deleteComment(session, {close = true} = {}) {
  const id = session.id;
  if (id) {
    const existing = comments.find(row => row.id === id);
    if (existing?.storage === "browser") localStorage.setItem(commentsStorageKey, JSON.stringify(localComments().filter(row => row.id !== id)));
    else { try { const response = await fetch(`${commentsApi}/delete`, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({id})}); if (!response.ok) throw new Error("delete rejected"); } catch (_) { showStatus(session, "Delete failed"); return; } }
    comments = comments.filter(row => row.id !== id);
  }
  session.id = "";
  if (session === currentSession) { commentForm.elements.id.value = ""; if (close) { commentForm.elements.text.value = ""; commentPopover.hidden = true; } }
  renderCommentBubbles();
}
$("#comment-delete").onclick = event => { event.preventDefault(); clearTimeout(saveTimer); saveTimer = null; const session = currentSession; enqueue(() => deleteComment(session)); };
commentForm.elements.text.addEventListener("input", () => { clearTimeout(saveTimer); $("#comment-message").textContent = "Saving…"; saveTimer = setTimeout(() => { saveTimer = null; queueSave(); }, 500); });
document.addEventListener("pointerdown", event => { if (!commentPopover.hidden && !commentPopover.contains(event.target) && !event.target.closest(".comment-bubble, .image-comment, #selection-comment")) closeComment(); });
document.addEventListener("keydown", event => { if (event.key === "Escape" && !commentPopover.hidden) closeComment(); });

const AGENT_INSTRUCTIONS = `Please address these report comments.
For each comment: locate its quote first, falling back to its line or target. Make the requested change, or ask me if its consequence is ambiguous. Then rebuild with \`pixi run report-build\`.
Append comments marked "storage": "browser" to ${reportSlug === "overview" ? "reports" : `reports/${reportSlug}`}/knowledge/comments.jsonl first. Record each resolution by appending {"id": "<comment id>", "event": "resolve", "timestamp": "<ISO time>", "resolution": "<what changed>"} to that file. Never edit or delete existing lines.
Afterwards, summarize what changed in the code, the runs, the findings, and the report.`;
async function copyText(text) {
  try { await navigator.clipboard.writeText(text); return true; } catch (_) {}
  const area = el("textarea"); area.value = text; area.setAttribute("readonly", ""); area.style.position = "fixed"; area.style.opacity = "0"; document.body.append(area); area.select();
  const copied = document.execCommand("copy"); area.remove(); return copied;
}
function flashTip(button, text) {
  button.dataset.tooltip = text; button.classList.add("show-tip"); clearTimeout(button._tipTimer);
  button._tipTimer = setTimeout(() => { button.dataset.tooltip = button.getAttribute("aria-label"); button.classList.remove("show-tip"); }, 1600);
}
const plural = count => `${count} comment${count === 1 ? "" : "s"}`;
$("#copy-comments").onclick = async event => {
  const button = event.currentTarget;
  await loadComments();
  const open = comments.map(({id, target, line, quote, text, storage}) => ({id, target, line: line || null, quote: quote || null, text, ...(storage ? {storage} : {})}));
  if (!open.length) { flashTip(button, "No open comments"); return; }
  const copied = await copyText(`${AGENT_INSTRUCTIONS}\n\nOpen comments (${open.length}):\n${JSON.stringify(open, null, 2)}\n`);
  flashTip(button, copied ? `Copied ${plural(open.length)}` : "Copy failed");
};

// Clearing appends resolve events; the comment history itself is never erased.
const clearConfirm = $("#clear-confirm");
function closeClearConfirm() { clearConfirm.hidden = true; $("#clear-comments").setAttribute("aria-expanded", "false"); }
$("#clear-comments").onclick = async event => {
  const button = event.currentTarget;
  if (!clearConfirm.hidden) { closeClearConfirm(); return; }
  await loadComments();
  if (!comments.length) { flashTip(button, "No open comments"); return; }
  $("#clear-confirm-text").textContent = `Clear ${plural(comments.length)}? They stay in the comment history.`;
  const rect = button.getBoundingClientRect();
  clearConfirm.style.left = `${Math.max(12, Math.min(window.innerWidth - 252, rect.left))}px`; clearConfirm.style.top = `${rect.bottom + 8}px`;
  clearConfirm.hidden = false; button.setAttribute("aria-expanded", "true"); clearConfirm.querySelector('[data-confirm="cancel"]').focus();
};
clearConfirm.onclick = async event => {
  const action = event.target.closest("[data-confirm]")?.dataset.confirm; if (!action) return;
  closeClearConfirm(); if (action !== "clear") return;
  const button = $("#clear-comments"), count = comments.length, hosted = comments.some(row => row.storage !== "browser");
  if (hosted) { try { const response = await fetch(`${commentsApi}/clear`, {method: "POST", headers: {"Content-Type": "application/json"}, body: "{}"}); if (!response.ok) throw new Error("clear rejected"); } catch (_) { flashTip(button, "Clear failed"); return; } }
  localStorage.removeItem(commentsStorageKey); closeComment(); await loadComments();
  flashTip(button, `Cleared ${plural(count)}`);
};
document.addEventListener("pointerdown", event => { if (!clearConfirm.hidden && !clearConfirm.contains(event.target) && !event.target.closest("#clear-comments")) closeClearConfirm(); });
document.addEventListener("keydown", event => { if (event.key === "Escape" && !clearConfirm.hidden) closeClearConfirm(); });
