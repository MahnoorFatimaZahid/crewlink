"use client";

import { FormEvent, useEffect, useState } from "react";

import { Announcement, api, ApiError, Audience } from "@/lib/api";

const PREVIEW_LIMIT = 120;
const NOTE_LIMIT = 2000;

type Mode = "manual" | "ai";

export default function Composer({
  token,
  onCreated,
}: {
  token: string;
  onCreated: (a: Announcement) => void;
}) {
  const [mode, setMode] = useState<Mode>("manual");
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const [preview, setPreview] = useState("");
  const [note, setNote] = useState("");

  const [kind, setKind] = useState<"general" | "meeting">("general");
  const [eventAt, setEventAt] = useState(""); // datetime-local value, in the leader's time zone
  const [needsAck, setNeedsAck] = useState(false);
  const [audience, setAudience] = useState("");
  const [aud, setAud] = useState<Audience | null>(null);

  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [info, setInfo] = useState("");

  useEffect(() => {
    api<Audience>("/classifications", { token })
      .then(setAud)
      .catch(() => setAud(null)); // dropdown falls back to "Whole local"
  }, [token]);

  const reach = !aud
    ? null
    : audience
      ? aud.classifications.find((c) => c.name === audience)?.active_members ?? 0
      : aud.active_members;

  // The leader's own choices: sent the same way for both modes.
  function options() {
    return {
      kind,
      event_at: kind === "meeting" && eventAt ? new Date(eventAt).toISOString() : null,
      needs_ack: needsAck,
      audience_classification: audience,
    };
  }

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    setInfo("");
    try {
      const created =
        mode === "ai"
          ? await api<Announcement>("/announcements/draft-from-note", {
              method: "POST", token, body: { note, ...options() },
            })
          : await api<Announcement>("/announcements", {
              method: "POST", token, body: { title, body, push_preview: preview, ...options() },
            });
      onCreated(created);
    } catch (err) {
      if (err instanceof ApiError && err.status === 503) {
        // AI is down: nothing was saved. Move the note into the manual form so no work is lost.
        setMode("manual");
        if (!body) setBody(note);
        setInfo("The AI helper is unavailable right now. Your note is in the body below. Please finish it by hand.");
      } else if (err instanceof ApiError && err.status === 429) {
        setError("Too many AI drafts in a minute. Wait a moment, or write it by hand.");
      } else {
        setError(err instanceof Error ? err.message : "Couldn't save.");
      }
    } finally {
      setBusy(false);
    }
  }

  const previewOver = preview.length > PREVIEW_LIMIT;

  return (
    <form className="panel" onSubmit={submit}>
      <h2>New announcement</h2>

      <div className="tabs" role="group" aria-label="How to write it">
        <button type="button" className="btn" aria-pressed={mode === "manual"} onClick={() => setMode("manual")}>
          Write it
        </button>
        <button type="button" className="btn" aria-pressed={mode === "ai"} onClick={() => setMode("ai")}>
          Draft with AI
        </button>
      </div>

      {mode === "ai" ? (
        <>
          <label htmlFor="note">
            Your note <span className="counter">{note.length}/{NOTE_LIMIT}</span>
          </label>
          <textarea id="note" value={note} maxLength={NOTE_LIMIT} required
                    placeholder="EMERGENCY mtg thurs 6pm at the hall re: westside contractor, everyone pls come"
                    onChange={(e) => setNote(e.target.value)} />
          <p className="hint">
            Only this note is sent to the AI. You&apos;ll review and approve the result before anything goes out.
          </p>
        </>
      ) : (
        <>
          <label htmlFor="title">Title</label>
          <input id="title" type="text" maxLength={200} value={title} required onChange={(e) => setTitle(e.target.value)} />
          <label htmlFor="body">Message</label>
          <textarea id="body" value={body} required onChange={(e) => setBody(e.target.value)} />
          <label htmlFor="preview">
            Push preview{" "}
            <span className={`counter ${previewOver ? "over" : ""}`}>{preview.length}/{PREVIEW_LIMIT}</span>
          </label>
          <input id="preview" type="text" value={preview} maxLength={PREVIEW_LIMIT} required
                 onChange={(e) => setPreview(e.target.value)} />
          <p className="hint">What members see on their lock screen.</p>
        </>
      )}

      <div className="row">
        <div>
          <label htmlFor="local">Local</label>
          {/* A leader belongs to exactly one local; the server takes it from their account,
              never from this form. Shown so it's always clear where a callout goes. */}
          <select id="local" disabled value="mine">
            <option value="mine">{aud?.local.name ?? "Your local"}</option>
          </select>
        </div>
        <div>
          <label htmlFor="audience">Audience</label>
          <select id="audience" value={audience} onChange={(e) => setAudience(e.target.value)}>
            <option value="">Whole local (all active members)</option>
            {aud?.classifications.map((c) => (
              <option key={c.name} value={c.name}>
                {c.name} ({c.active_members.toLocaleString()})
              </option>
            ))}
          </select>
        </div>
      </div>

      {aud && (
        <div className="notice info">
          Sending to: <b>{aud.local.name}</b>
          {audience ? <> · {audience}</> : null} · <b>{(reach ?? 0).toLocaleString()}</b> active members
        </div>
      )}

      <div className="row">
        <div>
          <label htmlFor="kind">Type</label>
          <select id="kind" value={kind} onChange={(e) => setKind(e.target.value as "general" | "meeting")}>
            <option value="general">General</option>
            <option value="meeting">Meeting</option>
          </select>
        </div>
        {kind === "meeting" && (
          <div>
            <label htmlFor="event">Meeting time</label>
            <input id="event" type="datetime-local" value={eventAt} required onChange={(e) => setEventAt(e.target.value)} />
            <p className="hint">Pushes stop once the meeting has started.</p>
          </div>
        )}
      </div>

      <label className="check">
        <input type="checkbox" checked={needsAck} onChange={(e) => setNeedsAck(e.target.checked)} />
        Ask members to acknowledge
      </label>

      {info && <div className="notice info">{info}</div>}
      {error && <div className="notice error" role="alert">{error}</div>}

      <div className="actions">
        <button className="btn primary" disabled={busy || (mode === "manual" && previewOver)}>
          {busy ? (mode === "ai" ? "Drafting…" : "Saving…") : mode === "ai" ? "Draft with AI" : "Save draft"}
        </button>
        <span className="hint">Saved as a draft. Nothing is sent until you approve and press Send.</span>
      </div>
    </form>
  );
}
