import { useCallback, useEffect, useRef, useState, type CSSProperties, type ReactNode } from "react";
import { IconButton } from "./Button";
import { CopyButton } from "./CopyButton";

/** One image of a gallery: its address, the title the caption shows (a file path), an optional extra caption,
 *  a download address and a reference to copy. */
export type GalleryItem = { src: string; title: string; caption?: ReactNode; download?: string; copy?: { text: string; label: string; done?: string } };

const MIN = 0.5, MAX = 4, STEP = 0.25, WHEEL = 0.15, SWIPE = 60;
type View = { zoom: number; x: number; y: number };
const RESET: View = { zoom: 1, x: 0, y: 0 };
const FOCUSABLE = "button:not([disabled]), a[href], input, select, [tabindex]:not([tabindex='-1'])";

/** The reports' lightbox for one ordered gallery: prev/next (buttons, arrow keys, a swipe), zoom (buttons, `+`/`-`,
 *  the wheel, a pinch, a double click) about the pointer, panning a zoomed image (mouse or touch), reset (button, `0`).
 *  Escape or the close button closes it. While open it is a modal dialog: focus stays inside and goes back to where
 *  it was on close, and the page behind does not scroll. `index` null means closed. */
export function Lightbox({ items, index, onIndex, onClose }: { items: GalleryItem[]; index: number | null; onIndex: (index: number) => void; onClose: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const stage = useRef<HTMLDivElement>(null);
  const image = useRef<HTMLImageElement>(null);
  const [view, setView] = useState<View>(RESET);
  const current = useRef<View>(RESET);
  const [state, setState] = useState<"loading" | "ready" | "failed">("loading");
  const [pixelated, setPixelated] = useState(false);
  const [panning, setPanning] = useState(false);
  const hover = useRef<{ x: number; y: number } | null>(null);
  const pointers = useRef(new Map<number, { x: number; y: number }>());
  const gesture = useRef<{ kind: "pan"; startX: number; startY: number } | { kind: "pinch"; distance: number; zoom: number } | { kind: "swipe"; x: number; y: number } | null>(null);
  const open = index !== null && items.length > 0;
  const at = open ? ((index % items.length) + items.length) % items.length : 0;
  const item = open ? items[at] : null;
  const count = items.length;

  const apply = useCallback((next: View) => { current.current = next; setView(next); }, []);
  /** Zoom to `value`, about `point` (client coordinates) so the pixel under it stays put; without one, about the view. */
  const zoomTo = useCallback((value: number, point: { x: number; y: number } | null = null) => {
    const { zoom, x, y } = current.current;
    const next = Math.max(MIN, Math.min(MAX, value));
    if (next <= 1) return apply({ zoom: next, x: 0, y: 0 });
    const ratio = next / zoom;
    const rect = image.current?.getBoundingClientRect();
    if (point && rect) {
      const cx = rect.left + rect.width / 2 - x, cy = rect.top + rect.height / 2 - y;  // the untransformed centre
      apply({ zoom: next, x: point.x - cx - ratio * (point.x - cx - x), y: point.y - cy - ratio * (point.y - cy - y) });
    } else apply({ zoom: next, x: x * ratio, y: y * ratio });
  }, [apply]);
  const go = useCallback((step: number) => { if (count > 1) onIndex((at + step + count) % count); }, [at, count, onIndex]);

  // open and close the modal dialog; lock the page's scroll; give focus back on close
  useEffect(() => {
    const box = dialog.current;
    if (!open || !box) return;
    const before = document.activeElement as HTMLElement | null;
    const html = document.documentElement, overflow = html.style.overflow;
    html.style.overflow = "hidden";
    if (!box.open) box.showModal();
    return () => {
      if (box.open) box.close();
      html.style.overflow = overflow;
      if (before && before.isConnected) before.focus({ preventScroll: true });
    };
  }, [open]);

  // a new image: back to 100%, centred, in the same render that shows it
  const [shown, setShown] = useState<string | undefined>(undefined);
  if (item?.src !== shown) {
    setShown(item?.src);
    current.current = RESET;
    setView(RESET); setState("loading"); setPixelated(false);
  }

  // the wheel zooms (a native listener: React's is passive and cannot keep the page from scrolling)
  useEffect(() => {
    const box = stage.current;
    if (!open || !box) return;
    const wheel = (event: WheelEvent) => {
      event.preventDefault();
      if (event.deltaY === 0) return;
      zoomTo(current.current.zoom + (event.deltaY < 0 ? WHEEL : -WHEEL), { x: event.clientX, y: event.clientY });
    };
    box.addEventListener("wheel", wheel, { passive: false });
    return () => box.removeEventListener("wheel", wheel);
  }, [open, zoomTo]);

  const onKeyDown = (event: React.KeyboardEvent<HTMLDialogElement>) => {
    if (event.metaKey || event.ctrlKey || event.altKey) return;
    const key = event.key;
    if (key === "ArrowLeft" || key === "ArrowRight") { event.preventDefault(); go(key === "ArrowLeft" ? -1 : 1); }
    else if (key === "+" || key === "=") { event.preventDefault(); zoomTo(current.current.zoom + STEP, hover.current); }
    else if (key === "-" || key === "_") { event.preventDefault(); zoomTo(current.current.zoom - STEP, hover.current); }
    else if (key === "0") { event.preventDefault(); apply(RESET); }
    else if (key === "Tab") {  // keep focus inside the dialog
      const all = [...(dialog.current?.querySelectorAll<HTMLElement>(FOCUSABLE) ?? [])].filter(e => e.offsetParent !== null || e === document.activeElement);
      if (!all.length) return;
      const first = all[0], last = all[all.length - 1];
      if (event.shiftKey && (document.activeElement === first || !dialog.current?.contains(document.activeElement))) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    }
  };

  const onPointerDown = (event: React.PointerEvent<HTMLImageElement>) => {
    if (event.pointerType === "mouse" && event.button !== 0) return;
    event.preventDefault();
    event.currentTarget.setPointerCapture(event.pointerId);
    pointers.current.set(event.pointerId, { x: event.clientX, y: event.clientY });
    const points = [...pointers.current.values()];
    if (points.length === 2) {
      gesture.current = { kind: "pinch", distance: Math.hypot(points[0].x - points[1].x, points[0].y - points[1].y) || 1, zoom: current.current.zoom };
    } else if (points.length === 1) {
      const { zoom, x, y } = current.current;
      gesture.current = zoom > 1 ? { kind: "pan", startX: event.clientX - x, startY: event.clientY - y } : event.pointerType !== "mouse" ? { kind: "swipe", x: event.clientX, y: event.clientY } : null;
      setPanning(zoom > 1);
    }
  };
  const onPointerMove = (event: React.PointerEvent<HTMLImageElement>) => {
    hover.current = { x: event.clientX, y: event.clientY };
    if (!pointers.current.has(event.pointerId)) return;
    pointers.current.set(event.pointerId, { x: event.clientX, y: event.clientY });
    const g = gesture.current;
    if (g?.kind === "pan") apply({ ...current.current, x: event.clientX - g.startX, y: event.clientY - g.startY });
    else if (g?.kind === "pinch") {
      const [a, b] = [...pointers.current.values()];
      if (!a || !b) return;
      zoomTo(g.zoom * Math.hypot(a.x - b.x, a.y - b.y) / g.distance, { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 });
    }
  };
  const onPointerEnd = (event: React.PointerEvent<HTMLImageElement>) => {
    if (!pointers.current.delete(event.pointerId)) return;
    const g = gesture.current;
    if (g?.kind === "swipe" && event.type === "pointerup") {
      const dx = event.clientX - g.x, dy = event.clientY - g.y;
      if (Math.abs(dx) > SWIPE && Math.abs(dx) > 1.5 * Math.abs(dy)) go(dx < 0 ? 1 : -1);
    }
    const rest = [...pointers.current.values()];
    if (rest.length === 1 && current.current.zoom > 1) {  // a pinch ended with one finger down: it pans on from here
      gesture.current = { kind: "pan", startX: rest[0].x - current.current.x, startY: rest[0].y - current.current.y };
    } else if (!rest.length) { gesture.current = null; setPanning(false); }
  };

  const style = { "--zoom": view.zoom, "--pan-x": `${view.x}px`, "--pan-y": `${view.y}px` } as CSSProperties;
  return (
    <dialog ref={dialog} className="lightbox app-lightbox" aria-label={item ? `Image ${at + 1} of ${count}: ${item.title}` : "Image viewer"}
      onCancel={event => { event.preventDefault(); onClose(); }} onClose={() => { if (open) onClose(); /* closed by the browser itself */ }} onKeyDown={onKeyDown}>
      {item && (
        <>
          <IconButton className="close" label="Close (Esc)" onClick={onClose}>×</IconButton>
          {count > 1 && <IconButton className="previous" label="Previous image (←)" onClick={() => go(-1)}>←</IconButton>}
          <div className="stage" ref={stage}>
            <img
              key={item.src} ref={image} src={item.src} alt={item.title} draggable={false}
              className={[pixelated && "is-pixelated", panning && "is-panning", state !== "ready" && "is-loading"].filter(Boolean).join(" ") || undefined}
              style={style}
              onLoad={event => { const e = event.currentTarget; setState("ready"); setPixelated(e.clientWidth > 2 * e.naturalWidth); }}
              onError={() => setState("failed")}
              onPointerDown={onPointerDown} onPointerMove={onPointerMove} onPointerUp={onPointerEnd} onPointerCancel={onPointerEnd}
              onPointerLeave={() => { hover.current = null; }}
              onDoubleClick={event => (current.current.zoom > 1 ? apply(RESET) : zoomTo(2, { x: event.clientX, y: event.clientY }))}
            />
            {state !== "ready" && <p className="lightbox-status" role="status">{state === "failed" ? "The image could not be loaded." : "Loading…"}</p>}
            <div className="lightbox-copy">
              <div className="lightbox-heading"><h3 className="lightbox-path">{item.title}</h3></div>
              <p className="lightbox-meta">
                {count > 1 && <span>{at + 1} of {count}</span>}
                {item.caption && <span>{item.caption}</span>}
                {item.download && <a className="lightbox-download" href={item.download} download>Download</a>}
                {item.copy && <CopyButton className="lightbox-ref" text={item.copy.text} label={item.copy.label} done={item.copy.done} />}
              </p>
            </div>
          </div>
          {count > 1 && <IconButton className="next" label="Next image (→)" onClick={() => go(1)}>→</IconButton>}
          <div className="zoom" role="group" aria-label="Zoom">
            <button type="button" aria-label="Zoom out (−)" title="Zoom out (−)" disabled={view.zoom <= MIN} onClick={() => zoomTo(current.current.zoom - STEP)}>−</button>
            <span aria-live="polite">{Math.round(view.zoom * 100)}%</span>
            <button type="button" aria-label="Zoom in (+)" title="Zoom in (+)" disabled={view.zoom >= MAX} onClick={() => zoomTo(current.current.zoom + STEP)}>+</button>
            <button type="button" title="Reset zoom (0)" onClick={() => apply(RESET)}>Reset</button>
          </div>
        </>
      )}
    </dialog>
  );
}

/** A gallery and its lightbox: `open(i)` shows image `i`; render `lightbox` once anywhere in the page. */
export function useLightbox(items: GalleryItem[]) {
  const [index, setIndex] = useState<number | null>(null);
  const close = useCallback(() => setIndex(null), []);
  return { open: setIndex as (index: number) => void, lightbox: <Lightbox items={items} index={index} onIndex={setIndex} onClose={close} /> };
}
