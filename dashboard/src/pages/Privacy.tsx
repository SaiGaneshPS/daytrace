// DT-36: your data and what you can do with it. The network check (DT-45) says in one big number that the hub has made
// or served no internet connection since it started, with every connection it did make or serve by network. Then
// where the data lives (the folder only on the hub computer, since it names that computer's user), its size, the
// events and the devices; the redaction rules (DT-44) to switch built-in ones on or off and keep your own words, try
// a title against them, and hide what is already stored; and the export and delete-all (DT-46), which only the hub
// computer can do. Deleting asks for the phrase to be typed every time: the field starts empty on each try, and the
// redaction rules are kept unless that try says otherwise.
import { type FormEvent, useEffect, useId, useRef, useState } from "react";
import { Link } from "react-router";
import { AI_TIMEOUT_MS, ApiError, api, useApi, usePolling } from "../api/client";
import type { components } from "../api/schema";
import ChartCard from "../components/ChartCard";
import { DEVICE_TYPE_LABELS } from "../theme/devices";

type Network = components["schemas"]["NetworkStatus"];
type Counts = components["schemas"]["NetworkCounts"];
type Rules = components["schemas"]["RedactionRules"];
type Storage = components["schemas"]["Storage"];
type Deleted = components["schemas"]["Deleted"];
type Device = components["schemas"]["DeviceInfo"];

export const DELETE_PHRASE = "delete all my daytrace data";
const NETWORK_REFRESH_MS = 15_000;
const MAX_WORDS = 50; // the hub's limits (redaction.py)
const MAX_RULES = 20;
// Working through every stored event (hiding words, deleting and compacting the file, counting matches) can take far
// longer than an ordinary answer on a big profile: waited for, so a slow success never reads as a failure.
const LONG_MS = AI_TIMEOUT_MS;

/** A size in bytes as people say it: "824 KB", "12.4 MB", "1.00 GB" (the unit chosen after rounding). */
export function sizeText(bytes: number): string {
  const kb = Math.max(1, Math.round(bytes / 1024));
  if (kb < 1024) return `${kb} KB`;
  const mb = Math.round((bytes / 1024 ** 2) * 10) / 10;
  if (mb < 1024) return `${mb.toFixed(1)} MB`;
  return `${(bytes / 1024 ** 3).toFixed(2)} GB`;
}

/** A word as the hub reads it (redaction.tokens: letter runs and digit runs, casefolded): two words with the same
 * key are the same to the hub, and a word with no key has nothing to look for. */
export function wordKey(word: string): string {
  const folded = word.toLowerCase().replace(/ß/g, "ss").replace(/ς/g, "σ");
  return (folded.match(/[\p{L}\p{Nl}\p{No}]+|\p{Nd}+/gu) ?? []).join(" ");
}

const whenText = (iso: string) => new Date(iso).toLocaleString([], { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
const dayText = (iso: string) => new Date(iso).toLocaleDateString([], { day: "numeric", month: "long", year: "numeric" });
const total = (counts: Counts) => counts.localhost + counts.lan + counts.tailscale + counts.internet;
const plural = (count: number, one: string, many = `${one}s`) => `${count.toLocaleString()} ${count === 1 ? one : many}`;

// --- the network check -------------------------------------------------------------------------------------------

function CountsLine({ counts, direction }: { counts: Counts; direction: "from" | "to" }) {
  const parts = [
    ["this computer", counts.localhost],
    ["your home network", counts.lan],
    ["Tailscale", counts.tailscale],
    ["the internet", counts.internet],
  ] as const;
  const shown = parts.filter(([, count]) => count > 0);
  if (!shown.length) return <>none</>;
  return <>{shown.map(([where, count]) => `${count.toLocaleString()} ${direction} ${where}`).join(", ")}</>;
}

function NetworkCard({ network, error }: { network: Network | undefined; error: ApiError | undefined }) {
  const clean = network?.internet_connections === 0;
  return (
    <ChartCard title="Internet connections" className="network-card" info="Counted by the hub itself: every request it made or served since it started, by network. Nothing in it can reach the internet: the only way out refuses any address that isn't this computer, your home network or Tailscale, and so does the way in.">
      {error && !network ? (
        <p className="muted">The network check couldn&apos;t load: {error.message}</p>
      ) : !network ? (
        <p className="muted">Checking...</p>
      ) : (
        <>
          <p className={`net-check ${clean ? "net-clean" : "net-bad"}`}>
            <span className="net-icon" aria-hidden="true">
              <svg viewBox="0 0 24 24" width="30" height="30" fill="none" stroke="currentColor" strokeWidth="2.6" strokeLinecap="round" strokeLinejoin="round">
                <path d={clean ? "M5 12.5l4.5 4.5L19 7.5" : "M12 7v6M12 17h.01"} />
              </svg>
            </span>
            <span>
              <strong className="net-count">{network.internet_connections.toLocaleString()}</strong> internet{" "}
              {network.internet_connections === 1 ? "connection" : "connections"}
              <span className="net-since">since the hub started, {whenText(network.since)}</span>
            </span>
          </p>
          <dl className="privacy-facts">
            <div>
              <dt>Served</dt>
              <dd>
                <CountsLine counts={network.incoming} direction="from" />
              </dd>
            </div>
            <div>
              <dt>Made (to the local model)</dt>
              <dd>
                <CountsLine counts={network.outgoing} direction="to" />
              </dd>
            </div>
            <div>
              <dt>Refused coming in</dt>
              <dd>{total(network.refused) ? <CountsLine counts={network.refused} direction="from" /> : "none"}</dd>
            </div>
            <div>
              <dt>Blocked going out</dt>
              <dd>
                {network.blocked.count === 0
                  ? "none"
                  : `${plural(network.blocked.count, "request")} to ${network.blocked.destinations.map((item) => `${item.host}:${item.port}`).join(", ")}`}
              </dd>
            </div>
            <div>
              <dt>Listening on</dt>
              <dd>{network.listening.length ? network.listening.join(", ") : "nothing yet"}</dd>
            </div>
            <div>
              <dt>Socket guard</dt>
              <dd>{network.guarded ? "On: nothing in the hub process can connect to the internet" : "Off"}</dd>
            </div>
          </dl>
        </>
      )}
    </ChartCard>
  );
}

// --- where the data lives ----------------------------------------------------------------------------------------

function StorageCard({ storage, error, devices, local }: { storage: Storage | undefined; error: ApiError | undefined; devices: Device[] | undefined; local: boolean }) {
  // Who sends data, and who can only read it (a phone's browser with a viewer token), told apart.
  const paired = (devices ?? []).filter((device) => device.revoked_at === null);
  const senders = paired.filter((device) => device.device_type !== "viewer");
  const viewers = paired.length - senders.length;
  return (
    <ChartCard title="Where your data lives" className="storage-card">
      {error && !storage ? (
        <p className="muted">This couldn&apos;t load: {error.message}</p>
      ) : !storage ? (
        <p className="muted">Loading...</p>
      ) : (
        <>
          <dl className="privacy-facts">
            <div>
              <dt>Profile</dt>
              <dd>{storage.profile}</dd>
            </div>
            <div>
              <dt>Folder</dt>
              <dd>{storage.folder ? <code>{storage.folder}</code> : "On the hub computer (its folder shows there)"}</dd>
            </div>
            <div>
              <dt>File</dt>
              <dd>
                <code>{storage.file}</code>
              </dd>
            </div>
            <div>
              <dt>Size</dt>
              <dd>{sizeText(storage.size_bytes)}</dd>
            </div>
            <div>
              <dt>Events</dt>
              <dd>
                {plural(storage.events, "event")}
                {storage.first_event && storage.last_event ? `, ${dayText(storage.first_event)} to ${dayText(storage.last_event)}` : ""}
              </dd>
            </div>
          </dl>
          <h3 className="section-subtitle">
            {devices ? `${plural(senders.length, "device")} sending data` : `${plural(storage.devices, "device")} paired`}
          </h3>
          {senders.length > 0 && (
            <ul className="privacy-devices">
              {senders.map((device) => (
                <li key={device.device_id}>
                  <strong>{device.name}</strong>
                  <span className="muted">
                    {DEVICE_TYPE_LABELS[device.device_type] ?? device.device_type}, {plural(device.event_count, "event")}
                  </span>
                </li>
              ))}
            </ul>
          )}
          {viewers > 0 && <p className="muted">{plural(viewers, "browser")} can read the dashboard, without sending anything.</p>}
          <Link to="/devices" className="link">
            {local ? "Pair or revoke devices" : "See the devices"}
          </Link>
        </>
      )}
    </ChartCard>
  );
}

// --- the redaction rules --------------------------------------------------------------------------------------------

type Draft = { key: number; name: string; words: string[] };

function CustomRule({ rule, onChange, onRemove }: { rule: Draft; onChange: (rule: Draft) => void; onRemove: () => void }) {
  const [word, setWord] = useState("");
  const [problem, setProblem] = useState<string | null>(null);
  const nameField = useId();
  const wordField = useId();
  const add = (event: FormEvent) => {
    event.preventDefault();
    const clean = word.split(/\s+/).filter(Boolean).join(" ");
    const key = wordKey(clean);
    if (!key) setProblem(`"${clean}" has no letters or digits to look for.`);
    else if (rule.words.some((known) => wordKey(known) === key)) setProblem(`"${clean}" is already in this rule.`);
    else if (rule.words.length >= MAX_WORDS) setProblem(`A rule holds up to ${MAX_WORDS} words or phrases.`);
    else {
      onChange({ ...rule, words: [...rule.words, clean] });
      setWord("");
      setProblem(null);
    }
  };
  return (
    <li className="custom-rule">
      <div className="custom-rule-head">
        <label htmlFor={nameField}>Rule name</label>
        <input id={nameField} className="date-input" value={rule.name} maxLength={60} onChange={(event) => onChange({ ...rule, name: event.target.value })} />
        <button type="button" className="button button-ghost" onClick={onRemove}>
          Remove rule
        </button>
      </div>
      {rule.words.length > 0 ? (
        <ul className="word-chips" aria-label={`Words in ${rule.name || "this rule"}`}>
          {rule.words.map((known) => (
            <li key={known} className="word-chip">
              <span>{known}</span>
              <button type="button" className="icon-button" aria-label={`Remove ${known}`} onClick={() => onChange({ ...rule, words: rule.words.filter((item) => item !== known) })}>
                <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" aria-hidden="true">
                  <path d="M6 6l12 12M18 6L6 18" />
                </svg>
              </button>
            </li>
          ))}
        </ul>
      ) : (
        <p className="muted">No words yet: a rule needs at least one.</p>
      )}
      <form className="row add-word" onSubmit={add}>
        <label htmlFor={wordField} className="visually-hidden">
          A word or phrase to hide in {rule.name || "this rule"}
        </label>
        <input id={wordField} className="date-input" placeholder="A word or phrase" value={word} maxLength={100}
          onChange={(event) => { setWord(event.target.value); setProblem(null); }} />
        <button type="submit" className="button button-ghost" disabled={word.trim().length < 2}>
          Add
        </button>
      </form>
      {problem && <p className="field-note">{problem}</p>}
    </li>
  );
}

function TryTitle({ unsaved }: { unsaved: boolean }) {
  const [title, setTitle] = useState("");
  const [result, setResult] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const asked = useRef(0); // only the answer for the title asked last is shown
  const field = useId();
  const check = async (event: FormEvent) => {
    event.preventDefault();
    const mine = ++asked.current;
    setBusy(true);
    try {
      const found = await api.post("/api/v1/privacy/redaction/check", { body: { title } });
      if (mine === asked.current) setResult(found.redacted ? `Stored as "${found.stored_as}", by the ${found.rule_name} rule.` : "Stored as it is: no rule matches.");
    } catch (error) {
      if (mine === asked.current) setResult(error instanceof ApiError ? error.message : "The title couldn't be checked.");
    } finally {
      if (mine === asked.current) setBusy(false);
    }
  };
  return (
    <form className="try-title" onSubmit={check}>
      <label htmlFor={field}>Try a window or event title against the saved rules</label>
      <div className="row">
        <input id={field} className="date-input" placeholder="MyBank - Account Summary" value={title} maxLength={500}
          onChange={(event) => { asked.current += 1; setTitle(event.target.value); setResult(null); setBusy(false); }} />
        <button type="submit" className="button button-ghost" disabled={!title.trim() || busy}>
          Check
        </button>
      </div>
      {unsaved && <p className="muted">Your changes aren&apos;t saved yet: the title is tried against the rules as saved.</p>}
      <p className="field-note field-hint" role="status">
        {result}
      </p>
    </form>
  );
}

function RedactionCard({ generation }: { generation: number }) {
  const rules = useApi("/api/v1/privacy/redaction", { quiet: true });
  const stored = useApi("/api/v1/privacy/redaction/stored", { quiet: true, timeout: LONG_MS });
  const [fresh, setFresh] = useState<Rules | null>(null); // what the last save answered, until the rules are read again
  const [disabled, setDisabled] = useState<string[] | null>(null); // null: as the hub has them
  const [custom, setCustom] = useState<Draft[] | null>(null);
  const [note, setNote] = useState<{ text: string; problem: boolean } | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const counter = useRef(0);
  const { reload: reloadRules } = rules;
  const { reload: reloadStored } = stored;
  useEffect(() => setFresh(null), [rules.data]);
  const seen = useRef(generation);
  useEffect(() => {
    if (seen.current === generation) return;
    seen.current = generation; // everything was deleted: what is shown here is read again, and drafts let go
    setDisabled(null);
    setCustom(null);
    setConfirming(false);
    setNote(null);
    setFresh(null);
    reloadRules();
    reloadStored();
  }, [generation, reloadRules, reloadStored]);
  const current = fresh ?? rules.data;
  const builtin = (current?.rules ?? []).filter((rule) => rule.builtin);
  const savedDisabled = builtin.filter((rule) => !rule.enabled).map((rule) => rule.id);
  const savedCustom: Draft[] = (current?.rules ?? []).filter((rule) => !rule.builtin).map((rule, index) => ({ key: -1 - index, name: rule.name, words: rule.words }));
  const shownDisabled = disabled ?? savedDisabled;
  const shownCustom = custom ?? savedCustom;
  const changed = disabled !== null || custom !== null;
  const edit = () => {
    setNote(null);
    setConfirming(false); // what is stored is hidden by the saved rules: changed ones must be saved first
  };
  const setRule = (key: number, next: Draft | null) => {
    setCustom(shownCustom.flatMap((rule) => (rule.key !== key ? [rule] : next ? [next] : [])));
    edit();
  };
  const save = async () => {
    setBusy(true);
    setNote(null);
    try {
      const answer = await api.put("/api/v1/privacy/redaction", {
        body: { disabled: shownDisabled, custom: shownCustom.map((rule) => ({ name: rule.name.trim() || "Your words", words: rule.words })) },
      });
      setFresh(answer); // the rules as the hub keeps them now (words tidied), shown at once
      setDisabled(null);
      setCustom(null);
      reloadRules();
      reloadStored();
      setNote({ text: "Saved. New titles follow these rules from now on.", problem: false });
    } catch (error) {
      setNote({ text: error instanceof ApiError ? error.message : "The rules couldn't be saved.", problem: true });
    } finally {
      setBusy(false);
    }
  };
  const apply = async () => {
    setBusy(true);
    try {
      const done = await api.post("/api/v1/privacy/redaction/apply", { body: { confirm: true }, timeout: LONG_MS });
      setNote({ text: `Hidden in ${plural(done.redacted, "stored event")}.`, problem: false });
    } catch (error) {
      setNote({ text: error instanceof ApiError ? `${error.message} Some stored events may already be hidden: the count below is read again.` : "The stored events couldn't be changed.", problem: true });
    } finally {
      reloadStored();
      setBusy(false);
      setConfirming(false);
    }
  };
  const matches = stored.current ? stored.data?.matches : undefined;
  return (
    <ChartCard title="Hide sensitive titles" className="redaction-card" info={`A title a rule matches is stored as "${current?.redacted ?? "[redacted]"}", never as written: banking, health, password managers and private windows are hidden unless you switch them off. Your own words also hide app names and sites.`}>
      {rules.error && !current ? (
        <div className="stack">
          <p className="muted">The rules couldn&apos;t load: {rules.error.message}</p>
          <button type="button" className="button button-ghost" onClick={reloadRules}>
            Try again
          </button>
        </div>
      ) : !current ? (
        <p className="muted">Loading the rules...</p>
      ) : (
        <>
          <fieldset className="rules-editor" disabled={busy}>
            <legend className="visually-hidden">The redaction rules</legend>
            <h3 className="section-subtitle">Built-in rules</h3>
            <ul className="rule-list">
              {builtin.map((rule) => (
                <li key={rule.id}>
                  <label className="switch-row">
                    <input
                      type="checkbox"
                      role="switch"
                      checked={!shownDisabled.includes(rule.id)}
                      onChange={(event) => {
                        setDisabled(event.target.checked ? shownDisabled.filter((id) => id !== rule.id) : [...shownDisabled, rule.id]);
                        edit();
                      }}
                    />
                    <span>
                      <strong>{rule.name}</strong>
                      <span className="muted">{rule.description}</span>
                    </span>
                  </label>
                </li>
              ))}
            </ul>
            <h3 className="section-subtitle">Your words</h3>
            {shownCustom.length > 0 && (
              <ul className="custom-rules">
                {shownCustom.map((rule) => (
                  <CustomRule key={rule.key} rule={rule} onChange={(next) => setRule(rule.key, next)} onRemove={() => setRule(rule.key, null)} />
                ))}
              </ul>
            )}
            <div className="row redaction-actions">
              <button
                type="button"
                className="button button-ghost"
                disabled={shownCustom.length >= MAX_RULES}
                onClick={() => {
                  counter.current += 1;
                  setCustom([...shownCustom, { key: counter.current, name: "Your words", words: [] }]);
                  edit();
                }}
              >
                Add a rule
              </button>
              <button type="button" className="button" onClick={save} disabled={!changed || shownCustom.some((rule) => !rule.words.length)}>
                {busy ? "Saving..." : "Save the rules"}
              </button>
              {changed && (
                <button type="button" className="button button-ghost" onClick={() => { setDisabled(null); setCustom(null); edit(); }}>
                  Undo changes
                </button>
              )}
            </div>
          </fieldset>
          <p className={`field-note redaction-note${note?.problem ? "" : " field-hint"}`} role="status">
            {note?.text}
          </p>
          <TryTitle unsaved={changed} />
          <div className="stored-matches">
            {stored.error && !stored.current ? (
              <>
                <p>The stored events couldn&apos;t be counted: {stored.error.message}</p>
                <button type="button" className="button button-ghost" onClick={reloadStored}>
                  Count again
                </button>
              </>
            ) : (
              <p>
                {matches === undefined
                  ? "Counting what is already stored..."
                  : matches === 0
                    ? "Nothing already stored matches these rules."
                    : `${plural(matches, "stored event")} ${matches === 1 ? "matches" : "match"} these rules: they were stored before the rules said so.`}
              </p>
            )}
            {matches !== undefined && matches > 0 && !confirming && (
              <>
                <button type="button" className="button button-ghost" onClick={() => setConfirming(true)} disabled={busy || changed}>
                  Hide them in stored data
                </button>
                {changed && <p className="muted">Save the rules first: what is stored is hidden by the saved rules.</p>}
              </>
            )}
            {confirming && (
              <div className="confirm-row" role="group" aria-label="Hide stored words">
                <p>This can&apos;t be undone: the words are gone from the stored events for good.</p>
                <div className="row">
                  <button type="button" className="button button-ghost" onClick={() => setConfirming(false)} disabled={busy}>
                    Keep them
                  </button>
                  <button type="button" className="button button-danger" onClick={apply} disabled={busy}>
                    {busy ? "Hiding..." : `Hide ${plural(matches ?? 0, "event")}`}
                  </button>
                </div>
              </div>
            )}
          </div>
        </>
      )}
    </ChartCard>
  );
}

// --- export and delete ------------------------------------------------------------------------------------------------

function DeleteDialog({ open, onClose, onDone }: { open: boolean; onClose: () => void; onDone: (said: string) => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const title = useId();
  const field = useId();
  const [typed, setTyped] = useState("");
  const [keep, setKeep] = useState(true);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  useEffect(() => {
    const element = dialog.current;
    if (!element) return;
    if (open && !element.open) {
      setTyped(""); // asked again every time: nothing typed before counts ...
      setKeep(true); // ... and the rules are kept unless this try says otherwise
      setProblem(null);
      element.showModal();
    }
    if (!open && element.open) element.close();
  }, [open]);
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (typed !== DELETE_PHRASE) return;
    setBusy(true);
    setProblem(null);
    try {
      const result = await api.post("/api/v1/privacy/delete", { body: { confirm: typed, keep_redaction_rules: keep }, timeout: LONG_MS });
      onDone(deletedText(result));
    } catch (error) {
      if (error instanceof ApiError && error.status >= 400 && error.status < 500) {
        setProblem(`Nothing was deleted: ${error.message}`); // refused before anything was touched
      } else {
        // No answer (or a broken one): the hub may have deleted everything, or be deleting it still.
        onDone("The hub didn't say how the delete went: it may have deleted everything, or be finishing now. The numbers above are read again.");
      }
    } finally {
      setBusy(false);
      setTyped("");
    }
  };
  return (
    <dialog ref={dialog} className="dialog" aria-labelledby={title} onCancel={(event) => busy && event.preventDefault()} onClose={onClose}>
      <form onSubmit={submit}>
        <h2 id={title}>Delete all your data?</h2>
        <p>
          Every event, device, goal, badge, story and nudge this profile holds is deleted and overwritten in the file. It can&apos;t be undone. Devices
          pair again to send new data.
        </p>
        <label htmlFor={field}>
          Type <strong>{DELETE_PHRASE}</strong> to confirm
        </label>
        <input id={field} className="date-input delete-phrase" autoComplete="off" spellCheck={false} value={typed} onChange={(event) => setTyped(event.target.value)} autoFocus />
        <label className="switch-row">
          <input type="checkbox" checked={keep} onChange={(event) => setKeep(event.target.checked)} />
          <span>Keep my redaction rules, so what is recorded next stays protected</span>
        </label>
        {problem && <p className="field-note">{problem}</p>}
        <div className="row dialog-actions">
          <button type="button" className="button button-ghost" onClick={onClose} disabled={busy}>
            Keep my data
          </button>
          <button type="submit" className="button button-danger" disabled={busy || typed !== DELETE_PHRASE}>
            {busy ? "Deleting..." : "Delete everything"}
          </button>
        </div>
      </form>
    </dialog>
  );
}

function deletedText(result: Deleted): string {
  const rows = Object.values(result.deleted).reduce((sum, count) => sum + count, 0);
  const events = result.deleted.events ?? 0;
  return `Deleted ${plural(rows, "row")}, ${plural(events, "event")} among them${result.wiped ? ", and overwritten in the file" : ""}.`;
}

function ExportDeleteCard({ local, unknown, onDeleted }: { local: boolean | undefined; unknown: ApiError | undefined; onDeleted: () => void }) {
  const [deleting, setDeleting] = useState(false);
  const [done, setDone] = useState<string | null>(null);
  if (local === undefined) {
    return (
      <ChartCard title="Export or delete" className="export-card">
        <p className="muted">
          {unknown
            ? `The hub didn't answer (${unknown.message}), so export and delete wait until it does: it is asked again every few seconds.`
            : "Checking whether this is the hub computer..."}
        </p>
      </ChartCard>
    );
  }
  return (
    <ChartCard title="Export or delete" className="export-card">
      {local ? (
        <>
          <p>One JSON file with everything this profile holds (device tokens excepted), to keep or to read.</p>
          {/* A plain link: the hub's Content-Disposition names the file (by the hub's date) and the browser streams
              it to disk, however large. */}
          <a className="button" href="/api/v1/privacy/export">
            Export all (JSON)
          </a>
          <div className="danger-zone">
            <p>Deleting empties the profile. The hub keeps running, and asks you to type a phrase every time.</p>
            <button type="button" className="button button-danger" onClick={() => { setDone(null); setDeleting(true); }}>
              Delete all data
            </button>
          </div>
          <p className="field-note field-hint" role="status">
            {done}
          </p>
          <DeleteDialog
            open={deleting}
            onClose={() => setDeleting(false)}
            onDone={(said) => {
              setDeleting(false);
              setDone(said);
              onDeleted();
            }}
          />
        </>
      ) : (
        <p className="muted">
          Export and delete work only on the hub computer itself, in its browser at <code>http://localhost</code>: a phone or another computer
          can&apos;t take or erase everything.
        </p>
      )}
    </ChartCard>
  );
}

export default function Privacy() {
  const health = useApi("/api/v1/health", { quiet: true });
  const local = health.data ? health.data.local === true : undefined; // unknown until the hub says
  const network = useApi("/api/v1/privacy/network", { quiet: true });
  const storage = useApi("/api/v1/privacy/storage", { quiet: true });
  const devices = useApi("/api/v1/devices", { quiet: true });
  const [generation, setGeneration] = useState(0); // counts delete-alls: everything shown is read again
  usePolling(NETWORK_REFRESH_MS, network.reload);
  usePolling(health.data ? null : 5_000, health.reload); // a hub still starting is asked again
  const refresh = () => {
    storage.reload();
    network.reload();
    devices.reload();
    setGeneration((count) => count + 1);
  };
  return (
    <div className="stack">
      <header className="page-head">
        <div>
          <p className="eyebrow">Yours, on your network</p>
          <h1>Privacy</h1>
        </div>
      </header>
      <div className="grid privacy-grid">
        <NetworkCard network={network.data} error={network.error} />
        <StorageCard storage={storage.data} error={storage.error} devices={devices.data?.devices} local={local === true} />
      </div>
      <RedactionCard generation={generation} />
      <ExportDeleteCard local={local} unknown={health.error} onDeleted={refresh} />
    </div>
  );
}
