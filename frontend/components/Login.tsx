"use client";

import { FormEvent, useState } from "react";

import { api, Role, Session } from "@/lib/api";

interface LoginResponse {
  token: string;
  role: Role;
  local_name: string;
}

export default function Login({ onLogin }: { onLogin: (s: Session) => void }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      const r = await api<LoginResponse>("/auth/login", { method: "POST", body: { username, password } });
      onLogin({ token: r.token, role: r.role, localName: r.local_name, username });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Login failed.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="login">
      <form className="panel" onSubmit={submit}>
        <h2>CrewLink leadership</h2>
        <label htmlFor="u">Username</label>
        <input id="u" type="text" autoComplete="username" value={username}
               onChange={(e) => setUsername(e.target.value)} required />
        <label htmlFor="p">Password</label>
        <input id="p" type="password" autoComplete="current-password" value={password}
               onChange={(e) => setPassword(e.target.value)} required />
        {error && <div className="notice error" role="alert">{error}</div>}
        <div className="actions">
          <button className="btn primary" disabled={busy}>{busy ? "Signing in…" : "Sign in"}</button>
        </div>
      </form>
    </main>
  );
}
