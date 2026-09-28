// DT-32: Devices. Pair phones and browsers, and see and manage every paired device.
//
// - On the hub computer (GET /health says `local`: the same check pairing and revoking make), a pairing code with a
//   ring for its 5 minutes, two QR codes (for the Daytrace app, DT-22, and for a phone's camera, which opens this
//   page on the phone and pairs its browser), and the hub's addresses. It can also give a token right there, for
//   the browser extension or iPhone Shortcuts. The hub says when the code has been used and by what (GET
//   /pair/status, by the code's id, never the code itself), and when a newer code (another tab) replaced it.
// - Anywhere else, "Pair this device": type the code (or arrive from the camera's QR code, which fills it in and
//   pairs at once, unless this browser is already paired) to view the dashboard in this browser, or to get a token
//   for iPhone Shortcuts or the browser extension. A token is shown once, with a Copy button (plain http on the
//   Wi-Fi has no Clipboard API, so an older way is used there).
// - The paired devices: platform, last contact, a live dot, their last seq and events, and, on the hub computer,
//   Revoke for devices with a token (asked every time). A revoked device leaves the list with a little animation;
//   its data stays, listed apart. The hub's own tracker and the demo data write directly: nothing to revoke.
// - Help to put the dashboard on a phone's home screen, and the Shortcuts guide.
import { AnimatePresence, motion } from "motion/react";
import { type FormEvent, useCallback, useEffect, useId, useRef, useState } from "react";
import { Link } from "react-router";
import { ApiError, api, getToken, setToken, toast, useApi, usePolling } from "../api/client";
import type { components } from "../api/schema";
import ChartCard from "../components/ChartCard";
import QrCode from "../components/QrCode";
import Tabs from "../components/Tabs";
import { embedded } from "../embed";
import { DEVICE_TYPE_LABELS } from "../theme/devices";
import { celebrate } from "../theme/motion";

type Started = components["schemas"]["PairStarted"];
type Claimed = components["schemas"]["PairClaimed"];
type Device = components["schemas"]["DeviceInfo"];
type DeviceType = Claimed["device_type"];

const CODE_LIFETIME_MS = 5 * 60_000; // the hub's pairing codes last 5 minutes
const LIVE_MS = 60_000;
const REFRESH_MS = 15_000;
const STATUS_MS = 2_000; // while a code is out: has it been used?
const SHORTCUTS_GUIDE = "https://github.com/SaiGaneshPS/daytrace/blob/development/ios/shortcuts/SETUP.md";

/** A name for this browser's device, from what it says about itself ("Android phone (Chrome)"). */
function guessName(): string {
  const agent = typeof navigator === "undefined" ? "" : navigator.userAgent;
  const device = /iPhone/.test(agent)
    ? "iPhone"
    : /iPad/.test(agent)
      ? "iPad"
      : /Android/.test(agent)
        ? "Android phone"
        : /Macintosh/.test(agent)
          ? "Mac"
          : /Windows/.test(agent)
            ? "Windows PC"
            : "Browser";
  const browser = /SamsungBrowser/.test(agent)
    ? "Samsung Internet"
    : /EdgA?\//.test(agent)
      ? "Edge"
      : /CriOS|Chrome\//.test(agent)
        ? "Chrome"
        : /FxiOS|Firefox\//.test(agent)
          ? "Firefox"
          : /Safari\//.test(agent)
            ? "Safari"
            : "";
  return browser ? `${device} (${browser})` : device;
}

type Purpose = { type: DeviceType; label: string; hint: string; name: () => string };
const VIEW: Purpose = {
  type: "viewer",
  label: "View the dashboard in this browser",
  hint: "This browser can then show your days. It never sends any data itself.",
  name: guessName,
};
const SHORTCUTS: Purpose = {
  type: "ios",
  label: "Get a token for iPhone Shortcuts",
  hint: "Paste it into the Daytrace Shortcuts when they ask for it.",
  name: () => "iPhone Shortcuts",
};
const EXTENSION: Purpose = {
  type: "browser",
  label: "Get a token for the browser extension",
  hint: "Paste it into the extension's options.",
  name: () => "Browser extension",
};
const FOR_A_NEW_BROWSER = [VIEW, SHORTCUTS, EXTENSION];
const FOR_A_PAIRED_BROWSER = [SHORTCUTS, EXTENSION, VIEW];
const TOKENS_ONLY = [SHORTCUTS, EXTENSION];

const spaced = (code: string) => `${code.slice(0, 3)} ${code.slice(3)}`;
const spoken = (code: string) => code.split("").join(" ");
const digits = (text: string) => text.replace(/[\s-]/g, "");

function ago(iso: string, now: number): string {
  const seconds = Math.max(0, Math.round((now - Date.parse(iso)) / 1000));
  if (seconds < 45) return "just now";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} h ago`;
  const days = Math.round(hours / 24);
  if (days === 1) return "yesterday";
  if (days < 7) return `${days} days ago`;
  return new Date(iso).toLocaleDateString([], { day: "numeric", month: "short" });
}

/** When the device was last in touch, or where its data comes from when it never is: the demo data and the hub
 * computer's own tracker write on the hub directly, without a token, so they never "check in". */
function contact(device: Device, now: number): string {
  if (device.last_seen) return `Last seen ${ago(device.last_seen, now)}`;
  if (!device.has_token) return device.device_id.startsWith("seed-") ? "Demo data, made on the hub" : "Tracked on the hub computer";
  return "Hasn't been in touch yet";
}

function clock(ms: number): string {
  const total = Math.ceil(ms / 1000);
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`;
}

/** Copies text, and says whether it worked. Plain http on the Wi-Fi isn't a secure context, so the Clipboard API
 * is missing there; the older way still works. */
async function copyText(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    // fall back below
  }
  const area = document.createElement("textarea");
  area.value = text;
  area.setAttribute("readonly", "");
  area.style.position = "fixed";
  area.style.opacity = "0";
  document.body.appendChild(area);
  area.select();
  let done = false;
  try {
    done = document.execCommand("copy");
  } catch {
    done = false;
  }
  area.remove();
  return done;
}

function CopyButton({ text, label }: { text: string; label: string }) {
  const [state, setState] = useState<"idle" | "copied" | "failed">("idle");
  useEffect(() => {
    if (state === "idle") return;
    const timer = window.setTimeout(() => setState("idle"), 2500);
    return () => window.clearTimeout(timer);
  }, [state]);
  return (
    <button type="button" className="button button-ghost copy-button" onClick={async () => setState((await copyText(text)) ? "copied" : "failed")}>
      <span aria-live="polite">{state === "copied" ? "Copied" : state === "failed" ? "Select it and copy" : label}</span>
    </button>
  );
}

/** The time of day (ms), ticking every `ms`, for "last seen 2 min ago". */
function useWallClock(ms: number): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), ms);
    return () => window.clearInterval(timer);
  }, [ms]);
  return now;
}

/** The monotonic time, ticking every `ms` (null: stopped). */
function useTicking(ms: number | null): number {
  const [now, setNow] = useState(() => performance.now());
  useEffect(() => {
    setNow(performance.now());
    if (ms === null) return;
    const timer = window.setInterval(() => setNow(performance.now()), ms);
    return () => window.clearInterval(timer);
  }, [ms]);
  return now;
}

// --- platform icons --------------------------------------------------------------------------------------------

function PlatformIcon({ type }: { type: string }) {
  const common = { viewBox: "0 0 24 24", width: 22, height: 22, fill: "none", stroke: "currentColor", strokeWidth: 2, strokeLinecap: "round" as const, strokeLinejoin: "round" as const, "aria-hidden": true };
  switch (type) {
    case "windows":
      return (
        <svg {...common}>
          <rect x="3" y="4" width="18" height="12" rx="2" />
          <path d="M8 20h8M12 16v4" />
        </svg>
      );
    case "macos":
      return (
        <svg {...common}>
          <rect x="4" y="4" width="16" height="11" rx="2" />
          <path d="M2 19h20" />
        </svg>
      );
    case "android":
    case "ios":
      return (
        <svg {...common}>
          <rect x="7" y="2" width="10" height="20" rx="3" />
          <path d="M11 18h2" />
        </svg>
      );
    case "browser":
      return (
        <svg {...common}>
          <circle cx="12" cy="12" r="9" />
          <path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18" />
        </svg>
      );
    default:
      return (
        <svg {...common}>
          <path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z" />
          <circle cx="12" cy="12" r="3" />
        </svg>
      );
  }
}

// --- getting a token -------------------------------------------------------------------------------------------

/** A new token, shown once. `hubUrl`: the address to use it at; null when the hub has none a phone can reach. */
function TokenReveal({ claimed, hubUrl, onDone }: { claimed: Claimed; hubUrl: string | null; onDone: () => void }) {
  return (
    <div className="token-reveal" role="status">
      <p>
        <strong>{claimed.name}</strong> is paired as <code>{claimed.device_id}</code>. Its token is shown only this once:
      </p>
      <div className="copy-row">
        <code className="token">{claimed.token}</code>
        <CopyButton text={claimed.token} label="Copy the token" />
      </div>
      {hubUrl ? (
        <div className="copy-row">
          <span>
            Hub address: <code>{hubUrl}</code>
          </span>
          <CopyButton text={hubUrl} label="Copy the address" />
        </div>
      ) : (
        <p className="muted">
          The hub has no address a phone can reach yet: connect this computer to your Wi-Fi or a network cable, and its
          address shows under Pair a device.
        </p>
      )}
      <p className="muted">
        Keep the token private: anyone who has it can send data as this device. If it leaks, revoke the device below and
        pair again.
      </p>
      {claimed.device_type === "ios" && (
        <p>
          <a href={SHORTCUTS_GUIDE} target="_blank" rel="noreferrer">
            How to set up the iPhone Shortcuts
          </a>
        </p>
      )}
      <button type="button" className="button" onClick={onDone}>
        Done, I&apos;ve saved it
      </button>
    </div>
  );
}

type ClaimProps = {
  purposes: Purpose[];
  /** A code already known (the hub computer's own), so it isn't asked for. */
  code?: string;
  /** A code from the camera's link. A new one can arrive while the form is open (the link opened again). */
  linkCode?: string;
  /** Pair at once with `linkCode`, for the first purpose. */
  autoSubmit?: boolean;
  hubUrl: string | null;
  onPaired: (claimed: Claimed) => void;
  /** Show a new token somewhere else (the pairing panel shows it in place of the code); here by default. */
  onToken?: (claimed: Claimed) => void;
};

function ClaimForm({ purposes, code: fixedCode, linkCode = "", autoSubmit = false, hubUrl, onPaired, onToken }: ClaimProps) {
  const ids = { code: useId(), name: useId(), hint: useId() };
  const [code, setCode] = useState(linkCode);
  const [purpose, setPurpose] = useState<Purpose>(purposes[0]);
  const [name, setName] = useState(() => purposes[0].name());
  const [named, setNamed] = useState(false); // the person typed a name: keep it when the purpose changes
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);
  const [token, setTokenShown] = useState<Claimed | null>(null);
  const [viewing, setViewing] = useState<Claimed | null>(null);
  const tried = useRef(""); // the link code last tried by itself: each is single use

  const claim = useCallback(
    async (chosen: Purpose, typed: string, deviceName: string) => {
      const value = digits(fixedCode ?? typed);
      if (!/^\d{6}$/.test(value)) {
        setFailure("The code is the 6 digits on the hub computer's Devices page.");
        return;
      }
      setBusy(true);
      setFailure(null);
      try {
        const claimed = await api.post("/api/v1/pair/claim", {
          body: { code: value, device_name: deviceName.trim() || chosen.name(), device_type: chosen.type, dashboard: false },
        });
        if (chosen.type === "viewer") {
          setToken(claimed.token);
          setViewing(claimed);
          void celebrate();
        } else if (onToken) {
          onToken(claimed);
        } else {
          setTokenShown(claimed);
        }
        setCode("");
        onPaired(claimed);
      } catch (error) {
        setFailure(error instanceof ApiError ? error.message : String(error));
      } finally {
        setBusy(false);
      }
    },
    [fixedCode, onPaired, onToken],
  );

  // A new code from the link (the camera's QR code again, in the same tab) fills the box, and pairs by itself when
  // this browser isn't paired yet. Only a new link code runs this: the name and purpose are read, not watched.
  const latest = useRef({ purposes, claim, name, named });
  latest.current = { purposes, claim, name, named };
  useEffect(() => {
    if (!linkCode) return;
    setCode(linkCode);
    setViewing(null);
    if (!autoSubmit || tried.current === linkCode) return;
    tried.current = linkCode;
    const now = latest.current;
    setPurpose(now.purposes[0]);
    void now.claim(now.purposes[0], linkCode, now.named ? now.name : now.purposes[0].name());
  }, [linkCode, autoSubmit]);

  const choose = (next: Purpose) => {
    setPurpose(next);
    if (!named) setName(next.name());
  };
  const submit = (event: FormEvent) => {
    event.preventDefault();
    void claim(purpose, code, name);
  };

  if (token) return <TokenReveal claimed={token} hubUrl={hubUrl} onDone={() => setTokenShown(null)} />;
  if (viewing) {
    return (
      <div className="pair-success" role="status">
        <p className="pair-success-title">This browser is paired as {viewing.name}.</p>
        <p>
          Your days are on <Link to="/">Today</Link>. The hub keeps this browser paired until it is revoked on the hub
          computer.
        </p>
      </div>
    );
  }
  return (
    <form className="claim-form" onSubmit={submit} aria-busy={busy}>
      {purposes.length > 1 && (
        <fieldset className="purposes">
          <legend>What is it for?</legend>
          {purposes.map((item) => (
            <label key={item.type} className="purpose">
              <input type="radio" name="purpose" checked={purpose.type === item.type} onChange={() => choose(item)} />
              <span>
                <span className="purpose-label">{item.label}</span>
                <span className="muted purpose-hint">{item.hint}</span>
              </span>
            </label>
          ))}
        </fieldset>
      )}
      {fixedCode === undefined && (
        <div className="field">
          <label htmlFor={ids.code}>Pairing code</label>
          <input
            id={ids.code}
            className="text-input code-input"
            inputMode="numeric"
            autoComplete="one-time-code"
            placeholder="123 456"
            maxLength={9}
            value={code}
            aria-describedby={ids.hint}
            onChange={(event) => setCode(event.target.value)}
          />
          <span className="muted" id={ids.hint}>
            On the hub computer, open Devices and choose &quot;Show a pairing code&quot;.
          </span>
        </div>
      )}
      <div className="field">
        <label htmlFor={ids.name}>Name in the device list</label>
        <input
          id={ids.name}
          className="text-input"
          maxLength={64}
          value={name}
          onChange={(event) => {
            setName(event.target.value);
            setNamed(true);
          }}
        />
      </div>
      {failure && (
        <p className="form-error" role="alert">
          {failure}
        </p>
      )}
      <button type="submit" className="button" disabled={busy}>
        {busy ? "Pairing..." : purpose.type === "viewer" ? "Pair this browser" : "Get the token"}
      </button>
    </form>
  );
}

// --- the hub computer's pairing panel --------------------------------------------------------------------------

/** A code on show, and when it was started (monotonic). */
type Code = { reply: Started; at: number };

function PairPanel({ onChanged }: { onChanged: () => void }) {
  const [code, setCode] = useState<Code | null>(null);
  const [ranOut, setRanOut] = useState(false);
  const [superseded, setSuperseded] = useState(false); // a newer code (another tab) replaced this one
  const [kind, setKind] = useState("app");
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);
  // Who used the code. DT-22: `returning` is a device that paired before and came back with its first id.
  const [paired, setPaired] = useState<{ name: string; deviceId: string; returning: boolean } | null>(null);
  // A token given here, with the address to use it at (the code's network address, gone with the code).
  const [issued, setIssued] = useState<{ claimed: Claimed; hubUrl: string | null } | null>(null);
  const live = code !== null && !ranOut && !superseded;
  const now = useTicking(live ? 1000 : null); // stops once the code has run out
  const left = code ? Math.min(CODE_LIFETIME_MS, Math.max(0, CODE_LIFETIME_MS - (now - code.at))) : 0;
  const active = live && left > 0;

  useEffect(() => {
    if (live && left === 0) setRanOut(true);
  }, [live, left]);

  // Ask the hub whether this code has been used, and by what. It knows (the list of devices only lets one guess).
  const checking = useRef(false);
  const lanUrl = code?.reply.url ?? null;
  const codeId = code?.reply.id ?? null;
  const check = useCallback(async () => {
    if (!codeId || checking.current) return;
    checking.current = true;
    try {
      const status = await api.get("/api/v1/pair/status");
      if (status.id !== codeId) {
        setSuperseded(true);
      } else if (status.used && status.claimed_by) {
        const by = status.claimed_by;
        setPaired({ name: by.name, deviceId: by.device_id, returning: by.returning ?? false });
        setCode(null);
        onChanged();
        void celebrate();
      }
    } catch {
      // the next check tries again
    } finally {
      checking.current = false;
    }
  }, [codeId, onChanged]);
  usePolling(active ? STATUS_MS : null, check);

  // A token given right here: show it in place of the code (which it used up), until it has been saved.
  const onToken = useCallback(
    (claimed: Claimed) => {
      setIssued({ claimed, hubUrl: lanUrl });
      setCode(null);
    },
    [lanUrl],
  );

  const start = async () => {
    setBusy(true);
    setFailure(null);
    setPaired(null);
    setIssued(null);
    try {
      const reply = await api.post("/api/v1/pair/start");
      setCode({ reply, at: performance.now() });
      setRanOut(false);
      setSuperseded(false);
    } catch (error) {
      setFailure(error instanceof ApiError ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  };

  const reply = code?.reply;
  const qr = (target: "app" | "browser") => `${reply?.qr ?? "/api/v1/pair/qr.png"}?for=${target}&id=${reply?.id ?? ""}`;
  const showQr = active && Boolean(reply?.qr);

  return (
    <ChartCard
      title="Pair a device"
      className="pair-card"
      info="A code works once, for 5 minutes. Phones need to be on the same Wi-Fi as this computer. The Daytrace app scans the first QR code; a phone's camera scans the second, which opens this page on the phone and pairs its browser."
    >
      {paired && (
        <p className="pair-success" role="status">
          <span className="pair-success-title">
            {paired.returning ? `${paired.name} is paired again as ${paired.deviceId}.` : `${paired.name} is paired.`}
          </span>
          {paired.returning && (
            <span> The same device as before, with its history. If that wasn't you, revoke it below.</span>
          )}
        </p>
      )}
      {failure && (
        <p className="form-error" role="alert">
          {failure}
        </p>
      )}
      {issued ? (
        <TokenReveal claimed={issued.claimed} hubUrl={issued.hubUrl} onDone={() => setIssued(null)} />
      ) : !code ? (
        <div className="pair-start">
          <p>Show a code to pair a phone, its browser, iPhone Shortcuts or the browser extension.</p>
          <button type="button" className="button" onClick={start} disabled={busy}>
            {busy ? "Starting..." : paired ? "Pair another device" : "Show a pairing code"}
          </button>
        </div>
      ) : (
        <div className="pair-panel">
          <div className="pair-code">
            <p className="eyebrow">Pairing code</p>
            <p className="code-digits number" aria-label={`Pairing code ${spoken(code.reply.code)}`}>
              {spaced(code.reply.code)}
            </p>
            {superseded ? (
              <p className="form-error" role="status">
                A newer code was started, in another tab or window, so this one no longer works.
              </p>
            ) : active ? (
              <p className="muted">
                <span className="number">{clock(left)}</span> left
              </p>
            ) : (
              <p className="form-error" role="status">
                This code has run out.
              </p>
            )}
            <button type="button" className="button button-ghost" onClick={start} disabled={busy}>
              New code
            </button>
            <div className="addresses">
              <p className="eyebrow">This hub&apos;s addresses</p>
              {reply && reply.urls.length > 0 ? (
                <ul>
                  {reply.urls.map((url) => (
                    <li key={url} className="copy-row">
                      <code>{url}</code>
                      <CopyButton text={url} label="Copy" />
                    </li>
                  ))}
                  {reply.mdns_url && (
                    <li className="copy-row">
                      <code>{reply.mdns_url}</code>
                      <CopyButton text={reply.mdns_url} label="Copy" />
                    </li>
                  )}
                </ul>
              ) : (
                <p className="muted">
                  This computer has no address a phone can reach right now. Connect it to your Wi-Fi or a network cable.
                </p>
              )}
            </div>
          </div>
          {active && (
            <Tabs
              label="Pair with"
              value={kind}
              onChange={setKind}
              className="pair-tabs"
              tabs={[
                {
                  id: "app",
                  label: "Daytrace app",
                  content: (
                    <div className="pair-way">
                      {showQr && <QrCode src={qr("app")} alt="QR code for the Daytrace app" left={left / CODE_LIFETIME_MS} />}
                      <ol className="howto">
                        <li>Open Daytrace on the Android phone.</li>
                        <li>Tap Pair with the hub, then Scan the QR code.</li>
                        <li>Or type the code there, with the address above.</li>
                      </ol>
                    </div>
                  ),
                },
                {
                  id: "browser",
                  label: "Phone browser",
                  content: (
                    <div className="pair-way">
                      {showQr && (
                        <QrCode src={qr("browser")} alt="QR code that opens this page on a phone and pairs its browser" left={left / CODE_LIFETIME_MS} />
                      )}
                      {lanUrl ? (
                        <ol className="howto">
                          <li>Point the phone&apos;s camera at this QR code and open the link.</li>
                          <li>
                            Or open <code>{lanUrl}/devices</code> on the phone and type the code.
                          </li>
                        </ol>
                      ) : (
                        <p className="muted">A phone can pair once this computer has an address it can reach (see the addresses).</p>
                      )}
                    </div>
                  ),
                },
                {
                  id: "token",
                  label: "Token",
                  content: (
                    <div className="pair-way">
                      <p className="muted">
                        For iPhone Shortcuts or the browser extension. You can also get the token on the device that needs
                        it, from this hub&apos;s Devices page there.
                      </p>
                      <ClaimForm purposes={TOKENS_ONLY} code={code.reply.code} hubUrl={lanUrl} onPaired={onChanged} onToken={onToken} />
                    </div>
                  ),
                },
              ]}
            />
          )}
        </div>
      )}
    </ChartCard>
  );
}

// --- the device list -------------------------------------------------------------------------------------------

function ConfirmRevoke({ device, busy, onCancel, onConfirm }: { device: Device | null; busy: boolean; onCancel: () => void; onConfirm: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const title = useId();
  useEffect(() => {
    const element = dialog.current;
    if (!element) return;
    if (device && !element.open) element.showModal();
    if (!device && element.open) element.close();
  }, [device]);
  return (
    <dialog
      ref={dialog}
      className="dialog"
      aria-labelledby={title}
      onCancel={(event) => busy && event.preventDefault()} // Escape can't pretend to stop a revoke already on its way
      onClose={onCancel}
    >
      <h2 id={title}>Revoke {device?.name}?</h2>
      <p>It stops sending, and it has to pair again to come back. Everything it already sent stays.</p>
      <div className="row dialog-actions">
        <button type="button" className="button button-ghost" onClick={onCancel} disabled={busy} autoFocus>
          Keep it
        </button>
        <button type="button" className="button button-danger" onClick={onConfirm} disabled={busy}>
          {busy ? "Revoking..." : "Revoke"}
        </button>
      </div>
    </dialog>
  );
}

function DeviceCard({ device, now, local, onRevoke }: { device: Device; now: number; local: boolean; onRevoke: () => void }) {
  const live = device.last_seen !== null && now - Date.parse(device.last_seen) < LIVE_MS;
  return (
    <motion.li
      layout
      initial={{ opacity: 0, y: 12 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, scale: 0.9, transition: { duration: 0.25 } }}
      className="device-card"
    >
      <span className={`platform platform-${device.device_type}`}>
        <PlatformIcon type={device.device_type} />
      </span>
      <div className="device-main">
        <p className="device-name">
          {device.name}
          {live && <span className="live-dot live" aria-hidden="true" />}
          {live && <span className="visually-hidden">, synced in the last minute</span>}
        </p>
        <p className="muted">
          {DEVICE_TYPE_LABELS[device.device_type] ?? device.device_type} <span aria-hidden="true">/</span> <code>{device.device_id}</code>
        </p>
        <p className="muted">{contact(device, now)}</p>
      </div>
      <dl className="device-stats">
        <div>
          <dt>Last seq</dt>
          <dd className="number">{device.last_seq ?? "none"}</dd>
        </div>
        <div>
          <dt>Last 24 h</dt>
          <dd className="number">{device.events_24h.toLocaleString()}</dd>
        </div>
        <div>
          <dt>All events</dt>
          <dd className="number">{device.event_count.toLocaleString()}</dd>
        </div>
      </dl>
      {local && device.has_token && (
        <button type="button" className="button button-ghost device-revoke" onClick={onRevoke} aria-label={`Revoke ${device.name}`}>
          Revoke
        </button>
      )}
    </motion.li>
  );
}

function DeviceList({ devices, error, local, reload }: { devices: Device[] | undefined; error: ApiError | undefined; local: boolean; reload: () => void }) {
  const [revoking, setRevoking] = useState<Device | null>(null);
  const [busy, setBusy] = useState(false);
  const now = useWallClock(30_000);

  const revoke = async () => {
    if (!revoking) return;
    setBusy(true);
    try {
      await api.delete("/api/v1/devices/{device_id}", { path: { device_id: revoking.device_id } });
      toast(`${revoking.name} is revoked. Its data stays.`, "info");
      setBusy(false);
      setRevoking(null);
      reload();
    } catch (failure) {
      toast(failure instanceof ApiError ? failure.message : String(failure));
      setBusy(false);
    }
  };

  // Not (or no longer) paired: an old list must not stay up as if nothing had happened.
  if (error?.status === 401) {
    return <p className="muted">This browser isn&apos;t paired{devices ? " any more" : ""}. Pair it above to see your devices.</p>;
  }
  if (!devices) return error ? <p className="muted">The devices couldn&apos;t load: {error.message}</p> : null;
  const paired = devices.filter((device) => device.revoked_at === null);
  const revoked = devices.filter((device) => device.revoked_at !== null);
  return (
    <>
      {error && (
        <p className="muted" role="status">
          The list couldn&apos;t be refreshed: {error.message}
        </p>
      )}
      {paired.length === 0 ? (
        <p className="muted">No devices are paired yet. Show a pairing code on the hub computer to add one.</p>
      ) : (
        <ul className="device-grid" aria-label="Paired devices">
          <AnimatePresence initial={false}>
            {paired.map((device) => (
              <DeviceCard key={device.device_id} device={device} now={now} local={local} onRevoke={() => setRevoking(device)} />
            ))}
          </AnimatePresence>
        </ul>
      )}
      {!local && paired.length > 0 && <p className="muted">Devices can be revoked on the hub computer.</p>}
      {revoked.length > 0 && (
        <details className="revoked">
          <summary>
            Revoked devices <span className="badge">{revoked.length}</span>
          </summary>
          <ul aria-label="Revoked devices">
            {revoked.map((device) => (
              <li key={device.device_id}>
                <span>{device.name}</span>
                <span className="muted">
                  <code>{device.device_id}</code>, revoked {ago(device.revoked_at as string, now)}, {device.event_count.toLocaleString()} events kept
                </span>
              </li>
            ))}
          </ul>
        </details>
      )}
      <ConfirmRevoke device={revoking} busy={busy} onCancel={() => busy || setRevoking(null)} onConfirm={revoke} />
    </>
  );
}

// --- help ------------------------------------------------------------------------------------------------------

/** `hubUrl`: the address a phone opens; null on the hub computer, whose own address (localhost) a phone can't use. */
function InstallHelp({ hubUrl }: { hubUrl: string | null }) {
  const address = hubUrl ? <code>{hubUrl}</code> : <>this hub&apos;s address (shown under Pair a device)</>;
  return (
    <ChartCard
      title="On your phone"
      info="Adding the dashboard to the home screen gives it an icon like an app. It needs to be on the hub's Wi-Fi to show your data."
    >
      <div className="grid grid-2 install-help">
        <section>
          <h3>Samsung Internet</h3>
          <ol className="howto">
            <li>Open {address} and pair the browser there.</li>
            <li>Tap the menu (three lines), then Add page to, then Home screen.</li>
          </ol>
        </section>
        <section>
          <h3>iPhone Safari</h3>
          <ol className="howto">
            <li>Open {address} in Safari and pair it there.</li>
            <li>Tap Share, then Add to Home Screen.</li>
          </ol>
        </section>
      </div>
      <p className="muted">
        Opening it away from home, without the hub, comes later with HTTPS on the hub. For the iPhone&apos;s own data,
        set up the{" "}
        <a href={SHORTCUTS_GUIDE} target="_blank" rel="noreferrer">
          iPhone Shortcuts
        </a>{" "}
        (the guide opens on GitHub).
      </p>
    </ChartCard>
  );
}

// --- the page --------------------------------------------------------------------------------------------------

const readLinkCode = () => /(?:^#|&)pair=(\d{6})\b/.exec(window.location.hash)?.[1] ?? "";
const stripLinkCode = () => {
  if (/(?:^#|&)pair=/.test(window.location.hash)) {
    window.history.replaceState(window.history.state, "", window.location.pathname + window.location.search);
  }
};

/** The code from the camera's QR code (`/devices#pair=123456`). Read while rendering (a pure read: React may render
 * more than once before the page shows), taken out of the address only once the page is on screen, and read again
 * when the link is opened again in the same tab (a change of the part after `#` doesn't reload the page). */
function useLinkCode(): string {
  const [code, setCode] = useState(readLinkCode);
  useEffect(() => {
    stripLinkCode();
    const onHash = () => {
      const next = readLinkCode();
      if (next) {
        setCode(next);
        stripLinkCode();
      }
    };
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);
  return code;
}

type PairState = "paired" | "unpaired" | "checking";

export default function Devices() {
  const health = useApi("/api/v1/health", { quiet: true });
  const devices = useApi("/api/v1/devices", { quiet: true });
  const linkCode = useLinkCode();
  const local = health.data?.local === true;
  const { reload } = devices;
  const { reload: reloadHealth } = health;
  const busy = useRef(false);
  busy.current = devices.loading;
  const refresh = useCallback(() => {
    if (!busy.current) reload();
    if (health.error) reloadHealth(); // a hub that was down when the page opened is picked up again
  }, [reload, reloadHealth, health.error]);
  usePolling(REFRESH_MS, refresh);

  // Is this browser paired? With a token, the device list answers: it loads (yes) or says 401 (no: the client drops
  // a refused token). The first answer picks the form's choices, and later ones don't swap them under the person.
  const token = getToken();
  const current: PairState = !token ? "unpaired" : devices.error?.status === 401 ? "unpaired" : devices.data || devices.error ? "paired" : "checking";
  const decided = useRef<PairState>("checking");
  if (decided.current === "checking" && current !== "checking") decided.current = current;
  const pairState = decided.current;

  return (
    <div className="stack">
      <header className="page-head">
        <div>
          <p className="eyebrow">Pair and manage</p>
          <h1>Devices</h1>
        </div>
      </header>

      {!health.data ? (
        <ChartCard title="Pair a device" loading={!health.error}>
          {health.error && (
            <div className="pair-start">
              <p className="muted">The hub can&apos;t be reached: {health.error.message}</p>
              <button type="button" className="button button-ghost" onClick={reloadHealth}>
                Try again
              </button>
            </div>
          )}
        </ChartCard>
      ) : local ? (
        <PairPanel onChanged={reload} />
      ) : embedded ? (
        // DT-58: inside the Android app the phone pairs through the app itself. A claim here would put another token
        // where the app's own dashboard token lives, so the page offers none.
        <ChartCard title="This phone" info="The Daytrace app pairs this phone: More, then This phone.">
          <p className="muted">
            This phone is paired with your hub through the Daytrace app. To pair another device, open Devices on the hub
            computer.
          </p>
        </ChartCard>
      ) : (
        <ChartCard title="Pair this device" loading={pairState === "checking"} info="Pairing needs a code from the hub computer's Devices page. A code works once, for 5 minutes.">
          {pairState === "paired" && (
            <p className="muted">
              This browser is already paired.{" "}
              {linkCode ? "To pair it again with the code from the link, press Pair this browser." : "You can still get a token here for another device."}
            </p>
          )}
          {pairState !== "checking" && (
            <ClaimForm
              key={pairState}
              purposes={pairState === "paired" && !linkCode ? FOR_A_PAIRED_BROWSER : FOR_A_NEW_BROWSER}
              linkCode={linkCode}
              autoSubmit={pairState === "unpaired"}
              hubUrl={window.location.origin}
              onPaired={reload}
            />
          )}
        </ChartCard>
      )}

      <ChartCard title="Your devices" loading={!devices.data && !devices.error} info="Every paired device, when it was last in touch, and what it has sent. A green dot means it synced in the last minute.">
        <DeviceList devices={devices.data?.devices} error={devices.error} local={local} reload={reload} />
      </ChartCard>

      {!embedded && <InstallHelp hubUrl={local ? null : window.location.origin} />}
    </div>
  );
}
