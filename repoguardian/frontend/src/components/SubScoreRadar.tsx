import React from "react";
import {
  RadarChart, Radar, PolarGrid, PolarAngleAxis, PolarRadiusAxis,
  ResponsiveContainer, Tooltip,
} from "recharts";

interface SubScores {
  code_quality: number;
  security: number;
  dependencies: number;
  documentation: number;
  test_coverage: number;
}

interface Props {
  subScores: SubScores;
}

export const SubScoreRadar: React.FC<Props> = ({ subScores }) => {
  const data = [
    { subject: "Code Quality", value: subScores.code_quality },
    { subject: "Security", value: subScores.security },
    { subject: "Dependencies", value: subScores.dependencies },
    { subject: "Documentation", value: subScores.documentation },
    { subject: "Test Coverage", value: subScores.test_coverage },
  ];

  return (
    <div style={{ background: "#1e293b", borderRadius: 16, padding: 24 }}>
      <h3 style={{ color: "#f1f5f9", margin: "0 0 16px", fontSize: 16 }}>Sub-Score Breakdown</h3>
      <ResponsiveContainer width="100%" height={280}>
        <RadarChart data={data}>
          <PolarGrid stroke="#334155" />
          <PolarAngleAxis dataKey="subject" tick={{ fill: "#94a3b8", fontSize: 12 }} />
          <PolarRadiusAxis domain={[0, 100]} tick={{ fill: "#64748b", fontSize: 10 }} />
          <Radar
            name="Score"
            dataKey="value"
            stroke="#6366f1"
            fill="#6366f1"
            fillOpacity={0.3}
          />
          <Tooltip
            contentStyle={{ background: "#0f172a", border: "1px solid #334155", color: "#f1f5f9" }}
            formatter={(v: number) => [`${v.toFixed(1)}`, "Score"]}
          />
        </RadarChart>
      </ResponsiveContainer>
    </div>
  );
};
