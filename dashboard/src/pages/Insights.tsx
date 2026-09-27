// DT-34: Insights, patterns over time. A range picker (today, 7 days, 30 days, or any span up to 92 days) and four
// tabs: Overview (DT-34), Apps and Devices (DT-55), Focus and Sleep (DT-56), Food and Calendar (DT-57). The tab, the
// range and an open app's detail live in the address (?tab=apps&range=7d&app=YouTube), so a link or a reload opens
// the same view, and switching tabs keeps the range.
import { type FormEvent, useEffect, useId, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router";
import ChartCard from "../components/ChartCard";
import { useToday } from "../components/DayPicker";
import Tabs from "../components/Tabs";
import AppsDevicesTab from "./insights/AppsDevicesTab";
import FocusSleepTab from "./insights/FocusSleepTab";
import OverviewTab from "./insights/OverviewTab";
import { EARLIEST, PRESETS, localZone, rangeProblem, spanOf, validRange } from "./insights/shared";

const TABS = [
  { id: "overview", label: "Overview" },
  { id: "apps", label: "Apps and Devices" },
  { id: "focus", label: "Focus and Sleep" },
  { id: "food", label: "Food and Calendar" },
] as const;
const DEFAULT_RANGE = "7d";

function RangePicker({ range, today, onChange }: { range: string; today: string; onChange: (range: string) => void }) {
  const custom = !PRESETS.some((preset) => preset.id === range);
  const [open, setOpen] = useState(custom);
  const span = spanOf(range, today) ?? { first: today, last: today };
  const [from, setFrom] = useState(span.first);
  const [to, setTo] = useState(span.last);
  const noteId = useId();
  const wasCustom = useRef(custom);
  useEffect(() => {
    // A custom range from the address (or Back) shows in the fields; Back to a preset closes them.
    if (custom) {
      setOpen(true);
      setFrom(span.first);
      setTo(span.last);
    } else if (wasCustom.current) {
      setOpen(false);
    }
    wasCustom.current = custom;
  }, [custom, span.first, span.last]);
  const toggle = () => {
    if (!open) {
      // Start from the range shown now (30 days shows the last 30 days), not whatever the fields held before.
      setFrom(span.first);
      setTo(span.last);
    }
    setOpen(!open);
  };
  const problem = rangeProblem(from, to, today);
  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (!problem) onChange(`${from}..${to}`);
  };
  return (
    <div className="range-picker">
      <div className="segmented" role="group" aria-label="Time range">
        {PRESETS.map((preset) => (
          <button
            key={preset.id}
            type="button"
            className="segment"
            aria-pressed={range === preset.id}
            onClick={() => {
              setOpen(false);
              onChange(preset.id);
            }}
          >
            {preset.label}
          </button>
        ))}
        <button type="button" className="segment" aria-pressed={custom} aria-expanded={open} onClick={toggle}>
          Custom
        </button>
      </div>
      {open && (
        <form className="range-form" onSubmit={submit} aria-label="Custom range">
          <div className="range-custom">
            <label className="range-field">
              <span>From</span>
              <input type="date" className="date-input" value={from} min={EARLIEST} max={today} onChange={(event) => setFrom(event.target.value)} />
            </label>
            <label className="range-field">
              <span>To</span>
              <input type="date" className="date-input" value={to} min={EARLIEST} max={today} onChange={(event) => setTo(event.target.value)} />
            </label>
            <button type="submit" className="button" disabled={problem !== null} aria-describedby={problem ? noteId : undefined}>
              Show
            </button>
          </div>
          {/* Always there, so a screen reader hears each new problem (a live region added with its text often isn't read). */}
          <p id={noteId} className="field-note" role="status">
            {problem}
          </p>
        </form>
      )}
    </div>
  );
}

function ComingSoon({ label }: { label: string }) {
  return (
    <ChartCard title={label}>
      <p className="muted">These charts are on their way: this tab fills in with the next update. The Overview has the range&apos;s numbers now.</p>
    </ChartCard>
  );
}

export default function Insights() {
  const [params, setParams] = useSearchParams();
  const today = useToday();
  const tz = useMemo(localZone, []);
  const asked = params.get("tab");
  const tab = TABS.some((item) => item.id === asked) ? (asked as (typeof TABS)[number]["id"]) : "overview";
  const wanted = params.get("range") ?? DEFAULT_RANGE;
  const range = validRange(wanted, today) ? wanted : DEFAULT_RANGE;
  const app = tab === "apps" ? params.get("app") : null;
  const change = (key: "tab" | "range" | "app", value: string | null) =>
    setParams((previous) => {
      const next = new URLSearchParams(previous);
      if (value === null) next.delete(key);
      else next.set(key, value);
      if (key === "tab") next.delete("app"); // an app's detail belongs to the Apps tab
      return next;
    });

  return (
    <div className="stack">
      <header className="page-head">
        <div>
          <p className="eyebrow">Patterns over time</p>
          <h1>Insights</h1>
        </div>
        <RangePicker range={range} today={today} onChange={(next) => change("range", next)} />
      </header>
      <Tabs
        label="Insights views"
        value={tab}
        onChange={(next) => change("tab", next)}
        tabs={TABS.map((item) => ({
          id: item.id,
          label: item.label,
          content:
            item.id === "overview" ? (
              <OverviewTab range={range} tz={tz} today={today} />
            ) : item.id === "apps" ? (
              <AppsDevicesTab range={range} tz={tz} today={today} app={app} onApp={(name) => change("app", name)} />
            ) : item.id === "focus" ? (
              <FocusSleepTab range={range} tz={tz} today={today} />
            ) : (
              <ComingSoon label={item.label} />
            ),
        }))}
      />
    </div>
  );
}
