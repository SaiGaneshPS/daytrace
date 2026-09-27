// DT-34: the Insights Overview tab. The range's numbers with how they changed against the days just before, screen
// time by device day by day, where it went (categories), phone against computer, when screens were on (weekday by
// hour), and the best and toughest days with the reason. Everything is the hub's (GET /insights/overview): the
// donut, the stacked days and the heatmap are cut from the same minutes, so they add up to the same total.
import { type ReactNode, useMemo } from "react";
import ChartCard from "../../components/ChartCard";
import { longDay, shortDay } from "../../components/DayPicker";
import StatCard, { formatMinutes } from "../../components/StatCard";
import { type ChartOption, categoryStyle, useEChart } from "../../theme/charts";
import { deviceColors } from "../../theme/devices";
import { ChangeChip, type InsightsData, type Series, metric, rangeWords, useInsights, valueOf } from "./shared";

type Props = { range: string; tz: string; today: string };

const WEEKDAYS: Record<string, string> = { Mon: "Monday", Tue: "Tuesday", Wed: "Wednesday", Thu: "Thursday", Fri: "Friday", Sat: "Saturday", Sun: "Sunday" };

const minutesText = (value: unknown) => (value === null || value === undefined || value === "-" ? "no data" : formatMinutes(Number(value)));
const hasValues = (series: Series | undefined) =>
  !!series && ((series.lines ?? []).some((line) => line.values.some((value) => value)) || (series.items ?? []).length > 0 || (series.cells ?? []).length > 0);

function Empty() {
  return <p className="muted">No screen time in this range.</p>;
}

function ScreenByDevice({ series }: { series: Series }) {
  const option = useMemo<ChartOption>(() => {
    const lines = series.lines ?? [];
    const colors = deviceColors(lines.map((line) => line.device_type)); // by kind, and two of a kind apart
    return {
      tooltip: { trigger: "axis", valueFormatter: minutesText },
      legend: { bottom: 0, type: "scroll" },
      grid: { left: 8, right: 16, top: 16, bottom: 40, containLabel: true },
      xAxis: { type: "category", boundaryGap: false, data: (series.x ?? []).map(shortDay) },
      yAxis: { type: "value", axisLabel: { formatter: (value: number) => formatMinutes(value) } },
      series: lines.map((line, index) => ({
        id: line.key ?? line.name,
        name: line.name,
        type: "line",
        stack: "screen",
        smooth: false, // a smoothed stack overshoots where a line drops (today, so far)
        showSymbol: false,
        areaStyle: { opacity: 0.35 },
        lineStyle: { width: 2 },
        emphasis: { focus: "series" },
        itemStyle: { color: colors[index] },
        data: line.values,
      })),
    };
  }, [series]);
  const chart = useEChart(option, `${series.title}, stacked by device, in minutes`);
  return <div ref={chart} className="chart" style={{ height: 300 }} />;
}

function Categories({ series, total }: { series: Series; total: number | null }) {
  const option = useMemo<ChartOption>(
    () => ({
      tooltip: {
        trigger: "item",
        formatter: (params: { name: string; value: number; percent: number }) => `${params.name}: ${formatMinutes(params.value)} (${params.percent}%)`,
      },
      legend: { bottom: 0, type: "scroll" },
      series: [
        {
          id: "categories",
          type: "pie",
          radius: ["52%", "74%"],
          center: ["50%", "44%"],
          label: { show: false },
          emphasis: { scale: true, scaleSize: 6 },
          data: (series.items ?? []).map((item) => ({ name: item.name, value: item.value, itemStyle: categoryStyle(item.category) })),
        },
      ],
    }),
    [series],
  );
  const chart = useEChart(option, `${series.title}: screen time by kind of app or site, in minutes`);
  return (
    <div className="donut">
      <div ref={chart} className="chart" style={{ height: 300 }} />
      {total !== null && (
        <p className="donut-total" aria-hidden="true">
          <strong>{formatMinutes(total)}</strong>
          <span>in all</span>
        </p>
      )}
    </div>
  );
}

function PhoneAndComputer({ series }: { series: Series }) {
  const option = useMemo<ChartOption>(
    () => ({
      tooltip: { trigger: "axis", axisPointer: { type: "shadow" }, valueFormatter: minutesText },
      legend: { bottom: 0 },
      grid: { left: 8, right: 16, top: 16, bottom: 40, containLabel: true },
      xAxis: { type: "category", data: (series.x ?? []).map(shortDay) },
      yAxis: { type: "value", axisLabel: { formatter: (value: number) => formatMinutes(value) } },
      series: (series.lines ?? []).map((line) => ({
        id: line.key ?? line.name,
        name: line.name,
        type: "bar",
        stack: "split",
        itemStyle: { ...categoryStyle(line.category), borderRadius: 0 },
        data: line.values,
      })),
    }),
    [series],
  );
  const chart = useEChart(option, `${series.title} each day, in minutes`);
  return <div ref={chart} className="chart" style={{ height: 280 }} />;
}

function Hours({ series }: { series: Series }) {
  const option = useMemo<ChartOption>(() => {
    const cells = series.cells ?? [];
    const hours = series.x ?? [];
    const weekdays = series.y ?? [];
    const next = (hour: string) => String((Number(hour) + 1) % 24).padStart(2, "0");
    return {
      tooltip: {
        position: "top",
        formatter: (params: { value: [number, number, number] }) => {
          const [hour, weekday, value] = params.value;
          return `${WEEKDAYS[weekdays[weekday]] ?? weekdays[weekday]}, ${hours[hour]}:00 to ${next(hours[hour])}:00: ${formatMinutes(value)}`;
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
        inRange: { color: ["var(--surface-2)", "var(--cat-work)"] },
      },
      series: [
        {
          id: "hours",
          type: "heatmap",
          data: cells.map((cell) => [cell.x, cell.y, cell.value]),
          // No pattern: a cell's shade is its only mark, and each one's minutes are in its tooltip.
          itemStyle: { borderColor: "var(--surface)", borderWidth: 2, borderRadius: 3, decal: { symbol: "none" } },
          emphasis: { itemStyle: { borderColor: "var(--text)", borderWidth: 1 } },
        },
      ],
    };
  }, [series]);
  const chart = useEChart(option, `${series.title}: minutes by weekday and hour of the day`);
  return <div ref={chart} className="chart" style={{ height: 300 }} data-no-swipe />;
}

type DayCardProps = { data: InsightsData | undefined; title: string; dayId: string; valueId: string; reason: string; tone: string; words?: string };

function DayCard({ data, title, dayId, valueId, reason, tone, words }: DayCardProps) {
  const day = data ? metric(data, dayId) : undefined;
  const value = valueOf(data, valueId);
  return (
    <ChartCard title={title} range={words} loading={!data} className={`day-card cat-${tone}`} source={data?.meta.source} info={day?.explain}>
      {typeof day?.value === "string" && typeof value === "number" ? (
        <>
          <p className="day-card-day">{longDay(day.value)}</p>
          <p className="day-card-why">
            {reason}: <strong>{formatMinutes(value)}</strong>
          </p>
        </>
      ) : (
        <p className="muted">{day?.explain ?? "Nothing to show for this range."}</p>
      )}
    </ChartCard>
  );
}

const STATS = [
  { id: "screen_time", change: "daily_average", label: "Screen time", tone: "work", format: formatMinutes },
  { id: "focused_time", change: "focused_time", label: "Focused time", tone: "study", format: formatMinutes },
  { id: "focus_score", change: "focus_score", label: "Focus score", tone: "study", format: (value: number) => String(Math.round(value)), unit: "/ 100" },
  { id: "pickups", change: "pickups", label: "Pickups a day", tone: "social", format: (value: number) => value.toLocaleString(undefined, { maximumFractionDigits: 1 }) },
  { id: "sleep", change: "sleep", label: "Sleep a night", tone: "health", format: formatMinutes },
  { id: "late_night", change: "late_night", label: "After 11 pm, a night", tone: "video", format: formatMinutes },
] as const;

export default function OverviewTab({ range, tz, today }: Props) {
  const { data, error, reload } = useInsights("overview", range, tz, today);
  if (error) {
    return (
      <ChartCard title="Overview">
        <p className="muted">The overview couldn&apos;t load: {error.message}</p>
        <button type="button" className="button button-ghost" onClick={reload}>
          Try again
        </button>
      </ChartCard>
    );
  }
  const words = data ? rangeWords(data) : undefined;
  const source = data?.meta.source;
  const series = data?.series ?? {};
  const changes = new Map((data?.changes ?? []).map((change) => [change.id, change]));
  const total = valueOf(data, "screen_time") ?? null;
  const card = (key: string, title: string, unit: string, body: (found: Series) => ReactNode) => {
    const found = series[key];
    return (
      <ChartCard title={found?.title ?? title} range={words} unit={unit} source={source} estimated={found?.estimated} loading={!data} info={found?.explain}>
        {found && hasValues(found) ? body(found) : <Empty />}
      </ChartCard>
    );
  };

  return (
    <div className="stack">
      <div className="grid stat-grid">
        {STATS.map((stat) => {
          const found = data ? metric(data, stat.id) : undefined;
          const average = stat.id === "screen_time" ? valueOf(data, "daily_average") : undefined;
          return (
            <StatCard
              key={stat.id}
              label={stat.label}
              value={valueOf(data, stat.id)}
              format={stat.format}
              unit={"unit" in stat ? stat.unit : undefined}
              tone={stat.tone}
              estimated={found?.estimated}
              hint={typeof average === "number" ? `${formatMinutes(average)} a day` : undefined}
            >
              <ChangeChip change={changes.get(stat.change)} days={data?.range.days ?? 0} />
            </StatCard>
          );
        })}
      </div>
      <div className="grid chart-grid">
        {card("screen_by_device", "Screen time by device", "minutes", (found) => (
          <>
            {total !== null && <p className="muted chart-total">{formatMinutes(total)} in all, each device counted.</p>}
            <ScreenByDevice series={found} />
          </>
        ))}
        {card("categories", "By category", "minutes", (found) => <Categories series={found} total={total} />)}
        {card("phone_vs_computer", "Phone and computer", "minutes", (found) => <PhoneAndComputer series={found} />)}
        {card("hours", "When screens were on", "minutes", (found) => <Hours series={found} />)}
      </div>
      <div className="grid grid-2">
        <DayCard data={data} title="Best day" dayId="best_day" valueId="best_day_focused" reason="The most focused time" tone="study" words={words} />
        <DayCard data={data} title="Toughest day" dayId="toughest_day" valueId="toughest_day_late" reason="The most screen time after 11 pm" tone="video" words={words} />
      </div>
    </div>
  );
}
