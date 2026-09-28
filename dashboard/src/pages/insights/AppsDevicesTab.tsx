// DT-55: the Insights Apps and Devices tab. Which apps and sites take the time (a leaderboard with each one's week
// and change, and a treemap of categories and their apps), each device by day, the switches between devices
// ("the PC's work, then the phone's social"), and when each device sent data, so a gap is never read as no use.
// Tapping an app (on the leaderboard or in the treemap) opens its detail: each day, the hours, the devices and its
// longest stretch. Everything is the hub's (GET /insights/apps, /insights/devices and /insights/apps/detail).
import { type CSSProperties, type ReactNode, useEffect, useId, useMemo, useRef } from "react";
import ChartCard from "../../components/ChartCard";
import { shortDay } from "../../components/DayPicker";
import Skeleton from "../../components/Skeleton";
import StatCard, { formatMinutes } from "../../components/StatCard";
import { timesWords } from "../../components/streakText";
import { CATEGORY_LABELS, type ChartOption, asCategory, categoryStyle, escapeHTML, useEChart } from "../../theme/charts";
import { deviceColors } from "../../theme/devices";
import { useMediaQuery } from "../../theme/motion";
import { Failed, SeriesCard, minutesText } from "./parts";
import { ChangeChip, type InsightsData, type Item, type Series, metric, rangeWords, useAppDetail, useInsights, valueOf } from "./shared";

type Props = { range: string; tz: string; today: string; app: string | null; onApp: (app: string | null) => void };

const OTHER_APPS = "Other apps"; // the treemap's box for a category's smaller apps: not one app, so not opened
const category = asCategory;
const WIDE = "(min-width: 40rem)";

// --- the leaderboard -------------------------------------------------------------------------------------------

function Sparkline({ values, tone }: { values: (number | null)[]; tone: string }) {
  const width = 84;
  const height = 26;
  const peak = Math.max(1, ...values.map((value) => value ?? 0));
  const step = values.length > 1 ? width / (values.length - 1) : 0;
  const lines: string[][] = [[]]; // a day with no data breaks the line
  values.forEach((value, index) => {
    if (value === null) {
      if (lines[lines.length - 1].length) lines.push([]);
      return;
    }
    lines[lines.length - 1].push(`${(index * step).toFixed(1)},${(height - 3 - (value / peak) * (height - 6)).toFixed(1)}`);
  });
  const last = values.length - 1;
  return (
    <svg className="spark" viewBox={`0 0 ${width} ${height}`} width={width} height={height} aria-hidden="true" style={{ color: `var(--cat-${tone})` }}>
      {lines
        .filter((points) => points.length)
        .map((points) =>
          points.length === 1 ? (
            <circle key={points[0]} cx={points[0].split(",")[0]} cy={points[0].split(",")[1]} r="2" fill="currentColor" />
          ) : (
            <polyline key={points[0]} points={points.join(" ")} fill="none" stroke="currentColor" strokeWidth="2" strokeLinejoin="round" strokeLinecap="round" />
          ),
        )}
      {values[last] !== null && values[last] !== undefined && (
        <circle cx={last * step} cy={height - 3 - ((values[last] ?? 0) / peak) * (height - 6)} r="2.5" fill="currentColor" />
      )}
    </svg>
  );
}

function Leaderboard({ series, days, onApp }: { series: Series; days: number; onApp: (app: string) => void }) {
  const items = series.items ?? [];
  const week = series.x ?? [];
  if (!items.length) return <p className="muted">No app time in this range.</p>;
  return (
    <ol className="leaders">
      {items.map((item: Item, index) => {
        const tone = category(item.category);
        const spark = item.spark ?? [];
        return (
          <li key={item.key ?? item.name} className="leader">
            <span className="leader-rank" aria-hidden="true">
              {index + 1}
            </span>
            <span className="leader-name">
              <span className="swatch" style={{ "--c": `var(--cat-${tone})` } as CSSProperties} aria-hidden="true" />
              <span className="leader-app">{item.name}</span>
            </span>
            <span className="leader-meta">
              <strong className="leader-time">{formatMinutes(item.value)}</strong>
              <span className="leader-spark" title={spark.map((value, day) => `${shortDay(week[day] ?? "")}: ${minutesText(value)}`).join(", ")}>
                <Sparkline values={spark} tone={tone} />
              </span>
              <ChangeChip change={item.change ?? undefined} days={days} />
            </span>
            {/* An icon, not the name: a button's words never break, and a long site name may have to. */}
            <button type="button" className="icon-button leader-open" aria-label={`Details for ${item.name}`} onClick={() => onApp(item.name)}>
              <svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M9 5l7 7-7 7" />
              </svg>
            </button>
          </li>
        );
      })}
    </ol>
  );
}

// --- the treemap -----------------------------------------------------------------------------------------------

type TreeClick = { name?: string; treePathInfo?: { name: string }[] };

function Treemap({ series, onApp }: { series: Series; onApp: (app: string) => void }) {
  const option = useMemo<ChartOption>(
    () => ({
      tooltip: {
        formatter: (params: { value: number; treePathInfo: { name: string }[] }) =>
          escapeHTML(
            `${params.treePathInfo
              .map((part) => part.name)
              .filter(Boolean)
              .join(": ")} ${formatMinutes(params.value)}`,
          ),
      },
      series: [
        {
          id: "treemap",
          type: "treemap",
          roam: false,
          nodeClick: false, // an app opens its detail instead (below)
          breadcrumb: { show: false },
          left: 0,
          right: 0,
          top: 0,
          bottom: 0,
          label: { show: true, formatter: "{b}", overflow: "truncate", color: "#fff", textBorderColor: "rgba(0, 0, 0, 0.5)", textBorderWidth: 2 },
          // A header for each category only (level 0 is the treemap's own root, which has no name to show).
          levels: [
            // A gap shows its parent's border color: the root's is the card's.
            { itemStyle: { gapWidth: 4, borderWidth: 0, borderColor: "var(--surface)" }, upperLabel: { show: false } },
            {
              itemStyle: { gapWidth: 2, borderWidth: 2, borderColor: "var(--surface)" },
              upperLabel: { show: true, height: 24, color: "#fff", textBorderColor: "rgba(0, 0, 0, 0.5)", textBorderWidth: 2, fontWeight: 700 },
            },
            { upperLabel: { show: false } },
          ],
          data: (series.items ?? []).map((group) => ({
            name: group.name,
            value: group.value,
            itemStyle: { ...categoryStyle(group.category), borderColor: `var(--cat-${category(group.category)})` },
            children: (group.children ?? []).map((child) => ({ name: child.name, value: child.value, itemStyle: categoryStyle(child.category) })),
          })),
        },
      ],
    }),
    [series],
  );
  const chart = useEChart(option, `${series.title}: minutes in each category and its apps. Choose an app for its details.`, {
    events: {
      click: (params) => {
        const clicked = params as TreeClick;
        const depth = clicked.treePathInfo?.length ?? 0; // the root, a category, an app
        if (depth === 3 && clicked.name && clicked.name !== OTHER_APPS) onApp(clicked.name);
      },
    },
  });
  const apps = (series.items ?? []).flatMap((group) =>
    (group.children ?? []).filter((child) => child.name !== OTHER_APPS).map((child) => ({ ...child, group: group.name })),
  );
  return (
    <>
      <div ref={chart} className="chart treemap" style={{ height: 340 }} data-no-swipe />
      {/* The chart is a picture: every app in it, as a list anyone can reach and open (a keyboard, a screen reader). */}
      <details className="tree-list">
        <summary>Every app in it, as a list</summary>
        <ul>
          {apps.map((app) => (
            <li key={`${app.group}|${app.name}`}>
              <span className="swatch" style={{ "--c": `var(--cat-${category(app.category)})` } as CSSProperties} aria-hidden="true" />
              <span className="tree-list-app">
                {app.name} <span className="muted">{app.group}</span>
              </span>
              <strong>{formatMinutes(app.value)}</strong>
              <button type="button" className="icon-button" aria-label={`Details for ${app.name}`} onClick={() => onApp(app.name)}>
                <svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M9 5l7 7-7 7" />
                </svg>
              </button>
            </li>
          ))}
        </ul>
      </details>
    </>
  );
}

// --- devices ---------------------------------------------------------------------------------------------------

function DevicesByDay({ series }: { series: Series }) {
  const option = useMemo<ChartOption>(() => {
    const lines = series.lines ?? [];
    const colors = deviceColors(lines.map((line) => line.device_type));
    return {
      tooltip: { trigger: "axis", axisPointer: { type: "shadow" }, valueFormatter: minutesText },
      legend: { bottom: 0, type: "scroll" },
      grid: { left: 8, right: 16, top: 16, bottom: 40, containLabel: true },
      xAxis: { type: "category", data: (series.x ?? []).map(shortDay) },
      yAxis: { type: "value", axisLabel: { formatter: (value: number) => formatMinutes(value) } },
      series: lines.map((line, index) => ({
        id: line.key ?? line.name,
        name: line.name,
        type: "bar",
        stack: "devices",
        itemStyle: { color: colors[index] },
        data: line.values,
      })),
    };
  }, [series]);
  const chart = useEChart(option, `${series.title}, stacked by device, in minutes`);
  return <div ref={chart} className="chart" style={{ height: 300 }} />;
}

function Handoffs({ series }: { series: Series }) {
  const wide = useMediaQuery(WIDE); // names beside the flows only where there is room; the list below has them all
  const links = series.links ?? [];
  const nodes = series.nodes ?? [];
  const categoryOf = new Map(nodes.map((name, index) => [name, category(series.node_categories?.[index])]));
  const option = useMemo<ChartOption>(() => {
    const targets = new Set(links.map((link) => link.target)); // the right side: what was taken up
    return {
      tooltip: {
        trigger: "item",
        formatter: (params: { dataType?: string; name: string; value: number; data: { source?: string; target?: string } }) =>
          escapeHTML(params.dataType === "edge" ? `${params.data.source}, ${params.data.target}: ${params.value} times` : `${params.name}: ${params.value} times`),
      },
      series: [
        {
          id: "handoffs",
          type: "sankey",
          left: 8,
          right: 8,
          top: 8,
          bottom: 8,
          nodeWidth: 12,
          nodeGap: 10,
          draggable: false,
          emphasis: { focus: "adjacency" },
          lineStyle: { color: "gradient", opacity: 0.35, curveness: 0.5 },
          label: { show: wide, color: "var(--text)", overflow: "truncate", width: 170 },
          data: nodes.map((name, index) => ({
            name,
            itemStyle: { color: `var(--cat-${category(series.node_categories?.[index])})` },
            label: { position: targets.has(name) ? "left" : "right" },
          })),
          links: links.map((link) => ({ source: link.source, target: link.target, value: link.value })),
        },
      ],
    };
  }, [series, wide, links, nodes]);
  const chart = useEChart(option, `${series.title}: from what you left to what you took up on another device`);
  return (
    <>
      <div ref={chart} className="chart" style={{ height: Math.max(220, (series.nodes?.length ?? 0) * 18) }} data-no-swipe />
      <ol className="handoff-list">
        {links.slice(0, 5).map((link) => (
          <li key={`${link.source}|${link.target}`}>
            <span className="swatch" style={{ "--c": `var(--cat-${categoryOf.get(link.source) ?? "other"})` } as CSSProperties} aria-hidden="true" />
            <span>
              {link.source}, {link.target}
            </span>
            <strong>{timesWords(link.value)}</strong>
          </li>
        ))}
      </ol>
    </>
  );
}

const SYNC_WORDS = { sent: "sent data", none: "no data", off: "not paired" } as const;
type SyncState = keyof typeof SYNC_WORDS;

/** When the hub last heard from a device, or nothing when it has no time for it (data it never sent itself, as
 * the demo's). */
function lastHeard(iso: string | number | null | undefined): string | null {
  if (typeof iso !== "string") return null;
  const moment = new Date(iso.replace(/(\.\d{3})\d+/, "$1")); // the hub's microseconds, as milliseconds
  if (Number.isNaN(moment.getTime())) return null;
  return `Last heard from ${moment.toLocaleString([], { weekday: "short", day: "numeric", month: "short", hour: "numeric", minute: "2-digit" })}`;
}

function SyncStrip({ series, heard }: { series: Series; heard: Map<string, string | number | null | undefined> }) {
  const days = series.x ?? [];
  const found = new Map((series.cells ?? []).map((cell) => [`${cell.y}:${cell.x}`, cell.value]));
  const state = (row: number, col: number): SyncState => {
    const value = found.get(`${row}:${col}`);
    return value === 1 ? "sent" : value === 0 ? "none" : "off";
  };
  if (!(series.y ?? []).length) return <p className="muted">No phone or computer is paired yet.</p>;
  return (
    <div className="sync">
      <ul className="sync-rows">
        {(series.y ?? []).map((name, row) => {
          const states = days.map((_, col) => state(row, col));
          const sent = states.filter((item) => item === "sent").length;
          const gaps = days.filter((_, col) => states[col] === "none").map(shortDay);
          return (
            <li key={name} className="sync-row">
              <p className="sync-head">
                <span className="sync-name">{name}</span>
                {lastHeard(heard.get(name)) && <span className="muted">{lastHeard(heard.get(name))}</span>}
              </p>
              <div className="sync-cells" style={{ gridTemplateColumns: `repeat(${days.length}, minmax(0, 1fr))` }} aria-hidden="true">
                {states.map((item, col) => (
                  <span key={days[col]} className={`sync-cell sync-${item}`} title={`${shortDay(days[col])}: ${SYNC_WORDS[item]}`} />
                ))}
              </div>
              <p className="visually-hidden">
                Sent data on {sent} of {days.length} days{gaps.length ? `; no data on ${gaps.join(", ")}` : ""}.
              </p>
            </li>
          );
        })}
      </ul>
      <ul className="sync-key" aria-hidden="true">
        {(Object.keys(SYNC_WORDS) as SyncState[]).map((item) => (
          <li key={item}>
            <span className={`sync-cell sync-${item}`} /> {SYNC_WORDS[item][0].toUpperCase() + SYNC_WORDS[item].slice(1)}
          </li>
        ))}
      </ul>
    </div>
  );
}

// --- one app's detail ------------------------------------------------------------------------------------------

function DetailBars({ series, labels, height }: { series: Series; labels: (value: string) => string; height: number }) {
  const option = useMemo<ChartOption>(
    () => ({
      tooltip: { trigger: "axis", axisPointer: { type: "shadow" }, valueFormatter: minutesText },
      grid: { left: 8, right: 12, top: 12, bottom: 8, containLabel: true },
      xAxis: { type: "category", data: (series.x ?? []).map(labels), axisLabel: { hideOverlap: true } },
      yAxis: { type: "value", axisLabel: { formatter: (value: number) => formatMinutes(value) } },
      series: [{ id: series.title, type: "bar", data: series.lines?.[0]?.values ?? [], itemStyle: { color: "var(--accent)", borderRadius: [4, 4, 0, 0] } }],
    }),
    [series, labels],
  );
  const chart = useEChart(option, `${series.title}, in minutes`);
  return <div ref={chart} className="chart" style={{ height }} data-no-swipe />;
}

const hourLabel = (hour: string) => hour;

function AppDrawer({ app, range, tz, today, onClose }: { app: string | null; range: string; tz: string; today: string; onClose: () => void }) {
  const { data, error, reload } = useAppDetail(app, range, tz, today);
  const dialog = useRef<HTMLDialogElement>(null);
  const title = useId();
  useEffect(() => {
    const element = dialog.current;
    if (!element) return;
    if (app !== null && !element.open) element.showModal();
    if (app === null && element.open) element.close();
  }, [app]);
  const longest = data?.longest;
  const tone = category(data?.category);
  const withData = (data?.series.daily?.lines?.[0]?.values ?? []).filter((value) => value !== null).length; // unknown days aren't unused
  return (
    // Closing by Escape or the button removes the app from the address; closing because the address lost it
    // (Back) must not add it again as a new step.
    <dialog ref={dialog} className="dialog drawer" aria-labelledby={title} onClose={() => app !== null && onClose()}>
      {app !== null && (
        <div className="drawer-body">
          <header className="drawer-head">
            <div>
              <p className="eyebrow">{data ? rangeWords(data) : "Loading"}</p>
              <h2 id={title}>{app}</h2>
              {data?.category && (
                <span className="chip" style={{ "--c": `var(--cat-${tone})`, "--c-ink": `var(--cat-${tone}-ink)` } as CSSProperties}>
                  {CATEGORY_LABELS[tone]}
                </span>
              )}
            </div>
            <button type="button" className="icon-button" aria-label="Close" onClick={() => dialog.current?.close()} autoFocus>
              <svg viewBox="0 0 24 24" width="22" height="22" aria-hidden="true" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round">
                <path d="M6 6l12 12M18 6L6 18" />
              </svg>
            </button>
          </header>
          {error ? (
            <div className="stack">
              <p className="muted">Its details couldn&apos;t load: {error.message}</p>
              <button type="button" className="button button-ghost" onClick={reload}>
                Try again
              </button>
            </div>
          ) : !data ? (
            <Skeleton height={220} radius={12} />
          ) : (
            <div className="stack">
              <div className="grid stat-grid">
                <StatCard label="In the range" value={valueOf(data, "total")} format={formatMinutes} tone={tone} />
                <StatCard label="Days used" value={valueOf(data, "days_used")} format={String} tone={tone} hint={`of ${withData} with data`} />
                <StatCard label="A day, when used" value={valueOf(data, "a_day_used")} format={formatMinutes} tone={tone} />
                <StatCard
                  label="Longest stretch"
                  value={valueOf(data, "longest")}
                  format={formatMinutes}
                  tone={tone}
                  hint={
                    longest
                      ? `${longest.device}, ${new Date(longest.start).toLocaleString([], { weekday: "short", day: "numeric", hour: "numeric", minute: "2-digit" })}`
                      : undefined
                  }
                />
              </div>
              {data.series.daily && (
                <ChartCard title={data.series.daily.title} unit="minutes" info={data.series.daily.explain} estimated={data.series.daily.estimated}>
                  <DetailBars series={data.series.daily} labels={shortDay} height={200} />
                </ChartCard>
              )}
              {data.series.hours && (
                <ChartCard title={data.series.hours.title} unit="minutes" info={data.series.hours.explain} estimated={data.series.hours.estimated}>
                  <DetailBars series={data.series.hours} labels={hourLabel} height={180} />
                </ChartCard>
              )}
              {!!data.series.devices?.items?.length && (
                <ChartCard title={data.series.devices.title} unit="minutes">
                  <ul className="detail-devices">
                    {data.series.devices.items.map((item) => (
                      <li key={item.key ?? item.name}>
                        <span>{item.name}</span>
                        <strong>{formatMinutes(item.value)}</strong>
                      </li>
                    ))}
                  </ul>
                </ChartCard>
              )}
            </div>
          )}
        </div>
      )}
    </dialog>
  );
}

// --- the tab ---------------------------------------------------------------------------------------------------

export default function AppsDevicesTab({ range, tz, today, app, onApp }: Props) {
  const apps = useInsights("apps", range, tz, today);
  const devices = useInsights("devices", range, tz, today);
  const appsData = apps.data;
  const devicesData = devices.data;
  const words = appsData ? rangeWords(appsData) : devicesData ? rangeWords(devicesData) : undefined;
  const days = (appsData ?? devicesData)?.range.days ?? 0;
  const topApp = appsData ? metric(appsData, "top_app")?.value : undefined;
  const topHandoff = devicesData ? metric(devicesData, "top_handoff")?.value : undefined;
  const heard = new Map((devicesData?.metrics ?? []).filter((item) => item.id.startsWith("last_seen:")).map((item) => [item.label, item.value]));

  const card = (data: InsightsData | undefined, key: string, title: string, unit: string, body: (found: Series) => ReactNode, empty: string, wide = false) => (
    <SeriesCard data={data} name={key} title={title} unit={unit} words={words} empty={empty} wide={wide}>
      {body}
    </SeriesCard>
  );
  const hasItems = (series: Series) => !!series.items?.length;
  const hasLines = (series: Series) => (series.lines ?? []).some((line) => line.values.some((value) => value));

  return (
    <div className="stack">
      <div className="grid stat-grid">
        <StatCard label="Apps and sites used" value={valueOf(appsData, "apps_used")} format={String} tone="work" error={apps.error?.message} />
        <StatCard
          label="Most used"
          value={valueOf(appsData, "top_app_time")}
          format={formatMinutes}
          tone="social"
          hint={typeof topApp === "string" ? topApp : undefined}
          error={apps.error?.message}
        />
        <StatCard
          label="App switches an hour"
          value={valueOf(appsData, "switches")}
          format={(value) => value.toLocaleString(undefined, { maximumFractionDigits: 1 })}
          tone="games"
          error={apps.error?.message}
        />
        <StatCard label="On the phone" value={valueOf(devicesData, "phone_share")} format={String} unit="%" tone="comms" error={devices.error?.message} />
        <StatCard
          label="Device switches"
          value={valueOf(devicesData, "handoffs")}
          format={String}
          tone="study"
          hint={typeof topHandoff === "string" ? `Most: ${topHandoff}` : undefined}
          error={devices.error?.message}
        />
      </div>
      {apps.error && <Failed what="The apps" message={apps.error.message} retry={apps.reload} />}
      {devices.error && <Failed what="The devices" message={devices.error.message} retry={devices.reload} />}
      <div className="grid chart-grid">
        {!apps.error &&
          card(appsData, "leaderboard", "Top apps and sites", "minutes", (found) => <Leaderboard series={found} days={days} onApp={onApp} />, "No app time in this range.", true)}
        {!apps.error &&
          card(appsData, "treemap", "Categories and their apps", "minutes", (found) =>
            hasItems(found) ? <Treemap series={found} onApp={onApp} /> : <p className="muted">No app time in this range.</p>,
          "No app time in this range.")}
        {!devices.error &&
          card(devicesData, "by_day", "Each device by day", "minutes", (found) =>
            hasLines(found) ? <DevicesByDay series={found} /> : <p className="muted">No device sent screen time in this range.</p>,
          "No device sent screen time in this range.")}
        {!devices.error &&
          card(devicesData, "handoffs", "Switching between devices", "switches", (found) =>
            found.links?.length ? <Handoffs series={found} /> : <p className="muted">No switches between devices in this range.</p>,
          "No switches between devices in this range.")}
        {!devices.error && card(devicesData, "sync", "When each device sent data", "days", (found) => <SyncStrip series={found} heard={heard} />, "No devices yet.")}
      </div>
      <AppDrawer app={app} range={range} tz={tz} today={today} onClose={() => onApp(null)} />
    </div>
  );
}
