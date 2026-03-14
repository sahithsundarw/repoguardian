import React from "react";
import { T } from "../theme";

interface LineProps {
  width?: string | number;
  height?: number;
  style?: React.CSSProperties;
}

export const SkeletonLine: React.FC<LineProps> = ({ width = "100%", height = 16, style }) => (
  <div
    className="skeleton"
    style={{ width, height, borderRadius: 6, ...style }}
  />
);

interface CardProps {
  rows?: number;
  height?: number;
}

export const SkeletonCard: React.FC<CardProps> = ({ rows = 3, height = 20 }) => (
  <div style={{
    background: T.surface, borderRadius: 16, padding: 24,
    display: "flex", flexDirection: "column", gap: 12,
  }}>
    <SkeletonLine width="40%" height={18} />
    {Array.from({ length: rows }).map((_, i) => (
      <SkeletonLine key={i} width={i === rows - 1 ? "65%" : "100%"} height={height} />
    ))}
  </div>
);

export const SkeletonDashboard: React.FC = () => (
  <div style={{ padding: 32, maxWidth: 1400, margin: "0 auto", display: "flex", flexDirection: "column", gap: 24 }}>
    {/* Header */}
    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
      <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
        <SkeletonLine width={120} height={14} />
        <SkeletonLine width={280} height={28} />
        <SkeletonLine width={180} height={13} />
      </div>
      <div style={{ display: "flex", gap: 8 }}>
        <SkeletonLine width={90} height={36} style={{ borderRadius: 8 }} />
        <SkeletonLine width={120} height={36} style={{ borderRadius: 8 }} />
      </div>
    </div>

    {/* Top row: 3 cards */}
    <div style={{ display: "grid", gridTemplateColumns: "200px 1fr 1fr", gap: 20 }}>
      <div style={{ background: T.surface, borderRadius: 16, padding: 32, display: "flex", flexDirection: "column", alignItems: "center", gap: 12 }}>
        <SkeletonLine width={80} height={72} style={{ borderRadius: 8 }} />
        <SkeletonLine width={60} height={36} style={{ borderRadius: 8 }} />
        <SkeletonLine width={120} height={18} />
      </div>
      <SkeletonCard rows={4} height={16} />
      <SkeletonCard rows={5} height={14} />
    </div>

    {/* Second row: 2 cards */}
    <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 20 }}>
      <SkeletonCard rows={6} height={12} />
      <SkeletonCard rows={5} height={14} />
    </div>

    {/* Findings */}
    <div style={{ background: T.surface, borderRadius: 16, overflow: "hidden" }}>
      <div style={{ padding: "16px 24px", borderBottom: `1px solid ${T.border}` }}>
        <SkeletonLine width={200} height={18} />
      </div>
      {[1, 2, 3, 4].map((i) => (
        <div key={i} style={{ padding: "12px 24px", borderBottom: `1px solid ${T.border}`, display: "flex", gap: 12, alignItems: "center" }}>
          <SkeletonLine width={64} height={22} style={{ borderRadius: 4 }} />
          <SkeletonLine width="60%" height={16} />
          <SkeletonLine width={60} height={22} style={{ borderRadius: 4 }} />
        </div>
      ))}
    </div>
  </div>
);
