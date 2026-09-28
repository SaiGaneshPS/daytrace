// DT-54: Wrapped, the week in review as a 9:16 card: screen time, focus, sleep, the top apps, the streaks, and three
// lines by the local model (every number checked by the hub, DT-41) or plain ones. The card is revealed piece by
// piece by CSS (all at once under reduced motion; motion's reveal would be skipped on a page opened directly, under
// the app's AnimatePresence initial={false}), saves as a PNG (html-to-image) and, where the browser can share files
// (phones), opens the share sheet. Every number is the hub's (GET /wrapped). The week lives in the address.
// The image is drawn only once the card is fully shown (never half faded in). Where there is a share sheet it is
// drawn ahead and kept until the card looks different (a new size, theme or text size): a phone shares only right
// after a tap, so the share sheet can't wait for the drawing. It never goes through fetch() or a data: URL, which
// the hub's Content-Security-Policy (connect-src 'self') refuses.
import { toBlob } from "html-to-image";
import { type CSSProperties, type RefObject, useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router";
import { AI_TIMEOUT_MS, useApi } from "../api/client";
import type { components } from "../api/schema";
import { Chevron, longDay, shiftDay, useToday } from "../components/DayPicker";
import Skeleton from "../components/Skeleton";
import { formatMinutes } from "../components/StatCard";
import { dayCount } from "../components/streakText";
import { embedded } from "../embed";
import { EARLIEST, localZone, valueOf } from "./insights/shared";

type WrappedWeek = components["schemas"]["Wrapped"];

/** The ISO week a day is in ("2026-W38"): weeks start on Monday, and week 1 holds the year's first Thursday. */
export function isoWeekOf(day: string): string {
  const [year, month, date] = day.split("-").map(Number);
  const thursday = new Date(Date.UTC(year, month - 1, date));
  thursday.setUTCDate(thursday.getUTCDate() - ((thursday.getUTCDay() + 6) % 7) + 3); // that week's Thursday
  const weekYear = thursday.getUTCFullYear();
  const first = new Date(Date.UTC(weekYear, 0, 4)); // the 4th of January is always in week 1
  first.setUTCDate(first.getUTCDate() - ((first.getUTCDay() + 6) % 7) + 3);
  const week = 1 + Math.round((thursday.getTime() - first.getTime()) / (7 * 86_400_000));
  return `${weekYear}-W${String(week).padStart(2, "0")}`;
}

/** The Monday of an ISO week ("2026-W38" is 2026-09-14). */
export function weekMonday(week: string): string {
  const [, yearText, weekText] = /^(\d{4})-W(\d{2})$/.exec(week) ?? [];
  const monday = new Date(Date.UTC(Number(yearText), 0, 4));
  monday.setUTCDate(monday.getUTCDate() - ((monday.getUTCDay() + 6) % 7) + (Number(weekText) - 1) * 7);
  return monday.toISOString().slice(0, 10);
}

export const shiftWeek = (week: string, by: number) => isoWeekOf(shiftDay(weekMonday(week), 7 * by));
/** The first week the hub has: the first whose Monday is on or after its earliest day (1970-W02). */
export const FIRST_WEEK = ((week) => (weekMonday(week) < EARLIEST ? shiftWeek(week, 1) : week))(isoWeekOf(EARLIEST));
/** A real ISO week the hub can have ("2026-W53" is, "2025-W53" isn't: 2025 has 52). */
export const isWeek = (week: string) =>
  /^\d{4}-W(0[1-9]|[1-4]\d|5[0-3])$/.test(week) && isoWeekOf(weekMonday(week)) === week && week >= FIRST_WEEK;

/** A piece of the card, revealed after the ones before it (styles.css .wrapped-card > *). */
const step = (order: number) => ({ "--step": order }) as CSSProperties;

function WrappedCard({ data, card }: { data: WrappedWeek; card: RefObject<HTMLElement | null> }) {
  const value = (id: string) => valueOf(data, id) ?? null;
  const screen = value("screen_time");
  const daily = value("daily_average");
  const stats = [
    { label: "Focused", value: value("focused_time"), text: (minutes: number) => formatMinutes(minutes) },
    { label: "Focus score", value: value("focus_score"), text: (score: number) => `${Math.round(score)}` },
    { label: "Sleep a night", value: value("sleep"), text: (minutes: number) => formatMinutes(minutes) },
    { label: "After 11 pm", value: value("late_night"), text: (minutes: number) => `${formatMinutes(minutes)} a night` },
  ];
  const streaks = data.streaks.filter((streak) => streak.days_with_data);
  return (
    <article ref={card} className="wrapped-card" aria-label={`Your week, ${longDay(data.first)} to ${longDay(data.last)}`}>
      <p className="wrapped-eyebrow" style={step(0)}>
        Your week{data.in_progress ? ", so far" : ""}
        <span>
          {longDay(data.first)} to {longDay(data.last)}
        </span>
      </p>
      <div className="wrapped-hero" style={step(1)}>
        <strong>{screen === null ? "No screen data" : formatMinutes(screen)}</strong>
        <span>{screen === null ? "this week" : `on screens${daily === null ? "" : `, ${formatMinutes(daily)} a day`}`}</span>
      </div>
      <dl className="wrapped-stats" style={step(2)}>
        {stats.map((stat) => (
          <div key={stat.label}>
            <dt>{stat.label}</dt>
            <dd>{stat.value === null ? "no data" : stat.text(stat.value)}</dd>
          </div>
        ))}
      </dl>
      {data.top_apps.length > 0 && (
        <section className="wrapped-apps" style={step(3)} aria-label="Top apps">
          <h2>Top apps</h2>
          <ol>
            {data.top_apps.map((app) => (
              <li key={app.app}>
                <span>{app.app}</span>
                <strong>{formatMinutes(app.minutes)}</strong>
              </li>
            ))}
          </ol>
        </section>
      )}
      {streaks.length > 0 && (
        <section className="wrapped-streaks" style={step(4)} aria-label="Streaks this week">
          <h2>Streaks</h2>
          <ul>
            {streaks.map((streak) => (
              <li key={streak.id}>
                <span>{streak.name}</span>
                <strong>
                  {streak.met} of {dayCount(streak.days_with_data)}
                </strong>
              </li>
            ))}
          </ul>
        </section>
      )}
      <ol className="wrapped-lines" style={step(5)} aria-label="The week in three lines">
        {data.lines.map((line, index) => (
          <li key={index /* by place: the plain lines can repeat one (the padding line) */}>{line}</li>
        ))}
      </ol>
      <p className="wrapped-foot" style={step(6)}>
        {data.model ? `Lines by ${data.model}, on this computer` : "Plain lines from your numbers"} · Daytrace
      </p>
    </article>
  );
}

async function cardImage(node: HTMLElement): Promise<Blob> {
  const background = getComputedStyle(document.documentElement).getPropertyValue("--surface").trim() || "#ffffff";
  const image = await toBlob(node, { pixelRatio: 2, cacheBust: true, backgroundColor: background, type: "image/png" });
  if (!image) throw new Error("the card could not be drawn");
  return image;
}

type Note = { text: string; problem: boolean };

export default function Wrapped() {
  const [params, setParams] = useSearchParams();
  const today = useToday();
  const thisWeek = isoWeekOf(today);
  const asked = params.get("week");
  const week = asked && isWeek(asked) && asked <= thisWeek ? asked : shiftWeek(thisWeek, -1); // the last whole week
  const tz = localZone();
  const loaded = useApi("/api/v1/wrapped", { query: { week, tz }, timeout: AI_TIMEOUT_MS, quiet: true });
  const data = loaded.current ? loaded.data : undefined;
  const card = useRef<HTMLElement>(null);
  const canShare = typeof navigator !== "undefined" && typeof navigator.share === "function" && typeof navigator.canShare === "function";

  const [shown, setShown] = useState<WrappedWeek | null>(null); // the card that has finished its reveal
  const ready = data !== undefined && shown === data;
  useEffect(() => {
    const node = card.current;
    if (!data || !node) return;
    let live = true;
    // However the pieces came in (or didn't, under reduced motion): the card is shown once nothing on it moves.
    const moving = node.getAnimations?.({ subtree: true }) ?? [];
    void Promise.all(moving.map((animation) => animation.finished.catch(() => undefined))).then(() => {
      if (live) setShown(data);
    });
    return () => {
      live = false;
    };
  }, [data]);

  const image = useRef<Promise<Blob> | null>(null);
  const [look, setLook] = useState(0); // counts the times the card may have come to look different
  const draw = () => {
    if (!card.current) return Promise.reject(new Error("no card"));
    const made = cardImage(card.current);
    made.catch(() => {
      if (image.current === made) image.current = null; // the next tap tries again
    });
    image.current = made;
    return made;
  };
  useEffect(() => {
    // A new size (a turned phone, a resized window, a new text size) or theme makes the kept image old.
    const node = card.current;
    if (!data || !node) return;
    let timer: number | undefined;
    const changed = () => {
      image.current = null;
      window.clearTimeout(timer);
      timer = window.setTimeout(() => setLook((count) => count + 1), 300); // once it has settled
    };
    let size = `${node.offsetWidth}x${node.offsetHeight}`;
    const resized = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(() => {
      const now = `${node.offsetWidth}x${node.offsetHeight}`;
      if (now !== size) {
        size = now;
        changed();
      }
    });
    resized?.observe(node);
    const scheme = window.matchMedia?.("(prefers-color-scheme: dark)");
    scheme?.addEventListener?.("change", changed);
    const theme = new MutationObserver(changed);
    theme.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme", "class", "style"] });
    return () => {
      resized?.disconnect();
      scheme?.removeEventListener?.("change", changed);
      theme.disconnect();
      window.clearTimeout(timer);
    };
  }, [data]);
  useEffect(() => {
    image.current = null;
    if (ready && canShare) void draw().catch(() => undefined); // only where a share sheet waits on it
  }, [ready, data, look, canShare]); // draw is made anew each render; these are what it depends on

  const [busy, setBusy] = useState<"save" | "share" | null>(null);
  const [note, setNote] = useState<Note | null>(null);
  useEffect(() => setNote(null), [week]); // a note is about the week it was said for
  const name = `daytrace-wrapped-${week}.png`;
  const go = (by: number) =>
    setParams((previous) => {
      const next = new URLSearchParams(previous);
      next.set("week", shiftWeek(week, by));
      return next;
    });

  const save = async () => {
    if (!ready) return;
    setBusy("save");
    setNote(null);
    try {
      const url = URL.createObjectURL(await (image.current ?? draw()));
      const link = document.createElement("a");
      link.download = name;
      link.href = url;
      link.click();
      window.setTimeout(() => URL.revokeObjectURL(url), 60_000); // after the download has started
      setNote({ text: `Saved as ${name}.`, problem: false });
    } catch {
      setNote({ text: "The card couldn't be saved as an image.", problem: true });
    } finally {
      setBusy(null);
    }
  };
  const share = async () => {
    if (!ready) return;
    setBusy("share");
    setNote(null);
    try {
      const file = new File([await (image.current ?? draw())], name, { type: "image/png" });
      if (!navigator.canShare({ files: [file] })) {
        setNote({ text: "This browser can't share images: save it instead.", problem: false });
        return;
      }
      await navigator.share({ files: [file], title: "My week on Daytrace" });
    } catch (error) {
      if (!(error instanceof DOMException && error.name === "AbortError")) setNote({ text: "The card couldn't be shared.", problem: true });
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="stack">
      <header className="page-head">
        <div>
          <p className="eyebrow">The week in review</p>
          <h1>Wrapped</h1>
        </div>
        <div className="week-picker" role="group" aria-label="Week">
          <button type="button" className="icon-button" aria-label="The week before" onClick={() => go(-1)} disabled={week <= FIRST_WEEK}>
            <Chevron direction="left" />
          </button>
          <span className="week-name">
            {longDay(weekMonday(week))} to {longDay(shiftDay(weekMonday(week), 6))}
          </span>
          <button type="button" className="icon-button" aria-label="The week after" onClick={() => go(1)} disabled={week >= thisWeek}>
            <Chevron direction="right" />
          </button>
        </div>
      </header>
      {loaded.error && !data && !loaded.loading ? (
        <section className="card">
          <p className="muted">This week couldn&apos;t load: {loaded.error.message}</p>
          <button type="button" className="button button-ghost" onClick={loaded.reload}>
            Try again
          </button>
        </section>
      ) : !data ? (
        <div className="wrapped-wait">
          <Skeleton height={560} radius={24} />
          <p className="muted" role="status">
            Writing your week: the local model can take a little while the first time.
          </p>
        </div>
      ) : (
        <div className="wrapped-layout">
          <WrappedCard key={week} data={data} card={card} />
          <div className="wrapped-actions">
            {embedded ? (
              // DT-58: the app's Dashboard tab can't save files, so it never offers a save that saves nothing.
              <p className="muted">To keep this card as an image, open Wrapped in your computer's browser and save it there.</p>
            ) : (
              <>
                <button type="button" className="button" onClick={save} disabled={!ready || busy !== null}>
                  {busy === "save" ? "Saving..." : "Save as image"}
                </button>
                {canShare && (
                  <button type="button" className="button button-ghost" onClick={share} disabled={!ready || busy !== null}>
                    {busy === "share" ? "Sharing..." : "Share"}
                  </button>
                )}
                <p className={`field-note${note?.problem ? "" : " field-hint"}`} role="status">
                  {note?.text}
                </p>
              </>
            )}
            {data.fallback && data.reason && <p className="muted wrapped-reason">Plain lines: {data.reason}.</p>}
          </div>
        </div>
      )}
    </div>
  );
}
