// DT-31: Today, the "where did my day go" page. Hero numbers that count up, then four sub-tabs (swipe on a phone):
// Timeline (a lane per device), Apps (the day's top 10, re-sorting live), Devices (minutes per device) and Health
// (last night's sleep, today's steps). Any day can be picked; today refreshes every 3 seconds while the page is
// visible, so a new session shows within a few seconds.
//
// Every number is the hub's: GET /timeline (the lanes and the screen-time total, which is the sum of the lanes'
// blocks, and when each device last synced) and GET /insights/day (focus, pickups, the phone and computer split, top
// apps, sleep, steps). Nothing is added up or estimated here. A refresh never cuts off a request still on its way,
// and a failed load says so where its numbers would be.
import { useCallback, useMemo, useRef, useState } from "react";
import { useApi, usePolling } from "../api/client";
import type { components } from "../api/schema";
import ChartCard from "../components/ChartCard";
import DayPicker, { longDay, useToday } from "../components/DayPicker";
import StatCard, { formatMinutes, spokenMinutes } from "../components/StatCard";
import Tabs from "../components/Tabs";
import Timeline from "../components/Timeline";
import { type ChartOption, categoryStyle, useEChart } from "../theme/charts";

type TimelineData = components["schemas"]["Timeline"];
type Summary = components["schemas"]["DaySummary"];

const REFRESH_TIMELINE = 3_000;
const REFRESH_SUMMARY = 10_000;
const NOW_LINE = 30_000; // how often the "now" line moves
const STEP_REFERENCE = 10_000; // a common daily reference, not a goal (goals come with DT-53)
const DEVICE_COLORS: Record<string, string> = {
  windows: "var(--cat-work)",
  macos: "var(--cat-study)",
  android: "var(--cat-comms)",
  ios: "var(--cat-social)",
};

function AppsRace({ summary }: { summary: Summary }) {
  const apps = summary.top_apps;
  // A bar race needs each app to keep its place in the data (realtimeSort then moves the bars): apps seen this day
  // keep their index, and apps that dropped out of the top 10 get 0 and sink out of view.
  const order = useRef<{ date: string; apps: string[] }>({ date: summary.date, apps: [] });
  if (order.current.date !== summary.date) order.current = { date: summary.date, apps: [] };
  for (const app of apps) if (!order.current.apps.includes(app.app)) order.current.apps.push(app.app);
  const known = order.current.apps;
  const option = useMemo<ChartOption>(
    () => ({
      grid: { left: 8, right: 64, top: 8, bottom: 8, containLabel: true },
      xAxis: { type: "value", max: "dataMax", axisLabel: { formatter: (value: number) => formatMinutes(value) } },
      yAxis: { type: "category", inverse: true, data: [...known], max: Math.min(known.length, 10) - 1, animationDuration: 300, animationDurationUpdate: 300 },
      tooltip: { trigger: "item", valueFormatter: (value: unknown) => `${Number(value).toLocaleString()} min` },
      series: [
        {
          id: "apps",
          type: "bar",
          realtimeSort: true,
          name: "Minutes",
          data: known.map((name) => {
            const app = apps.find((item) => item.app === name);
            return { value: app?.minutes ?? 0, itemStyle: categoryStyle(app?.category ?? "other") };
          }),
          label: { show: true, position: "right", valueAnimation: true, formatter: (params: { value: unknown }) => formatMinutes(Number(params.value)) },
        },
      ],
      animationDurationUpdate: 800,
      animationEasingUpdate: "linear",
    }),
    [apps, known],
  );
  const chart = useEChart(option, `Top ${apps.length} apps and sites by minutes`, { merge: true });
  if (!apps.length) return <p className="muted">No app time yet on this day.</p>;
  return <div ref={chart} className="chart" style={{ height: Math.max(220, apps.length * 36 + 30) }} />;
}

function DevicesDonut({ timeline }: { timeline: TimelineData }) {
  const lanes = useMemo(() => timeline.lanes.filter((lane) => lane.counted && lane.seconds > 0), [timeline.lanes]);
  const option = useMemo<ChartOption>(
    () => ({
      tooltip: { trigger: "item", valueFormatter: (value: unknown) => `${Number(value).toLocaleString()} min` },
      legend: { bottom: 0 },
      series: [
        {
          id: "devices",
          type: "pie",
          radius: ["48%", "72%"],
          center: ["50%", "45%"],
          label: { formatter: (params: { name: string; value: unknown }) => `${params.name}\n${formatMinutes(Number(params.value))}` },
          data: lanes.map((lane) => ({
            name: lane.name,
            value: lane.minutes,
            itemStyle: { color: DEVICE_COLORS[lane.device_type] ?? "var(--cat-other)" },
          })),
        },
      ],
    }),
    [lanes],
  );
  const chart = useEChart(option, "Screen time per device, in minutes", { merge: true });
  if (!lanes.length) return <p className="muted">No device has screen time on this day yet.</p>;
  return (
    <>
      <p className="muted">
        {formatMinutes(timeline.totals.minutes)} in all, counted per device and added up.
      </p>
      <div ref={chart} className="chart" style={{ height: 300 }} />
    </>
  );
}

function Health({ summary }: { summary: Summary }) {
  const sleep = summary.sleep_minutes;
  const steps = summary.steps;
  const share = steps === null ? 0 : Math.min(steps / STEP_REFERENCE, 1);
  const circumference = 2 * Math.PI * 52;
  return (
    <div className="grid grid-2">
      <ChartCard
        title="Last night's sleep"
        unit="hours and minutes"
        estimated={summary.sleep_estimated}
        info="From Health Connect or Health when your phone sends sleep; otherwise estimated from the longest stretch your phone wasn't used overnight."
      >
        {sleep === null ? (
          <p className="muted">No sleep found for last night.</p>
        ) : (
          <div className="sleep">
            <p className="stat-value">{formatMinutes(sleep)}</p>
            <div className="sleep-bar" role="img" aria-label={`Slept ${spokenMinutes(sleep)}`}>
              <span style={{ width: `${Math.min(sleep / 600, 1) * 100}%` }} />
            </div>
            <p className="muted sleep-scale" aria-hidden="true">
              <span>0h</span>
              <span>5h</span>
              <span>10h</span>
            </p>
          </div>
        )}
      </ChartCard>
      <ChartCard
        title="Steps"
        unit="steps"
        info={`Today's steps from your phone's health app. The ring fills at ${STEP_REFERENCE.toLocaleString()} steps, a common daily reference.`}
      >
        {steps === null ? (
          <p className="muted">No step data yet. Steps arrive from Health Connect on Android or Health on iPhone.</p>
        ) : (
          <div className="steps">
            <svg viewBox="0 0 120 120" width="132" height="132" role="img" aria-label={`${steps.toLocaleString()} steps`}>
              <circle cx="60" cy="60" r="52" className="ring-track" />
              <circle
                cx="60"
                cy="60"
                r="52"
                className="ring-fill"
                strokeDasharray={circumference}
                strokeDashoffset={circumference * (1 - share)}
                transform="rotate(-90 60 60)"
              />
            </svg>
            <p className="stat-value">{steps.toLocaleString()}</p>
          </div>
        )}
      </ChartCard>
    </div>
  );
}

export default function Today() {
  const tz = useMemo(() => Intl.DateTimeFormat().resolvedOptions().timeZone, []);
  const today = useToday();
  const [picked, setPicked] = useState<string | null>(null);
  const day = picked ?? today;
  const live = day === today;
  const query = { date: day, tz };

  const timeline = useApi("/api/v1/timeline", { query, quiet: true });
  const summary = useApi("/api/v1/insights/day", { query, quiet: true });
  // A refresh waits while the last request is still on its way (reloading would cut it off, and a slow or asleep
  // hub would then never show its error).
  const busy = useRef({ timeline: false, summary: false });
  busy.current = { timeline: timeline.loading, summary: summary.loading };
  const { reload: reloadTimeline } = timeline;
  const { reload: reloadSummary } = summary;
  const refreshTimeline = useCallback(() => busy.current.timeline || reloadTimeline(), [reloadTimeline]);
  const refreshSummary = useCallback(() => busy.current.summary || reloadSummary(), [reloadSummary]);
  usePolling(live ? REFRESH_TIMELINE : null, refreshTimeline);
  usePolling(live ? REFRESH_SUMMARY : null, refreshSummary);

  const [now, setNow] = useState(() => Date.now());
  const tick = useCallback(() => setNow(Date.now()), []);
  usePolling(live ? NOW_LINE : null, tick);

  // Data for another day (still on screen while the new day loads) must not show under this day's heading.
  const shown = timeline.data?.date === day ? timeline.data : undefined;
  const numbers = summary.data?.date === day ? summary.data : undefined;
  const failed = !shown && timeline.error;
  const summaryError = !numbers && summary.error ? summary.error.message : undefined;
  const so = numbers?.in_progress ? " so far" : "";

  return (
    <div className="stack">
      <header className="page-head">
        <div>
          <p className="eyebrow">{live ? longDay(day) : "Looking back"}</p>
          <h1>{live ? "Today" : longDay(day)}</h1>
        </div>
        <DayPicker day={day} today={today} onChange={(next) => setPicked(next === today ? null : next)} />
      </header>

      <div className="grid stat-grid">
        <StatCard
          label={`Screen time${so}`}
          value={shown ? (shown.totals.seconds ? shown.totals.minutes : shown.lanes.length ? 0 : null) : undefined}
          error={failed ? timeline.error?.message : undefined}
          format={formatMinutes}
          estimated={numbers?.screen_estimated}
          tone="work"
          hint={shown && shown.totals.any_screen_seconds ? `${formatMinutes(shown.totals.any_screen_minutes)} with any screen on` : undefined}
        />
        <StatCard
          label="Phone and computer"
          value={numbers ? numbers.phone_minutes : undefined}
          error={summaryError}
          format={formatMinutes}
          tone="comms"
          hint={numbers && numbers.computer_minutes !== null ? `on the phone, ${formatMinutes(numbers.computer_minutes)} on computers` : undefined}
        >
          {numbers && numbers.phone_minutes !== null && numbers.computer_minutes !== null && numbers.phone_minutes + numbers.computer_minutes > 0 && (
            <div className="split" aria-hidden="true">
              <span className="split-phone" style={{ flexGrow: numbers.phone_minutes }} />
              <span className="split-computer" style={{ flexGrow: numbers.computer_minutes }} />
            </div>
          )}
        </StatCard>
        <StatCard
          label={`Focused time${so}`}
          value={numbers ? numbers.focused_minutes : undefined}
          error={summaryError}
          format={formatMinutes}
          tone="study"
          hint={numbers?.focus_score !== null && numbers?.focus_score !== undefined ? `Focus score ${numbers.focus_score} out of 100` : "Work or study in blocks of 10+ minutes"}
        />
        <StatCard
          label={`Phone pickups${so}`}
          value={numbers ? numbers.pickups : undefined}
          error={summaryError}
          tone="social"
          hint={numbers?.switches_per_hour !== null && numbers?.switches_per_hour !== undefined ? `${numbers.switches_per_hour} app switches per hour` : undefined}
        />
      </div>

      <Tabs
        label="Today views"
        tabs={[
          {
            id: "timeline",
            label: "Timeline",
            content: (
              <ChartCard
                title="Timeline"
                range={live ? "Today, live" : longDay(day)}
                unit="minutes"
                estimated={shown?.meta.estimated}
                loading={!shown && !failed}
                info="Every session on every device, colored by category, each with its own pattern. Zoom with Ctrl and the wheel or with the slider, drag to pan, and hover or tap a block for its exact minutes. Browser sites show which sites were open; that time is already in the computer's lane."
              >
                {failed ? (
                  <p className="muted">The timeline couldn&apos;t load: {timeline.error?.message}</p>
                ) : shown && shown.lanes.length + shown.calendar.length + shown.sleep.length + shown.meals.length === 0 ? (
                  <p className="muted">Nothing recorded on this day yet.</p>
                ) : shown ? (
                  <Timeline data={shown} now={live ? now : undefined} />
                ) : null}
              </ChartCard>
            ),
          },
          {
            id: "apps",
            label: "Apps",
            content: (
              <ChartCard
                title="Top apps and sites"
                range={live ? "Today, live" : longDay(day)}
                unit="minutes"
                estimated={numbers?.screen_estimated}
                loading={!numbers && !summaryError}
                info="The apps and sites with the most time. Desktop browser time goes to the site you were on when the browser extension saw it."
              >
                {summaryError ? <p className="muted">The apps couldn&apos;t load: {summaryError}</p> : numbers && <AppsRace summary={numbers} />}
              </ChartCard>
            ),
          },
          {
            id: "devices",
            label: "Devices",
            content: (
              <ChartCard
                title="Minutes per device"
                range={live ? "Today, live" : longDay(day)}
                unit="minutes"
                estimated={shown?.meta.estimated}
                loading={!shown && !failed}
                info="Screen time counted per device and added up, so an hour on the phone while the computer was on counts on both."
              >
                {shown && <DevicesDonut timeline={shown} />}
              </ChartCard>
            ),
          },
          {
            id: "health",
            label: "Health",
            content: numbers ? (
              <Health summary={numbers} />
            ) : summaryError ? (
              <ChartCard title="Health">
                <p className="muted">Sleep and steps couldn&apos;t load: {summaryError}</p>
              </ChartCard>
            ) : (
              <ChartCard title="Health" loading />
            ),
          },
        ]}
      />
    </div>
  );
}
