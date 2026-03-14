import React from "react";
import {
  RadarChart, Radar, PolarGrid, PolarAngleAxis, PolarRadiusAxis,
  ResponsiveContainer, Tooltip,
} from "recharts";
import { T } from "../theme";

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
    <div style={{ background: T.surface, borderRadius: 16, padding: 24 }}>
      <h3 style={{ color: T.text, margin: "0 0 16px", fontSize: 16 }}>Sub-Score Breakdown</h3>
      <ResponsiveContainer width="100%" height={280}>
        <RadarChart data={data}>
          <PolarGrid stroke={T.border} />
          <PolarAngleAxis dataKey="subject" tick={{ fill: T.textMuted, fontSize: 12 }} />
          <PolarRadiusAxis domain={[0, 100]} tick={{ fill: T.textDim, fontSize: 10 }} />
          <Radar
            name="Score"
            dataKey="value"
            stroke={T.accent}
            fill={T.accent}
            fillOpacity={0.25}
          />
          <Tooltip
            contentStyle={{ background: T.bg, border: `1px solid ${T.border}`, color: T.text }}
            formatter={(v: number) => [`${v.toFixed(1)}`, "Score"]}
          />
        </RadarChart>
      </ResponsiveContainer>
    </div>
  );
};
