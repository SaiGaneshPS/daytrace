// DT-33: Ask your day, as a chat, plus the pieces the Story page shares: text that types itself out, the facts
// behind an answer, the "generated on this device" badge and a small chart.
//
// - Each question goes to POST /ask on its own (the hub keeps no conversation). The answer is shown only once the
//   hub has checked every number in it against the facts its tools returned, so it types out on arrival instead of
//   streaming from the model: a streamed answer could show a number before the check threw it out.
// - Every answer shows the facts used, the tools that found them and, when the hub sent a series, a chart.
// - The conversation is kept for this browser tab (sessionStorage), so leaving the page and coming back keeps it.
//   Leaving the page while a question is being answered stops it.
import { type FormEvent, type KeyboardEvent, type ReactNode, useEffect, useId, useMemo, useRef, useState } from "react";
import { AI_TIMEOUT_MS, ApiError, api, useApi } from "../api/client";
import type { components } from "../api/schema";
import { type ChartOption, useEChart } from "../theme/charts";
import { useMediaQuery, useReducedMotionPreference } from "../theme/motion";
import { formatMinutes } from "./StatCard";

type Answer = components["schemas"]["Answer"];
type Fact = components["schemas"]["FactOut"];
type Chart = components["schemas"]["Chart"];

const STORE_KEY = "daytrace.ask";
const RECHECK_MS = 30_000; // how often an AI that can't answer yet is checked again
const KEEP_TURNS = 20;
const MAX_QUESTION = 500; // the hub reads at most this much
export const EXAMPLES = [
  "Where did my afternoon go?",
  "Do late nights hurt my focus?",
  "How much YouTube did I watch last week?",
  "How did I sleep last night?",
  "What's on my calendar tomorrow?",
];
const TOOL_NAMES: Record<string, string> = {
  get_totals: "Screen time",
  get_sessions: "Sessions",
  get_focus: "Focus",
  get_sleep: "Sleep",
  get_calendar: "Calendar",
  compare_plan: "Plan vs actual",
};

// --- shared pieces ---------------------------------------------------------------------------------------------

/** The local AI's state (GET /ai/status). `offline` says why no model can be used, and `noTools` why the model
 * can't look anything up (it never calls a tool); each is null when that is fine (or not known yet). Until the
 * model is usable with tools, it is checked again every 30 s, so a page recovers by itself once it is. */
export function useAiStatus() {
  const status = useApi("/api/v1/ai/status", { quiet: true });
  const data = status.data;
  const offline = data && (!data.reachable || !data.model) ? (data.error ?? "The model server isn't answering.") : null;
  const noTools =
    data && !offline && data.tool_calling === false
      ? `${data.model} doesn't call tools, so it can't look up your data. Load a model that supports tool calling (function calling) in your model server.`
      : null;
  const settled = data !== undefined && !offline && data.tool_calling === true;
  const { reload } = status;
  useEffect(() => {
    if (data === undefined || settled) return;
    const timer = window.setInterval(reload, RECHECK_MS);
    return () => window.clearInterval(timer);
  }, [data, settled, reload]);
  return { data, offline, noTools, reload };
}

/** What is wrong with the local AI and what still works without it. */
export function AiOffline({ title = "The local AI is offline", reason, children, onCheck }: {
  title?: string;
  reason: string;
  children: ReactNode;
  onCheck: () => void;
}) {
  return (
    <div className="card ai-offline" role="status">
      <div>
        <p className="ai-offline-title">{title}</p>
        <p className="muted">{reason}</p>
        <p>{children}</p>
      </div>
      <button type="button" className="button button-ghost" onClick={onCheck}>
        Check again
      </button>
    </div>
  );
}

/** Seconds since `running` became true (0 while it is false), ticking once a second, and starting again from 0
 * when `what` changes (a new request). Measured on the monotonic clock, so setting the device's clock never makes
 * it jump. */
export function useElapsed(running: boolean, what?: unknown): number {
  const [seconds, setSeconds] = useState(0);
  useEffect(() => {
    setSeconds(0);
    if (!running) return;
    const started = performance.now();
    const timer = window.setInterval(() => setSeconds(Math.floor((performance.now() - started) / 1000)), 1000);
    return () => window.clearInterval(timer);
  }, [running, what]);
  return seconds;
}

/** "Still working" text for a long wait: the words go to screen readers once, the ticking seconds only to the eye
 * (a status that changed every second would be read out every second). */
export function Waiting({ what, seconds, className }: { what: string; seconds: number; className?: string }) {
  return (
    <span className={className}>
      <span role="status">
        {what}
        {seconds >= 20 ? ". A small model can take a minute." : "."}
      </span>
      {seconds >= 3 && (
        <span className="waiting-seconds" aria-hidden="true">
          {" "}
          {seconds} s
        </span>
      )}
    </span>
  );
}

/** Text that types itself out (at once when reduced motion is asked for, or `animate` is false: a restored answer).
 * While it types, screen readers get the whole text at once from a hidden copy and the eye gets the typed part;
 * then the hidden copy simply shows (no new text, so nothing is read out twice) and the typed one goes, so copying
 * or finding the text sees it once. */
export function TypedText({ text, className, animate = true }: { text: string; className?: string; animate?: boolean }) {
  const reduced = useReducedMotionPreference();
  const instant = reduced || !animate;
  const [shown, setShown] = useState(instant ? text.length : 0);
  useEffect(() => {
    if (instant) {
      setShown(text.length);
      return;
    }
    setShown(0);
    const duration = Math.min(Math.max(text.length * 14, 600), 2600); // short answers still read as typed
    const started = performance.now();
    let frame = requestAnimationFrame(function step(now) {
      // The first frame's time can be a little before `started`: never less than nothing typed.
      const count = Math.min(text.length, Math.max(0, Math.ceil(((now - started) / duration) * text.length)));
      setShown(count);
      if (count < text.length) frame = requestAnimationFrame(step);
    });
    return () => cancelAnimationFrame(frame);
  }, [text, instant]);
  const typing = shown < text.length;
  return (
    <p className={className} data-typing={typing || undefined}>
      <span className={typing ? "visually-hidden" : undefined}>{text}</span>
      {typing && (
        <span aria-hidden="true">
          {text.slice(0, shown)}
          <span className="caret" />
        </span>
      )}
    </p>
  );
}

/** A fact's value as the hub gave it, with its unit (and minutes also as hours and minutes). */
export function factValue(fact: Fact): string {
  const value = typeof fact.value === "number" ? fact.value.toLocaleString() : fact.value;
  switch (fact.unit) {
    case "minutes":
      return typeof fact.value === "number" && fact.value >= 60 ? `${value} min (${formatMinutes(fact.value)})` : `${value} min`;
    case "time":
      return String(value);
    case "percent":
      return `${value}%`;
    case "score":
      return `${value} out of 100`;
    case "times":
      return fact.value === 1 ? "once" : `${value} times`;
    default:
      return `${value} ${fact.unit}`;
  }
}

/** The facts an answer was built from, folded away until asked for. */
export function FactsList({ facts }: { facts: Fact[] }) {
  if (!facts.length) return null;
  return (
    <details className="facts">
      <summary>
        Facts used <span className="badge">{facts.length}</span>
      </summary>
      <ul>
        {facts.map((fact, index) => (
          <li key={`${fact.label}-${index}`}>
            <span className="fact-label">{fact.label}</span>
            <span className="fact-value number">{factValue(fact)}</span>
          </li>
        ))}
      </ul>
    </details>
  );
}

function ChipIcon() {
  return (
    <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="2.2" aria-hidden="true">
      <rect x="6" y="6" width="12" height="12" rx="2" />
      <path d="M9 2v3M15 2v3M9 19v3M15 19v3M2 9h3M2 15h3M19 9h3M19 15h3" strokeLinecap="round" />
    </svg>
  );
}

/** Who wrote the text: the local model (named), or the plain template when the model couldn't be used. */
export function ModelBadge({ model, fallback = false, reason = null, label = "Generated on this device" }: {
  model: string | null;
  fallback?: boolean;
  reason?: string | null;
  label?: string;
}) {
  if (fallback || !model) {
    return (
      <p className="model-line">
        <span className="badge badge-template">Written from the facts, without the AI</span>
        {reason && <span className="muted">{reason}</span>}
      </p>
    );
  }
  return (
    <p className="model-line">
      <span className="badge badge-local">
        <ChipIcon />
        {label}
      </span>
      <span className="muted">
        by <span className="model-name">{model}</span>
      </span>
    </p>
  );
}

function shortLabel(label: string): string {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(label)) return label;
  const [year, month, day] = label.split("-").map(Number);
  return new Date(year, month - 1, day).toLocaleDateString([], { weekday: "short", day: "numeric" });
}

/** The small series the hub sent with an answer, as bars. */
export function AnswerChart({ chart }: { chart: Chart }) {
  const minutes = chart.unit === "minutes";
  const option = useMemo<ChartOption>(
    () => ({
      grid: { left: 8, right: 8, top: 24, bottom: 8, containLabel: true },
      tooltip: { trigger: "axis", valueFormatter: (value: unknown) => `${Number(value).toLocaleString()} ${chart.unit}` },
      xAxis: { type: "category", data: chart.points.map((point) => shortLabel(point.label)), axisLabel: { hideOverlap: true } },
      yAxis: { type: "value", axisLabel: { formatter: (value: number) => (minutes ? formatMinutes(value) : value.toLocaleString()) } },
      series: [
        {
          id: "points",
          type: "bar",
          name: chart.unit,
          data: chart.points.map((point) => point.value),
          itemStyle: { color: "var(--accent)", borderRadius: [6, 6, 0, 0] },
          label: {
            show: chart.points.length <= 8,
            position: "top",
            formatter: (params: { value: unknown }) => (minutes ? formatMinutes(Number(params.value)) : Number(params.value).toLocaleString()),
          },
        },
      ],
    }),
    [chart, minutes],
  );
  const ref = useEChart(option, `${chart.title}, in ${chart.unit}`);
  return (
    <figure className="answer-chart">
      <figcaption>{chart.title}</figcaption>
      <div ref={ref} className="chart" style={{ height: 200 }} />
    </figure>
  );
}

// --- the chat --------------------------------------------------------------------------------------------------

type Failure = { status: number; code: string; message: string };
type Turn = {
  id: number;
  question: string;
  state: "asking" | "answered" | "failed";
  answer?: Answer;
  error?: Failure;
  /** Brought back from earlier (sessionStorage): shown as it was, not typed out again. */
  restored?: boolean;
};

function loadTurns(): Turn[] {
  try {
    const saved = JSON.parse(sessionStorage.getItem(STORE_KEY) ?? "[]") as Turn[];
    return Array.isArray(saved) ? saved.filter((turn) => turn.state !== "asking").map((turn) => ({ ...turn, restored: true })) : [];
  } catch {
    return []; // storage off (a private window) or unreadable: start fresh
  }
}

function saveTurns(turns: Turn[]): void {
  try {
    sessionStorage.setItem(STORE_KEY, JSON.stringify(turns.filter((turn) => turn.state !== "asking").slice(-KEEP_TURNS)));
  } catch {
    // Without storage the conversation lasts as long as the page.
  }
}

function Thinking() {
  const seconds = useElapsed(true);
  return (
    <div className="bubble bubble-answer thinking">
      <span className="dots" aria-hidden="true">
        <span />
        <span />
        <span />
      </span>
      <Waiting what="Looking at your data on this device" seconds={seconds} />
    </div>
  );
}

function AnswerView({ answer, animate }: { answer: Answer; animate: boolean }) {
  return (
    <div className="bubble bubble-answer">
      <TypedText text={answer.answer} className="answer-text" animate={animate} />
      {answer.declined ? (
        <p className="model-line">
          <span className="badge">Not about your day</span>
        </p>
      ) : (
        <ModelBadge model={answer.model} fallback={answer.fallback} reason={answer.reason} />
      )}
      {answer.tools_called.length > 0 && (
        <ul className="tools" aria-label="Looked up">
          {answer.tools_called.map((tool, index) => (
            <li key={`${tool}-${index}`} className="chip" title={tool}>
              {TOOL_NAMES[tool] ?? tool}
            </li>
          ))}
        </ul>
      )}
      {answer.chart && answer.chart.points.length > 0 && <AnswerChart chart={answer.chart} />}
      <FactsList facts={answer.facts_used} />
    </div>
  );
}

function FailureView({ error, onRetry, disabled }: { error: Failure; onRetry: () => void; disabled: boolean }) {
  const offline = error.code === "ai_unavailable";
  return (
    <div className="bubble bubble-answer bubble-failed">
      <p>
        {error.code === "stopped"
          ? "Stopped."
          : offline
            ? `The local AI is offline: ${error.message}`
            : `That couldn't be answered: ${error.message}`}
      </p>
      <button type="button" className="button button-ghost" onClick={onRetry} disabled={disabled}>
        Ask again
      </button>
    </div>
  );
}

type Props = {
  tz: string;
  /** Why questions can't be answered right now (the AI offline, or a model without tools), or null when they can. */
  blocked: string | null;
};

export default function ChatBox({ tz, blocked: blockedBy }: Props) {
  const [turns, setTurns] = useState<Turn[]>(loadTurns);
  const nextId = useRef(Math.max(0, ...turns.map((turn) => turn.id)) + 1); // ids never repeat, whatever the clock says
  const [draft, setDraft] = useState("");
  const running = useRef<AbortController | null>(null);
  const end = useRef<HTMLDivElement>(null);
  const input = useRef<HTMLTextAreaElement>(null);
  const reduced = useReducedMotionPreference();
  const touch = useMediaQuery("(pointer: coarse)");
  const inputId = useId();
  const asking = turns.some((turn) => turn.state === "asking");

  useEffect(() => saveTurns(turns), [turns]);
  useEffect(() => () => running.current?.abort(), []); // leaving the page stops the question
  useEffect(() => {
    if (turns.length) end.current?.scrollIntoView({ block: "nearest", behavior: reduced ? "auto" : "smooth" });
  }, [turns, reduced]);

  const update = (id: number, change: Partial<Turn>) =>
    setTurns((list) => list.map((turn) => (turn.id === id ? { ...turn, ...change } : turn)));

  /** `typed`: the question came from the box (so it is emptied, and focus goes back to it afterwards); a chip or
   * "Ask again" leaves whatever is being typed alone. */
  async function ask(question: string, { typed = false, replacing }: { typed?: boolean; replacing?: number } = {}) {
    const text = question.trim().slice(0, MAX_QUESTION);
    if (!text || running.current) return;
    const id = nextId.current++;
    setTurns((list) => [...list.filter((turn) => turn.id !== replacing), { id, question: text, state: "asking" }]);
    if (typed) setDraft("");
    if (typed && touch) input.current?.blur(); // close the keyboard, so the answer can be seen as it arrives
    const controller = new AbortController();
    running.current = controller;
    try {
      const answer = await api.post("/api/v1/ask", { body: { question: text, tz }, signal: controller.signal, timeout: AI_TIMEOUT_MS });
      update(id, { state: "answered", answer });
    } catch (error) {
      const failure = controller.signal.aborted
        ? { status: 0, code: "stopped", message: "Stopped." }
        : error instanceof ApiError
          ? { status: error.status, code: error.code, message: error.message }
          : { status: 0, code: "unexpected", message: String(error) };
      update(id, { state: "failed", error: failure });
    } finally {
      if (running.current === controller) running.current = null;
      // Back to the box for the next question, but not on a touch screen: its keyboard would cover the answer.
      if (typed && !touch) input.current?.focus({ preventScroll: true });
    }
  }

  const submit = (event: FormEvent) => {
    event.preventDefault();
    void ask(draft, { typed: true });
  };
  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      void ask(draft, { typed: true });
    }
  };
  const blocked = blockedBy !== null;

  return (
    <div className="chat">
      {turns.length === 0 ? (
        <div className="chat-empty">
          <p className="chat-empty-title">Ask anything about your days.</p>
          <p className="muted">
            Answers come from your own data, looked up by the AI on this device. Every number is checked against the
            facts it found, and the facts are listed under each answer.
          </p>
        </div>
      ) : (
        <ol className="chat-log" aria-label="Questions and answers" aria-live="polite">
          {turns.map((turn) => (
            <li key={turn.id} className="turn">
              <p className="bubble bubble-question">{turn.question}</p>
              {turn.state === "asking" && <Thinking />}
              {turn.state === "answered" && turn.answer && <AnswerView answer={turn.answer} animate={!turn.restored} />}
              {turn.state === "failed" && turn.error && (
                <FailureView
                  error={turn.error}
                  disabled={asking || blocked}
                  onRetry={() => void ask(turn.question, { replacing: turn.id })}
                />
              )}
            </li>
          ))}
        </ol>
      )}
      <div ref={end} />

      <ul className="examples" aria-label="Example questions">
        {EXAMPLES.map((example) => (
          <li key={example}>
            <button type="button" className="chip chip-button" disabled={asking || blocked} onClick={() => void ask(example)}>
              {example}
            </button>
          </li>
        ))}
      </ul>

      <form className="chat-form" onSubmit={submit}>
        <label htmlFor={inputId} className="visually-hidden">
          Ask about your day
        </label>
        <textarea
          id={inputId}
          ref={input}
          className="chat-input"
          rows={1}
          maxLength={MAX_QUESTION}
          placeholder={blocked ? "Questions wait until the AI can answer" : "Ask about your day"}
          value={draft}
          disabled={blocked}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={onKeyDown}
        />
        {asking ? (
          <button type="button" className="button button-ghost" onClick={() => running.current?.abort()}>
            Stop
          </button>
        ) : (
          <button type="submit" className="button" disabled={blocked || !draft.trim()}>
            Ask
          </button>
        )}
      </form>
      {turns.length > 0 && !asking && (
        <button type="button" className="link-button" onClick={() => setTurns([])}>
          Clear the conversation
        </button>
      )}
    </div>
  );
}
