"use client";
import { useEffect, useState } from "react";
import { api } from "@/lib/api";

export function AuthGate({ children }: { children: React.ReactNode }) {
  const [state, setState] = useState<"checking" | "in" | "out">("checking");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [bouncedFromSession, setBouncedFromSession] = useState(false);

  useEffect(() => {
    api.authStatus()
      .then((r) => setState(r.authenticated ? "in" : "out"))
      .catch(() => setState("out"));
    // Catch any 401 raised by api.req mid-session (cookie expired, server
    // restart wiped the SESSION_SECRET, etc.) and bounce to the login form
    // instead of letting the user stare at a parade of error toasts.
    const onUnauth = () => {
      setBouncedFromSession(true);
      setState("out");
    };
    window.addEventListener("avs:unauthenticated", onUnauth);
    return () => window.removeEventListener("avs:unauthenticated", onUnauth);
  }, []);

  if (state === "checking") {
    return <div className="p-6 text-ink-300 text-sm">Checking session…</div>;
  }

  if (state === "out") {
    return (
      // Full-viewport stage that sits over the sidebar so the sign-in is a
      // clean first impression rather than a form beside dead app chrome.
      // Re-declares the ambient washes because the opaque layer would
      // otherwise hide the body-level ones.
      <div
        className="fixed inset-0 z-40 bg-ink-950 flex items-center justify-center p-8"
        style={{
          backgroundImage:
            "radial-gradient(52rem 36rem at 12% -8%, rgba(124,92,255,0.13), transparent 60%)," +
            "radial-gradient(44rem 32rem at 105% 110%, rgba(255,92,138,0.09), transparent 55%)",
        }}
      >
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
          className="w-full max-w-sm card p-8 space-y-5 animate-fade-up shadow-lift"
        >
          <div>
            <div className="text-2xl font-bold tracking-tight text-grad">
              AvatarVideoStudio
            </div>
            <div className="text-xs text-ink-400 mt-1 uppercase tracking-widest">
              Cast → Studio → Video
            </div>
            <div className="text-sm text-ink-300 mt-3">
              {bouncedFromSession
                ? "Your session expired — please sign in again."
                : "Enter the shared MVP password to continue."}
            </div>
          </div>
          <label className="sr-only" htmlFor="avs-password">
            Password
          </label>
          <input
            id="avs-password"
            type="password"
            className="input"
            value={password}
            placeholder="Password"
            onChange={(e) => setPassword(e.target.value)}
            autoFocus
            required
            minLength={1}
          />
          {error && <div className="text-sm text-accent" role="alert">{error}</div>}
          <button type="submit" className="btn-primary w-full" disabled={!password}>
            Sign in
          </button>
        </form>
      </div>
    );
  }

  return <>{children}</>;
}
