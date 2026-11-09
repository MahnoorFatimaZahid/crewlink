"use client";

import { useEffect, useRef, useState } from "react";

import { api, formatDateTime, Stats, Status } from "@/lib/api";

// The API caches stats for 2 s, so any number of viewers is cheap.
const POLL_SENDING_MS = 3000;  // while pushes are going out
const POLL_SENT_MS = 10000;    // after: only reads/acks still change, so slow down

export default function LiveCounts({
  id,
  token,
  onStatus,
}: {
  id: string;
  token: string;
  onStatus: (s: Status) => void;
}) {
  const [stats, setStats] = useState<Stats | null>(null);
  const [error, setError] = useState("");
  const onStatusRef = useRef(onStatus);
  onStatusRef.current = onStatus;

  useEffect(() => {
    let stopped = false;
    let timer: ReturnType<typeof setTimeout>;

    async function tick() {
      let delay = POLL_SENDING_MS;
      try {
        const s = await api<Stats>(`/announcements/${id}/stats`, { token });
        if (stopped) return;
        setStats(s);
        setError("");
        onStatusRef.current(s.status);
        if (s.status === "sent") delay = POLL_SENT_MS;
      } catch (e) {
        if (!stopped) setError(e instanceof Error ? e.message : "Couldn't load counts.");
      }
      // Keep polling after "sent" too (more slowly): reads and acknowledgements keep arriving.
      if (!stopped) timer = setTimeout(tick, delay);
    }
    tick();
    return () => {
      stopped = true;
      clearTimeout(timer);
    };
  }, [id, token]);

  if (!stats) return error ? <div className="notice error">{error}</div> : <p className="hint">Loading counts…</p>;

  const c = stats.counts;
  const total = c.total;
  const rows: { label: string; n: number; tone?: string; show?: boolean }[] = [
    { label: "Sent to phones", n: c.sent, tone: "ok" },
    { label: "Arrived on device", n: c.on_device },
    { label: "Read", n: c.read },
    { label: "Acknowledged", n: c.acknowledged, show: stats.needs_ack },
    { label: "Still sending", n: c.in_progress, tone: "warn", show: c.in_progress > 0 },
    { label: "Failed after retries", n: c.failed, tone: "bad", show: c.failed > 0 },
    { label: "Skipped (meeting over or member suspended)", n: c.skipped, tone: "warn", show: c.skipped > 0 },
  ];

  return (
    <>
      <h3>Delivery</h3>
      {stats.status === "queued" && total === 0 ? (
        <p className="hint">Queued. Building the recipient list…</p>
      ) : (
        <p className="hint">
          {total.toLocaleString()} members in the audience · {c.not_seen.toLocaleString()} haven&apos;t read it yet
        </p>
      )}
      <div className="stats" aria-live="polite">
        {rows.filter((r) => r.show !== false).map((r) => {
          const pct = total ? Math.round((r.n / total) * 100) : 0;
          return (
            <div className="stat" key={r.label}>
              <div className="top">
                <span>{r.label}</span>
                <span>{r.n.toLocaleString()} · {pct}%</span>
              </div>
              <div className="bar"><i className={r.tone} style={{ width: `${pct}%` }} /></div>
            </div>
          );
        })}
      </div>
      <p className="asof">
        Updated {formatDateTime(stats.as_of)}. &quot;Sent&quot; means the push service accepted it;
        &quot;arrived&quot; means the member&apos;s app confirmed it.
      </p>
      {error && <div className="notice error">{error}</div>}
    </>
  );
}
