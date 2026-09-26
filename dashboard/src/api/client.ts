// DT-30: the typed API client, built on schema.d.ts (generated from the hub's /openapi.json by
// openapi-typescript: run `npm run gen:api` while the hub runs on this computer).
//
// - Every path, query, body and reply is typed from the schema: api.get("/api/v1/story", { query: { date } }).
// - GETs are tried up to 3 times when the hub can't be reached or is busy (502, 503, 504; Retry-After is
//   honoured). Other methods are sent once, since sending them twice could do something twice.
// - Errors come back as ApiError with the hub's error code and a message a person can read. A 401 also tells the
//   app shell that this browser isn't paired (UNPAIRED_EVENT); pairing (setToken) tells it again (PAIRED_EVENT).
// - A browser paired as a viewer (DT-32) keeps its token in localStorage and sends it; the dashboard on the hub's
//   own computer (http://localhost:<port>) needs none. A token the hub refuses (revoked) is forgotten and the call
//   tried once without it, so a stale token never locks out the hub's own computer.
// - toast() shows a short message, and useApi() loads data with loading and error states.
import { useCallback, useEffect, useState, useSyncExternalStore } from "react";
import type { paths } from "./schema";

export const API_BASE = "/api/v1";
export const UNPAIRED_EVENT = "daytrace:unpaired";
export const PAIRED_EVENT = "daytrace:paired";
const TOKEN_KEY = "daytrace.token";
const RETRIES = 2;
const RETRY_STATUSES = new Set([502, 503, 504]);

// --- types from the schema -----------------------------------------------------------------------------------

type Method = "get" | "post" | "put" | "delete";
/** The paths that have this method. */
export type PathsWith<M extends Method> = {
  [P in keyof paths]: paths[P][M] extends undefined ? never : P;
}[keyof paths];
type Operation<P extends keyof paths, M extends Method> = NonNullable<paths[P][M]>;
type Json<R> = R extends { content: { "application/json": infer T } } ? T : never;
type Responses<O> = O extends { responses: infer R } ? R : never;
/** What a successful call returns (200 or 201 JSON; nothing for 204). */
export type Reply<O> =
  Responses<O> extends infer R
    ? 200 extends keyof R
      ? Json<R[200]>
      : 201 extends keyof R
        ? Json<R[201]>
        : void
    : never;
type Params<O> = O extends { parameters: infer X } ? X : never;
// A part the schema requires is required here too (a path parameter, a required query or body), so forgetting
// one is a compile error instead of a request to the wrong URL.
type Part<X, K extends string> = X extends { [P in K]: infer V }
  ? { [P in K]: V }
  : X extends { [P in K]?: infer V }
    ? [V] extends [never | undefined]
      ? { [P in K]?: never }
      : { [P in K]?: V }
    : { [P in K]?: never };
type BodyPart<O> = O extends { requestBody: { content: { "application/json": infer B } } }
  ? { body: B }
  : O extends { requestBody?: { content: { "application/json": infer B } } }
    ? { body?: B }
    : { body?: never };
export type Options<O> = Part<Params<O>, "query"> & Part<Params<O>, "path"> & BodyPart<O> & { signal?: AbortSignal };
/** The options argument: optional when nothing in it is required. */
type Args<O, Extra = unknown> = {} extends Options<O> ? [options?: Options<O> & Extra] : [options: Options<O> & Extra];
/** The reply of GET `P`. */
export type GetReply<P extends PathsWith<"get">> = Reply<Operation<P, "get">>;

// --- errors and the token ------------------------------------------------------------------------------------

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;

  constructor(status: number, code: string, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
  }
}

/** The viewer token this browser was paired with (DT-32), if any. Storage can be off (private windows). */
export function getToken(): string | null {
  try {
    return localStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

export function setToken(token: string | null): void {
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token);
    else localStorage.removeItem(TOKEN_KEY);
  } catch {
    // Without storage the browser simply stays unpaired.
  }
  if (token) window.dispatchEvent(new Event(PAIRED_EVENT));
}

async function errorFrom(response: Response): Promise<ApiError> {
  let code = "http_error";
  let message = `The hub answered ${response.status}.`;
  try {
    const body = (await response.json()) as { error?: { code?: string; message?: string } };
    code = body.error?.code ?? code;
    message = body.error?.message ?? message;
  } catch {
    // Not the hub's JSON error: keep the plain message.
  }
  if (response.status === 401) {
    window.dispatchEvent(new Event(UNPAIRED_EVENT));
    message = "This browser isn't paired with the hub yet. Pair it on the Devices page.";
  }
  return new ApiError(response.status, code, message);
}

// --- requests ------------------------------------------------------------------------------------------------

function url(template: string, path?: object, query?: object): string {
  const filled = template.replace(/\{(\w+)\}/g, (_, name: string) =>
    encodeURIComponent(String((path as Record<string, unknown> | undefined)?.[name] ?? "")),
  );
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(query ?? {})) {
    if (value !== undefined && value !== null) search.set(key, String(value));
  }
  const text = search.toString();
  return text ? `${filled}?${text}` : filled;
}

function wait(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(resolve, ms);
    signal?.addEventListener("abort", () => {
      clearTimeout(timer);
      reject(signal.reason);
    });
  });
}

function retryDelay(attempt: number, response?: Response): number {
  const header = Number(response?.headers.get("Retry-After"));
  return Number.isFinite(header) && header > 0 ? Math.min(header, 10) * 1000 : 500 * 2 ** (attempt - 1);
}

type RawOptions = { query?: object; path?: object; body?: unknown; signal?: AbortSignal };

async function send<T>(method: string, template: string, options: RawOptions): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  let token = getToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  let body: string | undefined;
  if (options.body !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(options.body);
  }
  const target = url(template, options.path, options.query);
  const attempts = method === "GET" ? RETRIES + 1 : 1;
  for (let attempt = 1; ; attempt++) {
    let response: Response;
    try {
      response = await fetch(target, { method, headers, body, signal: options.signal, credentials: "same-origin" });
    } catch (error) {
      if (options.signal?.aborted) throw error;
      if (attempt < attempts) {
        await wait(retryDelay(attempt), options.signal);
        continue;
      }
      throw new ApiError(0, "unreachable", "Can't reach the hub. Is it running, and is this device on the same network?");
    }
    if (RETRY_STATUSES.has(response.status) && attempt < attempts) {
      await wait(retryDelay(attempt, response), options.signal);
      continue;
    }
    if (response.status === 401 && token) {
      // Refused before anything was done, so trying again is safe: without the token, the hub's own computer
      // is trusted, and anyone else is told to pair again.
      setToken(null);
      token = null;
      delete headers.Authorization;
      attempt -= 1;
      continue;
    }
    if (!response.ok) throw await errorFrom(response);
    if (response.status === 204) return undefined as T;
    return (await response.json()) as T;
  }
}

export const api = {
  get: <P extends PathsWith<"get">>(path: P, ...[options]: Args<Operation<P, "get">>) =>
    send<Reply<Operation<P, "get">>>("GET", path, (options ?? {}) as RawOptions),
  post: <P extends PathsWith<"post">>(path: P, ...[options]: Args<Operation<P, "post">>) =>
    send<Reply<Operation<P, "post">>>("POST", path, (options ?? {}) as RawOptions),
  put: <P extends PathsWith<"put">>(path: P, ...[options]: Args<Operation<P, "put">>) =>
    send<Reply<Operation<P, "put">>>("PUT", path, (options ?? {}) as RawOptions),
  delete: <P extends PathsWith<"delete">>(path: P, ...[options]: Args<Operation<P, "delete">>) =>
    send<Reply<Operation<P, "delete">>>("DELETE", path, (options ?? {}) as RawOptions),
};

// --- toasts --------------------------------------------------------------------------------------------------

export type Toast = { id: number; message: string; kind: "error" | "info" };
let toasts: Toast[] = [];
let nextToast = 1;
const toastListeners = new Set<() => void>();

function setToasts(list: Toast[]): void {
  toasts = list;
  toastListeners.forEach((listener) => listener());
}

/** A short message in the corner for 6 seconds. The same message twice at once shows once. */
export function toast(message: string, kind: Toast["kind"] = "error"): void {
  if (toasts.some((item) => item.message === message)) return;
  const item = { id: nextToast++, message, kind };
  setToasts([...toasts, item].slice(-3));
  setTimeout(() => dismissToast(item.id), 6000);
}

export function dismissToast(id: number): void {
  setToasts(toasts.filter((item) => item.id !== id));
}

export function useToasts(): Toast[] {
  return useSyncExternalStore(
    (listener) => {
      toastListeners.add(listener);
      return () => toastListeners.delete(listener);
    },
    () => toasts,
  );
}

// --- loading data --------------------------------------------------------------------------------------------

export type Loaded<T> = {
  data: T | undefined;
  error: ApiError | undefined;
  loading: boolean;
  reload: () => void;
};

/** GET `path` when the component shows (and again when the options change or reload() is called). While it
 * reloads, the last data stays. A failure shows a toast unless `quiet`. */
export function useApi<P extends PathsWith<"get">>(
  path: P,
  ...[given]: Args<Operation<P, "get">, { enabled?: boolean; quiet?: boolean }>
): Loaded<GetReply<P>> {
  const options = (given ?? {}) as RawOptions & { enabled?: boolean; quiet?: boolean };
  const { enabled = true, quiet = false } = options;
  const key = JSON.stringify([path, options.query ?? null, options.path ?? null]);
  const [state, setState] = useState<Omit<Loaded<GetReply<P>>, "reload">>({
    data: undefined,
    error: undefined,
    loading: enabled,
  });
  const [attempt, setAttempt] = useState(0);
  const reload = useCallback(() => setAttempt((count) => count + 1), []);
  useEffect(() => {
    if (!enabled) {
      setState((previous) => (previous.loading ? { ...previous, loading: false } : previous));
      return;
    }
    const controller = new AbortController();
    const [target, query, pathParams] = JSON.parse(key) as [P, object | null, object | null];
    setState((previous) => ({ ...previous, loading: true }));
    const request: RawOptions = { query: query ?? undefined, path: pathParams ?? undefined, signal: controller.signal };
    send<GetReply<P>>("GET", target, request)
      .then((data) => setState({ data, error: undefined, loading: false }))
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        const failure = error instanceof ApiError ? error : new ApiError(0, "unexpected", String(error));
        if (!quiet) toast(failure.message);
        setState((previous) => ({ data: previous.data, error: failure, loading: false }));
      });
    return () => controller.abort();
  }, [key, attempt, enabled, quiet]);
  return { ...state, reload };
}
