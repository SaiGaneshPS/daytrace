// DT-56: the Insights Focus and Sleep tab. How focus and sleep connect, honest about what is measured and what is
// estimated: focused minutes on a calendar (GitHub style), when distractions happen (weekday by hour), the focus
// score gauge with its formula, app switches an hour, each night's sleep (solid when measured by a health app,
// striped when worked out from the phone), bedtimes and wake times, and late nights against the next day's focus
// with its trend line, sample size and "correlation, not cause". Everything is the hub's (GET /insights/focus and
// /insights/sleep).
import { type CSSProperties, type ReactNode, useMemo } from "react";
import ChartCard from "../../components/ChartCard";
import { longDay, shiftDay, shortDay } from "../../components/DayPicker";
import StatCard, { formatMinutes } from "../../components/StatCard";
import { type ChartOption, escapeHTML, useEChart } from "../../theme/charts";
import { clock, hourAxis, nightWords } from "./clock";
import { Failed, Gauge, SeriesCard, WeekdayHoursHeatmap, minutesText } from "./parts";
import { type Series, metric, rangeWords, useInsights, valueOf } from "./shared";

type Props = { range: string; tz: string; today: string };

// Estimated sleep is striped and measured sleep is solid, whatever else the patterns mean elsewhere.
const STRIPED = { symbol: "rect", symbolSize: 1, dashArrayX: [1, 0], dashArrayY: [3, 4], rotation: -Math.PI / 4, color: "rgba(255, 255, 255, 0.55)" };
const SOLID = { symbol: "none" };

// --- focus -----------------------------------------------------------------------------------------------------

/** Days as a GitHub-style calendar: a column per week (Monday first), a row per weekday, darker for more focus. */
function FocusCalendar({ days, values }: { days: string[]; values: (number | null)[] }) {
  if (!days.length) return null;
  const weekday = (day: string) => (new Date(`${day}T12:00:00`).getDay() + 6) % 7; // Monday 0
  const start = shiftDay(days[0], -weekday(days[0]));
  const byDay = new Map(days.map((day, index) => [day, values[index]]));
  const weeks = Math.ceil((days.length + weekday(days[0])) / 7);
  const peak = Math.max(0, ...values.map((value) => value ?? 0));
  const known = days.filter((_, index) => values[index] !== null && values[index] !== undefined);
  const missing = days.filter((_, index) => values[index] === null || values[index] === undefined);
  const best = peak > 0 ? known.find((day) => byDay.get(day) === peak) : undefined; // the first day with the most
  const gaps = missing.length ? `, none on ${missing.map(shortDay).join(", ")}` : "";
  const summary = !known.length
    ? "No focus data in this range."
    : best
      ? `Focused minutes each day: most on ${longDay(best)} (${formatMinutes(peak)}); ${known.length} of ${days.length} days with data${gaps}.`
      : `Focused minutes each day: no focused time on any of the ${known.length} days with data${gaps}.`;
  const cells: ReactNode[] = [];
  for (let row = 0; row < 7; row += 1) {
    cells.push(
      <span key={`label-${row}`} className="cal-weekday" style={{ gridRow: row + 1, gridColumn: 1 }}>
        {row % 2 === 0 ? ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][row] : ""}
      </span>,
    );
    for (let week = 0; week < weeks; week += 1) {
      const day = shiftDay(start, week * 7 + row);
      if (!byDay.has(day)) continue;
      const value = byDay.get(day) ?? null;
      const level = value === null ? "none" : value <= 0 ? "zero" : "some";
      const style = { gridRow: row + 1, gridColumn: week + 2, "--level": value && peak ? Math.max(0.18, value / peak) : 0 } as CSSProperties;
      cells.push(<span key={day} className={`cal-day cal-${level}`} style={style} title={`${longDay(day)}: ${minutesText(value)}`} />);
    }
  }
  return (
    <div className="focus-calendar">
      <div className="cal-grid" role="img" aria-label={summary} style={{ gridTemplateColumns: `auto repeat(${weeks}, minmax(0, 2rem))` }}>
        {cells}
      </div>
      <p className="cal-key muted" aria-hidden="true">
        <span className="cal-day cal-none" /> No data <span className="cal-day cal-zero" /> None
        <span className="cal-day cal-some" style={{ "--level": 0.3 } as CSSProperties} />
        <span className="cal-day cal-some" style={{ "--level": 1 } as CSSProperties} /> More
      </p>
    </div>
  );
}

function SwitchesByDay({ series }: { series: Series }) {
  const option = useMemo<ChartOption>(
    () => ({
      tooltip: { trigger: "axis", valueFormatter: (value: unknown) => (value === null || value === undefined || value === "-" ? "no data" : `${value} an hour`) },
      grid: { left: 8, right: 16, top: 16, bottom: 8, containLabel: true },
      xAxis: { type: "category", boundaryGap: false, data: (series.x ?? []).map(shortDay) },
      yAxis: { type: "value", minInterval: 0.5 },
      series: [
        {
          id: "switches",
          name: series.lines?.[0]?.name ?? "Switches an hour",
          type: "line",
          smooth: true,
          connectNulls: false,
          symbolSize: 7,
          lineStyle: { width: 3 },
          itemStyle: { color: "var(--cat-games)" },
          areaStyle: { opacity: 0.12 },
          data: series.lines?.[0]?.values ?? [],
        },
      ],
    }),
    [series],
  );
  const chart = useEChart(option, `${series.title} each day`);
  return <div ref={chart} className="chart" style={{ height: 240 }} />;
}

// --- sleep -----------------------------------------------------------------------------------------------------

function SleepBars({ series }: { series: Series }) {
  const option = useMemo<ChartOption>(() => {
    const [measured, estimated] = series.lines ?? [];
    return {
      tooltip: { trigger: "axis", axisPointer: { type: "shadow" }, valueFormatter: minutesText },
      legend: { bottom: 0 },
      grid: { left: 8, right: 16, top: 16, bottom: 40, containLabel: true },
      xAxis: { type: "category", data: (series.x ?? []).map(shortDay) },
      yAxis: { type: "value", axisLabel: { formatter: (value: number) => formatMinutes(value) } },
      series: [
        {
          id: "measured",
          name: "Measured by a health app",
          type: "bar",
          stack: "sleep",
          itemStyle: { color: "var(--cat-health)", decal: SOLID, borderRadius: [4, 4, 0, 0] },
          data: measured?.values ?? [],
        },
        {
          id: "estimated",
          name: "Estimated from the phone",
          type: "bar",
          stack: "sleep",
          itemStyle: { color: "var(--cat-other)", decal: STRIPED, borderRadius: [4, 4, 0, 0] },
          data: estimated?.values ?? [],
        },
      ],
    };
  }, [series]);
  const chart = useEChart(option, `${series.title}, in minutes: solid bars measured by a health app, striped bars estimated from the phone`);
  return <div ref={chart} className="chart" style={{ height: 280 }} />;
}

function Schedule({ series, estimated }: { series: Series; estimated: Set<number> }) {
  const option = useMemo<ChartOption>(() => {
    const days = (series.x ?? []).map(shortDay);
    const [asleep, awake] = series.lines ?? [];
    const points = (values: (number | null)[]) =>
      values.map((value, index) =>
        value === null
          ? null
          : { value: [index, value], itemStyle: estimated.has(index) ? { color: "transparent", borderWidth: 2 } : undefined, symbol: estimated.has(index) ? "emptyCircle" : "circle" },
      );
    const axis = hourAxis([...(asleep?.values ?? []), ...(awake?.values ?? [])]); // whole hours, never "02:20"
    return {
      tooltip: {
        trigger: "item",
        formatter: (params: { seriesName: string; value: [number, number] }) =>
          escapeHTML(`${params.seriesName}, ${days[params.value[0]]}: ${clock(params.value[1])}${estimated.has(params.value[0]) ? " (estimated)" : ""}`),
      },
      legend: { bottom: 0 },
      grid: { left: 8, right: 16, top: 16, bottom: 40, containLabel: true },
      xAxis: { type: "category", data: days },
      yAxis: { type: "value", inverse: true, ...(axis ?? { scale: true }), axisLabel: { formatter: (value: number) => clock(value) } },
      series: [
        { id: "asleep", name: "Asleep", type: "scatter", symbolSize: 11, itemStyle: { color: "var(--cat-study)", borderColor: "var(--cat-study)" }, data: points(asleep?.values ?? []) },
        { id: "awake", name: "Awake", type: "scatter", symbolSize: 11, itemStyle: { color: "var(--cat-health)", borderColor: "var(--cat-health)" }, data: points(awake?.values ?? []) },
      ],
    };
  }, [series, estimated]);
  const chart = useEChart(option, `${series.title}: when you fell asleep and woke up each night; hollow points are estimated`);
  return <div ref={chart} className="chart" style={{ height: 280 }} />;
}

function LateVsFocus({ series }: { series: Series }) {
  const option = useMemo<ChartOption>(() => {
    const points = series.points ?? [];
    const slope = series.stats?.slope;
    const intercept = series.stats?.intercept;
    const xs = points.map((point) => point.x);
    const line =
      typeof slope === "number" && typeof intercept === "number" && xs.length
        ? [Math.min(...xs), Math.max(...xs)].map((x) => [x, Math.round((intercept + slope * x) * 10) / 10])
        : [];
    return {
      tooltip: {
        trigger: "item",
        // A night starts on its day and ends the next morning (the sleep charts name a night by that morning).
        formatter: (params: { name: string; value: [number, number] }) =>
          escapeHTML(`The night of ${nightWords(params.name, shortDay)}: ${formatMinutes(params.value[0])} after 11 pm, then a focus score of ${params.value[1]}`),
      },
      grid: { left: 8, right: 16, top: 32, bottom: 28, containLabel: true }, // room for the y axis's name
      // hideOverlap: on a phone "1h 20m" and "1h 40m" would run into each other; every other label is dropped instead
      xAxis: { type: "value", name: "Minutes after 11 pm", nameLocation: "middle", nameGap: 26, axisLabel: { formatter: (value: number) => formatMinutes(value), hideOverlap: true } },
      yAxis: { type: "value", name: "Next day's focus", nameTextStyle: { align: "left" }, min: 0, max: 100 },
      series: [
        {
          id: "nights",
          type: "scatter",
          symbolSize: 12,
          itemStyle: { color: "var(--cat-video)" },
          data: points.map((point) => ({ name: point.label, value: [point.x, point.y] })),
        },
        { id: "trend", type: "line", data: line, showSymbol: false, lineStyle: { width: 2, type: "dashed", color: "var(--muted)" }, silent: true },
      ],
    };
  }, [series]);
  const trend = typeof series.stats?.slope === "number" ? ", with a trend line" : "";
  const chart = useEChart(option, `${series.title}: each night's minutes after 11 pm against the next day's focus score${trend}`);
  return <div ref={chart} className="chart" style={{ height: 300 }} />;
}

/** "12 nights · rho -0.90 · p < 0.001": the size and strength of the pattern, always shown with the chart, or the
 * hub's reason there is no number. */
function PatternStats({ series }: { series: Series }) {
  const n = Number(series.stats?.n ?? 0);
  const rho = series.stats?.rho;
  const p = series.stats?.p;
  return (
    <div className="pattern">
      <p className="pattern-stats">
        <strong>
          {n} {n === 1 ? "night" : "nights"}
        </strong>
        {typeof rho === "number" ? (
          <>
            <span>rho {rho.toFixed(2)}</span>
            {typeof p === "number" && <span>p {p < 0.001 ? "< 0.001" : p.toFixed(3)}</span>}
          </>
        ) : (
          <span>No pattern to show. {series.reason ?? "There isn't enough to compare yet."}</span>
        )}
      </p>
      {series.note && <p className="pattern-note">{series.note}</p>}
    </div>
  );
}

// --- the tab ---------------------------------------------------------------------------------------------------

export default function FocusSleepTab({ range, tz, today }: Props) {
  const focus = useInsights("focus", range, tz, today);
  const sleep = useInsights("sleep", range, tz, today);
  const focusData = focus.data;
  const sleepData = sleep.data;
  const words = focusData ? rangeWords(focusData) : sleepData ? rangeWords(sleepData) : undefined;
  const bestDay = focusData ? metric(focusData, "best_day")?.value : undefined;
  const nightsEstimated = valueOf(sleepData, "estimated_nights");
  const nights = valueOf(sleepData, "nights");
  const estimatedIndexes = useMemo(
    () => new Set((sleepData?.series.sleep_by_night?.lines?.[1]?.values ?? []).flatMap((value, index) => (value === null ? [] : [index]))),
    [sleepData],
  );
  const hasLines = (series: Series) => (series.lines ?? []).some((line) => line.values.some((value) => value !== null));
  const focused = focusData?.series.focus_by_day?.lines?.find((line) => line.key === "focused");

  return (
    <div className="stack">
      <div className="grid stat-grid">
        <StatCard label="Focused time" value={valueOf(focusData, "focused_time")} format={formatMinutes} tone="study" error={focus.error?.message} />
        <StatCard
          label="Focus score, on average" // the gauge's card below is "Focus score": two regions never share a name
          value={valueOf(focusData, "focus_score")}
          format={(value) => String(Math.round(value))}
          unit="/ 100"
          tone="study"
          hint={typeof bestDay === "string" ? `Highest score: ${longDay(bestDay)} (${valueOf(focusData, "best_score")})` : undefined}
          error={focus.error?.message}
        />
        <StatCard
          label="Sleep a night"
          value={valueOf(sleepData, "sleep")}
          format={formatMinutes}
          tone="health"
          estimated={typeof nightsEstimated === "number" && nightsEstimated > 0}
          hint={typeof nights === "number" && typeof nightsEstimated === "number" ? `${nightsEstimated} of ${nights} nights estimated` : undefined}
          error={sleep.error?.message}
        />
        <StatCard label="After 11 pm, a night" value={valueOf(sleepData, "late_night")} format={formatMinutes} tone="video" error={sleep.error?.message} />
      </div>
      {focus.error && <Failed what="Focus" message={focus.error.message} retry={focus.reload} />}
      {sleep.error && <Failed what="Sleep" message={sleep.error.message} retry={sleep.reload} />}
      <div className="grid chart-grid">
        {!focus.error && (
          <ChartCard
            title="Focused time each day"
            range={words}
            unit="minutes"
            source={focusData?.meta.source}
            loading={!focusData}
            info={focusData ? metric(focusData, "focused_time")?.explain : undefined}
          >
            {focusData && focused ? <FocusCalendar days={focusData.series.focus_by_day?.x ?? []} values={focused.values} /> : <p className="muted">No focus data in this range.</p>}
          </ChartCard>
        )}
        {!focus.error && (
          <SeriesCard data={focusData} name="score" title="Focus score" unit="score" words={words} empty="No focus score in this range.">
            {(found) =>
              found.value === null || found.value === undefined ? (
                <p className="muted">No focus score in this range.</p>
              ) : (
                <Gauge value={found.value} max={found.max ?? 100} color="var(--cat-study)" label={found.title} />
              )
            }
          </SeriesCard>
        )}
        {!focus.error && (
          <SeriesCard data={focusData} name="distraction_hours" title="When distractions happen" unit="minutes" words={words} empty="No time in social, video or games in this range.">
            {(found) =>
              found.cells?.length ? (
                <WeekdayHoursHeatmap series={found} color="var(--cat-social)" label={`${found.title}: minutes in social, video and game apps by weekday and hour`} />
              ) : (
                <p className="muted">No time in social, video or games in this range.</p>
              )
            }
          </SeriesCard>
        )}
        {!focus.error && (
          <SeriesCard data={focusData} name="switches_by_day" title="App switches an hour" unit="switches per hour" words={words} empty="No app switches in this range.">
            {(found) => (hasLines(found) ? <SwitchesByDay series={found} /> : <p className="muted">No app switches in this range.</p>)}
          </SeriesCard>
        )}
        {!sleep.error && (
          <SeriesCard data={sleepData} name="sleep_by_night" title="Sleep each night" unit="minutes" words={words} empty="No sleep in this range.">
            {(found) => (hasLines(found) ? <SleepBars series={found} /> : <p className="muted">No sleep in this range.</p>)}
          </SeriesCard>
        )}
        {!sleep.error && (
          <SeriesCard data={sleepData} name="schedule" title="Bedtime and wake time" unit="time" words={words} empty="No sleep times in this range.">
            {(found) => (hasLines(found) ? <Schedule series={found} estimated={estimatedIndexes} /> : <p className="muted">No sleep times in this range.</p>)}
          </SeriesCard>
        )}
        {!focus.error && (
          <SeriesCard
            data={focusData}
            name="late_vs_focus"
            title="Late nights and the next day's focus"
            unit="score"
            words={words}
            empty="A pattern needs at least two days in the range."
            wide
          >
            {(found) => (
              <>
                {found.points?.length ? <LateVsFocus series={found} /> : <p className="muted">No nights with a next day to compare yet.</p>}
                <PatternStats series={found} />
              </>
            )}
          </SeriesCard>
        )}
      </div>
    </div>
  );
}
