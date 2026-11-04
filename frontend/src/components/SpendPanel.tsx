"use client";
import { useEffect, useState } from "react";
import Link from "next/link";
import { useToast } from "@/components/Toaster";
import { api, OpenRouterAccount, SpendBucket, SpendEntry, SpendSummary } from "@/lib/api";

const KIND_LABEL: Record<string, string> = {
  video: "Video clips",
  avatar: "Lip-sync",
  chat: "Planner",
  image: "Images",
  tts: "Voice",
  music: "Music",
  training: "Training",
};

const usd = (n: number) =>
  n >= 100 ? `$${n.toFixed(0)}` : n >= 1 ? `$${n.toFixed(2)}` : `$${n.toFixed(n === 0 ? 2 : 3)}`;

function shiftMonth(month: string, delta: number): string {
  const [y, m] = month.split("-").map(Number);
  const d = new Date(Date.UTC(y, m - 1 + delta, 1));
  return `${d.getUTCFullYear()}-${String(d.getUTCMonth() + 1).padStart(2, "0")}`;
}

function monthLabel(month: string): string {
  const [y, m] = month.split("-").map(Number);
  return new Date(Date.UTC(y, m - 1, 1)).toLocaleDateString(undefined, {
    month: "long",
    year: "numeric",
    timeZone: "UTC",
  });
}

function Bars({ rows, label }: { rows: SpendBucket[]; label: (k: string) => string }) {
  const max = Math.max(...rows.map((r) => r.cost_usd), 0.0001);
  if (!rows.length) return <div className="text-xs text-ink-400">Nothing yet.</div>;
  return (
    <ul className="space-y-1.5">
      {rows.map((r) => (
        <li key={r.key} className="text-xs">
          <div className="flex justify-between gap-2 mb-0.5">
            <span className="text-ink-200 truncate" title={r.key}>
              {label(r.key)}
            </span>
            <span className="text-ink-300 tabular-nums shrink-0">
              {usd(r.cost_usd)} <span className="text-ink-400">· {r.count}</span>
            </span>
          </div>
          <div className="h-1.5 rounded-full bg-ink-800 overflow-hidden">
            <div className="h-full rounded-full bg-accent" style={{ width: `${(r.cost_usd / max) * 100}%` }} />
          </div>
        </li>
      ))}
    </ul>
  );
}

export function SpendPanel() {
  const toast = useToast();
  const current = new Date().toISOString().slice(0, 7);
  const [month, setMonth] = useState(current);
  const [summary, setSummary] = useState<SpendSummary | null>(null);
  const [entries, setEntries] = useState<SpendEntry[]>([]);
  const [account, setAccount] = useState<OpenRouterAccount | null>(null);
  const [cap, setCap] = useState("");
  const [hardStop, setHardStop] = useState(true);
  const [pcts, setPcts] = useState("80, 100");
  const [saving, setSaving] = useState(false);

  const load = async (m: string) => {
    try {
      const [s, e] = await Promise.all([api.spendSummary(m), api.spendEntries({ month: m, limit: 15 })]);
      setSummary(s);
      setEntries(e);
      setCap(s.budget.monthly_usd ? String(s.budget.monthly_usd) : "");
      setHardStop(s.budget.hard_stop);
      setPcts(s.budget.alert_pcts.join(", "));
    } catch (err: any) {
      toast.error(err.message || "Couldn't load spend");
    }
  };

  useEffect(() => {
    load(month);
  }, [month]);

  useEffect(() => {
    api.openRouterAccount().then(setAccount).catch(() => setAccount(null));
  }, []);

  const save = async () => {
    const monthly = Number(cap || 0);
    const alertPcts = pcts
      .split(/[\s,]+/)
      .map(Number)
      .filter((n) => Number.isFinite(n) && n > 0);
    if (!Number.isFinite(monthly) || monthly < 0) {
      toast.error("Budget must be a positive number (or empty for none)");
      return;
    }
    setSaving(true);
    try {
      await api.setSpendBudget({ monthly_usd: monthly, hard_stop: hardStop, alert_pcts: alertPcts });
      toast.success(monthly ? `Monthly budget set to ${usd(monthly)}` : "Monthly budget removed");
      await load(month);
    } catch (err: any) {
      toast.error(err.message || "Couldn't save the budget");
    } finally {
      setSaving(false);
    }
  };

  if (!summary) {
    return (
      <section className="card p-4 mb-6">
        <h2 className="text-lg font-medium">Spend</h2>
        <div className="text-sm text-ink-400 mt-2">Loading…</div>
      </section>
    );
  }

  const b = summary.budget;
  const pct = summary.budget_pct;
  const tone = summary.blocked || (pct ?? 0) >= 100 ? "bg-red-500" : (pct ?? 0) >= 80 ? "bg-amber-500" : "bg-emerald-500";

  return (
    <section className="card p-4 mb-6" aria-labelledby="spend-heading">
      <div className="flex flex-wrap items-center justify-between gap-2 mb-3">
        <h2 id="spend-heading" className="text-lg font-medium">
          Spend
        </h2>
        <div className="flex items-center gap-1 text-xs">
          <button className="btn-ghost text-xs px-2 py-1" onClick={() => setMonth(shiftMonth(month, -1))} aria-label="Previous month">
            ←
          </button>
          <span className="text-ink-200 min-w-[8rem] text-center">{monthLabel(month)}</span>
          <button
            className="btn-ghost text-xs px-2 py-1"
            onClick={() => setMonth(shiftMonth(month, 1))}
            disabled={month >= current}
            aria-label="Next month"
          >
            →
          </button>
        </div>
      </div>

      {summary.mock_mode && (
        <p className="text-xs text-ink-400 mb-3">
          Mock providers are on, so nothing is billed or recorded. Real provider calls are recorded here as they happen.
        </p>
      )}
      {summary.blocked && (
        <div role="alert" className="text-sm rounded-md border border-red-500/40 bg-red-500/10 text-red-200 px-3 py-2 mb-3">
          The monthly budget is reached. New paid generations are blocked until you raise it below.
        </div>
      )}

      <div className="flex flex-wrap items-end gap-x-6 gap-y-2 mb-2">
        <div>
          <div className="text-3xl font-semibold tabular-nums">{usd(summary.total_usd)}</div>
          <div className="text-xs text-ink-400">
            {b.monthly_usd > 0 ? `of ${usd(b.monthly_usd)} budget` : "no monthly budget set"}
            {summary.estimated_usd > 0 && ` · ${usd(summary.estimated_usd)} estimated`}
            {summary.pending > 0 && ` · ${summary.pending} awaiting final cost`}
          </div>
        </div>
        {account?.available && month === current && (
          <div className="text-xs text-ink-400">
            OpenRouter reports {usd(account.usage_monthly ?? 0)} this month for this key
            {account.limit_remaining != null && ` · ${usd(account.limit_remaining)} credit limit left`}
          </div>
        )}
      </div>
      {pct != null && (
        <div
          className="h-2 rounded-full bg-ink-800 overflow-hidden mb-4"
          role="progressbar"
          aria-label="Budget used"
          aria-valuenow={Math.round(pct)}
          aria-valuemin={0}
          aria-valuemax={100}
        >
          <div className={`h-full ${tone}`} style={{ width: `${Math.min(pct, 100)}%` }} />
        </div>
      )}

      <div className="grid grid-cols-1 md:grid-cols-3 gap-5 mb-5">
        <div>
          <h3 className="text-xs uppercase tracking-wide text-ink-400 mb-2">By type</h3>
          <Bars rows={summary.by_kind} label={(k) => KIND_LABEL[k] || k} />
        </div>
        <div>
          <h3 className="text-xs uppercase tracking-wide text-ink-400 mb-2">By model</h3>
          <Bars rows={summary.by_model.slice(0, 6)} label={(k) => k || "—"} />
        </div>
        <div>
          <h3 className="text-xs uppercase tracking-wide text-ink-400 mb-2">Top projects</h3>
          {summary.top_projects.length ? (
            <ul className="space-y-1 text-xs">
              {summary.top_projects.slice(0, 6).map((p) => (
                <li key={p.project_id} className="flex justify-between gap-2">
                  <Link href={`/projects/${p.project_id}`} className="text-ink-200 hover:text-ink-100 truncate">
                    {p.title || `Project ${p.project_id}`}
                  </Link>
                  <span className="text-ink-300 tabular-nums">{usd(p.cost_usd)}</span>
                </li>
              ))}
            </ul>
          ) : (
            <div className="text-xs text-ink-400">Nothing yet.</div>
          )}
        </div>
      </div>

      <form
        className="flex flex-wrap items-end gap-3 mb-5"
        onSubmit={(e) => {
          e.preventDefault();
          save();
        }}
      >
        <label className="text-xs text-ink-300">
          Monthly budget (USD)
          <input
            className="input block mt-1 w-32"
            inputMode="decimal"
            placeholder="none"
            value={cap}
            onChange={(e) => setCap(e.target.value)}
          />
        </label>
        <label className="text-xs text-ink-300">
          Alert at (% of budget)
          <input className="input block mt-1 w-32" value={pcts} onChange={(e) => setPcts(e.target.value)} />
        </label>
        <label className="flex items-center gap-2 text-xs text-ink-300 pb-2">
          <input
            type="checkbox"
            className="accent-accent"
            checked={hardStop}
            onChange={(e) => setHardStop(e.target.checked)}
          />
          Block new paid generations at 100%
        </label>
        <button className="btn-primary text-sm" disabled={saving}>
          {saving ? "Saving…" : "Save budget"}
        </button>
      </form>

      <h3 className="text-xs uppercase tracking-wide text-ink-400 mb-2">Recent charges</h3>
      {entries.length ? (
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead className="text-ink-400 text-left">
              <tr>
                <th className="font-normal py-1 pr-3">When</th>
                <th className="font-normal py-1 pr-3">Type</th>
                <th className="font-normal py-1 pr-3">Model</th>
                <th className="font-normal py-1 pr-3">Project</th>
                <th className="font-normal py-1 text-right">Cost</th>
              </tr>
            </thead>
            <tbody>
              {entries.map((e) => (
                <tr key={e.id} className="border-t border-ink-800">
                  <td className="py-1 pr-3 text-ink-300 whitespace-nowrap">
                    {new Date(e.created_at + "Z").toLocaleString([], {
                      month: "short",
                      day: "numeric",
                      hour: "2-digit",
                      minute: "2-digit",
                    })}
                  </td>
                  <td className="py-1 pr-3 text-ink-200">{KIND_LABEL[e.kind] || e.kind}</td>
                  <td className="py-1 pr-3 text-ink-300 max-w-[14rem] truncate" title={e.note || e.model}>
                    {e.model || e.provider}
                  </td>
                  <td className="py-1 pr-3">
                    {e.project_id ? (
                      <Link href={`/projects/${e.project_id}`} className="text-ink-200 hover:text-ink-100">
                        #{e.project_id}
                      </Link>
                    ) : (
                      <span className="text-ink-400">—</span>
                    )}
                  </td>
                  <td className="py-1 text-right tabular-nums text-ink-200 whitespace-nowrap">
                    {usd(e.cost_usd)}
                    {e.is_estimate && (
                      <span
                        className="ml-1 text-ink-400"
                        title={e.source === "pending" ? "Estimate until OpenRouter reports the final cost" : "Rate-card estimate (provider reports no cost)"}
                      >
                        {e.source === "pending" ? "pending" : "est."}
                      </span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <div className="text-xs text-ink-400">No charges this month.</div>
      )}
    </section>
  );
}
