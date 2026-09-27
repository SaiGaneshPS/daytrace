// DT-57: the Insights Food and Calendar tab. Meal habits and how well the calendar's plans matched what happened:
// when you ate (a dot per meal, by type, with late-night eating shaded), meals each day, the foods logged most (as
// logged: no calorie claims), each event split into what its time went to (on plan, off plan, other screens, no
// screen, adding up to its length), the share of plans kept, planned time by day, and time in meetings each day.
// Everything is the hub's (GET /insights/food and /insights/calendar).
import { useMemo } from "react";
import ChartCard from "../../components/ChartCard";
import { longDay, shortDay } from "../../components/DayPicker";
import StatCard, { formatMinutes } from "../../components/StatCard";
import { type ChartOption, escapeHTML, useEChart } from "../../theme/charts";
import { useMediaQuery } from "../../theme/motion";
import { Failed, Gauge, SeriesCard, minutesText } from "./parts";
import { type Series, metric, rangeWords, useInsights, valueOf } from "./shared";

type Props = { range: string; tz: string; today: string };

/** Each meal type's color and mark (never color alone). */
const MEALS: Record<string, { label: string; color: string; symbol: string }> = {
  breakfast: { label: "Breakfast", color: "var(--cat-games)", symbol: "circle" },
  lunch: { label: "Lunch", color: "var(--cat-comms)", symbol: "rect" },
  dinner: { label: "Dinner", color: "var(--cat-video)", symbol: "triangle" },
  snack: { label: "Snack", color: "var(--cat-social)", symbol: "diamond" },
  other: { label: "Other", color: "var(--cat-other)", symbol: "pin" },
};
/** What a planned event's time went to, as the hub splits it, and a part for a day nobody's screens could say. */
const PARTS: Record<string, { color: string; decal: Record<string, unknown> }> = {
  on_plan: { color: "var(--cat-study)", decal: { symbol: "none" } },
  off_plan: { color: "var(--cat-social)", decal: { symbol: "rect", symbolSize: 1, dashArrayX: [1, 0], dashArrayY: [3, 4], rotation: Math.PI / 4, color: "rgba(255,255,255,0.55)" } },
  other: { color: "var(--cat-comms)", decal: { symbol: "circle", symbolSize: 0.6, dashArrayX: [[6, 6], [0, 6, 6, 0]], dashArrayY: [5, 0], color: "rgba(255,255,255,0.55)" } },
  idle: { color: "var(--surface-2)", decal: { symbol: "none" } },
  unknown: { color: "var(--border)", decal: { symbol: "rect", symbolSize: 1, dashArrayX: [1, 0], dashArrayY: [2, 5], rotation: -Math.PI / 4, color: "rgba(0,0,0,0.25)" } },
};
const LATE_FROM = 22; // the hub's late-night eating: 22:00 to 04:00
const LATE_UNTIL = 4;
const hour = (value: number) => `${String(Math.floor(value) % 24).padStart(2, "0")}:${String(Math.round((value % 1) * 60)).padStart(2, "0")}`;

// --- food ------------------------------------------------------------------------------------------------------

function MealTimes({ series }: { series: Series }) {
  const option = useMemo<ChartOption>(() => {
    const days = (series.x ?? []).map(shortDay);
    const points = series.points ?? [];
    const groups = Object.keys(MEALS).filter((group) => points.some((point) => (point.group ?? "other") === group));
    return {
      tooltip: {
        trigger: "item",
        formatter: (params: { seriesName: string; value: [number, number]; name: string }) =>
          escapeHTML(`${params.seriesName}, ${days[params.value[0]]} at ${hour(params.value[1])}: ${params.name}`),
      },
      legend: { bottom: 0 },
      grid: { left: 8, right: 16, top: 16, bottom: 40, containLabel: true },
      xAxis: { type: "category", data: days },
      yAxis: { type: "value", min: 0, max: 24, interval: 4, inverse: true, axisLabel: { formatter: (value: number) => hour(value) } },
      series: groups.map((group, index) => ({
        id: group,
        name: MEALS[group].label,
        type: "scatter",
        symbol: MEALS[group].symbol,
        symbolSize: 12,
        itemStyle: { color: MEALS[group].color },
        data: points.filter((point) => (point.group ?? "other") === group).map((point) => ({ name: point.label, value: [point.x, point.y] })),
        // Late-night eating, shaded once (on the first series): 22:00 to midnight and midnight to 04:00.
        ...(index === 0
          ? {
              markArea: {
                silent: true,
                itemStyle: { color: "var(--surface-2)", opacity: 0.7 },
                label: { show: true, position: "insideTopLeft", color: "var(--muted)", formatter: "Late" },
                data: [
                  [{ yAxis: LATE_FROM }, { yAxis: 24 }],
                  [{ yAxis: 0 }, { yAxis: LATE_UNTIL }],
                ],
              },
            }
          : {}),
      })),
    };
  }, [series]);
  const chart = useEChart(option, `${series.title}: each meal at its time of day, by type; late-night eating (22:00 to 04:00) is shaded`);
  return <div ref={chart} className="chart" style={{ height: 320 }} />;
}

function MealsByDay({ series }: { series: Series }) {
  const option = useMemo<ChartOption>(
    () => ({
      tooltip: { trigger: "axis", axisPointer: { type: "shadow" }, valueFormatter: (value: unknown) => (value === null || value === undefined || value === "-" ? "no data" : String(value)) },
      legend: { bottom: 0 },
      grid: { left: 8, right: 16, top: 16, bottom: 40, containLabel: true },
      xAxis: { type: "category", data: (series.x ?? []).map(shortDay) },
      yAxis: { type: "value", minInterval: 1 },
      series: (series.lines ?? []).map((line) => {
        const meal = MEALS[line.key ?? "other"] ?? MEALS.other;
        return { id: line.key ?? line.name, name: meal.label, type: "bar", stack: "meals", itemStyle: { color: meal.color }, data: line.values };
      }),
    }),
    [series],
  );
  const chart = useEChart(option, `${series.title}, by type`);
  return <div ref={chart} className="chart" style={{ height: 260 }} />;
}

function TopFoods({ series }: { series: Series }) {
  const items = series.items ?? [];
  if (!items.length) return <p className="muted">No meals logged in this range.</p>;
  return (
    <ol className="top-foods">
      {items.map((item) => (
        <li key={item.name}>
          <span>{item.name}</span>
          <strong>{item.value === 1 ? "once" : `${item.value} times`}</strong>
        </li>
      ))}
    </ol>
  );
}

// --- calendar --------------------------------------------------------------------------------------------------

function EventSplit({ series }: { series: Series }) {
  const wide = useMediaQuery("(min-width: 40rem)"); // narrower: shorter names and fewer ticks, the list below has them in full
  const option = useMemo<ChartOption>(() => {
    const events = [...(series.items ?? [])].reverse(); // the longest at the top
    const names = events.map((event) => `${event.name} (${event.key ? shortDay(event.key) : ""})`);
    const parts = ["on_plan", "off_plan", "other", "idle"];
    const labels: Record<string, string> = { on_plan: "On plan", off_plan: "Off plan", other: "Other screen time", idle: "No screen", unknown: "No screen data that day" };
    const partOf = (children: (typeof events)[number]["children"], key: string) => children?.find((child) => child.key === key)?.value ?? 0;
    const bars = [
      ...parts.map((key) => ({
        id: key,
        name: labels[key],
        type: "bar",
        stack: "event",
        barMaxWidth: 26,
        itemStyle: { color: PARTS[key].color, decal: PARTS[key].decal },
        data: events.map((event) => (event.children ? partOf(event.children, key) : 0)),
      })),
      // An event on a day with no screen data: its whole length is unknown, never 0 on plan.
      ...(events.some((event) => !event.children)
        ? [{ id: "unknown", name: labels.unknown, type: "bar", stack: "event", barMaxWidth: 26, itemStyle: { color: PARTS.unknown.color, decal: PARTS.unknown.decal },
             data: events.map((event) => (event.children ? 0 : event.value)) }]
        : []),
    ];
    return {
      tooltip: { trigger: "axis", axisPointer: { type: "shadow" }, valueFormatter: minutesText },
      legend: { bottom: 0, type: "scroll" },
      grid: { left: 8, right: 16, top: 8, bottom: 40, containLabel: true },
      xAxis: { type: "value", splitNumber: wide ? 5 : 2, axisLabel: { hideOverlap: true, formatter: (value: number) => formatMinutes(value) } },
      yAxis: { type: "category", data: names, axisLabel: { width: wide ? 150 : 90, overflow: "truncate" } },
      series: bars,
    };
  }, [series, wide]);
  const chart = useEChart(option, `${series.title}: each event's minutes on plan, off plan, on other screens and with no screen`);
  const events = series.items ?? [];
  return (
    <>
      <div ref={chart} className="chart" style={{ height: Math.max(220, events.length * 34 + 70) }} data-no-swipe />
      <ul className="event-list">
        {events.map((event, index) => (
          <li key={`${event.key}|${event.name}|${index}`}>
            <span>
              {event.name} <span className="muted">{event.key ? longDay(event.key) : ""}</span>
            </span>
            <strong>{event.share === null || event.share === undefined ? "no screen data" : `${event.share}% as planned`}</strong>
          </li>
        ))}
      </ul>
    </>
  );
}

function PlanByDay({ series }: { series: Series }) {
  const option = useMemo<ChartOption>(
    () => ({
      tooltip: { trigger: "axis", axisPointer: { type: "shadow" }, valueFormatter: minutesText },
      legend: { bottom: 0, type: "scroll" },
      grid: { left: 8, right: 16, top: 16, bottom: 40, containLabel: true },
      xAxis: { type: "category", data: (series.x ?? []).map(shortDay) },
      yAxis: { type: "value", axisLabel: { formatter: (value: number) => formatMinutes(value) } },
      series: (series.lines ?? []).map((line) => ({
        id: line.key ?? line.name,
        name: line.name,
        type: "bar",
        stack: "plan",
        itemStyle: { color: PARTS[line.key ?? "idle"]?.color ?? "var(--cat-other)", decal: PARTS[line.key ?? "idle"]?.decal },
        data: line.values,
      })),
    }),
    [series],
  );
  const chart = useEChart(option, `${series.title}: each day's planned minutes by what they went to`);
  return <div ref={chart} className="chart" style={{ height: 280 }} />;
}

function Meetings({ series }: { series: Series }) {
  const option = useMemo<ChartOption>(
    () => ({
      tooltip: { trigger: "axis", axisPointer: { type: "shadow" }, valueFormatter: minutesText },
      grid: { left: 8, right: 16, top: 16, bottom: 8, containLabel: true },
      xAxis: { type: "category", data: (series.x ?? []).map(shortDay) },
      yAxis: { type: "value", minInterval: 1, axisLabel: { formatter: (value: number) => formatMinutes(value) } },
      series: [{ id: "meetings", name: "In meetings", type: "bar", itemStyle: { color: "var(--cat-work)", borderRadius: [4, 4, 0, 0] }, data: series.lines?.[0]?.values ?? [] }],
    }),
    [series],
  );
  const chart = useEChart(option, `${series.title}, in minutes`);
  return <div ref={chart} className="chart" style={{ height: 240 }} />;
}

// --- the tab ---------------------------------------------------------------------------------------------------

export default function FoodCalendarTab({ range, tz, today }: Props) {
  const food = useInsights("food", range, tz, today);
  const calendar = useInsights("calendar", range, tz, today);
  const foodData = food.data;
  const calendarData = calendar.data;
  const words = foodData ? rangeWords(foodData) : calendarData ? rangeWords(calendarData) : undefined;
  const busiest = calendarData ? metric(calendarData, "busiest_day")?.value : undefined;
  const topItem = foodData ? metric(foodData, "top_item")?.value : undefined;
  const onPlan = valueOf(calendarData, "on_plan");
  const hasLines = (series: Series) => (series.lines ?? []).some((line) => line.values.some((value) => value !== null));
  const hasAny = (series: Series) => (series.lines ?? []).some((line) => line.values.some((value) => value));

  return (
    <div className="stack">
      <div className="grid stat-grid">
        <StatCard
          label="Meals logged"
          value={valueOf(foodData, "meals")}
          format={String}
          tone="health"
          hint={typeof topItem === "string" ? `Most logged: ${topItem}` : undefined}
          error={food.error?.message}
        />
        <StatCard label="Late-night meals" value={valueOf(foodData, "late_meals")} format={String} tone="video" hint="22:00 to 04:00" error={food.error?.message} />
        <StatCard
          label="Planned time"
          value={valueOf(calendarData, "planned")}
          format={formatMinutes}
          tone="study"
          hint={typeof busiest === "string" ? `Busiest: ${longDay(busiest)}` : undefined}
          error={calendar.error?.message}
        />
        <StatCard label="In meetings" value={valueOf(calendarData, "meetings")} format={formatMinutes} tone="work" error={calendar.error?.message} />
      </div>
      {food.error && <Failed what="Food" message={food.error.message} retry={food.reload} />}
      {calendar.error && <Failed what="The calendar" message={calendar.error.message} retry={calendar.reload} />}
      <div className="grid chart-grid">
        {!food.error && (
          <SeriesCard data={foodData} name="meal_times" title="When you ate" unit="time of day" words={words} empty="No meals logged in this range.">
            {(found) => (found.points?.length ? <MealTimes series={found} /> : <p className="muted">No meals logged in this range.</p>)}
          </SeriesCard>
        )}
        {!food.error && (
          <SeriesCard data={foodData} name="meals_by_day" title="Meals by day" unit="meals" words={words} empty="No meals logged in this range.">
            {(found) => (hasAny(found) ? <MealsByDay series={found} /> : <p className="muted">No meals logged in this range.</p>)}
          </SeriesCard>
        )}
        {!food.error && (
          <SeriesCard data={foodData} name="top_items" title="Most logged foods" unit="times" words={words} empty="No meals logged in this range.">
            {(found) => <TopFoods series={found} />}
          </SeriesCard>
        )}
        {!calendar.error && (
          <SeriesCard data={calendarData} name="blocks" title="Longest events" unit="minutes" words={words} empty="No calendar events in this range." wide>
            {(found) => (found.items?.length ? <EventSplit series={found} /> : <p className="muted">No calendar events in this range.</p>)}
          </SeriesCard>
        )}
        {!calendar.error && (
          <SeriesCard data={calendarData} name="plan_by_day" title="Planned time and where it went" unit="minutes" words={words} empty="No calendar events in this range.">
            {(found) => (hasAny(found) ? <PlanByDay series={found} /> : <p className="muted">No planned time in this range.</p>)}
          </SeriesCard>
        )}
        {!calendar.error && (
          <ChartCard
            title="Spent as planned"
            range={words}
            unit="percent"
            source={calendarData?.meta.source}
            loading={!calendarData}
            info={calendarData ? metric(calendarData, "on_plan")?.explain : undefined}
          >
            {typeof onPlan === "number" ? (
              <Gauge value={onPlan} max={100} suffix="%" color="var(--cat-study)" label="Spent as planned" />
            ) : (
              <p className="muted">No planned time on a day with screen data.</p>
            )}
          </ChartCard>
        )}
        {!calendar.error && (
          <SeriesCard data={calendarData} name="meetings_by_day" title="Meetings each day" unit="minutes" words={words} empty="No meeting data in this range.">
            {(found) =>
              hasAny(found) ? (
                <Meetings series={found} />
              ) : hasLines(found) ? (
                <p className="muted">No time in meeting apps (Zoom, Teams, Meet and the like) in this range.</p>
              ) : (
                <p className="muted">No screen data in this range.</p>
              )
            }
          </SeriesCard>
        )}
      </div>
    </div>
  );
}
