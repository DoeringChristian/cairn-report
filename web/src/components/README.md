# Components

The UI's palette: every building block the pages repeat. **Pages compose these; new UI starts here.** If a page needs
something that is not here, add it here (with a clear props API and a specimen on the palette page), then use it.
No one-off markup or styles in pages.

The look is the reports' own: `src/cairn/reports/template/report.css` and `report-extra.css`, and `web/src/app.css`
for what those have no class for, built from their variables (`--paper`, `--ink`, `--muted`, `--line`, `--card`,
`--accent`, `--accent-soft`, `--warn`, `--radius`, `--shadow`) alone. No new colours, no dark theme of its own.

The live reference is the palette page, `/palette` (the rail footer's "Components" link): every component in its
variants with sample content, in light, dark and at phone width.

| File | Components | For |
| --- | --- | --- |
| `Button.tsx` | `Button` (`small`, `primary`), `LinkButton` (`href` or `to`), `IconButton`, `ToggleGroup`, `RowActions` | actions, links that look like buttons, the lightbox's round buttons, a view switch, a table row's buttons |
| `CopyButton.tsx` | `CopyButton`, `COPY_ICON` | copying a reference, with the reports' tooltip |
| `Pill.tsx` | `Pill` (`tone`), `StatusPill`, `AliasPills` | status, aliases, pending and deleted marks |
| `Hero.tsx` | `Hero`, `Crumbs`, `HeroLine` | the page header, its breadcrumbs and a line of facts |
| `Section.tsx` | `Section`, `SectionHead`, `SubHeading`, `Toolbar`, `SearchInput` | headed sections (with actions), sub-headings, search and sort rows |
| `Card.tsx` | `Card` (with a `bar`), `Panel` | a framed area; a titled list (Reference, Inputs, Consumers) |
| `RefRows.tsx` | `RefRows`, `RefRow` | the copyable rows of a `Panel` |
| `Metric.tsx` | `MetricGrid`, `MetricCard` | big numbers |
| `Table.tsx` | `DataTable`, `KeyValueTable` | tables in the report theme (sideways scroll on a phone) |
| `Select.tsx` | `Select` | our own listbox instead of the system menu |
| `Tabs.tsx` | `Tabs` | tabs with counts |
| `States.tsx` | `Loading`, `ErrorNote`, `Hint`, `Note` (`tone`) | loading, errors, empty lists, admonitions |
| `Code.tsx` | `CodeBlock`, `JsonBlock`, `TextBlock`, `CodeChips` | code, JSON, raw text and logs, code chips |
| `Field.tsx` | `Field` | a labelled input |
| `Thumbnail.tsx` | `Thumbnail`, `ThumbnailSpacer`, `FigureGrid`, `FigureCard` | image files in lists and figure cards; they open the lightbox |
| `Lightbox.tsx` | `Lightbox`, `useLightbox`, `GalleryItem` | the reports' image viewer for one gallery |

Import from the index: `import { Button, DataTable } from "../components";`.

Every image shown in the UI opens the same lightbox: build the gallery (`GalleryItem[]`, in display order), call
`useLightbox(items)`, render its `lightbox` once and call `open(index)`. Load images lazily (`loading="lazy"`);
the file endpoints serve no smaller sizes, so thumbnails are the file itself sized by CSS (skip the image above
a size limit and the thumbnail shows an icon).
