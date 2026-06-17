"use client";
import { useCallback, useRef, useState } from "react";
import clsx from "clsx";

export type DropzoneFileResult =
  | { name: string; ok: true }
  | { name: string; ok: false; error: string };

type Props = {
  accept?: string;
  multiple?: boolean;
  disabled?: boolean;
  hint?: string;
  // Called once per file. Resolves to undefined on success, throws on failure.
  // The parent controls the actual upload + any side effects (e.g. refresh
  // the asset grid). Files are uploaded in parallel up to `concurrency`.
  onUpload: (file: File) => Promise<unknown>;
  concurrency?: number;
  // Called after the *whole* batch settles, so the parent can refresh once
  // instead of after every file.
  onSettled?: (results: DropzoneFileResult[]) => void;
};

export function Dropzone({
  accept = "image/png,image/jpeg,image/webp",
  multiple = true,
  disabled = false,
  hint,
  onUpload,
  concurrency = 2,
  onSettled,
}: Props) {
  const input = useRef<HTMLInputElement>(null);
  const [over, setOver] = useState(false);
  const [progress, setProgress] = useState<
    { name: string; state: "pending" | "uploading" | "done" | "error"; error?: string }[]
  >([]);

  const runBatch = useCallback(
    async (files: File[]) => {
      if (!files.length) return;
      setProgress(files.map((f) => ({ name: f.name, state: "pending" })));

      const results: DropzoneFileResult[] = new Array(files.length);
      let cursor = 0;
      const workers = Array.from({ length: Math.min(concurrency, files.length) }, async () => {
        while (true) {
          const i = cursor++;
          if (i >= files.length) return;
          setProgress((p) => p.map((r, idx) => (idx === i ? { ...r, state: "uploading" } : r)));
          try {
            await onUpload(files[i]);
            results[i] = { name: files[i].name, ok: true };
            setProgress((p) => p.map((r, idx) => (idx === i ? { ...r, state: "done" } : r)));
          } catch (e: any) {
            const msg = e?.message || "Upload failed";
            results[i] = { name: files[i].name, ok: false, error: msg };
            setProgress((p) =>
              p.map((r, idx) => (idx === i ? { ...r, state: "error", error: msg } : r))
            );
          }
        }
      });
      await Promise.all(workers);
      onSettled?.(results);
      // Clear the per-file list shortly after success so the drawer doesn't
      // accumulate stale rows across batches; keep errors visible.
      setTimeout(() => {
        setProgress((p) => p.filter((r) => r.state === "error"));
      }, 1800);
    },
    [concurrency, onUpload, onSettled]
  );

  const onFiles = (list: FileList | null) => {
    if (!list || disabled) return;
    runBatch(Array.from(list));
  };

  return (
    <div className="space-y-2">
      <div
        onDragOver={(e) => {
          if (disabled) return;
          e.preventDefault();
          setOver(true);
        }}
        onDragLeave={() => setOver(false)}
        onDrop={(e) => {
          e.preventDefault();
          setOver(false);
          onFiles(e.dataTransfer.files);
        }}
        onClick={() => !disabled && input.current?.click()}
        role="button"
        tabIndex={disabled ? -1 : 0}
        onKeyDown={(e) => {
          if (disabled) return;
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            input.current?.click();
          }
        }}
        aria-disabled={disabled}
        className={clsx(
          "rounded-lg border border-dashed p-4 text-center transition cursor-pointer select-none",
          disabled
            ? "border-ink-800 text-ink-500 cursor-not-allowed"
            : over
            ? "border-accent bg-accent/10 text-ink-100"
            : "border-ink-700 hover:border-ink-500 text-ink-300"
        )}
      >
        <div className="text-sm">
          {over ? "Drop files to upload" : "Drag files here or click to browse"}
        </div>
        {hint && <div className="text-[10px] text-ink-400 mt-1">{hint}</div>}
        <input
          ref={input}
          type="file"
          hidden
          accept={accept}
          multiple={multiple}
          onChange={(e) => {
            onFiles(e.target.files);
            // Reset so re-selecting the same file still fires onChange.
            e.target.value = "";
          }}
        />
      </div>

      {progress.length > 0 && (
        <ul className="space-y-1 text-xs">
          {progress.map((r, i) => (
            <li
              key={`${r.name}-${i}`}
              className={clsx(
                "flex items-center gap-2 rounded px-2 py-1",
                r.state === "done" && "bg-emerald-500/10 text-emerald-300",
                r.state === "error" && "bg-red-500/10 text-red-300",
                (r.state === "uploading" || r.state === "pending") && "bg-ink-800 text-ink-200"
              )}
            >
              <span className="w-4 text-center">
                {r.state === "done" ? "✓" : r.state === "error" ? "✕" : r.state === "uploading" ? "…" : "·"}
              </span>
              <span className="truncate flex-1">{r.name}</span>
              {r.state === "error" && r.error && (
                <span className="text-[10px] text-red-300/80 truncate max-w-[40%]">{r.error}</span>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
