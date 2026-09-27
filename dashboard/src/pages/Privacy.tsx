// DT-36: your data and what you can do with it. The network check (DT-45) says in one big number that the hub has made
// or served no internet connection since it started, with every connection it did make or serve by network. Then
// where the data lives (the folder only on the hub computer, since it names that computer's user), its size, the
// events and the devices; the redaction rules (DT-44) to switch built-in ones on or off and keep your own words, try
// a title against them, and hide what is already stored; and the export and delete-all (DT-46), which only the hub
// computer can do. Deleting asks for the phrase to be typed every time: the field starts empty on each try.
import { type FormEvent, useEffect, useId, useRef, useState } from "react";
import { Link } from "react-router";
import { ApiError, api, useApi, usePolling } from "../api/client";
import type { components } from "../api/schema";
import ChartCard from "../components/ChartCard";
import { DEVICE_TYPE_LABELS } from "../theme/devices";

type Network = components["schemas"]["NetworkStatus"];
type Counts = components["schemas"]["NetworkCounts"];
type Rule = components["schemas"]["RedactionRule"];
type Storage = components["schemas"]["Storage"];
type Deleted = components["schemas"]["Deleted"];
type Device = components["schemas"]["DeviceInfo"];

export const DELETE_PHRASE = "delete all my daytrace data";
const NETWORK_REFRESH_MS = 15_000;
const MAX_WORDS = 50; // the hub's limits (redaction.py)
const MAX_RULES = 20;

/** A size in bytes as people say it: "824 KB", "12.4 MB". */
export function sizeText(bytes: number): string {
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
  if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
  return `${(bytes / 1024 ** 3).toFixed(2)} GB`;
}

const whenText = (iso: string) => new Date(iso).toLocaleString([], { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
const dayText = (iso: string) => new Date(iso).toLocaleDateString([], { day: "numeric", month: "long", year: "numeric" });
const total = (counts: Counts) => counts.localhost + counts.lan + counts.tailscale + counts.internet;
const plural = (count: number, one: string, many = `${one}s`) => `${count.toLocaleString()} ${count === 1 ? one : many}`;

// --- the network check -------------------------------------------------------------------------------------------

function CountsLine({ counts }: { counts: Counts }) {
  const parts = [
    ["this computer", counts.localhost],
    ["your home network", counts.lan],
    ["Tailscale", counts.tailscale],
    ["the internet", counts.internet],
  ] as const;
  const shown = parts.filter(([, count]) => count > 0);
  if (!shown.length) return <>none</>;
  return <>{shown.map(([where, count]) => `${count.toLocaleString()} from ${where}`).join(", ")}</>;
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
                <CountsLine counts={network.incoming} />
              </dd>
            </div>
            <div>
              <dt>Made (to the local model)</dt>
              <dd>
                <CountsLine counts={network.outgoing} />
              </dd>
            </div>
            <div>
              <dt>Refused coming in</dt>
              <dd>{total(network.refused) ? <CountsLine counts={network.refused} /> : "none"}</dd>
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
  const nameField = useId();
  const wordField = useId();
  const add = (event: FormEvent) => {
    event.preventDefault();
    const clean = word.split(/\s+/).filter(Boolean).join(" ");
    if (clean.length < 2 || rule.words.some((known) => known.toLowerCase() === clean.toLowerCase()) || rule.words.length >= MAX_WORDS) return;
    onChange({ ...rule, words: [...rule.words, clean] });
    setWord("");
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
        <input id={wordField} className="date-input" placeholder="A word or phrase" value={word} maxLength={100} onChange={(event) => setWord(event.target.value)} />
        <button type="submit" className="button button-ghost" disabled={word.trim().length < 2}>
          Add
        </button>
      </form>
    </li>
  );
}

function TryTitle() {
  const [title, setTitle] = useState("");
  const [result, setResult] = useState<string | null>(null);
  const field = useId();
  const check = async (event: FormEvent) => {
    event.preventDefault();
    try {
      const found = await api.post("/api/v1/privacy/redaction/check", { body: { title } });
      setResult(found.redacted ? `Stored as "${found.stored_as}", by the ${found.rule_name} rule.` : "Stored as it is: no rule matches.");
    } catch (error) {
      setResult(error instanceof ApiError ? error.message : "The title couldn't be checked.");
    }
  };
  return (
    <form className="try-title" onSubmit={check}>
      <label htmlFor={field}>Try a window or event title</label>
      <div className="row">
        <input id={field} className="date-input" placeholder="MyBank - Account Summary" value={title} maxLength={500} onChange={(event) => { setTitle(event.target.value); setResult(null); }} />
        <button type="submit" className="button button-ghost" disabled={!title.trim()}>
          Check
        </button>
      </div>
      <p className="field-note field-hint" role="status">
        {result}
      </p>
    </form>
  );
}

function RedactionCard({ onApplied }: { onApplied: () => void }) {
  const rules = useApi("/api/v1/privacy/redaction", { quiet: true });
  const stored = useApi("/api/v1/privacy/redaction/stored", { quiet: true });
  const [disabled, setDisabled] = useState<string[] | null>(null); // null: as the hub has them
  const [custom, setCustom] = useState<Draft[] | null>(null);
  const [note, setNote] = useState<{ text: string; problem: boolean } | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const counter = useRef(0);
  const builtin = (rules.data?.rules ?? []).filter((rule) => rule.builtin);
  const savedDisabled = builtin.filter((rule) => !rule.enabled).map((rule) => rule.id);
  const savedCustom: Draft[] = (rules.data?.rules ?? []).filter((rule) => !rule.builtin).map((rule: Rule, index) => ({ key: -1 - index, name: rule.name, words: rule.words }));
  const shownDisabled = disabled ?? savedDisabled;
  const shownCustom = custom ?? savedCustom;
  const changed = disabled !== null || custom !== null;
  const setRule = (key: number, next: Draft | null) => {
    setCustom(shownCustom.flatMap((rule) => (rule.key !== key ? [rule] : next ? [next] : [])));
    setNote(null);
    setConfirming(false); // what is stored is hidden by the saved rules: changed ones must be saved first
  };
  const save = async () => {
    setBusy(true);
    setNote(null);
    try {
      await api.put("/api/v1/privacy/redaction", {
        body: { disabled: shownDisabled, custom: shownCustom.map((rule) => ({ name: rule.name.trim() || "Your words", words: rule.words })) },
      });
      setDisabled(null);
      setCustom(null);
      rules.reload();
      stored.reload();
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
      const done = await api.post("/api/v1/privacy/redaction/apply", { body: { confirm: true } });
      setNote({ text: `Hidden in ${plural(done.redacted, "stored event")}.`, problem: false });
      stored.reload();
      onApplied();
    } catch (error) {
      setNote({ text: error instanceof ApiError ? error.message : "The stored events couldn't be changed.", problem: true });
    } finally {
      setBusy(false);
      setConfirming(false);
    }
  };
  const matches = stored.data?.matches;
  return (
    <ChartCard title="Hide sensitive titles" className="redaction-card" info={`A title a rule matches is stored as "${rules.data?.redacted ?? "[redacted]"}", never as written: banking, health, password managers and private windows are hidden unless you switch them off. Your own words also hide app names and sites.`}>
      {rules.error && !rules.data ? (
        <p className="muted">The rules couldn&apos;t load: {rules.error.message}</p>
      ) : (
        <>
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
                      setNote(null);
                      setConfirming(false);
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
                setConfirming(false);
              }}
            >
              Add a rule
            </button>
            <button type="button" className="button" onClick={save} disabled={!changed || busy || shownCustom.some((rule) => !rule.words.length)}>
              {busy ? "Saving..." : "Save the rules"}
            </button>
            {changed && (
              <button type="button" className="button button-ghost" onClick={() => { setDisabled(null); setCustom(null); setNote(null); }} disabled={busy}>
                Undo changes
              </button>
            )}
          </div>
          <p className={`field-note${note?.problem ? "" : " field-hint"}`} role="status">
            {note?.text}
          </p>
          <TryTitle />
          <div className="stored-matches">
            <p>
              {matches === undefined
                ? "Counting what is already stored..."
                : matches === 0
                  ? "Nothing already stored matches these rules."
                  : `${plural(matches, "stored event")} ${matches === 1 ? "matches" : "match"} these rules: they were stored before the rules said so.`}
            </p>
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

function DeleteDialog({ open, onClose, onDeleted }: { open: boolean; onClose: () => void; onDeleted: (result: Deleted) => void }) {
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
      setTyped(""); // asked again every time: nothing typed before counts
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
      const result = await api.post("/api/v1/privacy/delete", { body: { confirm: typed, keep_redaction_rules: keep } });
      onDeleted(result);
    } catch (error) {
      setProblem(error instanceof ApiError ? error.message : "Nothing was deleted: the hub couldn't be reached.");
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

function ExportDeleteCard({ local, onDeleted }: { local: boolean; onDeleted: () => void }) {
  const [deleting, setDeleting] = useState(false);
  const [done, setDone] = useState<string | null>(null);
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
            onDeleted={(result) => {
              setDeleting(false);
              setDone(deletedText(result));
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
  const local = health.data?.local === true;
  const network = useApi("/api/v1/privacy/network", { quiet: true });
  const storage = useApi("/api/v1/privacy/storage", { quiet: true });
  const devices = useApi("/api/v1/devices", { quiet: true });
  usePolling(NETWORK_REFRESH_MS, network.reload);
  const refresh = () => {
    storage.reload();
    network.reload();
    devices.reload();
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
        <StorageCard storage={storage.data} error={storage.error} devices={devices.data?.devices} local={local} />
      </div>
      <RedactionCard onApplied={storage.reload} />
      <ExportDeleteCard local={local} onDeleted={refresh} />
    </div>
  );
}
