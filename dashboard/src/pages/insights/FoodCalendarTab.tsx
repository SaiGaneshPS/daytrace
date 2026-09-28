// DT-57: the Insights Food and Calendar tab. Meal habits and how well the calendar's plans matched what happened:
// when you ate (a dot per meal, by type, with late-night eating shaded), meals each day, the foods logged most (as
// logged: no calorie claims), each event split into what its time went to (on plan, off plan, other screens, no
// screen, adding up to its length), the share of plans kept, planned time by day, and time in meetings each day.
// Everything is the hub's (GET /insights/food and /insights/calendar); the chart options are in foodCharts.ts.
import { useMemo } from "react";
import ChartCard from "../../components/ChartCard";
import { longDay, shortDay } from "../../components/DayPicker";
import StatCard, { formatMinutes } from "../../components/StatCard";
import { timesWords } from "../../components/streakText";
import { type ChartOption, escapeHTML, useEChart } from "../../theme/charts";
import { useMediaQuery } from "../../theme/motion";
import { type Format, eventSplitOption, lateWindow, mealTimesOption, mealsByDayOption, planByDayOption } from "./foodCharts";
import { Failed, Gauge, SeriesCard, minutesText } from "./parts";
import { type Series, metric, rangeWords, useInsights, valueOf } from "./shared";

type Props = { range: string; tz: string; today: string };

const FORMAT: Format = { day: shortDay, minutes: formatMinutes, escape: escapeHTML };
const WIDE = "(min-width: 40rem)";
const pad = (hour: number) => `${String(hour).padStart(2, "0")}:00`;

/** "22:00 to 04:00", from the hub's late-night window. */
function lateWords(series: Series | undefined): string | undefined {
  const late = lateWindow(series);
  return late ? `${pad(late.from)} to ${pad(late.until)}` : undefined;
}

// --- food ------------------------------------------------------------------------------------------------------

function MealTimes({ series }: { series: Series }) {
  const option = useMemo(() => mealTimesOption(series, FORMAT) as ChartOption, [series]);
  const late = lateWords(series);
  const chart = useEChart(option, `${series.title}: each meal at its time of day, by type${late ? `; late-night eating (${late}) is shaded` : ""}`);
  return <div ref={chart} className="chart" style={{ height: 320 }} />;
}

function MealsByDay({ series }: { series: Series }) {
  const option = useMemo(() => mealsByDayOption(series, FORMAT) as ChartOption, [series]);
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
          <strong>{timesWords(item.value)}</strong>
        </li>
      ))}
    </ol>
  );
}

// --- calendar --------------------------------------------------------------------------------------------------

function EventSplit({ series }: { series: Series }) {
  const wide = useMediaQuery(WIDE); // narrower: shorter names and fewer ticks, the list below has them in full
  const option = useMemo(() => eventSplitOption(series, FORMAT, wide) as ChartOption, [series, wide]);
  const chart = useEChart(option, `${series.title}: each event's minutes on plan, off plan, on other screens and with no screen`);
  const events = series.items ?? [];
  return (
    <>
      <div ref={chart} className="chart" style={{ height: Math.max(220, events.length * 34 + 70) }} />
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
  const option = useMemo(() => planByDayOption(series, FORMAT) as ChartOption, [series]);
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
        <StatCard label="Late-night meals" value={valueOf(foodData, "late_meals")} format={String} tone="video" hint={lateWords(foodData?.series.meal_times)} error={food.error?.message} />
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
