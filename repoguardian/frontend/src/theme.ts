/**
 * Deep Space color palette — single source of truth.
 * Import T from here instead of hard-coding hex values.
 */
export const T = {
  // Backgrounds
  bg:         "#0B0E14",   // Deep Charcoal — page background
  surface:    "#111827",   // Card / panel background
  surfaceHi:  "#1a2235",   // Elevated / hover surface
  border:     "#1E293B",   // Slate Gray — dividers & borders

  // Primary accent — Electric Emerald
  accent:     "#10B981",
  accentDim:  "#059669",   // Pressed / active state
  accentGlow: "rgba(16,185,129,0.15)",

  // Severity
  critical:   "#EF4444",
  high:       "#F59E0B",   // Burnt Orange
  medium:     "#FBBF24",
  low:        "#10B981",
  info:       "#94A3B8",

  // Text
  text:       "#F1F5F9",
  textMuted:  "#94A3B8",
  textDim:    "#64748B",

  // Semantic
  success:    "#10B981",
  warning:    "#F59E0B",
  error:      "#EF4444",
} as const;

export type Theme = typeof T;
