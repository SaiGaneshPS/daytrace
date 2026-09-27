// DT-54: the badges, earned and not yet. A badge seen unlocked for the first time pops and throws confetti once;
// this browser remembers which it has celebrated (localStorage), so it never does again for the same badge. Without
// storage (a private window) it remembers for as long as the page is open.
import { type CSSProperties, useEffect, useRef, useState } from "react";
import type { components } from "../api/schema";
import { celebrate } from "../theme/motion";
import { longDay } from "./DayPicker";

type Achievement = components["schemas"]["Achievement"];

export const CELEBRATED_KEY = "daytrace.badges.celebrated";
const remembered = new Set<string>(); // for this page's life, when storage can't be used

export function readCelebrated(): Set<string> {
  try {
    const stored = JSON.parse(window.localStorage.getItem(CELEBRATED_KEY) ?? "[]");
    return new Set([...remembered, ...(Array.isArray(stored) ? stored.filter((id): id is string => typeof id === "string") : [])]);
  } catch {
    return new Set(remembered);
  }
}

export function markCelebrated(ids: string[]): void {
  for (const id of ids) remembered.add(id);
  try {
    window.localStorage.setItem(CELEBRATED_KEY, JSON.stringify([...readCelebrated(), ...ids].filter((id, i, all) => all.indexOf(id) === i)));
  } catch {
    // no storage: the in-memory set above keeps it to once per page
  }
}

/** The unlocked badges not celebrated on this browser yet. */
export function toCelebrate(achievements: Achievement[], celebrated: Set<string>): string[] {
  return achievements.filter((badge) => badge.unlocked && !celebrated.has(badge.id)).map((badge) => badge.id);
}

const TONES = ["study", "comms", "video", "work", "games", "social"];

function Medal({ unlocked, tone }: { unlocked: boolean; tone: string }) {
  return (
    <svg viewBox="0 0 48 48" width="44" height="44" aria-hidden="true" className="medal" style={{ "--medal": unlocked ? `var(--cat-${tone})` : "var(--border)" } as CSSProperties}>
      <path className="medal-ribbon" d="M14 3h8l4 12h-8zM34 3h-8l-4 12h8z" />
      <circle className="medal-disc" cx="24" cy="29" r="15" />
      <path className="medal-star" d="M24 20.5l2.6 5.3 5.9.9-4.3 4.1 1 5.8-5.2-2.7-5.2 2.7 1-5.8-4.3-4.1 5.9-.9z" />
    </svg>
  );
}

function progressWords(badge: Achievement): string | null {
  const progress = badge.progress;
  if (!progress) return null;
  const amount = (value: number) => value.toLocaleString(undefined, { maximumFractionDigits: 0 });
  return `${amount(progress.value)} of ${amount(progress.target)} ${progress.unit}`;
}

export default function BadgeShelf({ achievements }: { achievements: Achievement[] }) {
  const [fresh, setFresh] = useState<Set<string>>(new Set());
  const shelf = useRef<HTMLUListElement>(null);
  useEffect(() => {
    const newly = toCelebrate(achievements, readCelebrated());
    if (!newly.length) return;
    markCelebrated(newly);
    setFresh(new Set(newly));
    const first = shelf.current?.querySelector<HTMLElement>(`[data-badge="${newly[0]}"]`);
    const box = first?.getBoundingClientRect();
    void celebrate(box ? { x: (box.left + box.width / 2) / window.innerWidth, y: (box.top + box.height / 2) / window.innerHeight } : undefined);
  }, [achievements]);

  return (
    <ul className="badges" ref={shelf}>
      {achievements.map((badge, index) => {
        const progress = progressWords(badge);
        const done = badge.progress ? Math.min(100, Math.round((100 * badge.progress.value) / badge.progress.target)) : badge.unlocked ? 100 : 0;
        return (
          <li
            key={badge.id}
            data-badge={badge.id}
            className={`badge-tile${badge.unlocked ? " badge-unlocked" : " badge-locked"}${fresh.has(badge.id) ? " badge-new" : ""}`}
          >
            <Medal unlocked={badge.unlocked} tone={TONES[index % TONES.length]} />
            <div className="badge-text">
              <strong>{badge.name}</strong>
              <span className="muted">{badge.rule}</span>
              {badge.unlocked ? (
                <span className="badge-earned">{badge.earned_on ? `Earned ${longDay(badge.earned_on)}` : "Earned"}</span>
              ) : (
                <>
                  {progress && <span className="badge-progress-words">{progress}</span>}
                  <span className="badge-progress" aria-hidden="true">
                    <span style={{ width: `${done}%` }} />
                  </span>
                </>
              )}
              <span className="visually-hidden">{badge.unlocked ? "Unlocked." : "Not yet."}</span>
            </div>
          </li>
        );
      })}
    </ul>
  );
}
