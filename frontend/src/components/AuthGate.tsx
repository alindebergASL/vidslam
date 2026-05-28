"use client";
import { useEffect, useState } from "react";
import { api } from "@/lib/api";

export function AuthGate({ children }: { children: React.ReactNode }) {
  const [state, setState] = useState<"checking" | "in" | "out">("checking");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.authStatus()
      .then((r) => setState(r.authenticated ? "in" : "out"))
      .catch(() => setState("out"));
  }, []);

  if (state === "checking") {
    return <div className="p-6 text-ink-300 text-sm">Checking session…</div>;
  }

  if (state === "out") {
    return (
      <div className="min-h-screen flex items-center justify-center p-8">
        <form
          onSubmit={async (e) => {
            e.preventDefault();
            setError(null);
            try {
              await api.login(password);
              setState("in");
            } catch (err: any) {
              setError(err.message || "Invalid password");
            }
          }}
          className="w-full max-w-sm card p-6 space-y-4"
        >
          <div>
            <div className="text-xl font-semibold">AvatarVideoStudio</div>
            <div className="text-sm text-ink-300 mt-1">
              Enter the shared MVP password to continue.
            </div>
          </div>
          <input
            type="password"
            className="input"
            value={password}
            placeholder="Password"
            onChange={(e) => setPassword(e.target.value)}
            autoFocus
          />
          {error && <div className="text-sm text-accent">{error}</div>}
          <button type="submit" className="btn-primary w-full">
            Sign in
          </button>
        </form>
      </div>
    );
  }

  return <>{children}</>;
}
