import type { ReactNode } from "react";

export function Panel({
  title,
  right,
  children,
  className = "",
  bodyClassName = "",
}: {
  title: ReactNode;
  right?: ReactNode;
  children: ReactNode;
  className?: string;
  bodyClassName?: string;
}) {
  return (
    <section
      className={`flex h-full min-h-0 flex-col rounded-lg border border-line bg-panel ${className}`}
    >
      <header className="flex items-center justify-between gap-2 border-b border-line px-3 py-2">
        <h2 className="text-[11px] font-semibold uppercase tracking-[0.12em] text-muted">
          {title}
        </h2>
        {right}
      </header>
      <div className={`min-h-0 flex-1 ${bodyClassName}`}>{children}</div>
    </section>
  );
}

export function DemoTag({ className = "" }: { className?: string }) {
  return (
    <span
      className={`rounded bg-wait/15 px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wider text-wait ring-1 ring-wait/40 ${className}`}
      title="Simulated data — not real market prices"
    >
      Demo data
    </span>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className="px-3 py-6 text-center text-xs text-faint">{children}</p>;
}

export function Skeleton({ className = "" }: { className?: string }) {
  return <div className={`animate-pulse rounded bg-white/5 ${className}`} />;
}
