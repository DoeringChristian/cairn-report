let figures = [], active = 0, zoom = 1, panX = 0, panY = 0, pendingSelection = null;
const $ = selector => document.querySelector(selector);
/* The builder puts versioned URLs on this script tag: the files change name when their content does, so the
   browser can keep them; only index.html is checked on each visit. */
const PAGE = document.currentScript?.dataset || {};
const PLOTLY_SRC = PAGE.plotly || "plotly.min.js";  // loaded on first use
const el = (tag, className, text) => { const node = document.createElement(tag); if (className) node.className = className; if (text !== undefined) node.textContent = text; return node; };
/* The builder tells each page where the site root is (relative) and which report (slug) it shows,
   so the same build works at any address: a project server, the store UI's /p/<project>/r/, or plain static hosting. */
const siteRoot = new URL(PAGE.root || "./", location.href).pathname;
/* `cairn share` writes one page that carries the shared reports (#cairn-bundle); links between
   reports then switch the report inside the page, so it works on hosts that serve a single file. */
const BUNDLE = (() => { const node = document.getElementById("cairn-bundle"); return node ? JSON.parse(node.textContent) : null; })();
const VIRTUAL_SITE = "https://site.invalid/";  // bundle mode: report links resolve against this stand-in address
let reportKey = BUNDLE ? bundleStart() : PAGE.report || "";
const siteHref = href => (BUNDLE ? VIRTUAL_SITE : siteRoot) + href;  // report hrefs are relative to the site root
const storageScope = () => `${BUNDLE ? "bundle:" : siteRoot}${reportKey}`;
let site = {reports: []};
const siteData = BUNDLE ? Promise.resolve(site = BUNDLE.site)
  : fetch(siteHref("site.json"), {cache: "no-cache"}).then(r => r.ok ? r.json() : site).catch(() => site).then(value => (site = value));
// A plain #anchor opens the report that has it, so links to any section of the shared page work.
function bundleStart() {
  const id = decodeURIComponent(location.hash.slice(1)), start = BUNDLE.start;
  if (!id || BUNDLE.reports[start].narrative.includes(`id="${id}"`)) return start;
  return Object.keys(BUNDLE.reports).find(key => BUNDLE.reports[key].narrative.includes(`id="${id}"`)) ?? start;
}
function bundleTarget(href) {
  let url; try { url = new URL(href, VIRTUAL_SITE + (reportKey ? `${reportKey}/` : "")); } catch (_) { return null; }
  if (url.origin !== new URL(VIRTUAL_SITE).origin) return null;
  const key = decodeURIComponent(url.pathname).replace(/^\//, "").replace(/(^|\/)index\.html$/, "").replace(/\/$/, "");
  return key in BUNDLE.reports ? {key, hash: decodeURIComponent(url.hash.slice(1))} : null;
}
/* Each copy button copies a location marker: one line an LLM can act on
   (report file and line, with the quoted text or the figure's id, title, and source file). */
const COPY_ICON = '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="8.5" y="8.5" width="11" height="11" rx="2"/><path d="M15.5 8.5V6.5a2 2 0 0 0-2-2h-7a2 2 0 0 0-2 2v7a2 2 0 0 0 2 2h2"/></svg>';
async function locationMarker({line = "", quote = "", figure = null}) {
  const data = await reportData, source = data.source || `${reportKey || ""}/report.md`.replace(/^\//, "");
  const place = line && line !== "unknown" ? `${source}:${line}` : source;
  if (figure) {
    const file = figure.trace?.label || figure.source, from = file ? ` (source: ${file})` : "";
    return `${place} figure #${figure.id} "${figure.title}"${from}`;
  }
  return quote ? `${place} "${quote.replace(/\s+/g, " ").trim()}"` : place;
}
async function copyMarker(button, spec) {
  const copied = await copyText(await locationMarker(spec));
  flashTip(button, copied ? "Location copied" : "Copy failed");
}
function copyIcon(spec) {
  const button = el("button", "image-copy"); button.type = "button"; button.title = "Copy location"; button.setAttribute("aria-label", "Copy location"); button.dataset.tooltip = "Copy location"; button.innerHTML = COPY_ICON;
  button.onclick = event => { event.preventDefault(); event.stopPropagation(); copyMarker(button, typeof spec === "function" ? spec() : spec); };
  return button;
}
async function plotSpec(figure) { if (!figure._spec && BUNDLE) figure._spec = BUNDLE.reports[reportKey].files?.[figure.src]; if (!figure._spec) { const response = await fetch(figure.src); if (!response.ok) throw new Error(`Could not load ${figure.src}`); figure._spec = await response.json(); } return figure._spec; }
/* Plotly is large: fetch it only when the first interactive figure comes near the screen. */
let plotlyLoading = null;
function loadPlotly() {
  if (window.Plotly) return Promise.resolve(window.Plotly);
  return plotlyLoading ||= new Promise((resolve, reject) => {
    const script = document.createElement("script"); script.src = PLOTLY_SRC; script.async = true;
    script.onload = () => resolve(window.Plotly); script.onerror = () => { plotlyLoading = null; reject(new Error("Could not load Plotly")); };
    document.head.append(script);
  });
}
/* Figures load and plots render only when they approach the viewport, so long reports open fast. */
const nearObserver = "IntersectionObserver" in window ? new IntersectionObserver(entries => entries.forEach(entry => {
  if (entry.isIntersecting) { nearObserver.unobserve(entry.target); const action = entry.target._near; entry.target._near = null; action?.(); }
}), {rootMargin: "1200px 0px"}) : null;
function whenNear(node, action) { if (!nearObserver) { action(); return; } node._near = action; nearObserver.observe(node); }
async function renderPlot(node, figure, full = false) { const [spec] = await Promise.all([plotSpec(figure), loadPlotly()]); const layout = {...(spec.layout || {}), autosize: true}; const config = {responsive: true, displaylogo: false, ...(spec.config || {})};
  if (full) delete layout.height;
  else { layout.margin = {...(layout.margin || {}), t: Math.max(layout.margin?.t ?? 0, 48)}; Object.assign(config, {displayModeBar: true, scrollZoom: false, modeBarButtonsToRemove: ["sendDataToCloud", "select2d", "lasso2d", "autoScale2d", "hoverClosestCartesian", "hoverCompareCartesian", "toggleSpikelines"]}); } // Card: compact tool bar in the reserved top margin; scroll zoom stays off so page scrolling works; full tools live in the lightbox.
  await Plotly.react(node, spec.data || [], layout, config); }

// The builder renders every card from report.md; the page only adds behavior to them.
function hydrateFigure(card) {
  const item = {trace: JSON.parse(card.dataset.trace || "{}"), id: card.dataset.figureId, type: card.dataset.figureType, src: card.dataset.src, fps: Number(card.querySelector("video")?.dataset.fps) || 0, source: (card.querySelector(".figure-source")?.textContent || "").replace(/^Source\s*·\s*/, ""), title: card.querySelector("h3")?.textContent || "", caption: card.querySelector(".figure-copy p")?.textContent || "", line: card.dataset.line || ""};
  const index = figures.push(item) - 1;
  card.classList.add("copy-anchor");
  if (item.type === "plotly") {
    const expand = el("button", "figure-expand", "⛶"); expand.type = "button"; expand.title = "Open interactive figure"; expand.setAttribute("aria-label", "Open interactive figure"); expand.onclick = event => { event.stopPropagation(); openFigure(index); };
    card.append(expand, copyIcon({line: item.line, figure: item})); whenNear(card, () => renderPlot(card.querySelector(".plotly-preview"), item).catch(() => card.classList.add("is-failed")));
  } else {
    card.tabIndex = 0; card.setAttribute("role", "button"); card.onclick = () => openFigure(index); card.onkeydown = event => { if (event.key === "Enter" || event.key === " ") openFigure(index); };
    card.append(copyIcon({line: item.line, figure: item}));
    const video = card.querySelector("video");
    if (video) hydrateVideoCard(card, video);
    const image = card.querySelector("img");
    if (image) {
      /* The builder sets the card's shape from the image size; for an image it could not measure, take it on load. */
      const fit = () => { if (!image.naturalWidth) return; const ratio = image.naturalWidth / image.naturalHeight; card.style.setProperty("--image-ratio", ratio.toFixed(4)); card.style.setProperty("--figure-ratio", Math.min(Math.max(ratio, 0.75), 10).toFixed(3)); card.classList.toggle("is-wide", ratio > 2); card.classList.toggle("is-tall", ratio < 1); };
      if (!card.style.getPropertyValue("--figure-ratio")) image.addEventListener("load", fit, {once: true});
      /* An image shown at more than twice its pixel size (a small reconstruction, a 64-pixel sample) keeps its pixels sharp. */
      image.addEventListener("load", () => { if (image.naturalWidth && image.clientWidth > 2 * image.naturalWidth) image.classList.add("is-pixelated"); });
      if (image.dataset.src) whenNear(card, () => { image.src = image.dataset.src; });
    }
  }
}
/* A video card loads near the viewport and plays, muted and looped, only while mostly on screen
   (never with reduced motion); the lightbox has the full player. */
const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)");
const playObserver = "IntersectionObserver" in window ? new IntersectionObserver(entries => entries.forEach(entry => {
  const video = entry.target;
  if (entry.isIntersecting && !reducedMotion.matches) video.play().catch(() => {}); else video.pause();
}), {threshold: .5}) : null;
const clock = seconds => { const whole = Math.max(0, seconds || 0); const minutes = Math.floor(whole / 60); return `${minutes}:${(whole - minutes * 60).toFixed(2).padStart(5, "0")}`; };
function hydrateVideoCard(card, video) {
  card.classList.add("video-card");
  const badge = el("span", "video-badge", "▶"); card.querySelector(".figure-copy").before(badge);
  video.addEventListener("loadedmetadata", () => {
    const ratio = video.videoWidth / video.videoHeight;
    if (ratio && !card.style.getPropertyValue("--figure-ratio")) { card.style.setProperty("--image-ratio", ratio.toFixed(4)); card.style.setProperty("--figure-ratio", Math.min(Math.max(ratio, 0.75), 10).toFixed(3)); card.classList.toggle("is-wide", ratio > 2); card.classList.toggle("is-tall", ratio < 1); }
    badge.textContent = `▶ ${clock(video.duration).replace(/\.\d+$/, "")}`;
  }, {once: true});
  whenNear(card, () => { video.preload = "metadata"; video.src = video.dataset.src; playObserver?.observe(video); });
}

/* The header appears as soon as the small report-data.json arrives; the text follows, then figures as they near the screen. */
let reportData, narrativeText;
function fetchReport() {
  if (BUNDLE) { const entry = BUNDLE.reports[reportKey]; reportData = Promise.resolve(entry.data); narrativeText = Promise.resolve(entry.narrative); return; }
  reportData = fetch(PAGE.reportData || "report-data.json").then(r => r.json());
  narrativeText = fetch(PAGE.narrative || "narrative.html").then(r => r.text());
}
function showHeader(data) {
  document.title = data.title; $("#title").textContent = data.title; $("#subtitle").textContent = data.subtitle;
  $("#status").textContent = data.status; $("#status").hidden = !data.status; $("#report-summary").textContent = data.summary || ""; $("#report-summary").hidden = !data.summary; $("#eyebrow").textContent = data.eyebrow || ""; $("#eyebrow").hidden = !data.eyebrow; $("#updated").textContent = `Updated · ${data.updated}`;
  showData(data.inputs || {});
  if (!BUNDLE) $(".brand").href = siteRoot;  // the project index lists every report
  bundleLinks($(".hero"));
}
/* The Data panel: each input the report was built from, pinned to an exact version: the build records its id, name,
   number, and exact reference (`name@v<n>`, in the report's own project; data-artifact), so nothing here takes a
   reference apart. Served by the store UI (`cairn ui`, under /p/<project>/r/), it links to that version's page in the
   same project (/p/<project>/a/<name>@v<n>, or /p/<project>/v/<id>); in local and bundle modes it stays plain text. */
const STORE_SERVED = !BUNDLE && /^\/p\/[^/]+\/r\//.test(location.pathname);
function artifactPage(input) {
  const here = /^\/p\/([^/]+)\/r\//.exec(location.pathname);
  if (!STORE_SERVED || !here) return null;
  if (input.name && input.version != null) return `/p/${here[1]}/a/${input.name.split("/").map(encodeURIComponent).join("/")}@v${input.version}`;
  return input.id ? `/p/${here[1]}/v/${input.id}` : null;
}
function showData(inputs) {
  const panel = $("#data-panel"); if (!panel) return;
  const list = $("#data-list"); list.replaceChildren();
  Object.entries(inputs).forEach(([name, input]) => {
    const page = artifactPage(input), row = el("li", "data-row"), version = el(page ? "a" : "span", "data-version", input.resolved);
    if (page) version.href = page;
    version.dataset.artifact = input.resolved; version.title = input.requested ? `declared as ${input.requested}` : input.resolved;
    const button = el("button", "image-copy"); button.type = "button"; button.title = "Copy exact version"; button.setAttribute("aria-label", "Copy exact version"); button.dataset.tooltip = "Copy exact version"; button.innerHTML = COPY_ICON;
    button.onclick = async () => flashTip(button, (await copyText(input.resolved)) ? "Version copied" : "Copy failed");
    row.append(el("span", "data-name", name), version, el("span", "data-files", `${input.files} file${input.files === 1 ? "" : "s"}`), button);
    list.append(row);
  });
  panel.hidden = !list.childElementCount;
}
let pageBound = false;
function renderReport(place) {
  $("#report-prose").setAttribute("aria-busy", "true");
  reportData.then(showHeader);
  return Promise.all([reportData, narrativeText]).then(([data, narrative]) => {
    const prose = $("#report-prose"); prose.innerHTML = narrative; prose.removeAttribute("aria-busy");
    bundleLinks(prose); loadReportList(data).then(() => bundleLinks($("#report-list")));
    bindFiguresInDocumentOrder();
    if (!pageBound) { pageBound = true; bindSelectionCopy(); trackPosition(); }
    place();
  });
}
fetchReport();
renderReport(() => { if (location.hash) revealHashTarget(); else restorePosition(); });
if (BUNDLE) {
  $('footer a[href="manifest.json"]')?.remove();  // the build manifest stays with the project
  // Capture on window, ahead of any link handling the host page adds (which may open links in a new tab).
  window.addEventListener("click", event => {
    const link = event.target.closest?.("a[data-bundle-report]");
    if (!link || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault(); event.stopImmediatePropagation(); showReport(link.dataset.bundleReport, link.dataset.bundleHash || "");
  }, true);
}
/* Bundle mode: a link to another report becomes a plain #anchor link (hosts leave those to the page) that
   names its report; the click handler above switches to it. */
function bundleLinks(root) {
  if (!BUNDLE) return;
  root.querySelectorAll("a[href]").forEach(link => {
    const href = link.getAttribute("href"); if (href.startsWith("#")) return;
    const target = bundleTarget(href); if (!target) return;
    link.dataset.bundleReport = target.key; link.dataset.bundleHash = target.hash; link.setAttribute("href", `#${target.hash}`);
  });
}
function showReport(key, hash = "") {
  if (key === reportKey && hash) { location.hash = hash; revealHashTarget(); return; }
  savePosition(); if (lightbox.open) lightbox.close();
  reportKey = key; figures = []; active = 0; nearObserver?.disconnect();
  fetchReport();
  renderReport(() => {
    history.replaceState(null, "", location.pathname + location.search + (hash ? `#${hash}` : ""));
    if (hash) revealHashTarget(); else if (!restorePosition()) scrollTo({top: 0, behavior: "instant"});
  });
}
// Content arrives after load, so the browser cannot scroll to #anchor links (backlinks, report: links) by itself.
function revealHashTarget() {
  const id = decodeURIComponent(location.hash.slice(1)); const target = id && document.getElementById(id);
  if (!target) return;
  target.scrollIntoView({block: "center", behavior: "instant"}); target.classList.add("is-link-target");  // instant: an anchor must not depend on an animation finishing
  setTimeout(() => target.classList.remove("is-link-target"), 2200);
}
window.addEventListener("hashchange", revealHashTarget);

/* Remember where the reader was, per report, as "this block, this many pixels from the top": that survives
   rebuilds and late layout better than a raw scroll offset. A #anchor in the URL always wins. */
const positionKey = () => `cairn-report-position:${storageScope()}`;
if ("scrollRestoration" in history) history.scrollRestoration = "manual";
function readingAnchor() {
  for (const block of document.querySelectorAll("#report-prose [data-source-line]")) {
    const box = block.getBoundingClientRect();
    if (box.bottom > 0) return {line: block.dataset.sourceLine, offset: Math.round(box.top), y: Math.round(scrollY)};
  }
  return {y: Math.round(scrollY)};
}
function savePosition() { try { localStorage.setItem(positionKey(), JSON.stringify(readingAnchor())); } catch (_) {} }
function restorePosition() {
  let saved = null; try { saved = JSON.parse(localStorage.getItem(positionKey()) || "null"); } catch (_) {}
  if (!saved) return false;
  let moved = false; const stop = () => { moved = true; };
  ["wheel", "touchstart", "keydown", "pointerdown"].forEach(type => addEventListener(type, stop, {once: true, passive: true}));
  const apply = () => {
    if (moved) return;
    const block = saved.line && document.querySelector(`#report-prose [data-source-line="${CSS.escape(saved.line)}"]`);
    scrollTo({top: block ? block.getBoundingClientRect().top + scrollY - saved.offset : saved.y, behavior: "instant"});
  };
  apply(); document.fonts?.ready.then(apply); setTimeout(apply, 400);  // again once fonts and late layout settle
  return true;
}
function trackPosition() {
  let pending = false;
  addEventListener("scroll", () => { if (!pending) { pending = true; setTimeout(() => { pending = false; savePosition(); }, 300); } }, {passive: true});
  addEventListener("pagehide", savePosition);
  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "hidden") savePosition(); });
}

/* Hovering another report in the sidebar fetches its text ahead of the click (served from cache afterwards). */
async function prefetchReport(href) {
  const base = new URL(href, location.href); base.hash = ""; base.search = "";
  try {
    const page = await (await fetch(base)).text();  // its index.html names the versioned data files
    for (const key of ["data-report-data", "data-narrative"]) {
      const match = page.match(new RegExp(`${key}="([^"]+)"`)); if (!match) continue;
      const link = document.createElement("link"); link.rel = "prefetch"; link.href = new URL(match[1], base).href; document.head.append(link);
    }
  } catch (_) {}
}

// The sidebar lists every report of the site, flat and sorted by title.
function reportLink(report) {
  const link = el("a", "report-list-link");
  link.append(el("span", null, report.title));
  link.href = report.href.startsWith("/") ? report.href : siteHref(report.href);  // the store UI's site.json gives absolute paths
  link.title = report.subtitle || report.title;
  if (report.current || (report.key !== undefined && report.key === reportKey)) link.setAttribute("aria-current", "page");
  else if (!BUNDLE && !report.plain) { link.addEventListener("pointerenter", () => prefetchReport(link.href), {once: true}); link.addEventListener("focus", () => prefetchReport(link.href), {once: true}); }
  return link;
}
/* Served by the store UI (/p/<project>/r/...): a small button back to the page the reader came from (an app page of the
   same site, by the referrer) or else to the project page. Locally built and shared sites have no such prefix: no button. */
function addBackLink(projectTitle) {
  const served = /^\/p\/[^/]+\/r\//.exec(location.pathname);
  if (BUNDLE || !served || $("#back-link")) return;
  let target = served[0].replace(/r\/$/, ""), label = `← ${projectTitle || "Project"}`;
  try {
    const from = new URL(document.referrer);
    if (from.origin === location.origin && (from.pathname === "/" || /^\/p\/[^/]+\/(?!r\/)/.test(from.pathname)) && from.href !== location.href) { target = from.pathname + from.search; label = "← Back"; }
  } catch (_) {}
  const link = el("a", "back-link", label); link.id = "back-link"; link.href = target; link.title = `Back to ${target}`;
  document.body.append(link);
}
async function loadReportList(data) {
  const {reports = [], title, brand, mark, groups} = await siteData;
  if (brand || title) $("#site-title").textContent = brand || title;
  if (mark) $(".brand span").textContent = mark;
  addBackLink(title);
  const addToggle = () => {  // phones: the rail is a bar; its lists open under a Browse button
    const rail = $(".rail"); if (!rail || $(".rail-toggle")) return;
    const toggle = el("button", "rail-toggle", "Browse"); toggle.type = "button"; toggle.setAttribute("aria-expanded", "false");
    toggle.onclick = () => toggle.setAttribute("aria-expanded", String(rail.classList.toggle("open")));
    $(".brand").after(toggle);
  };
  if (groups || reports.length > 1) addToggle();
  if (groups) {  // the store UI sends the sections of its own rail: Projects, Reports, Artifacts, Runs
    const note = $(".rail-note");
    groups.forEach(group => {
      const section = el("section", "report-list"), label = el("p", "rail-label", `${group.label} · ${group.items.length}`), nav = el("nav");
      nav.setAttribute("aria-label", group.label);
      group.items.forEach(item => nav.append(reportLink(item)));
      section.append(label, nav); note.before(section);
    });
    return;
  }
  const list = $("#report-list"); list.replaceChildren();
  if (reports.length < 2) return;
  [...reports].sort((a, b) => a.title.localeCompare(b.title)).forEach(report => list.append(reportLink(report)));
  $("#report-list-section").hidden = false;
}

const lightbox = $("#lightbox"), lightboxImage = $("#lightbox-image"), lightboxPlot = $("#lightbox-plot"), lightboxVideo = $("#lightbox-video");
let viewMedia = lightboxImage;  // the image or the video the zoom, pan, and wheel act on
lightboxImage.addEventListener("load", () => lightboxImage.classList.toggle("is-pixelated", lightboxImage.clientWidth > 2 * lightboxImage.naturalWidth));
async function showFigure(index) { active = (index + figures.length) % figures.length; const figure = figures[active]; const interactive = figure.type === "plotly", video = figure.type === "video"; lightboxImage.hidden = interactive || video; lightboxVideo.hidden = !video; $("#video-controls").hidden = !video; lightboxPlot.hidden = !interactive; $(".zoom").hidden = interactive; viewMedia = video ? lightboxVideo : lightboxImage; if (!video) stopVideo(); if (interactive) { lightboxImage.src = ""; await renderPlot(lightboxPlot, figure, true); } else { if (globalThis.Plotly) Plotly.purge(lightboxPlot); if (video) { lightboxImage.removeAttribute("src"); startVideo(figure); } else { lightboxImage.src = figure.src; lightboxImage.alt = figure.title; } resetView(); } $("#lightbox-title").textContent = figure.title; $("#lightbox-caption").textContent = figure.caption || ""; renderTrace(figure.trace || {}); }
// Every enlarged figure says where it came from: its file, or the input (name@vN//file) and the exact artifact version.
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
    ["Input", trace.input],
    ["Artifact", trace.artifact],
    ["Generated", trace.generated],
    ["Read in block", (trace.inputs || []).join(", ")],
    ["Report", trace.report],
  ].filter(([, value]) => value);
  const list = $("#lightbox-trace"); list.replaceChildren();
  rows.forEach(([label, value]) => {
    const row = el("div"), cell = el("dd");
    if (value.href && /^https?:\/\//.test(value.href)) { const link = el("a", null, value.text); link.href = value.href; link.target = "_blank"; link.rel = "noopener noreferrer"; cell.append(link); }
    else cell.textContent = value.text ?? value;
    row.append(el("dt", null, label), cell); list.append(row);
  });
  applyTraceState();
}
function openFigure(index) { showFigure(index); lightbox.showModal(); }
function applyView() { for (const media of [lightboxImage, lightboxVideo]) { media.style.setProperty("--zoom", zoom); media.style.setProperty("--pan-x", `${panX}px`); media.style.setProperty("--pan-y", `${panY}px`); } $("#zoom-label").textContent = `${Math.round(zoom * 100)}%`; }
let hoverPoint = null;
function imageCenter() { const media = viewMedia, parent = media.offsetParent; if (!parent) { const rect = media.getBoundingClientRect(); return {x: rect.left + rect.width / 2 - panX, y: rect.top + rect.height / 2 - panY}; } const box = parent.getBoundingClientRect(); return {x: box.left + media.offsetLeft + media.offsetWidth / 2, y: box.top + media.offsetTop + media.offsetHeight / 2}; }
// Zoom about `point` (client coordinates) so the pixel under it stays fixed; without a point, zoom about the current view.
function setZoom(value, point = null) { const next = Math.max(.5, Math.min(4, value)); if (next <= 1) { panX = 0; panY = 0; } else if (point) { const center = imageCenter(), ratio = next / zoom; panX = point.x - center.x - ratio * (point.x - center.x - panX); panY = point.y - center.y - ratio * (point.y - center.y - panY); } else { const ratio = next / zoom; panX *= ratio; panY *= ratio; } zoom = next; applyView(); }
function resetView() { zoom = 1; panX = 0; panY = 0; applyView(); }
lightbox.querySelector(".close").onclick = () => lightbox.close(); lightbox.querySelector(".previous").onclick = () => showFigure(active - 1); lightbox.querySelector(".next").onclick = () => showFigure(active + 1);
$("#lightbox-copy").innerHTML = COPY_ICON;
$("#lightbox-copy").onclick = event => { const figure = figures[active]; copyMarker(event.currentTarget, {line: figure.line, figure}); };
lightbox.querySelector(".zoom").onclick = event => { const action = event.target.dataset.zoom; if (action === "in") setZoom(zoom + .25); if (action === "out") setZoom(zoom - .25); if (action === "reset") resetView(); };
for (const media of [lightboxImage, lightboxVideo]) {
  // The wheel zooms; on a video, a sideways or Shift wheel scrubs frame by frame instead.
  media.addEventListener("wheel", event => { event.preventDefault(); if (media === lightboxVideo && (event.shiftKey || Math.abs(event.deltaX) > Math.abs(event.deltaY))) { scrubWheel(event.shiftKey ? event.deltaY : event.deltaX); return; } setZoom(zoom + (event.deltaY < 0 ? .15 : -.15), {x: event.clientX, y: event.clientY}); }, {passive: false});
  media.addEventListener("pointermove", event => { hoverPoint = {x: event.clientX, y: event.clientY}; });
  media.addEventListener("pointerleave", () => { hoverPoint = null; });
  media.draggable = false; media.addEventListener("dragstart", event => event.preventDefault());
  media.addEventListener("pointerdown", event => { if (zoom <= 1) return; event.preventDefault(); const startX = event.clientX - panX, startY = event.clientY - panY; let moved = false; media.setPointerCapture(event.pointerId); media.classList.add("is-panning"); const move = next => { moved = true; panX = next.clientX - startX; panY = next.clientY - startY; applyView(); }; const done = () => { media.classList.remove("is-panning"); media._panned = moved; media.removeEventListener("pointermove", move); media.removeEventListener("pointerup", done); media.removeEventListener("pointercancel", done); }; media.addEventListener("pointermove", move); media.addEventListener("pointerup", done); media.addEventListener("pointercancel", done); });
}

/* The lightbox video player: play/pause, a scrub bar, frame steps, speed. The frame rate comes from the
   builder (read from the MP4); otherwise it is the shortest frame interval seen while playing, 30 fps until then. */
let frameTime = 1 / 30, knownRate = false, scrubbing = false, wheelRest = 0;
const videoControls = $("#video-controls"), scrubBar = $("#video-scrub"), playButton = $("#video-play"), speedSelect = $("#video-speed");
function startVideo(figure) {
  if (lightboxVideo.dataset.src !== figure.src) { lightboxVideo.dataset.src = figure.src; lightboxVideo.src = figure.src; knownRate = figure.fps > 0; frameTime = knownRate ? 1 / figure.fps : 1 / 30; }
  lightboxVideo.playbackRate = Number(speedSelect.value);
  lightboxVideo.currentTime = 0; lightboxVideo.play().catch(() => {}); syncVideo();
}
function stopVideo() { lightboxVideo.pause(); if (lightboxVideo.dataset.src) { lightboxVideo.removeAttribute("src"); delete lightboxVideo.dataset.src; lightboxVideo.load(); } }
function syncVideo() {
  const duration = lightboxVideo.duration || 0, time = lightboxVideo.currentTime || 0;
  if (!scrubbing) { scrubBar.max = duration || 1; scrubBar.value = time; }
  scrubBar.style.setProperty("--progress", `${duration ? (time / duration) * 100 : 0}%`);
  const frame = Math.round(time / frameTime) + 1, frames = Math.max(1, Math.round(duration / frameTime));
  $("#video-time").textContent = `${clock(time)} / ${clock(duration)} · frame ${Math.min(frame, frames)}/${frames}`;
  playButton.textContent = lightboxVideo.paused ? "▶" : "❚❚"; playButton.setAttribute("aria-label", lightboxVideo.paused ? "Play" : "Pause");
}
function togglePlay() { if (lightboxVideo.paused) lightboxVideo.play().catch(() => {}); else lightboxVideo.pause(); }
function seekVideo(time) { const duration = lightboxVideo.duration || 0; lightboxVideo.currentTime = Math.min(Math.max(time, 0), Math.max(duration - frameTime / 2, 0)); syncVideo(); }
function stepFrame(count) { lightboxVideo.pause(); seekVideo((Math.round(lightboxVideo.currentTime / frameTime) + count) * frameTime + frameTime / 2); }
function scrubWheel(delta) { wheelRest += delta; const steps = Math.trunc(wheelRest / 30); if (steps) { wheelRest -= steps * 30; stepFrame(steps); } }
if ("requestVideoFrameCallback" in HTMLVideoElement.prototype) {
  let last = null;
  const measure = (_, meta) => { if (!knownRate && last !== null && !lightboxVideo.paused) { const delta = meta.mediaTime - last; if (delta > 0.002 && delta < frameTime) frameTime = delta; } last = meta.mediaTime; syncVideo(); lightboxVideo.requestVideoFrameCallback(measure); };
  lightboxVideo.requestVideoFrameCallback(measure);
  lightboxVideo.addEventListener("loadedmetadata", () => { last = null; lightboxVideo.requestVideoFrameCallback(measure); });
}
["timeupdate", "play", "pause", "loadedmetadata", "seeked", "ratechange"].forEach(type => lightboxVideo.addEventListener(type, syncVideo));
lightboxVideo.addEventListener("click", () => { if (lightboxVideo._panned) { lightboxVideo._panned = false; return; } if (zoom <= 1) togglePlay(); });
playButton.onclick = togglePlay;
$("#video-back").onclick = () => stepFrame(-1); $("#video-forward").onclick = () => stepFrame(1);
speedSelect.onchange = () => { lightboxVideo.playbackRate = Number(speedSelect.value); };
let resumeAfterScrub = false;
scrubBar.addEventListener("pointerdown", () => { scrubbing = true; resumeAfterScrub = !lightboxVideo.paused; lightboxVideo.pause(); });
scrubBar.addEventListener("input", () => { lightboxVideo.currentTime = Number(scrubBar.value); syncVideo(); });
scrubBar.addEventListener("change", () => { scrubbing = false; if (resumeAfterScrub) lightboxVideo.play().catch(() => {}); resumeAfterScrub = false; });
scrubBar.addEventListener("pointerup", () => scrubBar.blur());  // after a drag, the arrow keys browse figures again
lightbox.addEventListener("close", stopVideo);
document.addEventListener("keydown", event => { if (!lightbox.open) return; const onVideo = !lightboxVideo.hidden;
  if (onVideo && event.target === scrubBar && event.key.startsWith("Arrow")) return;  // the scrub bar's own keys
  if (onVideo && (event.key === "k" || (event.key === " " && !event.target.closest("select, input")))) { event.preventDefault(); togglePlay(); return; }
  if (onVideo && (event.key === "," || event.key === ".")) { stepFrame(event.key === "," ? -1 : 1); return; }
  if (onVideo && event.shiftKey && (event.key === "ArrowLeft" || event.key === "ArrowRight")) { seekVideo(lightboxVideo.currentTime + (event.key === "ArrowLeft" ? -1 : 1)); return; }
  if (event.key === "ArrowLeft") showFigure(active - 1); if (event.key === "ArrowRight") showFigure(active + 1); if (event.key === "+" || event.key === "=") setZoom(zoom + .25, hoverPoint); if (event.key === "-") setZoom(zoom - .25, hoverPoint); if (event.key === "i" && !event.metaKey && !event.ctrlKey) toggleTrace(); if (event.key === "Escape") lightbox.close(); });

function bindFiguresInDocumentOrder() {
  let offset = 0;
  document.querySelectorAll("#report-prose .figure-card, #report-prose img").forEach(node => {
    if (node.matches(".figure-card")) { hydrateFigure(node); return; }
    if (node.closest(".figure-card")) return;
    const image = node, line = image.closest("[data-source-line]")?.dataset.sourceLine || "unknown";
    const id = `markdown-line-${line}-${offset++}`;
    const index = figures.push({id, line, src: image.src, title: image.alt || `Figure from Markdown line ${line}`, caption: `Generated from report.md line ${line}`}) - 1;
    image.tabIndex = 0; image.setAttribute("role", "button"); image.onclick = () => openFigure(index); image.onkeydown = event => { if (event.key === "Enter" || event.key === " ") openFigure(index); }; const anchor = image.closest("[data-source-line]") || image.parentElement; anchor.classList.add("copy-anchor"); anchor.append(copyIcon({line, figure: figures[index]}));
  });
}

const selectionCopy = $("#selection-copy");
selectionCopy.innerHTML = COPY_ICON;
selectionCopy.onmousedown = event => event.preventDefault();  // keep the selection while clicking
selectionCopy.onclick = () => { if (pendingSelection) copyMarker(selectionCopy, pendingSelection); };
function bindSelectionCopy() {
  const report = $("main");
  const inspect = () => {
    const selection = window.getSelection(), hide = () => { selectionCopy.hidden = true; };
    if (!selection?.rangeCount || !selection.toString().trim()) return hide();
    const range = selection.getRangeAt(0), start = range.startContainer;
    const node = start.nodeType === Node.TEXT_NODE ? start.parentElement : start;
    if (!node || !report.contains(node)) return hide();
    const card = node.closest(".figure-card"), block = node.closest("[data-source-line]");
    const rect = range.getBoundingClientRect();
    pendingSelection = {line: card?.dataset.line || block?.dataset.sourceLine || "", quote: selectionQuote(range).slice(0, 1000)};
    selectionCopy.style.left = `${Math.max(8, Math.min(window.innerWidth - 40, rect.right + 6))}px`;
    selectionCopy.style.top = `${Math.max(8, Math.min(window.innerHeight - 40, rect.bottom + 6))}px`;
    selectionCopy.hidden = false;
  };
  report.addEventListener("mouseup", () => setTimeout(inspect));
  report.addEventListener("touchend", () => setTimeout(inspect));
  report.addEventListener("keyup", event => { if (event.shiftKey) setTimeout(inspect); });
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
async function copyText(text) {
  try { await navigator.clipboard.writeText(text); return true; } catch (_) {}
  const area = el("textarea"); area.value = text; area.setAttribute("readonly", ""); area.style.position = "fixed"; area.style.opacity = "0"; document.body.append(area); area.select();
  const copied = document.execCommand("copy"); area.remove(); return copied;
}
function flashTip(button, text) {
  button.dataset.tooltip = text; button.classList.add("show-tip"); clearTimeout(button._tipTimer);
  button._tipTimer = setTimeout(() => { button.dataset.tooltip = button.getAttribute("aria-label"); button.classList.remove("show-tip"); }, 1600);
}
