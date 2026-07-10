"use client";
import { useEffect, useRef, useState } from "react";
import { AuthGate } from "@/components/AuthGate";
import { api, BrandKit } from "@/lib/api";

export default function BrandPage() {
  return (
    <AuthGate>
      <Inner />
    </AuthGate>
  );
}

function Inner() {
  const [kits, setKits] = useState<BrandKit[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState("");

  const reload = () =>
    api.listBrandKits().then((rows) => {
      setKits(rows);
      setLoaded(true);
    });
  useEffect(() => {
    reload();
  }, []);

  return (
    <div className="p-8 max-w-5xl">
      <header className="mb-6 flex items-end justify-between">
        <div>
          <h1 className="text-3xl font-semibold tracking-tight">Brand Kits</h1>
          <p className="text-ink-300 mt-1">
            Reusable logo, colors, and defaults applied to a project&apos;s end card.
          </p>
        </div>
        <button className="btn-primary" onClick={() => setCreating(true)}>
          + New Brand Kit
        </button>
      </header>

      {!loaded ? (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4" aria-hidden>
          {Array.from({ length: 2 }).map((_, i) => (
            <div key={i} className="card p-4 space-y-3">
              <div className="h-4 skeleton w-1/3" />
              <div className="aspect-[16/9] skeleton" />
              <div className="h-3 skeleton w-1/2" />
            </div>
          ))}
        </div>
      ) : kits.length === 0 ? (
        <div className="card p-10 text-center">
          <div className="text-3xl mb-2">◆</div>
          <div className="text-base font-medium mb-1">No brand kits yet</div>
          <div className="text-xs text-ink-300 max-w-md mx-auto">
            A brand kit saves a logo + colors + default CTA/disclosure once and
            applies them to a project&apos;s end card and caption color, so every
            video on the same brand looks consistent.
          </div>
          <div className="text-[11px] text-ink-400 mt-3">
            Click <em>+ New Brand Kit</em> at the top to build one.
          </div>
        </div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          {kits.map((k) => (
            <BrandCard key={k.id} kit={k} onChange={reload} />
          ))}
        </div>
      )}

      {creating && (
        <div className="fixed inset-0 z-30 bg-black/60 flex items-center justify-center p-4">
          <div className="card w-full max-w-sm p-6 space-y-4">
            <div className="text-lg font-semibold">New Brand Kit</div>
            <div>
              <label className="label">Name</label>
              <input className="input" value={name} onChange={(e) => setName(e.target.value)} />
            </div>
            <div className="flex justify-end gap-2">
              <button className="btn-ghost" onClick={() => setCreating(false)}>
                Cancel
              </button>
              <button
                className="btn-primary"
                disabled={!name.trim()}
                onClick={async () => {
                  await api.createBrandKit({ name });
                  setName("");
                  setCreating(false);
                  reload();
                }}
              >
                Create
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function BrandCard({ kit, onChange }: { kit: BrandKit; onChange: () => void }) {
  const [k, setK] = useState(kit);
  const logoInput = useRef<HTMLInputElement>(null);

  const save = (patch: Partial<BrandKit>) => {
    const next = { ...k, ...patch };
    setK(next);
    api.updateBrandKit(k.id, patch).then(onChange);
  };

  return (
    <div className="card p-4">
      <div className="flex items-start justify-between mb-3">
        <input
          className="input max-w-[60%] font-medium"
          aria-label="Brand kit name"
          value={k.name}
          onChange={(e) => setK({ ...k, name: e.target.value })}
          onBlur={() => save({ name: k.name })}
        />
        <button
          className="text-ink-400 hover:text-accent text-xs"
          onClick={async () => {
            if (!window.confirm(`Delete brand kit "${k.name}"?`)) return;
            await api.deleteBrandKit(k.id);
            onChange();
          }}
        >
          Delete
        </button>
      </div>

      {/* End-card preview */}
      <div
        className="rounded-lg aspect-[16/9] flex flex-col items-center justify-center gap-2 mb-3 overflow-hidden"
        style={{ backgroundColor: k.end_card_bg_color }}
      >
        {k.logo_public_token && (
          <img
            src={`${api.brandLogoUrl(k.id)}?t=${k.logo_public_token}`}
            alt={`${k.name} logo`}
            className="max-h-12 object-contain"
          />
        )}
        <div className="text-sm font-semibold" style={{ color: k.end_card_text_color }}>
          {k.default_cta_text || "Your CTA here"}
        </div>
      </div>

      <div className="grid grid-cols-3 gap-2 mb-3">
        <ColorField label="Primary" value={k.primary_color} onChange={(v) => save({ primary_color: v })} />
        <ColorField label="Card bg" value={k.end_card_bg_color} onChange={(v) => save({ end_card_bg_color: v })} />
        <ColorField label="Card text" value={k.end_card_text_color} onChange={(v) => save({ end_card_text_color: v })} />
      </div>

      <div className="space-y-2">
        <div>
          <label className="label" htmlFor={`bk-${k.id}-cta`}>Default CTA</label>
          <input
            id={`bk-${k.id}-cta`}
            aria-label="Default CTA"
            className="input"
            value={k.default_cta_text}
            onChange={(e) => setK({ ...k, default_cta_text: e.target.value })}
            onBlur={() => save({ default_cta_text: k.default_cta_text })}
          />
        </div>
        <div>
          <label className="label" htmlFor={`bk-${k.id}-disclosure`}>Default disclosure</label>
          <input
            id={`bk-${k.id}-disclosure`}
            aria-label="Default disclosure"
            className="input"
            value={k.default_disclosure_text}
            onChange={(e) => setK({ ...k, default_disclosure_text: e.target.value })}
            onBlur={() => save({ default_disclosure_text: k.default_disclosure_text })}
            placeholder="AI-generated virtual creator"
          />
        </div>
        <button className="btn-ghost text-xs" onClick={() => logoInput.current?.click()}>
          {k.logo_public_token ? "Replace logo" : "Upload logo"}
        </button>
        <input
          ref={logoInput}
          type="file"
          hidden
          accept="image/png,image/jpeg,image/webp"
          onChange={async (e) => {
            if (!e.target.files) return;
            const updated = await api.uploadBrandLogo(k.id, e.target.files[0]);
            setK(updated);
            onChange();
          }}
        />
      </div>
    </div>
  );
}

function ColorField({
  label,
  value,
  onChange,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
}) {
  return (
    <div>
      <label className="label" htmlFor={`color-${label.replace(/\s+/g, "-").toLowerCase()}`}>{label}</label>
      <div className="flex items-center gap-1">
        <input
          id={`color-${label.replace(/\s+/g, "-").toLowerCase()}`}
          aria-label={`${label} color`}
          type="color"
          value={value}
          onChange={(e) => onChange(e.target.value)}
          className="w-8 h-8 rounded bg-transparent border border-ink-700 cursor-pointer"
        />
        <span className="text-[10px] text-ink-400">{value}</span>
      </div>
    </div>
  );
}
