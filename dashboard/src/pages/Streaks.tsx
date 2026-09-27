// DT-54: streaks, today's goals and the badges. Each streak has a flame that grows with the run, its current and
// best counts, and today: done, at risk with what is left ("45 more minutes"), missed, or no data yet. Opening one
// shows its rule and the days that counted. The goals are rings, and each target can be changed in place (the
// streaks built on it, and the badges earned from them, are judged again). Every number is the hub's (GET /streaks,
// /goals and /achievements, DT-53), asked again each minute and as soon as the day changes.
import { type FormEvent, useEffect, useId, useRef, useState } from "react";
import { ApiError, api, useApi, useDaily } from "../api/client";
import type { components } from "../api/schema";
import BadgeShelf from "../components/BadgeShelf";
import ChartCard, { EstimatedBadge } from "../components/ChartCard";
import { longDay, shortDay, useToday } from "../components/DayPicker";
import ProgressRing from "../components/ProgressRing";
import { formatMinutes } from "../components/StatCard";
import StreakFlame from "../components/StreakFlame";
import { amountWords, dayCount, flameState, todayWords } from "../components/streakText";
import { localZone } from "./insights/shared";

type Streak = components["schemas"]["Streak"];
type Goal = components["schemas"]["Goal"];

const HISTORY = 30; // days listed under each streak

/** A run of days in words: one day, or its first to its last. */
const between = (dates: string[]) => (dates.length === 1 ? longDay(dates[0]) : `${longDay(dates[0])} to ${longDay(dates[dates.length - 1])}`);

function StreakCard({ streak }: { streak: Streak }) {
  const [open, setOpen] = useState(false);
  const detail = useId();
  const counted = new Set(streak.counted);
  const met = streak.days.filter((day) => day.status === "met").length;
  return (
    <article className={`card streak-card streak-${flameState(streak)}`}>
      <button type="button" className="streak-head" aria-expanded={open} aria-controls={detail} onClick={() => setOpen((shown) => !shown)}>
        <StreakFlame days={streak.current} state={flameState(streak)} />
        <span className="streak-main">
          <span className="streak-name">{streak.name}</span>
          <span className="streak-count">{dayCount(streak.current)}</span>
          <span className="streak-today">{todayWords(streak)}</span>
        </span>
        <span className="streak-best">
          Best <strong>{dayCount(streak.best)}</strong>
          {streak.estimated && <EstimatedBadge />}
        </span>
      </button>
      {open && (
        <div id={detail} className="streak-detail">
          <p>
            <strong>{streak.rule}.</strong> Counts on days with {streak.needs}; a day without it neither adds to a streak nor breaks it.
          </p>
          <ol className="streak-days" aria-hidden="true">
            {streak.days.map((day) => (
              <li
                key={day.date}
                className={`streak-day day-${day.status}${counted.has(day.date) ? " day-counted" : ""}`}
                title={`${shortDay(day.date)}: ${day.status.replace("_", " ")}${day.value === null ? "" : ` (${amountWords(day.value, streak.unit)})`}`}
              />
            ))}
          </ol>
          <p className="muted">
            Met on {met} of the last {dayCount(streak.days.length)}.
            {streak.counted.length ? ` This run: ${between(streak.counted)}.` : " No run right now."}
            {streak.best_dates.length ? ` The best: ${between(streak.best_dates)}.` : ""}
          </p>
        </div>
      )}
    </article>
  );
}

const GOAL_TONES: Record<string, string> = { focus_target: "study", social_cap: "social", bedtime: "health" };

function goalValue(goal: Goal, value: number | string | null): string {
  if (value === null) return "no data";
  return typeof value === "number" && goal.unit === "minutes" ? formatMinutes(value) : String(value);
}

function GoalCard({ goal, onSaved }: { goal: Goal; onSaved: () => void }) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(String(goal.target));
  const [problem, setProblem] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const field = useId();
  const input = useRef<HTMLInputElement>(null);
  const change = useRef<HTMLButtonElement>(null);
  const opened = useRef(false); // focus follows the form: into it when opened, back to Change when it closes
  useEffect(() => {
    if (editing) input.current?.focus();
    else if (opened.current) change.current?.focus();
    opened.current = editing;
  }, [editing]);
  const time = goal.unit === "time";
  const open = () => {
    setDraft(String(goal.target));
    setProblem(null); // a refusal from before doesn't greet the next try
    setEditing(true);
  };
  const save = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setProblem(null);
    try {
      await api.put("/api/v1/goals/{goal_id}", { path: { goal_id: goal.id }, body: { target: time ? draft : Number(draft) }, query: { tz: localZone() } });
      setEditing(false);
      onSaved();
    } catch (error) {
      setProblem(error instanceof ApiError ? error.message : "That target couldn't be saved.");
    } finally {
      setBusy(false);
    }
  };
  const status = { met: "done", missed: "missed", at_risk: "not yet", no_data: "no data yet" }[goal.today.status];
  return (
    <ChartCard title={goal.label} info={goal.explain} estimated={goal.today.estimated} className="goal-card">
      <ProgressRing
        percent={goal.today.progress}
        tone={GOAL_TONES[goal.id] ?? "other"}
        label={`${goal.label}: ${goalValue(goal, goal.today.value)} against ${goal.kind === "at_most" ? "a limit of" : "a target of"} ${goalValue(goal, goal.target)}, ${status}`}
      >
        <strong>{goalValue(goal, goal.today.value)}</strong>
        <span>
          {goal.kind === "at_most" ? "limit" : "of"} {goalValue(goal, goal.target)}
        </span>
      </ProgressRing>
      <p className="goal-rule">{goal.rule}</p>
      {editing ? (
        <form className="goal-form" onSubmit={save}>
          <label htmlFor={field}>{time ? "Asleep by" : "Minutes"}</label>
          <input
            ref={input}
            id={field}
            className="date-input"
            type={time ? "time" : "number"}
            min={time ? undefined : Number(goal.min)}
            max={time ? undefined : Number(goal.max)}
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
          />
          <button type="submit" className="button" disabled={busy || !draft}>
            {busy ? "Saving..." : "Save"}
          </button>
          <button type="button" className="button button-ghost" onClick={() => setEditing(false)} disabled={busy}>
            Cancel
          </button>
          <p className={`field-note${problem ? "" : " field-hint"}`} role="status">
            {problem ?? (time ? `Between ${goal.min} and ${goal.max}.` : `From ${goal.min} to ${goal.max} minutes.`)}
          </p>
        </form>
      ) : (
        <button ref={change} type="button" className="button button-ghost" onClick={open}>
          Change the {goal.kind === "at_most" ? "limit" : "target"}
        </button>
      )}
    </ChartCard>
  );
}

export default function Streaks() {
  const tz = localZone();
  const today = useToday();
  const streaks = useApi("/api/v1/streaks", { query: { tz, days: HISTORY } });
  const goals = useApi("/api/v1/goals", { query: { tz } });
  const badges = useApi("/api/v1/achievements", { query: { tz } });
  useDaily(streaks.reload, today);
  useDaily(goals.reload, today);
  useDaily(badges.reload, today);
  const list = streaks.data?.streaks ?? [];
  const fresh = streaks.data && list.every((streak) => streak.best === 0);
  const saved = () => {
    // A new target judges the past days again: the goal's streak, and the badges earned from streaks.
    goals.reload();
    streaks.reload();
    badges.reload();
  };
  return (
    <div className="stack">
      <header className="page-head">
        <div>
          <p className="eyebrow">Keep it going</p>
          <h1>Streaks</h1>
        </div>
      </header>
      {fresh && (
        <section className="card empty-streaks" aria-labelledby="first-streak">
          <h2 id="first-streak">Your first streak starts today</h2>
          <p className="muted">Meet a goal today and its flame lights. Each day in a row makes it bigger; a day with no data never breaks it.</p>
        </section>
      )}
      {/* A failed reload keeps what is on screen: the error shows only when there is nothing to show. */}
      <section aria-labelledby="streaks-title" className="stack">
        <h2 id="streaks-title" className="section-title">
          Your streaks
        </h2>
        {streaks.data ? (
          <div className="grid streak-grid">
            {list.map((streak) => (
              <StreakCard key={streak.id} streak={streak} />
            ))}
          </div>
        ) : streaks.error ? (
          <p className="muted">The streaks couldn&apos;t load: {streaks.error.message}</p>
        ) : (
          <p className="muted">Loading the streaks...</p>
        )}
      </section>
      <section aria-labelledby="goals-title" className="stack">
        <h2 id="goals-title" className="section-title">
          Today&apos;s goals
        </h2>
        {goals.error && !goals.data ? (
          <p className="muted">The goals couldn&apos;t load: {goals.error.message}</p>
        ) : (
          <div className="grid goal-grid">
            {(goals.data?.goals ?? []).map((goal) => (
              <GoalCard key={goal.id} goal={goal} onSaved={saved} />
            ))}
          </div>
        )}
      </section>
      <section aria-labelledby="badges-title" className="stack">
        <h2 id="badges-title" className="section-title">
          Badges {badges.data ? <span className="muted">({badges.data.unlocked} of {badges.data.achievements.length})</span> : null}
        </h2>
        {badges.error && !badges.data ? (
          <p className="muted">The badges couldn&apos;t load: {badges.error.message}</p>
        ) : (
          <BadgeShelf achievements={badges.data?.achievements ?? []} />
        )}
      </section>
    </div>
  );
}
