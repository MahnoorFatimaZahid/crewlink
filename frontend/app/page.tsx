"use client";

import { useCallback, useEffect, useState } from "react";

import Composer from "@/components/Composer";
import Detail from "@/components/Detail";
import Login from "@/components/Login";
import StatusBadge from "@/components/StatusBadge";
import {
  Announcement, api, ApiError, formatDateTime, loadSession, saveSession, Session, UNAUTHORIZED_EVENT,
} from "@/lib/api";

export default function Home() {
  const [session, setSession] = useState<Session | null>(null);
  const [ready, setReady] = useState(false);
  const [items, setItems] = useState<Announcement[]>([]);
  const [listError, setListError] = useState("");
  const [selected, setSelected] = useState<string | "new">("new");

  // sessionStorage only exists in the browser, so read it after mount.
  useEffect(() => {
    setSession(loadSession());
    setReady(true);
  }, []);

  const logout = useCallback(() => {
    saveSession(null);
    setSession(null);
    setItems([]);
    setSelected("new");
  }, []);

  // Any 401 anywhere (list, detail, counts, compose) means the token is dead.
  useEffect(() => {
    window.addEventListener(UNAUTHORIZED_EVENT, logout);
    return () => window.removeEventListener(UNAUTHORIZED_EVENT, logout);
  }, [logout]);

  const refresh = useCallback(async () => {
    if (!session || session.role !== "leader") return;
    try {
      setItems(await api<Announcement[]>("/announcements", { token: session.token }));
      setListError("");
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) return logout(); // token no longer valid
      setListError(e instanceof Error ? e.message : "Couldn't load announcements.");
    }
  }, [session, logout]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  if (!ready) return null;

  if (!session) {
    return (
      <Login
        onLogin={(s) => {
          saveSession(s);
          setSession(s);
        }}
      />
    );
  }

  return (
    <>
      <header className="topbar">
        <div className="brand">
          CrewLink<small>{session.localName} · {session.username}</small>
        </div>
        <button className="btn" onClick={logout}>Log out</button>
      </header>

      {session.role !== "leader" ? (
        <main className="login">
          <div className="panel">
            <h2>Leaders only</h2>
            <p>This screen is for local leadership. Members read callouts in the CrewLink app.</p>
          </div>
        </main>
      ) : (
        <main className="shell">
          <aside className="panel">
            <button className="btn primary" style={{ width: "100%" }} onClick={() => setSelected("new")}>
              + New announcement
            </button>
            {listError && <div className="notice error">{listError}</div>}
            <ul className="list">
              {items.map((a) => (
                <li key={a.id}>
                  <button className={selected === a.id ? "active" : ""} onClick={() => setSelected(a.id)}>
                    <span className="t">{a.title}</span>
                    <span className="m">
                      <StatusBadge status={a.status} />
                      {a.ai_generated && <span className="badge ai">AI</span>}
                      {formatDateTime(a.sent_at ?? a.created_at)}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
            {!items.length && !listError && <p className="hint">No announcements yet.</p>}
          </aside>

          <section>
            {selected === "new" ? (
              <Composer
                token={session.token}
                onCreated={(a) => {
                  refresh();
                  setSelected(a.id);
                }}
              />
            ) : (
              <Detail key={selected} id={selected} token={session.token} onChanged={refresh} />
            )}
          </section>
        </main>
      )}
    </>
  );
}
