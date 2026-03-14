import React from "react";
import {
  LineChart, Line, XAxis, YAxis, CartesianGrid,
  Tooltip, ResponsiveContainer, ReferenceLine,
} from "recharts";
import type { TrendPoint } from "../api/client";
import { format, parseISO } from "date-fns";

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
    <div style={{ background: "#1e293b", borderRadius: 16, padding: 24 }}>
      <h3 style={{ color: "#f1f5f9", margin: "0 0 16px", fontSize: 16 }}>
        30-Day Health Trend
      </h3>
      <ResponsiveContainer width="100%" height={200}>
        <LineChart data={chartData}>
          <CartesianGrid strokeDasharray="3 3" stroke="#334155" />
          <XAxis dataKey="date" tick={{ fill: "#64748b", fontSize: 11 }} />
          <YAxis domain={[0, 100]} tick={{ fill: "#64748b", fontSize: 11 }} />
          <Tooltip
            contentStyle={{ background: "#0f172a", border: "1px solid #334155", color: "#f1f5f9" }}
            formatter={(v: number, _: string, props: { payload: { grade: string } }) =>
              [`${v} (${props.payload.grade})`, "Health Score"]
            }
          />
          <ReferenceLine y={75} stroke="#22c55e" strokeDasharray="4 2" label={{ value: "B", fill: "#22c55e", fontSize: 11 }} />
          <ReferenceLine y={60} stroke="#eab308" strokeDasharray="4 2" label={{ value: "C", fill: "#eab308", fontSize: 11 }} />
          <Line
            type="monotone"
            dataKey="score"
            stroke="#6366f1"
            strokeWidth={2}
            dot={{ fill: "#6366f1", r: 3 }}
            activeDot={{ r: 5 }}
          />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
};
