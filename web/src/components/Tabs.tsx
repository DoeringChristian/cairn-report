/** Tabs in the report theme: bold labels, the current one underlined in the accent, an optional count. */
export function Tabs<T extends string>({ tabs, value, onChange, label }: { tabs: { value: T; label: string; count?: number }[]; value: T; onChange: (value: T) => void; label?: string }) {
  const move = (event: React.KeyboardEvent, index: number) => {
    const next = event.key === "ArrowRight" ? index + 1 : event.key === "ArrowLeft" ? index - 1 : event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 : null;
    if (next === null) return;
    event.preventDefault();
    const tab = tabs[(next + tabs.length) % tabs.length];
    onChange(tab.value);
    (event.currentTarget.parentElement?.children[(next + tabs.length) % tabs.length] as HTMLElement | undefined)?.focus();
  };
  return (
    <div className="tabs" role="tablist" aria-label={label}>
      {tabs.map((tab, index) => (
        <button key={tab.value} type="button" role="tab" aria-selected={tab.value === value} tabIndex={tab.value === value ? 0 : -1}
          onClick={() => onChange(tab.value)} onKeyDown={event => move(event, index)}>
          {tab.label}{tab.count !== undefined && <> <span>{tab.count}</span></>}
        </button>
      ))}
    </div>
  );
}
