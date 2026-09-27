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
import { type InsightsData, type Series, metric, rangeWords, useInsights, valueOf } from "./shared";

type Props = { range: string; tz: string; today: string };

const WEEKDAY_NAMES: Record<string, string> = { Mon: "Monday", Tue: "Tuesday", Wed: "Wednesday", Thu: "Thursday", Fri: "Friday", Sat: "Saturday", Sun: "Sunday" };
const minutesText = (value: unknown) => (value === null || value === undefined || value === "-" ? "no data" : formatMinutes(Number(value)));
// Estimated sleep is striped and measured sleep is solid, whatever else the patterns mean elsewhere.
const STRIPED = { symbol: "rect", symbolSize: 1, dashArrayX: [1, 0], dashArrayY: [3, 4], rotation: -Math.PI / 4, color: "rgba(255, 255, 255, 0.55)" };
const SOLID = { symbol: "none" };

/** "23:30" for minutes after 18:00 the evening before (the hub's scale for bedtimes and wake times). */
function clock(sinceSix: number): string {
  const total = Math.round(sinceSix) + 18 * 60;
  const hours = Math.floor(total / 60) % 24;
  return `${String(hours).padStart(2, "0")}:${String(Math.round(total) % 60).padStart(2, "0")}`;
}

// --- focus -----------------------------------------------------------------------------------------------------

/** Days as a GitHub-style calendar: a column per week (Monday first), a row per weekday, darker for more focus. */
function FocusCalendar({ days, values }: { days: string[]; values: (number | null)[] }) {
  if (!days.length) return null;
  const weekday = (day: string) => (new Date(`${day}T12:00:00`).getDay() + 6) % 7; // Monday 0
  const start = shiftDay(days[0], -weekday(days[0]));
  const byDay = new Map(days.map((day, index) => [day, values[index]]));
  const weeks = Math.ceil((days.length + weekday(days[0])) / 7);
  const peak = Math.max(1, ...values.map((value) => value ?? 0));
  const known = days.filter((_, index) => values[index] !== null && values[index] !== undefined);
  const best = known.reduce<string | null>((top, day) => ((byDay.get(day) ?? 0) > (top ? (byDay.get(top) ?? 0) : -1) ? day : top), null);
  const missing = days.filter((_, index) => values[index] === null || values[index] === undefined);
  const summary = best
    ? `Focused minutes each day: most on ${longDay(best)} (${formatMinutes(byDay.get(best) ?? 0)}); ${known.length} of ${days.length} days with data${
        missing.length ? `, none on ${missing.map(shortDay).join(", ")}` : ""
      }.`
    : "No focus data in this range.";
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
      const style = { gridRow: row + 1, gridColumn: week + 2, "--level": value ? Math.max(0.18, value / peak) : 0 } as CSSProperties;
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

function DistractionHours({ series }: { series: Series }) {
  const option = useMemo<ChartOption>(() => {
    const cells = series.cells ?? [];
    const hours = series.x ?? [];
    const weekdays = series.y ?? [];
    const next = (hour: string) => String((Number(hour) + 1) % 24).padStart(2, "0");
    return {
      tooltip: {
        position: "top",
        formatter: (params: { value: [number, number, number] }) => {
          const [hour, day, value] = params.value;
          return escapeHTML(`${WEEKDAY_NAMES[weekdays[day]] ?? weekdays[day]}, ${hours[hour]}:00 to ${next(hours[hour])}:00: ${formatMinutes(value)}`);
        },
      },
      grid: { left: 8, right: 8, top: 8, bottom: 56, containLabel: true },
      xAxis: { type: "category", data: hours, axisLabel: { interval: 2 }, splitArea: { show: false } },
      yAxis: { type: "category", data: weekdays, inverse: true },
      visualMap: {
        min: 0,
        max: Math.max(1, ...cells.map((cell) => cell.value)),
        orient: "horizontal",
        left: "center",
        bottom: 0,
        itemHeight: 140,
        itemWidth: 12,
        text: ["More", "Less"],
        textStyle: { color: "var(--muted)" },
        inRange: { color: ["var(--surface-2)", "var(--cat-social)"] },
      },
      series: [
        {
          id: "distractions",
          type: "heatmap",
          data: cells.map((cell) => [cell.x, cell.y, cell.value]),
          itemStyle: { borderColor: "var(--surface)", borderWidth: 2, borderRadius: 3, decal: SOLID },
          emphasis: { itemStyle: { borderColor: "var(--text)", borderWidth: 1 } },
        },
      ],
    };
  }, [series]);
  const chart = useEChart(option, `${series.title}: minutes in social, video and game apps by weekday and hour`);
  return <div ref={chart} className="chart" style={{ height: 300 }} data-no-swipe />;
}

function ScoreGauge({ series }: { series: Series }) {
  const option = useMemo<ChartOption>(
    () => ({
      series: [
        {
          id: "score",
          type: "gauge",
          min: 0,
          max: series.max ?? 100,
          startAngle: 210,
          endAngle: -30,
          radius: "92%",
          center: ["50%", "58%"],
          progress: { show: true, width: 16, roundCap: true, itemStyle: { color: "var(--cat-study)", decal: SOLID } },
          axisLine: { roundCap: true, lineStyle: { width: 16, color: [[1, "var(--surface-2)"]] } },
          axisTick: { show: false },
          splitLine: { show: false },
          axisLabel: { show: false },
          pointer: { show: false },
          anchor: { show: false },
          title: { show: false },
          detail: { valueAnimation: true, offsetCenter: [0, "0%"], fontSize: 40, fontWeight: 800, color: "var(--text)", formatter: "{value}" },
          data: [{ value: series.value ?? 0, name: series.title, itemStyle: { decal: SOLID } }],
        },
      ],
    }),
    [series],
  );
  const chart = useEChart(option, `${series.title}: ${series.value ?? "no data"} out of ${series.max ?? 100}`);
  return <div ref={chart} className="chart" style={{ height: 220 }} />;
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
    const points = (values: (number | null)[]) =>
      values.map((value, index) =>
        value === null
          ? null
          : { value: [index, value], itemStyle: estimated.has(index) ? { color: "transparent", borderWidth: 2 } : undefined, symbol: estimated.has(index) ? "emptyCircle" : "circle" },
      );
    const [asleep, awake] = series.lines ?? [];
    return {
      tooltip: {
        trigger: "item",
        formatter: (params: { seriesName: string; value: [number, number]; dataIndex: number }) =>
          escapeHTML(`${params.seriesName}, ${days[params.value[0]]}: ${clock(params.value[1])}${estimated.has(params.value[0]) ? " (estimated)" : ""}`),
      },
      legend: { bottom: 0 },
      grid: { left: 8, right: 16, top: 16, bottom: 40, containLabel: true },
      xAxis: { type: "category", data: days },
      yAxis: { type: "value", inverse: true, scale: true, axisLabel: { formatter: (value: number) => clock(value) } },
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
        formatter: (params: { seriesId: string; data: { label?: string; value: [number, number] } }) =>
          params.seriesId === "trend"
            ? "The trend line"
            : escapeHTML(`The night of ${shortDay(params.data.label ?? "")}: ${formatMinutes(params.data.value[0])} after 11 pm, then a focus score of ${params.data.value[1]}`),
      },
      grid: { left: 8, right: 16, top: 32, bottom: 28, containLabel: true }, // room for the y axis's name
      xAxis: { type: "value", name: "Minutes after 11 pm", nameLocation: "middle", nameGap: 26, axisLabel: { formatter: (value: number) => formatMinutes(value) } },
      yAxis: { type: "value", name: "Next day's focus", nameTextStyle: { align: "left" }, min: 0, max: 100 },
      series: [
        {
          id: "nights",
          type: "scatter",
          symbolSize: 12,
          itemStyle: { color: "var(--cat-video)" },
          data: points.map((point) => ({ value: [point.x, point.y], label: point.label })),
        },
        { id: "trend", type: "line", data: line, showSymbol: false, lineStyle: { width: 2, type: "dashed", color: "var(--muted)" }, silent: true },
      ],
    };
  }, [series]);
  const chart = useEChart(option, `${series.title}: each night's minutes after 11 pm against the next day's focus score, with a trend line`);
  return <div ref={chart} className="chart" style={{ height: 300 }} />;
}

/** "12 nights · rho -0.90 · p 0.0001": the size and strength of the pattern, always shown with the chart. */
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
          <span>Too few nights for a pattern yet (it needs 3).</span>
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

  const card = (data: InsightsData | undefined, key: string, title: string, unit: string, body: (found: Series) => ReactNode, empty: string, wide = false) => {
    const found = data?.series[key];
    return (
      <ChartCard
        title={found?.title ?? title}
        range={words}
        unit={unit}
        source={data?.meta.source}
        estimated={found?.estimated}
        loading={!data}
        info={found?.explain}
        className={wide ? "chart-card-wide" : undefined}
      >
        {found ? body(found) : <p className="muted">{empty}</p>}
      </ChartCard>
    );
  };
  const hasLines = (series: Series) => (series.lines ?? []).some((line) => line.values.some((value) => value !== null));
  const failed = (what: string, error: { message: string } | undefined, retry: () => void) =>
    error && (
      <ChartCard title={what}>
        <p className="muted">
          {what} couldn&apos;t load: {error.message}
        </p>
        <button type="button" className="button button-ghost" onClick={retry}>
          Try again
        </button>
      </ChartCard>
    );
  const focusLine = focusData?.series.focus_by_day?.lines?.find((line) => line.name === "Focused");

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
          hint={typeof bestDay === "string" ? `Best: ${longDay(bestDay)} (${valueOf(focusData, "best_score")})` : undefined}
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
      {failed("Focus", focus.error, focus.reload)}
      {failed("Sleep", sleep.error, sleep.reload)}
      <div className="grid chart-grid">
        {!focus.error && (
          <ChartCard title="Focused time each day" range={words} unit="minutes" source={focusData?.meta.source} loading={!focusData} info={focusData?.series.focus_by_day?.explain}>
            {focusData && focusLine ? <FocusCalendar days={focusData.series.focus_by_day?.x ?? []} values={focusLine.values} /> : <p className="muted">No focus data in this range.</p>}
          </ChartCard>
        )}
        {!focus.error &&
          card(focusData, "score", "Focus score", "score", (found) => (found.value === null || found.value === undefined ? <p className="muted">No focus score in this range.</p> : <ScoreGauge series={found} />), "No focus score in this range.")}
        {!focus.error &&
          card(focusData, "distraction_hours", "When distractions happen", "minutes", (found) =>
            found.cells?.length ? <DistractionHours series={found} /> : <p className="muted">No time in social, video or games in this range.</p>,
          "No time in social, video or games in this range.")}
        {!focus.error &&
          card(focusData, "switches_by_day", "App switches an hour", "switches per hour", (found) =>
            hasLines(found) ? <SwitchesByDay series={found} /> : <p className="muted">No app switches in this range.</p>,
          "No app switches in this range.")}
        {!sleep.error &&
          card(sleepData, "sleep_by_night", "Sleep each night", "minutes", (found) =>
            hasLines(found) ? <SleepBars series={found} /> : <p className="muted">No sleep in this range.</p>,
          "No sleep in this range.")}
        {!sleep.error &&
          card(sleepData, "schedule", "Bedtime and wake time", "time", (found) =>
            hasLines(found) ? <Schedule series={found} estimated={estimatedIndexes} /> : <p className="muted">No sleep times in this range.</p>,
          "No sleep times in this range.")}
        {!focus.error &&
          card(
            focusData,
            "late_vs_focus",
            "Late nights and the next day's focus",
            "score",
            (found) => (
              <>
                {found.points?.length ? <LateVsFocus series={found} /> : <p className="muted">No nights with a next day to compare yet.</p>}
                <PatternStats series={found} />
              </>
            ),
            "A pattern needs at least two days in the range.",
            true,
          )}
      </div>
    </div>
  );
}
