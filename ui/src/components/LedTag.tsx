// Tiny status LED: violet = LLM, purple = Local/Semantic

import React from "react";

type Mode = "llm" | "local" | "fallback" | "loading" | "error";
type Size = "sm" | "md";

const colors: Record<Mode, { dot: string; ring: string; text: string; defaultLabel: string }> = {
  llm:      { dot: "bg-violet-500",  ring: "ring-violet-300",  text: "text-violet-800",  defaultLabel: "LLM" },
  local:    { dot: "bg-purple-500",  ring: "ring-purple-300",  text: "text-purple-800",  defaultLabel: "Local" },
  fallback: { dot: "bg-amber-500",   ring: "ring-amber-300",   text: "text-amber-800",   defaultLabel: "Fallback" },
  loading:  { dot: "bg-slate-400",   ring: "ring-slate-300",   text: "text-slate-700",   defaultLabel: "Working…" },
  error:    { dot: "bg-rose-500",    ring: "ring-rose-300",    text: "text-rose-800",    defaultLabel: "Error" },
};

export default function StatusLED({
  mode,
  label,
  pulsing = false,
  size = "sm",
  title,
}: {
  mode: Mode;
  label?: string;
  pulsing?: boolean;
  size?: Size;
  title?: string;
}) {
  const c = colors[mode];
  const wh = size === "md" ? "w-2.5 h-2.5" : "w-2 h-2";
  const ring = size === "md" ? "ring-2" : "ring";
  return (
    <span className={`inline-flex items-center gap-1 ${c.text}`} title={title || label || c.defaultLabel}>
      <span className={`${wh} rounded-full ${c.dot} ${ring} ${c.ring} ${pulsing ? "animate-pulse" : ""}`} />
      <span className="text-xs">{label || c.defaultLabel}</span>
    </span>
  );
}