"use client";
import { useEffect, useRef, useState } from "react";
import { api, Asset, CustomModel } from "@/lib/api";
import { useToast } from "@/components/Toaster";

type Kind = CustomModel["kind"];

const KIND_LABEL: Record<Kind, string> = {
  character_lora: "Character LoRA",
  style_lora: "Style LoRA",
  voice_clone: "Voice clone",
};

const KIND_HELP: Record<Kind, string> = {
  character_lora:
    "10-50 portrait photos. Trains an identity adapter so every future image/clip looks like this character.",
  style_lora:
    "Existing renders or curated stills. Trains a brand-aesthetic adapter you can apply alongside a character LoRA.",
  voice_clone:
    "Short audio samples (3-10 minutes of clean speech). Returns an ElevenLabs voice_id auto-bound to this avatar.",
};

// Rough cost + wall-clock expectations per kind so nobody is surprised when
// they flip to real providers. Mirrors the estimates documented in
// scripts/test_real_providers.md §8; mock mode is always free + instant.
const KIND_COST: Record<Kind, { cost: string; time: string }> = {
  character_lora: { cost: "$2–5", time: "10–20 min" },
  style_lora: { cost: "$2–5", time: "10–20 min" },
  voice_clone: { cost: "free on paid tiers", time: "instant" },
};

const ALLOWED_KINDS: Record<"avatar" | "ingredient", Kind[]> = {
  avatar: ["character_lora", "style_lora", "voice_clone"],
  ingredient: ["style_lora"],
};

function StatusBadge({ status }: { status: CustomModel["status"] }) {
  const color =
    status === "completed"
      ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-300"
      : status === "failed" || status === "cancelled"
      ? "border-red-500/40 bg-red-500/10 text-red-300"
      : "border-ink-700 bg-ink-800 text-ink-200";
  return (
    <span className={`chip text-[10px] py-0.5 ${color}`}>{status}</span>
  );
}

export function TrainPanel({
  kind,
  id,
  assets,
}: {
  kind: "avatar" | "ingredient";
  id: number;
  assets: Asset[];
}) {
  const [jobs, setJobs] = useState<CustomModel[]>([]);
  const [opening, setOpening] = useState(false);
  const toast = useToast();
  const pollers = useRef<Map<number, ReturnType<typeof setInterval>>>(new Map());

  const refresh = async () => {
    try {
      setJobs(await api.listTrainingJobs({ owner_kind: kind, owner_id: id }));
    } catch {
      /* show nothing on transient errors */
    }
  };
  useEffect(() => {
    refresh();
    return () => {
      for (const t of pollers.current.values()) clearInterval(t);
      pollers.current.clear();
    };
  }, [kind, id]);

  // Poll in-flight jobs so the chip flips from "training" → "completed"
  // without a manual refresh. Cleared when the component unmounts or the
  // job hits terminal.
  useEffect(() => {
    for (const j of jobs) {
      const terminal = ["completed", "failed", "cancelled"].includes(j.status);
      if (terminal) {
        const t = pollers.current.get(j.id);
        if (t) {
          clearInterval(t);
          pollers.current.delete(j.id);
        }
        continue;
      }
      if (pollers.current.has(j.id)) continue;
      pollers.current.set(
        j.id,
        setInterval(async () => {
          try {
            const fresh = await api.getTrainingJob(j.id);
            setJobs((prev) => prev.map((x) => (x.id === fresh.id ? fresh : x)));
          } catch {
            /* keep polling */
          }
        }, 2000)
      );
    }
  }, [jobs]);

  return (
    <div className="card p-4 mb-4 space-y-3">
      <div className="flex items-center justify-between gap-2">
        <div className="text-sm font-medium">Custom models</div>
        <button
          className="btn-ghost text-xs"
          onClick={() => setOpening(true)}
          disabled={assets.length === 0}
          title={
            assets.length === 0
              ? "Upload at least one asset first"
              : "Train a LoRA or voice clone"
          }
        >
          + Train
        </button>
      </div>
      {jobs.length === 0 ? (
        <div className="text-xs text-ink-400">
          No trained models yet. Custom models let you generate with this cast
          member as a base — see <em>+ Train</em>.
        </div>
      ) : (
        <ul className="space-y-1.5">
          {jobs.map((j) => (
            <li
              key={j.id}
              className="flex items-center justify-between gap-2 text-xs bg-ink-800/60 rounded-md px-2.5 py-1.5"
            >
              <div className="min-w-0 flex-1">
                <div className="font-medium text-ink-100 truncate">
                  {j.name || `${KIND_LABEL[j.kind]} #${j.id}`}
                </div>
                <div className="text-ink-400 truncate">
                  {KIND_LABEL[j.kind]}
                  {j.provider ? ` · ${j.provider}` : ""}
                  {j.cost_usd ? ` · $${j.cost_usd.toFixed(2)}` : ""}
                  {j.error ? ` · ${j.error.slice(0, 80)}` : ""}
                </div>
                {!["completed", "failed", "cancelled"].includes(j.status) && (
                  <div
                    className="h-1 mt-1 bg-ink-700 rounded-full overflow-hidden"
                    role="progressbar"
                    aria-valuenow={Math.round(j.progress * 100)}
                    aria-valuemin={0}
                    aria-valuemax={100}
                  >
                    <div
                      className="h-full grad transition-all"
                      style={{ width: `${Math.round(j.progress * 100)}%` }}
                    />
                  </div>
                )}
              </div>
              <StatusBadge status={j.status} />
              {!["completed", "failed", "cancelled"].includes(j.status) ? (
                <button
                  className="text-ink-400 hover:text-accent text-xs"
                  onClick={async () => {
                    try {
                      await api.cancelTraining(j.id);
                      await refresh();
                    } catch (e: any) {
                      toast.error(e.message || "Cancel failed");
                    }
                  }}
                >
                  Cancel
                </button>
              ) : (
                <button
                  className="text-ink-400 hover:text-accent text-xs"
                  onClick={async () => {
                    if (!window.confirm("Delete this trained model?")) return;
                    try {
                      await api.deleteTraining(j.id);
                      await refresh();
                    } catch (e: any) {
                      toast.error(e.message || "Delete failed");
                    }
                  }}
                  aria-label={`Delete trained model ${j.name || j.id}`}
                >
                  ✕
                </button>
              )}
            </li>
          ))}
        </ul>
      )}

      {opening && (
        <TrainModal
          kind={kind}
          ownerId={id}
          assets={assets}
          onClose={() => setOpening(false)}
          onCreated={async () => {
            setOpening(false);
            await refresh();
            toast.success("Training kicked off — watch the progress bar above");
          }}
        />
      )}
    </div>
  );
}

function TrainModal({
  kind,
  ownerId,
  assets,
  onClose,
  onCreated,
}: {
  kind: "avatar" | "ingredient";
  ownerId: number;
  assets: Asset[];
  onClose: () => void;
  onCreated: () => void;
}) {
  const allowed = ALLOWED_KINDS[kind];
  const [trainKind, setTrainKind] = useState<Kind>(allowed[0]);
  const [name, setName] = useState("");
  const [selected, setSelected] = useState<number[]>(assets.map((a) => a.id));
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const toast = useToast();

  const submit = async () => {
    if (selected.length === 0) {
      setError("Pick at least one asset to train on.");
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      await api.startTraining(kind, ownerId, {
        name: name.trim(),
        kind: trainKind,
        training_asset_ids: selected,
      });
      onCreated();
    } catch (e: any) {
      setError(e.message || "Training submit failed");
      toast.error(e.message || "Training submit failed");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div
      className="fixed inset-0 z-40 bg-black/60 flex items-center justify-center p-4"
      onClick={onClose}
    >
      <div
        className="card w-full max-w-lg p-6 space-y-4"
        onClick={(e) => e.stopPropagation()}
      >
        <div>
          <div className="text-lg font-semibold">Train a custom model</div>
          <div className="text-xs text-ink-400 mt-0.5">
            Picks {kind === "avatar" ? "an avatar" : "an ingredient"} as the
            training subject. Runs in the background; the panel polls for
            progress.
          </div>
        </div>

        <div>
          <label className="label" htmlFor="train-kind">Kind</label>
          <div className="flex flex-wrap gap-2">
            {allowed.map((k) => (
              <button
                key={k}
                type="button"
                onClick={() => setTrainKind(k)}
                className={`chip text-xs ${trainKind === k ? "chip-active" : ""}`}
                aria-pressed={trainKind === k}
              >
                {KIND_LABEL[k]}
              </button>
            ))}
          </div>
          <div className="text-[11px] text-ink-400 mt-1.5">
            {KIND_HELP[trainKind]}
          </div>
          <div className="text-[11px] text-ink-300 mt-1 flex items-center gap-3">
            <span title="Estimated cost when running against the real provider; mock mode is free">
              ≈ {KIND_COST[trainKind].cost}
            </span>
            <span title="Typical wall-clock time on the real provider">
              ⏱ {KIND_COST[trainKind].time}
            </span>
          </div>
        </div>

        <div>
          <label className="label" htmlFor="train-name">Name</label>
          <input
            id="train-name"
            aria-label="Trained model name"
            className="input"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder={`e.g. ${trainKind === "voice_clone" ? "Naina warm voice" : "Naina LoRA v1"}`}
          />
        </div>

        <div>
          <div className="flex items-center justify-between mb-1">
            <div className="label !mb-0">
              Training assets ({selected.length}/{assets.length})
            </div>
            <button
              type="button"
              className="text-[11px] text-ink-400 hover:text-ink-100"
              onClick={() =>
                setSelected(selected.length === assets.length ? [] : assets.map((a) => a.id))
              }
            >
              {selected.length === assets.length ? "Clear" : "Select all"}
            </button>
          </div>
          <div className="grid grid-cols-4 sm:grid-cols-6 gap-2 max-h-44 overflow-y-auto">
            {assets.map((a) => {
              const on = selected.includes(a.id);
              return (
                <button
                  key={a.id}
                  type="button"
                  onClick={() =>
                    setSelected((cur) =>
                      cur.includes(a.id)
                        ? cur.filter((x) => x !== a.id)
                        : [...cur, a.id]
                    )
                  }
                  className={`relative aspect-square overflow-hidden rounded border ${
                    on ? "border-accent ring-2 ring-accent/60" : "border-ink-700 opacity-50"
                  }`}
                  aria-pressed={on}
                  aria-label={`${on ? "Remove" : "Add"} asset ${a.id} ${on ? "from" : "to"} training set`}
                >
                  <img
                    src={api.publicAsset(a.public_token)}
                    alt=""
                    className="w-full h-full object-cover"
                  />
                </button>
              );
            })}
          </div>
        </div>

        {error && <div className="text-xs text-accent">{error}</div>}

        <div className="flex justify-end gap-2 pt-2">
          <button className="btn-ghost" onClick={onClose} disabled={submitting}>
            Cancel
          </button>
          <button
            className="btn-primary"
            onClick={submit}
            disabled={submitting || selected.length === 0 || !name.trim()}
          >
            {submitting ? "Starting…" : "Start training"}
          </button>
        </div>
      </div>
    </div>
  );
}
