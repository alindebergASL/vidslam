"use client";
import { useCallback, useEffect, useId, useRef, useState } from "react";
import { api, ApiKeyStatus, ApiKeyTestResult } from "@/lib/api";
import { adultFeatures, useContentMode } from "@/components/AdultModePanel";

// Settings → API keys. Keys are write-only from the browser: the server
// returns presence, source and the last 4 characters, never the value. The
// typed value lives only in the (uncontrolled) password input and is cleared
// right after a save attempt.

// Model kinds (as the API names them) in the words the Models panel uses.
const KIND_LABEL: Record<string, string> = {
  chat: "planner", image: "image", video: "video", tts: "voice", music: "music", lipsync: "lip-sync",
  bg_removal: "remove background", upscale: "upscale",
};

const SOURCE_CHIP: Record<ApiKeyStatus["source"], { text: string; cls: string }> = {
  app: { text: "Saved in app", cls: "border-emerald-500/40 bg-emerald-500/10 text-emerald-300" },
  env: { text: "From server env", cls: "border-sky-500/40 bg-sky-500/10 text-sky-300" },
  none: { text: "Not set", cls: "border-ink-700 bg-ink-900 text-ink-300" },
  unreadable: { text: "Unreadable — re-enter", cls: "border-red-500/40 bg-red-500/10 text-red-300" },
};

/** Each provider's own content switch, shown on its row. Both only apply
 * while Adult mode is on: off, they're always on. */
const CONTENT_OPTIONS: { key: string; name: string; offText: string; onSaved: string; offSaved: string }[] = [
  {
    key: "check_content",
    name: "content check",
    offText:
      "Off = uncensored output on the open models Runware hosts; vendor models (Google, Kling, Wan…) stay moderated by their vendor.",
    onSaved: "content check is on: its open models are moderated too.",
    offSaved: "content check is off: its open models are uncensored (minors are always refused).",
  },
  {
    key: "safe_filter",
    name: "safe filter",
    offText:
      "Off = no filter on Sogni's own models; vendor models (GPT Image, Seedance, Wan…) stay moderated by their vendor.",
    onSaved: "safe filter is on: its own models are filtered too.",
    offSaved: "safe filter is off: its own models are unfiltered (minors are always refused).",
  },
];

function when(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "" : d.toLocaleString();
}

export function ApiKeysPanel() {
  const [keys, setKeys] = useState<ApiKeyStatus[] | null>(null);
  const [mock, setMock] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const contentMode = useContentMode();
  // Adult content on this deployment (null while loading: no filter switch shown yet).
  const features = adultFeatures(contentMode);

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
        API keys &amp; providers
      </h2>
      <p className="text-xs text-ink-400 mb-1">
        Keys saved here override the server&apos;s <code>.env</code> and are stored encrypted on the
        server. They are never shown again: only the last 4 characters are displayed. Clear an
        app-saved key to fall back to the server environment.
      </p>
      <p className="text-xs text-ink-400 mb-3">
        Saved keys reach the background worker within about 15 seconds. Switch a provider off to hide
        its models from every picker; new work that needs it is refused, and anything already running
        finishes.
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
            <KeyRow
              key={k.name}
              row={k}
              onChange={replace}
              adult={contentMode?.adult ?? null}
              features={features}
            />
          ))}
        </ul>
      )}
    </section>
  );
}

function KeyRow({
  row,
  onChange,
  adult,
  features,
}: {
  row: ApiKeyStatus;
  onChange: (r: ApiKeyStatus) => void;
  /** Adult mode (null: unknown). Off forces the content check and safe filter on. */
  adult: boolean | null;
  /** Adult content on this deployment (null: unknown). Not true: no content check switch. */
  features: boolean | null;
}) {
  const uid = useId();
  const inputId = `${uid}-input`;
  const helpId = `${uid}-help`;
  const statusId = `${uid}-status`;
  const inputRef = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState<null | "save" | "clear" | "test" | "switch" | "option">(null);
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

  const toggle = async () => {
    const next = !(row.enabled ?? true);
    if (!next && !window.confirm(`Turn ${row.label} off? Its models disappear from every picker, and new work that needs it is refused until you turn it back on.`)) return;
    setBusy("switch");
    setMessage(null);
    setTest(null);
    try {
      const updated = await api.setProviderEnabled(row.name, next);
      onChange(updated);
      const uses = (updated.in_use ?? []).map((k) => KIND_LABEL[k] ?? k).join(", ");
      setMessage({
        ok: true,
        text: next
          ? `${row.label} is on.`
          : `${row.label} is off.${uses ? ` Your active ${uses} model${(updated.in_use ?? []).length > 1 ? "s use" : " uses"} it: pick another in Settings → Models.` : ""}`,
      });
      // Pickers elsewhere on the page re-read the catalog.
      window.dispatchEvent(new Event("providers-changed"));
    } catch (err: any) {
      setMessage({ ok: false, text: err.message || "Couldn't switch the provider." });
    } finally {
      setBusy(null);
    }
  };

  const contentOption = row.options ? CONTENT_OPTIONS.find((o) => o.key in row.options!) : undefined;

  const setCheck = async (value: boolean) => {
    if (!contentOption) return;
    setBusy("option");
    setMessage(null);
    try {
      const updated = await api.setProviderOptions(row.name, { [contentOption.key]: value });
      onChange(updated);
      setMessage({
        ok: true,
        text: `${row.label} ${value ? contentOption.onSaved : contentOption.offSaved}`,
      });
    } catch (err: any) {
      setMessage({ ok: false, text: err.message || "Couldn't change the setting." });
    } finally {
      setBusy(null);
    }
  };

  const on = row.enabled ?? true;
  // Providers added later (enabled_default null) turn on by themselves once a key is saved.
  const autoOn = row.enabled_default === null && !row.enabled_explicit && !row.set;
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
          <button
            type="button"
            role="switch"
            aria-checked={on}
            aria-label={`${row.label} provider`}
            className={`relative inline-flex h-5 w-9 shrink-0 items-center rounded-full border transition-colors ${
              on ? "border-emerald-500/60 bg-emerald-500/40" : "border-ink-700 bg-ink-800"
            } disabled:opacity-50`}
            disabled={busy !== null}
            onClick={toggle}
            title={on ? `Turn ${row.label} off` : `Turn ${row.label} on`}
          >
            <span
              aria-hidden="true"
              className={`inline-block h-3.5 w-3.5 rounded-full bg-white transition-transform ${on ? "translate-x-4" : "translate-x-1"}`}
            />
          </button>
          <span className="text-[11px] text-ink-300 w-14">{busy === "switch" ? "…" : on ? "On" : "Off"}</span>
          <span className={`rounded-full border px-2 py-0.5 text-[11px] font-medium ${chip.cls}`}>{chip.text}</span>
          {row.last4 && (
            <span className="text-[11px] text-ink-300" aria-label={`ends in ${row.last4}`}>
              …{row.last4}
            </span>
          )}
        </div>
      </div>
      {features === false && (row.name === "runware" || row.name === "sogni") && (
        <p className="mt-2 text-[11px] text-ink-300">
          {row.label} {row.name === "runware" ? "content check" : "safe filter"}: always on. Prompts involving
          minors are always refused.
        </p>
      )}
      {features === true && row.options && contentOption && (
        <label className="mt-2 flex items-start gap-2 text-[11px] text-ink-300">
          <input
            type="checkbox"
            className="mt-0.5"
            checked={adult === false || !!row.options[contentOption.key]}
            disabled={busy !== null || adult === false}
            onChange={(e) => setCheck(e.target.checked)}
          />
          <span>
            {row.label} {contentOption.name}
            {busy === "option" ? " …" : ""}.{" "}
            {adult === false ? <>Always on while Adult mode is off (Settings → Adult content).</> : contentOption.offText}{" "}
            Prompts involving minors are always refused. It stays on for a prompt that mentions a child, and for
            pictures of Cast characters not ticked &ldquo;Adult content allowed&rdquo;.
          </span>
        </label>
      )}
      {row.warning && (
        <p role="note" className="text-[11px] text-amber-300 mt-1">
          {row.warning}
        </p>
      )}
      <p id={helpId} className="text-[11px] text-ink-400 mt-1">
        Powers {row.powers}.
        {autoOn && <> Turns on when you save a key.</>}
        {!on && !autoOn && <> Off: its models are hidden and new work that needs it is refused.</>}
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
