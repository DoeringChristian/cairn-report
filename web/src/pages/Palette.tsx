import { useState, type ReactNode } from "react";
import {
  AliasPills, Button, Card, CodeBlock, CodeChips, CopyButton, Crumbs, DataTable, ErrorNote, Field, FigureCard, FigureGrid, Hero, HeroLine,
  Hint, IconButton, JsonBlock, KeyValueTable, LinkButton, Loading, MetricCard, MetricGrid, Note, Panel, Pill, RefRow, RefRows, RowActions,
  SearchInput, Section, Select, StatusPill, SubHeading, Tabs, TextBlock, Thumbnail, ThumbnailSpacer, ToggleGroup, Toolbar, useLightbox,
  type GalleryItem,
} from "../components";

/** A sample figure as an SVG data URI (no request): a bar chart of `bars` values in a `w`x`h` frame. */
function figure(title: string, w: number, h: number, bars: number[], color = "#176b5b") {
  const pad = Math.round(Math.min(w, h) * 0.12), bw = (w - 2 * pad) / bars.length, top = Math.max(...bars);
  const rects = bars.map((v, i) => {
    const bh = (v / top) * (h - 2.4 * pad);
    return `<rect x="${(pad + i * bw + bw * 0.12).toFixed(1)}" y="${(h - pad - bh).toFixed(1)}" width="${(bw * 0.76).toFixed(1)}" height="${bh.toFixed(1)}" fill="${color}"/>`;
  }).join("");
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}"><rect width="${w}" height="${h}" fill="#fffef9"/>`
    + `<text x="${pad}" y="${pad * 0.8}" font-family="Georgia, serif" font-size="${Math.round(pad * 0.45)}" fill="#202621">${title}</text>`
    + `<line x1="${pad}" y1="${h - pad}" x2="${w - pad}" y2="${h - pad}" stroke="#697169" stroke-width="2"/>${rects}</svg>`;
  return `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
}

const IMAGES: GalleryItem[] = [
  { title: "figures/loss-histogram.png", src: figure("Loss per batch", 1600, 1000, [3, 7, 12, 9, 5, 2, 1]) },
  { title: "figures/accuracy-by-depth.png", src: figure("Accuracy by depth", 1800, 700, [52, 61, 70, 76, 79, 81, 82, 82], "#486f91") },
  { title: "figures/residuals.svg", src: figure("Residuals", 900, 1200, [2, 5, 9, 5, 2], "#b56231") },
  { title: "samples/denoised-0042.webp", src: figure("Sample 42", 1200, 1200, [4, 8, 6, 9, 3, 7], "#9a5b1e") },
].map(item => ({ ...item, download: item.src, caption: "sample image", copy: { text: `study:datasets/toy@v2//${item.title}`, label: "Copy file reference", done: "Reference copied" } }));

const VERSIONS = [
  { version: "v3", label: "tuned", status: "running", created: "2026-10-16 09:12 UTC", aliases: [], host: "kuma-g042" },
  { version: "v2", label: "second", status: "completed", created: "2026-10-15 17:40 UTC", aliases: ["latest", "best"], host: "lab-laptop" },
  { version: "v1", label: "first", status: "deleted", created: "2026-10-14 08:03 UTC", aliases: [], host: "lab-laptop" },
  { version: "01JA7Q2W3E…", label: "draft", status: "pending", created: "2026-10-16 09:30 UTC", aliases: [], host: "lab-laptop" },
];

/** One entry of the palette: the component's name, what it is for, and live examples. */
function Specimen({ name, note, children }: { name: string; note: ReactNode; children: ReactNode }) {
  return (
    <div className="palette-specimen">
      <p className="palette-name"><code>{name}</code> <span className="muted">{note}</span></p>
      <div className="palette-demo">{children}</div>
    </div>
  );
}

/** Every building block of the UI with realistic sample content in its variants: the visual reference (`/palette`). */
export function PalettePage() {
  document.title = "Components - Cairn";
  const [select, setSelect] = useState("v2");
  const [tab, setTab] = useState<"reports" | "artifacts" | "runs">("artifacts");
  const [toggle, setToggle] = useState<"runs" | "data">("runs");
  const [query, setQuery] = useState("");
  const [token, setToken] = useState("");
  const { open, lightbox } = useLightbox(IMAGES);
  return (
    <main className="palette">
      <Hero eyebrow={<Crumbs items={[{ label: "Projects", to: "/" }, { label: "components" }]} />} title="Components"
        subtitle="The UI's building blocks, each in its variants. Pages compose these; new UI starts here (web/src/components)." status="palette">
        <HeroLine>Hero with <code>Crumbs</code>, a subtitle, a status and a <code>HeroLine</code>: <AliasPills aliases={["stable", "best"]} /></HeroLine>
      </Hero>

      <Section title="Buttons">
        <Specimen name="Button" note="an action; small in table rows, primary for the one main action, disabled while busy">
          <Button>Expand all (3 levels)</Button> <Button small>Preview</Button> <Button primary>Open report v2</Button> <Button disabled>Busy</Button>
        </Specimen>
        <Specimen name="LinkButton" note="a link that looks like a button: href outside the app, to for an app route">
          <LinkButton href="#download" small>Download</LinkButton> <LinkButton to="/" small>Projects</LinkButton> <LinkButton href="#zip" primary>Open report v3</LinkButton>
        </Specimen>
        <Specimen name="ToggleGroup" note="pick one of a few views">
          <ToggleGroup label="Runs in the graph" value={toggle} onChange={setToggle} options={[{ value: "runs", label: "Show runs" }, { value: "data", label: "Data only" }]} />
        </Specimen>
        <Specimen name="CopyButton" note="copies a reference; a tooltip confirms">
          <span className="palette-row"><CopyButton text="study:datasets/toy@v2" label="Copy exact version" done="Version copied" /> <code>study:datasets/toy@v2</code></span>
        </Specimen>
        <Specimen name="RowActions" note="the buttons at the end of a table row">
          <RowActions><Button small>Preview</Button><LinkButton small href="#download">Download</LinkButton><CopyButton text="study:toy-fit@v1//result.json" label="Copy file reference" done="Reference copied" /></RowActions>
        </Specimen>
        <Specimen name="IconButton" note="the lightbox's round icon buttons (on its dark backdrop)">
          <div className="palette-dark"><IconButton label="Previous image">←</IconButton><IconButton label="Next image">→</IconButton><IconButton label="Close">×</IconButton></div>
        </Specimen>
      </Section>

      <Section title="Pills">
        <Specimen name="StatusPill" note="a version's status">
          <span className="palette-pills">{["completed", "running", "failed", "interrupted", "pending", "deleted"].map(s => <StatusPill key={s} status={s} />)}</span>
        </Specimen>
        <Specimen name="Pill · AliasPills" note="tones: default, tentative, pending, deleted, alias">
          <span className="palette-pills"><Pill>active</Pill><Pill tone="tentative">tentative</Pill><Pill tone="pending">pending</Pill><Pill tone="deleted">deleted</Pill><AliasPills aliases={["stable", "best", "baseline"]} /></span>
        </Specimen>
      </Section>

      <Section title="Section heading" actions={<Select label="Palette version" value={select} onChange={setSelect} choices={VERSIONS.map(v => ({ value: v.version, label: `${v.version} (${v.label})`, note: v.status === "completed" ? undefined : v.status }))} />}>
        <Specimen name="Section · SectionHead" note="a serif heading with actions on its right (here a Select)">
          <SubHeading>SubHeading</SubHeading>
          <Hint>A <code>SubHeading</code> splits a section, as the Run panel's Metrics, System and Output.</Hint>
        </Specimen>
      </Section>

      <Section title="Panels and cards">
        <Specimen name="Panel · RefRows · RefRow" note="copyable references">
          <Panel title="Reference">
            <RefRows>
              <RefRow name="Version" value="study:datasets/toy@v2" copy={{ text: "study:datasets/toy@v2", label: "Copy exact version", done: "Version copied" }} />
              <RefRow name="Pull" value="cairn pull study:datasets/toy@v2" copy={{ text: "cairn pull study:datasets/toy@v2", label: "Copy pull command", done: "Command copied" }} />
              <RefRow name="Files" value={<LinkButton small href="#zip">Download zip</LinkButton>} extra="9.3 KB" />
              <RefRow name="Digest" mono value="sha256:503f9d2f1796db5915385401a858d393ac5ad63e416ef394b7ee0e521c4e0c16" copy={{ text: "sha256:503f…", label: "Copy digest", done: "Digest copied" }} />
              <RefRow name="datasets/toy" value="v1" extra={<StatusPill status="deleted" />} copy={{ text: "study:datasets/toy@v1", label: "Copy exact version" }} />
            </RefRows>
          </Panel>
          <Panel title="Consumers"><Hint>No consumers yet.</Hint></Panel>
        </Specimen>
        <Specimen name="MetricGrid · MetricCard" note="a big value, compact for a long one">
          <MetricGrid>
            <MetricCard label="Files" value={5} note="9.3 KB" />
            <MetricCard label="Created" compact value={"2026‑10‑16 07:07 UTC"} note="on lab-laptop" />
            <MetricCard label="Versions" value={2} note="label second" />
          </MetricGrid>
        </Specimen>
        <Specimen name="Card" note="a frame with an optional bar of hint and controls (the lineage graph's)">
          <Card bar={<><span className="muted">Inputs on the left, consumers on the right.</span><Button small>Expand all</Button></>}>
            <Hint>The card's content.</Hint>
          </Card>
        </Specimen>
      </Section>

      <Section title="Tables">
        <Specimen name="DataTable" note="the current row selected, tombstones greyed, pending in italics">
          <DataTable head={["Version", "Label", "Status", "Created", "Aliases", "Source host"]}>
            {VERSIONS.map(v => (
              <tr key={v.version} className={v.version === select ? "selected" : v.status === "deleted" ? "tombstone" : v.status === "pending" ? "pending" : undefined}>
                <td className="nowrap"><a href="#v"><strong>{v.version}</strong></a></td>
                <td>{v.label}</td>
                <td><StatusPill status={v.status} /></td>
                <td className="muted nowrap">{v.created}</td>
                <td><AliasPills aliases={v.aliases.filter(a => a !== "latest")} /></td>
                <td className="muted">{v.host}</td>
              </tr>
            ))}
          </DataTable>
        </Specimen>
        <Specimen name="KeyValueTable" note="facts (bold keys, mono values) or a config (code)">
          <KeyValueTable rows={[["Host", "kuma-g042 (32 CPUs), NVIDIA H100"], ["Command", "python train.py model.depth=4"], ["Slurm job", "4818812 (array task 3) on h100"]]} />
          <KeyValueTable head={["Key", "Value"]} code rows={[["lr", "0.1"], ["epochs", "3"], ["model.depth", "4"]]} />
        </Specimen>
        <Specimen name="Thumbnail · ThumbnailSpacer" note="image files in a file list open the lightbox; a spacer keeps names aligned; no src: an icon">
          <DataTable head={["File", "Size", ""]}>
            {[...IMAGES.slice(0, 2).map((item, i) => ({ path: item.title, size: "8.6 KB", at: i as number | null, src: item.src as string | undefined })),
              { path: "figures/huge-scan.png", size: "61.2 MB", at: 2, src: undefined }, { path: "data/summary.json", size: "17 B", at: null, src: undefined }].map(f => (
              <tr key={f.path}>
                <td className="mono wrap-any"><span className="file-name">
                  {f.at !== null ? <Thumbnail src={f.src} href="#file" label={`Open ${f.path} in the viewer`} onOpen={() => open(f.at!)} /> : <ThumbnailSpacer />}
                  <a href="#file">{f.path}</a>
                </span></td>
                <td className="muted nowrap">{f.size}</td>
                <td><RowActions><Button small>Preview</Button><LinkButton small href="#download">Download</LinkButton></RowActions></td>
              </tr>
            ))}
          </DataTable>
        </Specimen>
      </Section>

      <Section title="Images">
        <Specimen name="FigureGrid · FigureCard" note="a version's first images; a click opens the lightbox">
          <FigureGrid>
            {IMAGES.map((item, i) => <FigureCard key={item.title} src={item.src} href={item.src} title={item.title} onOpen={() => open(i)} />)}
          </FigureGrid>
        </Specimen>
        <Specimen name="Lightbox · useLightbox" note="one gallery: ← → and swipe, + − 0 the wheel and a pinch to zoom, drag to pan, Esc to close">
          <Button onClick={() => open(0)}>Open the gallery</Button>
        </Specimen>
      </Section>

      <Section title="Controls">
        <Specimen name="Tabs" note="with counts; arrows move between them">
          <Tabs label="Kinds" value={tab} onChange={setTab} tabs={[{ value: "reports", label: "Reports", count: 3 }, { value: "artifacts", label: "Artifacts", count: 12 }, { value: "runs", label: "Runs", count: 48 }]} />
        </Specimen>
        <Specimen name="Toolbar · SearchInput · Select" note="the select is our own listbox (keyboard: arrows, Home, End, typing)">
          <Toolbar>
            <SearchInput label="Search" placeholder="Search names, titles, status, aliases" value={query} onChange={setQuery} />
            <Select label="Sort" value="updated" onChange={() => undefined} choices={[{ value: "updated", label: "Sort: updated" }, { value: "name", label: "Sort: name" }]} />
          </Toolbar>
        </Specimen>
        <Specimen name="Field" note="a labelled input (the login form)">
          <form className="login-form palette-form" onSubmit={event => event.preventDefault()}>
            <Field label="Token" type="password" autoComplete="off" value={token} onChange={event => setToken(event.target.value)} />
          </form>
        </Specimen>
      </Section>

      <Section title="States">
        <Specimen name="Loading" note="while data loads"><Loading what="Loading the run" /></Specimen>
        <Specimen name="Hint" note="an empty list or a short explanation"><Hint>No reports yet.</Hint></Specimen>
        <Specimen name="ErrorNote" note="an API error"><ErrorNote error={Object.assign(new Error("no artifact 'models/teacher' in project 'study'"), { status: 404 })} /></Specimen>
        <Specimen name="Note" note="tones: info, warning, pending, deleted">
          <Note title="Note"><p>An admonition in the accent.</p></Note>
          <Note tone="warning" title="Warning"><p>The remote refused the version: quota exceeded.</p></Note>
          <Note tone="pending" title="Pending"><p>This repo has this version, but its remote does not have it yet: its uid <code>01JA7Q2W3E</code> names it.</p></Note>
          <Note tone="deleted" title="Deleted"><p>v1 was deleted 2026-10-15 10:00 UTC by owner: superseded.</p></Note>
        </Specimen>
      </Section>

      <Section title="Code">
        <Specimen name="JsonBlock · CodeBlock" note="metadata, any value as JSON">
          <JsonBlock value={{ kind: "run", config: { lr: 0.1, model: { depth: 4 } }, overrides: ["model.depth=4"] }} />
          <CodeBlock>cairn pull study:datasets/toy@v2</CodeBlock>
        </Specimen>
        <Specimen name="TextBlock" note="raw text that scrolls in its box (a log tail; dark inside a table)">
          <TextBlock>{"epoch 1  loss 0.912  acc 0.41\nepoch 2  loss 0.507  acc 0.68\nepoch 3  loss 0.233  acc 0.88\ntraining done"}</TextBlock>
        </Specimen>
        <Specimen name="CodeChips" note="short code in a line (a run's overrides)">
          <Hint>Overrides: <CodeChips items={["model.depth=4", "optim.lr=0.1", "seed=7"]} /></Hint>
        </Specimen>
      </Section>
      {lightbox}
    </main>
  );
}
