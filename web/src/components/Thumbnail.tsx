import type { MouseEvent, ReactNode } from "react";

/** A link that opens the lightbox on a plain click; a middle or modified click still opens the file in a new tab. */
function opener(onOpen: () => void) {
  return (event: MouseEvent<HTMLAnchorElement>) => {
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    onOpen();
  };
}

const IMAGE_ICON = (
  <svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3.5" y="4.5" width="17" height="15" rx="2" /><circle cx="9" cy="10" r="1.6" /><path d="m4 18 5-5 3.5 3.5L15 14l5 5" /></svg>
);

/** A small square preview of an image file that opens it in the lightbox. Without `src` (a file too large to load
 *  for a thumbnail) it shows an image icon. The image loads lazily and is sized by CSS. */
export function Thumbnail({ src, href, label, onOpen }: { src?: string; href: string; label: string; onOpen: () => void }) {
  return (
    <a className="thumb" href={href} aria-label={label} title={label} onClick={opener(onOpen)}>
      {src ? <img src={src} alt="" loading="lazy" decoding="async" draggable={false} /> : IMAGE_ICON}
    </a>
  );
}

/** Holds the place of a thumbnail in a row without one, so the names of a list line up. */
export function ThumbnailSpacer() {
  return <span className="thumb thumb-spacer" aria-hidden="true" />;
}

/** The figure cards of a version's images, as in a report. */
export function FigureGrid({ children }: { children: ReactNode }) {
  return <div className="figure-grid app-figures">{children}</div>;
}

/** A figure card: the image (lazy) and its path; a click opens the lightbox. */
export function FigureCard({ src, href, title, onOpen }: { src: string; href: string; title: string; onOpen: () => void }) {
  return (
    <a className="figure-card" href={href} onClick={opener(onOpen)} aria-label={`Open ${title}`}>
      <img src={src} alt={title} loading="lazy" decoding="async" />
      <div className="figure-copy"><p className="mono">{title}</p></div>
    </a>
  );
}
