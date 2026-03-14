import React from "react";

interface Props {
  score: number;
  grade: string;
  delta7d: number;
  velocity: "IMPROVING" | "STABLE" | "DEGRADING";
}

const gradeColor: Record<string, string> = {
  A: "#22c55e", B: "#84cc16", C: "#eab308", D: "#f97316", F: "#ef4444",
};

const velocityIcon: Record<string, string> = {
  IMPROVING: "↑", STABLE: "→", DEGRADING: "↓",
};

const velocityColor: Record<string, string> = {
  IMPROVING: "#22c55e", STABLE: "#94a3b8", DEGRADING: "#ef4444",
};

export const HealthScoreCard: React.FC<Props> = ({ score, grade, delta7d, velocity }) => {
  const color = gradeColor[grade] ?? "#94a3b8";
  return (
    <div style={{
      background: "#1e293b", borderRadius: 16, padding: 32,
      display: "flex", flexDirection: "column", alignItems: "center",
      gap: 8, minWidth: 200,
    }}>
      <div style={{ fontSize: 72, fontWeight: 900, color, lineHeight: 1 }}>
        {Math.round(score)}
      </div>
      <div style={{
        fontSize: 36, fontWeight: 700,
        background: color, color: "#fff",
        borderRadius: 8, padding: "2px 16px",
      }}>
        {grade}
      </div>
      <div style={{
        fontSize: 18, color: velocityColor[velocity],
        fontWeight: 600, marginTop: 8,
      }}>
        {velocityIcon[velocity]} {delta7d > 0 ? "+" : ""}{delta7d.toFixed(1)} pts (7d)
      </div>
      <div style={{ color: "#64748b", fontSize: 14 }}>Repository Health Score</div>
    </div>
  );
};
