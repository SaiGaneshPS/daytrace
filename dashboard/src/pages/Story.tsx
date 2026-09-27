// DT-33: the day's story, told by the local AI (GET /story). Pick a day; the story types itself out, with who wrote
// it (the model on this device, by name, or the plain template when the model was away), the facts behind every
// number, and two small charts next to it: the top apps and the focus score's parts (GET /insights/day).
//
// The hub checks every number in the story against its facts before sending it. When the AI is offline the page
// says so; the story comes from the template and the charts keep working, since they never needed the model.
import { useEffect, useMemo, useState } from "react";
import { AI_TIMEOUT_MS, useApi } from "../api/client";
import type { components } from "../api/schema";
import ChartCard from "../components/ChartCard";
import { AiOffline, FactsList, ModelBadge, TypedText, useAiStatus, useElapsed, Waiting } from "../components/ChatBox";
import DayPicker, { longDay, useToday } from "../components/DayPicker";
import { SkeletonText } from "../components/Skeleton";
import { formatMinutes } from "../components/StatCard";
import { type ChartOption, categoryStyle, useEChart } from "../theme/charts";

type Summary = components["schemas"]["DaySummary"];

const MINI_APPS = 5;
const SETTLE_MS = 500; // a day must stay picked this long before its story is asked for

/** `value`, once it has stayed the same for `ms`. Stepping through days asks the hub for the last day's story only:
 * a story the browser gave up on is still written by the model, and would hold up the one wanted. */
function useSettled<T>(value: T, ms: number): T {
  const [settled, setSettled] = useState(value);
  useEffect(() => {
    const timer = window.setTimeout(() => setSettled(value), ms);
    return () => window.clearTimeout(timer);
  }, [value, ms]);
  return settled;
}

function TopApps({ summary }: { summary: Summary }) {
  const apps = useMemo(() => summary.top_apps.slice(0, MINI_APPS), [summary.top_apps]);
  const option = useMemo<ChartOption>(
    () => ({
      grid: { left: 8, right: 56, top: 4, bottom: 4, containLabel: true },
      xAxis: { type: "value", show: false },
      yAxis: { type: "category", inverse: true, data: apps.map((app) => app.app), axisTick: { show: false }, axisLine: { show: false } },
      tooltip: { trigger: "item", valueFormatter: (value: unknown) => `${Number(value).toLocaleString()} min` },
      series: [
        {
          id: "apps",
          type: "bar",
          name: "Minutes",
          barMaxWidth: 18,
          data: apps.map((app) => ({ value: app.minutes, itemStyle: { ...categoryStyle(app.category), borderRadius: 6 } })),
          label: { show: true, position: "right", color: "var(--text)", textBorderWidth: 0, formatter: (params: { value: unknown }) => formatMinutes(Number(params.value)) },
        },
      ],
    }),
    [apps],
  );
  const chart = useEChart(option, `Top ${apps.length} apps and sites by minutes`);
  if (!apps.length) return <p className="muted">No app time on this day.</p>;
  return <div ref={chart} className="chart" style={{ height: apps.length * 34 + 16 }} />;
}

function FocusParts({ summary }: { summary: Summary }) {
  const { work_or_study_minutes: work, focused_minutes: focused, distracted_minutes: distracted } = summary;
  const parts = useMemo(
    () => [
      { name: "Work or study", value: work, category: "work" },
      { name: "Focused", value: focused, category: "study" },
      { name: "Distracted", value: distracted, category: "social" },
    ],
    [work, focused, distracted],
  );
  // A hub older than these numbers leaves them out: unknown, not zero.
  const known = parts.every((part) => typeof part.value === "number") && parts.some((part) => (part.value ?? 0) > 0);
  const option = useMemo<ChartOption>(
    () => ({
      grid: { left: 8, right: 56, top: 4, bottom: 4, containLabel: true },
      xAxis: { type: "value", show: false },
      yAxis: { type: "category", inverse: true, data: parts.map((part) => part.name), axisTick: { show: false }, axisLine: { show: false } },
      tooltip: { trigger: "item", valueFormatter: (value: unknown) => `${Number(value).toLocaleString()} min` },
      series: [
        {
          id: "focus",
          type: "bar",
          name: "Minutes",
          barMaxWidth: 18,
          data: parts.map((part) => ({ value: part.value ?? 0, itemStyle: { ...categoryStyle(part.category), borderRadius: 6 } })),
          label: { show: true, position: "right", color: "var(--text)", textBorderWidth: 0, formatter: (params: { value: unknown }) => formatMinutes(Number(params.value)) },
        },
      ],
    }),
    [parts],
  );
  const chart = useEChart(option, "Work or study, focused and distracted minutes");
  if (!known) return <p className="muted">No work, study or distraction time on this day.</p>;
  return (
    <>
      <div ref={chart} className="chart" style={{ height: 120 }} />
      {summary.focus_score !== null && (
        <p className="muted">
          Focus score <strong className="number">{summary.focus_score}</strong> out of 100: focused time against work,
          study and distraction together.
        </p>
      )}
    </>
  );
}

export default function Story() {
  const tz = useMemo(() => Intl.DateTimeFormat().resolvedOptions().timeZone, []);
  const today = useToday();
  const [picked, setPicked] = useState<string | null>(null);
  const day = picked ?? today;
  const live = day === today;
  const query = { date: day, tz };

  const ai = useAiStatus();
  const storyDay = useSettled(day, SETTLE_MS);
  const story = useApi("/api/v1/story", { query: { date: storyDay, tz }, quiet: true, timeout: AI_TIMEOUT_MS, enabled: storyDay === day });
  const summary = useApi("/api/v1/insights/day", { query, quiet: true });
  // Another day's story or numbers (still on screen while this day loads) must not show under this day's heading.
  const told = story.data?.date === day ? story.data : undefined;
  const numbers = summary.data?.date === day ? summary.data : undefined;
  // Writing while a request is on its way (a retry too) or about to be sent; a failure shows only once it is over.
  const writing = !told && (story.loading || storyDay !== day || !story.error);
  const failed = !told && !writing ? story.error : undefined;
  const seconds = useElapsed(writing, day);
  const summaryError = !numbers && summary.error ? summary.error.message : undefined;

  return (
    <div className="stack">
      <header className="page-head">
        <div>
          <p className="eyebrow">{live ? "Your day so far" : "Looking back"}</p>
          <h1>{live ? "Today's story" : longDay(day)}</h1>
        </div>
        <DayPicker day={day} today={today} onChange={(next) => setPicked(next === today ? null : next)} />
      </header>

      {ai.offline && (
        <AiOffline reason={ai.offline} onCheck={ai.reload}>
          Stories are written from a plain template until it&apos;s back, with the same checked numbers. The charts
          still work.
        </AiOffline>
      )}

      <div className="story-layout">
        <ChartCard
          title="Story"
          range={live ? "Today, so far" : longDay(day)}
          className="story-card"
          info="Written by the AI on this device from your day's numbers. Every number in it is checked against the facts listed below it; a story that gets one wrong is written again, or replaced by a plain template."
        >
          <div aria-busy={writing} aria-live="polite">
            {told ? (
              <>
                <TypedText key={`${told.date}:${told.story}`} text={told.story} className="story-text" />
                <ModelBadge model={told.model} fallback={told.fallback} reason={told.reason} />
                {told.in_progress && (
                  <p className="muted">The day isn&apos;t over: this is the story so far, written again at most every 15 minutes.</p>
                )}
                <FactsList facts={told.facts_used} />
                {told.in_progress && (
                  <div className="row">
                    <button type="button" className="button button-ghost" onClick={story.reload} disabled={story.loading}>
                      {story.loading ? "Checking..." : "Check for a newer story"}
                    </button>
                    {!story.loading && story.error && (
                      <p className="muted" role="status">
                        Couldn&apos;t check for a newer story: {story.error.message}
                      </p>
                    )}
                  </div>
                )}
              </>
            ) : failed ? (
              <div className="story-failed">
                <p className="muted">The story couldn&apos;t load: {failed.message}</p>
                <button type="button" className="button button-ghost" onClick={story.reload}>
                  Try again
                </button>
              </div>
            ) : (
              <div className="story-writing">
                <SkeletonText lines={4} />
                <Waiting what="Writing the story on this device" seconds={seconds} className="muted" />
              </div>
            )}
          </div>
        </ChartCard>

        <div className="story-side">
          <ChartCard
            title="Top apps"
            range={live ? "Today, so far" : longDay(day)}
            unit="minutes"
            estimated={numbers?.screen_estimated}
            loading={!numbers && !summaryError}
            info="The apps and sites with the most time that day, colored by category."
          >
            {summaryError ? <p className="muted">The apps couldn&apos;t load: {summaryError}</p> : numbers && <TopApps summary={numbers} />}
          </ChartCard>
          <ChartCard
            title="Focus and distraction"
            range={live ? "Today, so far" : longDay(day)}
            unit="minutes"
            loading={!numbers && !summaryError}
            info="Work or study: time in work and study apps. Focused: the part of it in blocks of 10 minutes or more with no phone distraction. Distracted: time in social, video and game apps. Overlapping devices count once."
          >
            {summaryError ? <p className="muted">Focus couldn&apos;t load: {summaryError}</p> : numbers && <FocusParts summary={numbers} />}
          </ChartCard>
        </div>
      </div>
    </div>
  );
}
