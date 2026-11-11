"use client";
import { useEffect, useState } from "react";
import clsx from "clsx";
import { Dialog } from "@/components/Dialog";
import { api, errorText, ImageOp, ImageOpEstimate, ImageOpModel, ImageOpSource, StudioJob } from "@/lib/api";

// Picture operations (remove background, upscale): prices in plain words,
// the models offered, and the Upscale dialog. Shared by Studio and Cast.

/** "$0.04", "$0.0006": enough digits to show a small price. */
export function usd(x: number): string {
  if (x <= 0) return "$0";
  if (x >= 0.1) return `$${x.toFixed(2)}`;
  for (const d of [2, 3, 4]) {
    const r = Number(x.toFixed(d));
    if (r > 0 && Math.abs(r - x) < x * 0.1) return `$${x.toFixed(d)}`;
  }
  return `$${x.toFixed(4)}`;
}

/** The first dollar amount in a price text ("about $0.04 per image" → 0.04); null = unknown. */
export function priceFromText(text?: string | null): number | null {
  const m = /\$\s*([0-9]+(?:\.[0-9]+)?)/.exec(text || "");
  return m ? Number(m[1]) : null;
}

/** The price for upscaling ×`factor` from an upscaler's price text ("about
 *  $0.0006 for ×2, $0.0013 for ×4" → the ×4 one); a text without per-factor
 *  prices gives its only price; null when that factor isn't listed. */
export function priceForFactor(text: string | null | undefined, factor: number): number | null {
  const pairs = [...(text || "").matchAll(/\$\s*([0-9]+(?:\.[0-9]+)?)\s+for\s+×\s*(\d+)/g)];
  if (pairs.length === 0) return priceFromText(text);
  const hit = pairs.find((m) => Number(m[2]) === factor);
  return hit ? Number(hit[1]) : null;
}

/** A picker entry without a price text that rounds to $0 (a tiny or unknown
 *  rate isn't "free"); "free" stays. */
export function withKnownPrice<T extends { price_text?: string }>(m: T): T {
  const p = priceFromText(m.price_text);
  return p === 0 && !/free/i.test(m.price_text || "") ? { ...m, price_text: "" } : m;
}

/** "about $0.0006", "free", or "" when the price isn't known. */
export function opPriceShort(m?: ImageOpModel | null): string {
  if (!m) return "";
  if (m.free) return "free";
  const p = priceFromText(m.price_text);
  return p !== null ? `about ${usd(p)}` : "";
}

/** The models offered for both operations, loaded once per page, and the
 *  rough price of removing a background with the active model (from the
 *  estimate, which knows it even when the models list doesn't). */
export type OpModels = Record<ImageOp, ImageOpModel[]> & {
  removeBgPrice?: string;
  /** The lists have been asked for (an empty list after this = they couldn't be loaded). */
  loaded?: boolean;
};

export function useOpModels(): OpModels {
  const [models, setModels] = useState<OpModels>({ remove_background: [], upscale: [] });
  useEffect(() => {
    let alive = true;
    Promise.all([
      api.imageOpModels("remove_background").catch(() => [] as ImageOpModel[]),
      api.imageOpModels("upscale").catch(() => [] as ImageOpModel[]),
      api.imageOpEstimate("remove_background").catch(() => null),
    ]).then(([rb, up, est]) => {
      if (!alive) return;
      const removeBgPrice = est ? (est.free ? "free" : est.cost_usd > 0 ? `about ${usd(est.cost_usd)}` : "") : undefined;
      setModels({ remove_background: rb, upscale: up, removeBgPrice, loaded: true });
    });
    return () => {
      alive = false;
    };
  }, []);
  return models;
}

export function activeOpModel(list: ImageOpModel[]): ImageOpModel | null {
  return list.find((m) => m.active) || list[0] || null;
}

/** The model a dialog starts on: the active one when it can run now, else
 *  the first one that can (e.g. the free upscale), else the active one. */
export function defaultOpModel(list: ImageOpModel[]): ImageOpModel | null {
  const active = activeOpModel(list);
  if (active?.usable) return active;
  return list.find((m) => m.usable) || active;
}

/** "Remove background · about $0.0006" for a menu or button (no price when
 *  the active model can't run now: the run would be refused). */
export function removeBgLabel(models: OpModels): string {
  const active = activeOpModel(models.remove_background);
  if (active && !active.usable) return "Remove background";
  const price = models.removeBgPrice || opPriceShort(active);
  return price ? `Remove background · ${price}` : "Remove background";
}

export const OP_TITLES: Record<ImageOp, string> = {
  remove_background: "Remove background",
  upscale: "Upscale",
};

/** The source an operation job ran on, as a short chip text ("from #12", "from Cast #34"). */
export function sourceChipText(job: Pick<StudioJob, "source_job_id" | "source_asset_id">): string | null {
  if (job.source_job_id) return `from #${job.source_job_id}`;
  if (job.source_asset_id) return `from Cast #${job.source_asset_id}`;
  return null;
}

/**
 * The Upscale dialog: ×2 or ×4, a model (the active upscale model first, a
 * free basic option), and the price and output size before it runs. Too
 * large an output can't be started.
 */
export function UpscaleDialog({
  source,
  sourceLabel,
  models,
  modelsLoaded = false,
  onClose,
  onStarted,
}: {
  source: ImageOpSource;
  sourceLabel: string;
  models: ImageOpModel[];
  /** The models list was asked for: empty after this means it couldn't be loaded. */
  modelsLoaded?: boolean;
  onClose: () => void;
  onStarted: (job: StudioJob) => void;
}) {
  const [factor, setFactor] = useState(2);
  const [model, setModel] = useState(() => defaultOpModel(models)?.id || "");
  const [est, setEst] = useState<ImageOpEstimate | null>(null);
  const [estError, setEstError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const picked = models.find((m) => m.id === model) || null;
  const factors = picked?.factors?.length ? picked.factors : [2, 4];

  // The models list may arrive after the dialog opened.
  useEffect(() => {
    if (!model && models.length) setModel(defaultOpModel(models)?.id || "");
  }, [models, model]);
  // A model that doesn't take this factor: use one it does.
  useEffect(() => {
    if (!factors.includes(factor)) setFactor(factors[0]);
  }, [model]); // eslint-disable-line react-hooks/exhaustive-deps

  // A new choice: the old estimate (price, size, too large) no longer
  // applies, so it goes and Run waits for the one that matches.
  useEffect(() => {
    let alive = true;
    setEst(null);
    setEstError(null);
    const t = setTimeout(() => {
      api
        .imageOpEstimate("upscale", { model: model || undefined, factor, source })
        .then((e) => alive && setEst(e))
        .catch((e) => {
          if (!alive) return;
          setEst(null);
          setEstError(errorText(e, "Couldn't work out the price"));
        });
    }, 150);
    return () => {
      alive = false;
      clearTimeout(t);
    };
  }, [model, factor, "jobId" in source ? source.jobId : -source.assetId]); // eslint-disable-line react-hooks/exhaustive-deps

  // No list (it couldn't be loaded): the server's default model runs.
  const noList = modelsLoaded && models.length === 0;
  const pickedRuns = noList || Boolean(picked?.usable);
  const start = async () => {
    setBusy(true);
    setError(null);
    try {
      const job = await api.runImageOp("upscale", source, { model: model || undefined, factor });
      onStarted(job);
    } catch (e) {
      setError(errorText(e, "Couldn't start the upscale"));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog title="Upscale" onClose={onClose}>
      <p className="text-xs text-ink-300">Makes a larger copy of {sourceLabel}. The original stays.</p>
      <fieldset>
        <legend className="label">Size</legend>
        <div className="flex gap-2" role="radiogroup" aria-label="Upscale factor">
          {[2, 4].map((f) => (
            <button
              key={f}
              type="button"
              role="radio"
              aria-checked={factor === f}
              disabled={!factors.includes(f)}
              onClick={() => setFactor(f)}
              className={clsx("chip text-sm px-4 disabled:opacity-40", factor === f && "chip-active")}
              title={factors.includes(f) ? `${f} times wider and taller` : "This model doesn't do this size"}
            >
              ×{f}
            </button>
          ))}
        </div>
      </fieldset>
      <div>
        <label className="label" htmlFor="upscale-model">Model</label>
        {noList ? (
          <p className="text-sm text-ink-400">Couldn&apos;t load the list of models; the default model will be used.</p>
        ) : models.length === 0 ? (
          <p className="text-sm text-ink-400">Loading models…</p>
        ) : (
          <select id="upscale-model" className="input" value={model} onChange={(e) => setModel(e.target.value)}>
            {models.map((m) => (
              <option key={m.id} value={m.id} disabled={!m.usable && !m.active}>
                {m.name}
                {m.price_text ? ` · ${m.price_text}` : m.free ? " · free" : ""}
                {m.usable ? "" : " (needs a key)"}
              </option>
            ))}
          </select>
        )}
        {picked?.description && <p className="text-[11px] text-ink-400 mt-1">{picked.description}</p>}
      </div>
      <div
        className={clsx(
          "rounded-md border px-3 py-2 text-sm",
          est?.too_large ? "border-amber-500/40 bg-amber-500/10 text-amber-100" : "border-ink-700 bg-ink-900"
        )}
        aria-live="polite"
      >
        {estError ? (
          <span className="text-red-300">{estError}</span>
        ) : est ? (
          est.message
        ) : (
          <span className="text-ink-400">Working out the price…</span>
        )}
      </div>
      {picked && !pickedRuns && (
        <p className="text-xs text-amber-200">
          {picked.name} can&apos;t run right now (its provider is off or needs a key). Pick another model.
        </p>
      )}
      {error && <p className="text-xs text-accent" role="alert">{error}</p>}
      <div className="flex justify-end gap-2 pt-1">
        <button className="btn-ghost" onClick={onClose}>
          Cancel
        </button>
        <button
          className="btn-primary disabled:opacity-50"
          disabled={busy || (!model && !noList) || !pickedRuns || !est || est.too_large || Boolean(estError)}
          onClick={start}
        >
          {busy ? "Starting…" : `Upscale ×${factor} →`}
        </button>
      </div>
    </Dialog>
  );
}
