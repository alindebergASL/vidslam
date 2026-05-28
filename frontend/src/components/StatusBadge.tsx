export function StatusBadge({
  status,
  className = "",
}: {
  status: string;
  className?: string;
}) {
  const tone =
    status === "completed"
      ? "bg-emerald-500/20 text-emerald-300 border-emerald-500/40"
      : status === "failed"
      ? "bg-red-500/20 text-red-300 border-red-500/40"
      : status === "draft"
      ? "bg-ink-700 text-ink-200 border-ink-600"
      : "bg-amber-500/20 text-amber-300 border-amber-500/40";
  return (
    <span
      className={`inline-flex items-center gap-2 rounded-full border px-2.5 py-0.5 text-xs ${tone} ${className}`}
    >
      <span className="w-1.5 h-1.5 rounded-full bg-current" />
      {status}
    </span>
  );
}
