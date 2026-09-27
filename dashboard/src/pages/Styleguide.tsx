// DT-52: the design system on one page (/styleguide, not in the menu): the hero card, the category palette, a chart
// in a ChartCard, counting numbers, tabs, skeletons, buttons and confetti. Page tickets copy from here, and the
// Playwright tests check the pieces here. All numbers on it are sample data.
import { useMemo, useState } from "react";
import AnimatedNumber from "../components/AnimatedNumber";
import ChartCard from "../components/ChartCard";
import Skeleton, { SkeletonText } from "../components/Skeleton";
import Tabs from "../components/Tabs";
import { CATEGORIES, CATEGORY_LABELS, type ChartOption, categoryStyle, useEChart } from "../theme/charts";
import { celebrate } from "../theme/motion";

const SAMPLE: Record<(typeof CATEGORIES)[number], number> = {
  social: 42, video: 65, work: 190, study: 85, comms: 30, games: 20, health: 12, other: 18,
};
const NUMBERS = [155, 240, 90, 312]; // "Change the number" steps through these
const WEEK = [["Mon", 190], ["Tue", 160], ["Wed", 210], ["Thu", 130], ["Fri", 175], ["Sat", 80], ["Sun", 60]] as const;

function CategoryChart() {
  const option = useMemo<ChartOption>(
    () => ({
      tooltip: { trigger: "item", valueFormatter: (value: unknown) => `${value} min` },
      xAxis: { type: "category", data: CATEGORIES.map((category) => CATEGORY_LABELS[category]), axisLabel: { interval: 0, rotate: 30 } },
      yAxis: { type: "value", name: "minutes" },
      series: [
        {
          type: "bar",
          name: "Minutes",
          label: { show: true, position: "top" },
          data: CATEGORIES.map((category) => ({ value: SAMPLE[category], itemStyle: categoryStyle(category) })),
        },
      ],
    }),
    [],
  );
  const chart = useEChart(option, "Sample screen time by category, in minutes");
  return <div ref={chart} className="chart" />;
}

function WeekChart() {
  const option = useMemo<ChartOption>(
    () => ({
      tooltip: { trigger: "axis" },
      xAxis: { type: "category", data: WEEK.map(([day]) => day) },
      yAxis: { type: "value", name: "minutes" },
      series: [{ type: "line", name: "Work", data: WEEK.map(([, minutes]) => minutes), itemStyle: categoryStyle("work"), areaStyle: { opacity: 0.15 } }],
    }),
    [],
  );
  const chart = useEChart(option, "Sample work minutes per day of the week");
  return <div ref={chart} className="chart" style={{ height: 200 }} />;
}

function ShareChart() {
  const option = useMemo<ChartOption>(
    () => ({
      tooltip: { trigger: "item" },
      series: [{
        type: "pie",
        radius: ["45%", "72%"],
        label: { formatter: "{b}" },
        data: (["work", "study", "social", "video"] as const).map((category) => ({
          name: CATEGORY_LABELS[category], value: SAMPLE[category], itemStyle: categoryStyle(category),
        })),
      }],
    }),
    [],
  );
  const chart = useEChart(option, "Sample share of the day by category");
  return <div ref={chart} className="chart" />;
}

export default function Styleguide() {
  const [step, setStep] = useState(0);
  const [loading, setLoading] = useState(true);
  const minutes = NUMBERS[step % NUMBERS.length];
  return (
    <div className="stack">
      <section className="hero" aria-labelledby="styleguide-title">
        <p className="hero-label">Design system (sample data)</p>
        <h1 id="styleguide-title" className="hero-number">
          <AnimatedNumber value={minutes} /> minutes
        </h1>
        <p>Every page uses these tokens, cards and charts, in light and dark, on phones and wide screens.</p>
        <div className="row">
          <button type="button" className="button button-ghost" onClick={() => setStep((current) => current + 1)}>
            Change the number
          </button>
          <button type="button" className="button button-ghost" onClick={() => void celebrate()}>
            Celebrate
          </button>
        </div>
      </section>

      <section className="card" aria-labelledby="palette-title">
        <h2 id="palette-title">Categories</h2>
        <div className="palette">
          {CATEGORIES.map((category) => (
            <span key={category} className={`chip cat-${category}`}>
              <span className="swatch" aria-hidden="true" />
              {CATEGORY_LABELS[category]}
            </span>
          ))}
        </div>
      </section>

      <div className="grid grid-2">
        <ChartCard
          title="Screen time by category"
          range="Sample day"
          unit="minutes"
          estimated
          info="Every minute on a screen, counted once per device and added up. Each bar has its own pattern, so the categories can be told apart without color."
        >
          <CategoryChart />
        </ChartCard>
        <ChartCard
          title="Loading example"
          range="Today"
          unit="minutes"
          loading={loading}
          actions={
            loading && (
              <button type="button" className="button button-ghost" onClick={() => setLoading(false)}>
                Finish loading
              </button>
            )
          }
        >
          <ShareChart />
        </ChartCard>
      </div>

      <section className="card" aria-labelledby="tabs-title">
        <h2 id="tabs-title">Tabs</h2>
        <Tabs
          label="Time range"
          tabs={[
            { id: "day", label: "Day", content: <p>One day, hour by hour. Swipe on a phone, or use the arrow keys.</p> },
            {
              id: "week",
              label: "Week",
              content: (
                <>
                  <p>Seven days side by side. Drag across the chart: it scrubs, it doesn&apos;t switch tabs.</p>
                  <WeekChart />
                </>
              ),
            },
            { id: "month", label: "Month", content: <p>A calendar of the month.</p> },
          ]}
        />
      </section>

      <section className="card" aria-labelledby="skeleton-title">
        <h2 id="skeleton-title">Skeletons</h2>
        <div className="row">
          <Skeleton width={48} height={48} radius="50%" />
          <div style={{ flex: 1 }}>
            <SkeletonText lines={3} />
          </div>
        </div>
      </section>
    </div>
  );
}
