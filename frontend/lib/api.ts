// Thin client for the Django API. Every call goes to /api/* on this same origin;
// next.config.ts proxies it to Django.

export type Role = "leader" | "member";

export interface Session {
  token: string;
  username: string;
  role: Role;
  localName: string;
}

export type Status = "draft" | "approved" | "queued" | "sending" | "sent";

export interface Announcement {
  id: string;
  title: string;
  body: string;
  push_preview: string;
  needs_ack: boolean;
  kind: "general" | "meeting";
  event_at: string | null;
  audience_classification: string;
  status: Status;
  version: number;
  ai_generated: boolean;
  approved_by: number | null;
  approved_at: string | null;
  sent_at: string | null;
  created_at: string;
  ai_provider?: string;
}

export interface Stats {
  status: Status;
  needs_ack: boolean;
  counts: {
    total: number;
    in_progress: number;
    sent: number;
    failed: number;
    skipped: number;
    on_device: number;
    read: number;
    acknowledged: number;
    not_seen: number;
  };
  as_of: string;
}

export interface Audience {
  local: { id: string; name: string };
  active_members: number;
  classifications: { name: string; active_members: number }[];
}

/** Unambiguous everywhere: "8 Nov 2026, 9:42 pm" (never 08/11 vs 11/08). */
export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return "";
  return new Date(iso).toLocaleString("en-GB", {
    day: "numeric", month: "short", year: "numeric", hour: "numeric", minute: "2-digit", hour12: true,
  });
}

/** Fired when the API says our token is no longer valid; the page logs out. */
export const UNAUTHORIZED_EVENT = "crewlink:unauthorized";

export class ApiError extends Error {
  constructor(public status: number, message: string, public code?: string) {
    super(message);
  }
}

/** Turn DRF's error shapes ({detail}, {field: [msgs]}) into one readable line. */
function messageFrom(data: unknown, status: number): string {
  if (data && typeof data === "object") {
    const d = data as Record<string, unknown>;
    if (typeof d.detail === "string") return d.detail;
    const parts = Object.entries(d).map(([field, v]) => {
      const text = Array.isArray(v) ? v.join(" ") : String(v);
      return field === "non_field_errors" ? text : `${field.replace(/_/g, " ")}: ${text}`;
    });
    if (parts.length) return parts.join(" · ");
  }
  return status >= 500 ? "The server had a problem. Please try again." : `Request failed (${status}).`;
}

export async function api<T>(
  path: string,
  opts: { method?: string; body?: unknown; token?: string } = {},
): Promise<T> {
  const headers: Record<string, string> = {};
  if (opts.body !== undefined) headers["Content-Type"] = "application/json";
  if (opts.token) headers["Authorization"] = `Token ${opts.token}`;

  let res: Response;
  try {
    res = await fetch(`/api${path}`, {
      method: opts.method ?? "GET",
      headers,
      body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
      cache: "no-store",
    });
  } catch {
    throw new ApiError(0, "Can't reach the server. Check your connection.");
  }

  const text = await res.text();
  let data: unknown = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = null; // e.g. an HTML error page from a proxy
  }
  if (res.status === 401 && opts.token) {
    // Expired or revoked token, from ANY screen: send the leader back to login.
    window.dispatchEvent(new Event(UNAUTHORIZED_EVENT));
  }
  if (!res.ok) {
    const code = data && typeof data === "object" ? (data as { code?: string }).code : undefined;
    throw new ApiError(res.status, messageFrom(data, res.status), code);
  }
  return data as T;
}

// The token lives in sessionStorage: gone when the tab closes. (An httpOnly
// cookie would be safer against XSS; listed under "What I cut".)
const KEY = "crewlink.session";

export function loadSession(): Session | null {
  try {
    const raw = sessionStorage.getItem(KEY);
    return raw ? (JSON.parse(raw) as Session) : null;
  } catch {
    return null;
  }
}

export function saveSession(s: Session | null) {
  try {
    if (s) sessionStorage.setItem(KEY, JSON.stringify(s));
    else sessionStorage.removeItem(KEY);
  } catch {
    /* storage blocked: the session just won't survive a reload */
  }
}
