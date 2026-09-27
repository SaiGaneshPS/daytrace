// DT-56: the pieces every Insights tab builds with: a card for one of the hub's series (its title, range, unit,
// source, "Estimated" badge and explanation), the card saying a tab's answer couldn't load, and the weekday by hour
// heatmap (the Overview's screens and the Focus tab's distractions).
import { type ReactNode, useMemo } from "react";
import ChartCard from "../../components/ChartCard";
import { formatMinutes } from "../../components/StatCard";
import { type ChartOption, escapeHTML, useEChart } from "../../theme/charts";
import type { InsightsData, Series } from "./shared";

export const WEEKDAY_NAMES: Record<string, string> = {
  Mon: "Monday", Tue: "Tuesday", Wed: "Wednesday", Thu: "Thursday", Fri: "Friday", Sat: "Saturday", Sun: "Sunday",
};

/** A chart's minutes for a tooltip, "no data" for a gap. */
export const minutesText = (value: unknown) => (value === null || value === undefined || value === "-" ? "no data" : formatMinutes(Number(value)));

type SeriesCardProps = {
  data: InsightsData | undefined;
  /** The series' name in the hub's answer. */
  name: string;
  /** The title while loading (the hub's title once it answers). */
  title: string;
  unit: string;
  words?: string;
  /** Said when the answer has no such series. */
  empty: string;
  /** The card takes the whole row. */
  wide?: boolean;
  children: (found: Series) => ReactNode;
};

/** A card for one of the hub's series, with its title, range, unit, source, "Estimated" and explanation. */
export function SeriesCard({ data, name, title, unit, words, empty, wide, children }: SeriesCardProps) {
  const found = data?.series[name];
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
      {found ? children(found) : <p className="muted">{empty}</p>}
    </ChartCard>
  );
}

/** A tab's answer couldn't load: why, and Try again. */
export function Failed({ what, message, retry }: { what: string; message: string; retry: () => void }) {
  return (
    <ChartCard title={what}>
      <p className="muted">
        {what} couldn&apos;t load: {message}
      </p>
      <button type="button" className="button button-ghost" onClick={retry}>
        Try again
      </button>
    </ChartCard>
  );
}

/** A number out of `max` on an arc (the focus score, the share of plans kept), in `color` (a token). */
export function Gauge({ value, max, color, label, suffix = "" }: { value: number; max: number; color: string; label: string; suffix?: string }) {
  const option = useMemo<ChartOption>(
    () => ({
      series: [
        {
          id: "gauge",
          type: "gauge",
          min: 0,
          max,
          startAngle: 210,
          endAngle: -30,
          radius: "92%",
          center: ["50%", "58%"],
          progress: { show: true, width: 16, roundCap: true, itemStyle: { color, decal: { symbol: "none" } } },
          axisLine: { roundCap: true, lineStyle: { width: 16, color: [[1, "var(--surface-2)"]] } },
          axisTick: { show: false },
          splitLine: { show: false },
          axisLabel: { show: false },
          pointer: { show: false },
          anchor: { show: false },
          title: { show: false },
          detail: { valueAnimation: true, offsetCenter: [0, "0%"], fontSize: 40, fontWeight: 800, color: "var(--text)", formatter: `{value}${suffix}` },
          data: [{ value, name: label, itemStyle: { decal: { symbol: "none" } } }],
        },
      ],
    }),
    [value, max, color, label, suffix],
  );
  const chart = useEChart(option, `${label}: ${value}${suffix} out of ${max}${suffix}`);
  return <div ref={chart} className="chart" style={{ height: 220 }} />;
}

/** Minutes by weekday and hour of the day, shaded from the card's gray to `color` (a token). */
export function WeekdayHoursHeatmap({ series, color, label }: { series: Series; color: string; label: string }) {
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
          return escapeHTML(`${WEEKDAY_NAMES[weekdays[weekday]] ?? weekdays[weekday]}, ${hours[hour]}:00 to ${next(hours[hour])}:00: ${formatMinutes(value)}`);
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
        inRange: { color: ["var(--surface-2)", color] },
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
  }, [series, color]);
  const chart = useEChart(option, label);
  return <div ref={chart} className="chart" style={{ height: 300 }} data-no-swipe />;
}
