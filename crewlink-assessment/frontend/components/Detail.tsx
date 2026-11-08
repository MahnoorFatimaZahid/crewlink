"use client";

import { useCallback, useEffect, useState } from "react";

import LiveCounts from "@/components/LiveCounts";
import StatusBadge from "@/components/StatusBadge";
import { Announcement, api, ApiError, formatDateTime } from "@/lib/api";

const PREVIEW_LIMIT = 120;
const EDITABLE = ["draft", "approved"];

export default function Detail({ id, token, onChanged }: { id: string; token: string; onChanged: () => void }) {
  const [ann, setAnn] = useState<Announcement | null>(null);
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const [preview, setPreview] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const show = useCallback((a: Announcement) => {
    setAnn(a);
    setTitle(a.title);
    setBody(a.body);
    setPreview(a.push_preview);
  }, []);

  const load = useCallback(async () => {
    try {
      show(await api<Announcement>(`/announcements/${id}`, { token }));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Couldn't load this announcement.");
    }
  }, [id, token, show]);

  useEffect(() => {
    load();
  }, [load]);

  /** Run one action; on a 409 (someone else changed it) reload so the leader sees the truth. */
  async function run(action: () => Promise<void>) {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await action();
      onChanged();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Something went wrong.");
      if (e instanceof ApiError && e.status === 409) await load();
    } finally {
      setBusy(false);
    }
  }

  if (!ann) {
    return <div className="panel">{error ? <div className="notice error">{error}</div> : <p className="hint">Loading…</p>}</div>;
  }

  const editable = EDITABLE.includes(ann.status);
  const dirty = title !== ann.title || body !== ann.body || preview !== ann.push_preview;
  const previewOver = preview.length > PREVIEW_LIMIT;

  const save = () =>
    run(async () => {
      show(await api<Announcement>(`/announcements/${ann.id}`, {
        method: "PATCH", token, body: { title, body, push_preview: preview },
      }));
      setNotice("Saved. Editing clears any approval, so review and approve this version.");
    });

  const approve = () =>
    run(async () => {
      // We send the version on screen: if anyone edited it since, the API refuses (409).
      show(await api<Announcement>(`/announcements/${ann.id}/approve`, {
        method: "POST", token, body: { version: ann.version },
      }));
    });

  const send = () => {
    if (!window.confirm(`Send "${ann.title}" to ${ann.audience_classification || "the whole local"} now?`)) return;
    run(async () => {
      await api(`/announcements/${ann.id}/send`, { method: "POST", token });
      await load();
    });
  };

  return (
    <div className="panel">
      <h2>
        {ann.title} <StatusBadge status={ann.status} /> {ann.ai_generated && <span className="badge ai">AI draft</span>}
      </h2>
      <p className="hint">
        Version {ann.version} · {ann.kind === "meeting" ? "Meeting" : "General"} ·{" "}
        {ann.audience_classification || "Whole local"}
        {ann.event_at && <> · Meeting {formatDateTime(ann.event_at)}</>}
        {ann.needs_ack && <> · Acknowledgement requested</>}
        {ann.sent_at && <> · Sent {formatDateTime(ann.sent_at)}</>}
      </p>

      {ann.ai_generated && editable && (
        <div className="notice info">
          Written by AI from your note. Check every fact (dates, times, places) before approving.
        </div>
      )}

      {editable ? (
        <>
          <label htmlFor="d-title">Title</label>
          <input id="d-title" type="text" maxLength={200} value={title} onChange={(e) => setTitle(e.target.value)} />
          <label htmlFor="d-body">Message</label>
          <textarea id="d-body" value={body} onChange={(e) => setBody(e.target.value)} />
          <label htmlFor="d-preview">
            Push preview{" "}
            <span className={`counter ${previewOver ? "over" : ""}`}>{preview.length}/{PREVIEW_LIMIT}</span>
          </label>
          <input id="d-preview" type="text" value={preview} maxLength={PREVIEW_LIMIT}
                 onChange={(e) => setPreview(e.target.value)} />
        </>
      ) : (
        <>
          <h3>Message</h3>
          <div className="readonly">{ann.body}</div>
        </>
      )}

      <h3>Lock screen preview</h3>
      <div className="phone">
        <b>CrewLink</b>
        {editable ? preview : ann.push_preview}
      </div>

      {notice && <div className="notice ok">{notice}</div>}
      {error && <div className="notice error" role="alert">{error}</div>}

      {editable && (
        <div className="actions">
          {dirty ? (
            <>
              <button className="btn primary" onClick={save} disabled={busy || previewOver}>Save changes</button>
              <button className="btn" onClick={() => show(ann)} disabled={busy}>Discard</button>
            </>
          ) : ann.status === "draft" ? (
            <>
              <button className="btn primary" onClick={approve} disabled={busy}>
                Approve this text (version {ann.version})
              </button>
              <span className="hint">You&apos;re approving exactly what&apos;s shown above.</span>
            </>
          ) : (
            <>
              <button className="btn danger" onClick={send} disabled={busy}>Send now</button>
              <span className="hint">Approved. Sending can&apos;t be undone.</span>
            </>
          )}
        </div>
      )}

      {!editable && (
        <LiveCounts
          id={ann.id}
          token={token}
          onStatus={(s) => {
            if (s !== ann.status) {
              load();       // header badge
              onChanged();  // sidebar list badge (queued -> sending -> sent)
            }
          }}
        />
      )}
    </div>
  );
}
