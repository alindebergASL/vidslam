"use client";
import { useCallback, useEffect, useId, useRef, useState } from "react";
import { api, ApiKeyStatus, ApiKeyTestResult } from "@/lib/api";

// Settings → API keys. Keys are write-only from the browser: the server
// returns presence, source and the last 4 characters, never the value. The
// typed value lives only in the (uncontrolled) password input and is cleared
// right after a save attempt.

const SOURCE_CHIP: Record<ApiKeyStatus["source"], { text: string; cls: string }> = {
  app: { text: "Saved in app", cls: "border-emerald-500/40 bg-emerald-500/10 text-emerald-300" },
  env: { text: "From server env", cls: "border-sky-500/40 bg-sky-500/10 text-sky-300" },
  none: { text: "Not set", cls: "border-ink-700 bg-ink-900 text-ink-300" },
  unreadable: { text: "Unreadable — re-enter", cls: "border-red-500/40 bg-red-500/10 text-red-300" },
};

function when(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "" : d.toLocaleString();
}

export function ApiKeysPanel() {
  const [keys, setKeys] = useState<ApiKeyStatus[] | null>(null);
  const [mock, setMock] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const r = await api.listApiKeys();
      setKeys(r.keys);
      setMock(r.mock_providers);
      setError(null);
    } catch (e: any) {
      setError(e.message || "Couldn't load API keys");
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const replace = (row: ApiKeyStatus) =>
    setKeys((ks) => (ks ? ks.map((k) => (k.name === row.name ? row : k)) : ks));

  return (
    <section className="card p-4 mb-6" aria-labelledby="api-keys-heading">
      <h2 id="api-keys-heading" className="text-lg font-medium mb-2">
        API keys
      </h2>
      <p className="text-xs text-ink-400 mb-1">
        Keys saved here override the server&apos;s <code>.env</code> and are stored encrypted on the
        server. They are never shown again: only the last 4 characters are displayed. Clear an
        app-saved key to fall back to the server environment.
      </p>
      <p className="text-xs text-ink-400 mb-3">
        Saved keys reach the background worker within about 15 seconds.
      </p>
      {mock && (
        <p className="text-xs text-amber-200 border border-amber-500/30 bg-amber-500/5 rounded-md px-3 py-2 mb-3">
          Mock mode is on (<code>MOCK_PROVIDERS</code>): every provider is mocked and no key is used
          until mock mode is turned off on the server. You can still save keys now.
        </p>
      )}
      {error && (
        <p role="alert" className="text-xs text-accent mb-3">
          {error}
        </p>
      )}
      {!keys && !error && (
        <div className="space-y-2" aria-busy="true">
          {Array.from({ length: 3 }).map((_, i) => (
            <div key={i} className="h-16 skeleton" />
          ))}
        </div>
      )}
      {keys && (
        <ul className="space-y-3">
          {keys.map((k) => (
            <KeyRow key={k.name} row={k} onChange={replace} />
          ))}
        </ul>
      )}
    </section>
  );
}

function KeyRow({ row, onChange }: { row: ApiKeyStatus; onChange: (r: ApiKeyStatus) => void }) {
  const uid = useId();
  const inputId = `${uid}-input`;
  const helpId = `${uid}-help`;
  const statusId = `${uid}-status`;
  const inputRef = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState<null | "save" | "clear" | "test">(null);
  const [hasInput, setHasInput] = useState(false);
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null);
  const [test, setTest] = useState<ApiKeyTestResult | null>(null);
  const chip = SOURCE_CHIP[row.source] ?? SOURCE_CHIP.none;

  const clearInput = () => {
    if (inputRef.current) inputRef.current.value = "";
    setHasInput(false);
  };

  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    const value = (inputRef.current?.value ?? "").trim();
    if (!value) {
      setMessage({ ok: false, text: "Paste a key first." });
      return;
    }
    setBusy("save");
    setMessage(null);
    setTest(null);
    try {
      const updated = await api.setApiKey(row.name, value);
      onChange(updated);
      setMessage({ ok: true, text: `${row.label} key saved.` });
    } catch (err: any) {
      // Safety net: never echo the submitted key back on screen (or to a
      // screen reader via the aria-live status), even if an error detail
      // (e.g. a stringified pydantic 422 with `input`) happens to contain it.
      const raw = String(err?.message || "");
      const text = raw ? (raw.includes(value) ? raw.split(value).join("[redacted]") : raw) : "Couldn't save the key.";
      setMessage({ ok: false, text });
    } finally {
      // Never keep the value around longer than needed, success or not.
      clearInput();
      setBusy(null);
    }
  };

  const clear = async () => {
    const fallback = row.env_var ? ` The server's ${row.env_var} from .env will be used if it is set.` : "";
    const question =
      row.source === "unreadable"
        ? `Remove the unreadable ${row.label} key stored in the app?${fallback}`
        : `Remove the ${row.label} key saved in the app?${fallback}`;
    if (!window.confirm(question)) return;
    setBusy("clear");
    setMessage(null);
    setTest(null);
    try {
      const updated = await api.clearApiKey(row.name);
      onChange(updated);
      setMessage({ ok: true, text: `${row.label} key cleared.` });
    } catch (err: any) {
      setMessage({ ok: false, text: err.message || "Couldn't clear the key." });
    } finally {
      setBusy(null);
    }
  };

  const runTest = async () => {
    setBusy("test");
    setMessage(null);
    setTest(null);
    try {
      setTest(await api.testApiKey(row.name));
    } catch (err: any) {
      setTest({ ok: false, detail: err.message || "Test failed." });
    } finally {
      setBusy(null);
    }
  };

  const updated = row.source === "app" ? when(row.updated_at) : "";

  return (
    <li
      className={`rounded-md border px-3 py-3 ${
        row.source === "unreadable" ? "border-red-500/40 bg-red-500/5" : "border-ink-800 bg-ink-900"
      }`}
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="min-w-0">
          <div className="text-sm font-medium">{row.label}</div>
          <code className="text-[11px] text-ink-400">{row.env_var}</code>
        </div>
        <div className="flex items-center gap-2">
          <span className={`rounded-full border px-2 py-0.5 text-[11px] font-medium ${chip.cls}`}>{chip.text}</span>
          {row.last4 && (
            <span className="text-[11px] text-ink-300" aria-label={`ends in ${row.last4}`}>
              …{row.last4}
            </span>
          )}
        </div>
      </div>
      {row.warning && (
        <p role="note" className="text-[11px] text-amber-300 mt-1">
          {row.warning}
        </p>
      )}
      <p id={helpId} className="text-[11px] text-ink-400 mt-1">
        Powers {row.powers}.
        {updated && <> Saved {updated}.</>}
        {row.docs_url && (
          <>
            {" "}
            <a href={row.docs_url} target="_blank" rel="noopener noreferrer" className="underline hover:text-ink-200">
              Get a {row.label} key
              <span className="sr-only"> (opens in a new tab)</span>
            </a>
          </>
        )}
      </p>

      <form className="mt-2 flex flex-wrap items-end gap-2" onSubmit={save} autoComplete="off">
        <div className="flex-1 min-w-[12rem]">
          <label htmlFor={inputId} className="label text-xs">
            {row.set ? `Replace ${row.label} key` : `${row.label} key`}
          </label>
          <input
            id={inputId}
            ref={inputRef}
            type="password"
            name={`apikey-${row.name}`}
            className="input text-sm"
            autoComplete="off"
            autoCorrect="off"
            autoCapitalize="off"
            spellCheck={false}
            data-1p-ignore
            data-lpignore="true"
            defaultValue=""
            placeholder={row.set ? "Paste a new key to replace it" : "Paste key"}
            aria-describedby={`${helpId} ${statusId}`}
            disabled={busy !== null}
            onChange={(e) => setHasInput(e.target.value.trim().length > 0)}
          />
        </div>
        <button type="submit" className="btn-primary text-xs" disabled={busy !== null || !hasInput}>
          {busy === "save" ? "Saving…" : "Save"}
        </button>
        {(row.source === "app" || row.source === "unreadable") && (
          <button type="button" className="btn-ghost text-xs" disabled={busy !== null} onClick={clear}>
            {busy === "clear" ? "Clearing…" : "Clear"}
          </button>
        )}
        <button
          type="button"
          className="btn-ghost text-xs"
          disabled={busy !== null || !row.set}
          onClick={runTest}
          title={row.set ? undefined : "Set a key first"}
        >
          {busy === "test" ? "Testing…" : "Test"}
        </button>
      </form>

      <div id={statusId} aria-live="polite" role="status" className="text-[11px] mt-1.5 min-h-[1em]">
        {message && <span className={message.ok ? "text-emerald-300" : "text-red-300"}>{message.text}</span>}
        {test && (
          <span className={test.ok ? "text-emerald-300" : "text-red-300"}>
            {test.ok ? "✓ Key works" : "✕ Test failed"}
            {test.detail ? `: ${test.detail}` : ""}
          </span>
        )}
      </div>
    </li>
  );
}
