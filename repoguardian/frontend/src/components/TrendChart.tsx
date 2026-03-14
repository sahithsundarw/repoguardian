import React from "react";
import {
  LineChart, Line, XAxis, YAxis, CartesianGrid,
  Tooltip, ResponsiveContainer, ReferenceLine,
} from "recharts";
import type { TrendPoint } from "../api/client";
import { format, parseISO } from "date-fns";
import { T } from "../theme";

interface Props {
  data: TrendPoint[];
}

export const TrendChart: React.FC<Props> = ({ data }) => {
  const chartData = data.map((d) => ({
    date: format(parseISO(d.timestamp), "MMM d"),
    score: Math.round(d.overall_score),
    grade: d.grade,
  }));

  return (
    <div style={{ background: T.surface, borderRadius: 16, padding: 24 }}>
      <h3 style={{ color: T.text, margin: "0 0 16px", fontSize: 16 }}>
        30-Day Health Trend
      </h3>
      <ResponsiveContainer width="100%" height={200}>
        <LineChart data={chartData}>
          <CartesianGrid strokeDasharray="3 3" stroke={T.border} />
          <XAxis dataKey="date" tick={{ fill: T.textDim, fontSize: 11 }} />
          <YAxis domain={[0, 100]} tick={{ fill: T.textDim, fontSize: 11 }} />
          <Tooltip
            contentStyle={{ background: T.bg, border: `1px solid ${T.border}`, color: T.text }}
            formatter={(v: number, _: string, props: { payload: { grade: string } }) =>
              [`${v} (${props.payload.grade})`, "Health Score"]
            }
          />
          <ReferenceLine y={75} stroke={T.success} strokeDasharray="4 2" label={{ value: "B", fill: T.success, fontSize: 11 }} />
          <ReferenceLine y={60} stroke={T.warning} strokeDasharray="4 2" label={{ value: "C", fill: T.warning, fontSize: 11 }} />
          <Line
            type="monotone"
            dataKey="score"
            stroke={T.accent}
            strokeWidth={2}
            dot={{ fill: T.accent, r: 3 }}
            activeDot={{ r: 5 }}
          />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
};
