import React, { useState } from "react";
import type { Finding } from "../api/client";
import { api } from "../api/client";

interface Props {
  findings: Finding[];
  onAction?: () => void;
}

const severityColor: Record<string, string> = {
  CRITICAL: "#ef4444", HIGH: "#f97316",
  MEDIUM: "#eab308", LOW: "#22c55e", INFO: "#94a3b8",
};

const statusBadge: Record<string, { bg: string; label: string }> = {
  open:     { bg: "#334155", label: "Open" },
  approved: { bg: "#166534", label: "Approved" },
  rejected: { bg: "#7f1d1d", label: "Rejected" },
  snoozed:  { bg: "#1e3a5f", label: "Snoozed" },
};

export const FindingsTable: React.FC<Props> = ({ findings, onAction }) => {
  const [expanded, setExpanded] = useState<string | null>(null);
  const [loading, setLoading] = useState<string | null>(null);

  const handleAction = async (findingId: string, action: string) => {
    setLoading(findingId + action);
    try {
      await api.hitl.action(findingId, action);
      onAction?.();
    } catch (e) {
      console.error("HITL action failed:", e);
    } finally {
      setLoading(null);
    }
  };

  return (
    <div style={{ background: "#1e293b", borderRadius: 16, overflow: "hidden" }}>
      <div style={{ padding: "16px 24px", borderBottom: "1px solid #334155" }}>
        <h3 style={{ color: "#f1f5f9", margin: 0, fontSize: 16 }}>
          Active Findings ({findings.length})
        </h3>
      </div>

      {findings.length === 0 && (
        <div style={{ padding: 32, textAlign: "center", color: "#64748b" }}>
          ✨ No active findings
        </div>
      )}

      {findings.map((f) => (
        <div key={f.id} style={{ borderBottom: "1px solid #334155" }}>
          {/* Row */}
          <div
            style={{
              padding: "12px 24px",
              display: "flex",
              alignItems: "center",
              gap: 12,
              cursor: "pointer",
            }}
            onClick={() => setExpanded(expanded === f.id ? null : f.id)}
          >
            {/* Severity badge */}
            <span style={{
              background: severityColor[f.severity],
              color: "#fff",
              fontSize: 11,
              fontWeight: 700,
              padding: "2px 8px",
              borderRadius: 4,
              minWidth: 64,
              textAlign: "center",
            }}>
              {f.severity}
            </span>

            {/* Title */}
            <div style={{ flex: 1, color: "#f1f5f9", fontSize: 14 }}>
              {f.title}
              {f.file_path && (
                <span style={{ color: "#64748b", marginLeft: 8, fontSize: 12 }}>
                  {f.file_path}{f.line_start ? `:${f.line_start}` : ""}
                </span>
              )}
            </div>

            {/* Status */}
            <span style={{
              background: statusBadge[f.status]?.bg ?? "#334155",
              color: "#cbd5e1",
              fontSize: 11,
              padding: "2px 8px",
              borderRadius: 4,
            }}>
              {statusBadge[f.status]?.label ?? f.status}
            </span>

            {/* Confidence */}
            <span style={{ color: "#64748b", fontSize: 12, minWidth: 40 }}>
              {Math.round(f.confidence * 100)}%
            </span>

            <span style={{ color: "#475569" }}>{expanded === f.id ? "▲" : "▼"}</span>
          </div>

          {/* Expanded details */}
          {expanded === f.id && (
            <div style={{
              padding: "0 24px 16px",
              borderTop: "1px solid #0f172a",
              background: "#162032",
            }}>
              <p style={{ color: "#94a3b8", fontSize: 14, margin: "12px 0 8px" }}>
                {f.description}
              </p>

              {f.evidence && (
                <pre style={{
                  background: "#0f172a", color: "#e2e8f0",
                  padding: 12, borderRadius: 8,
                  fontSize: 12, overflowX: "auto",
                  margin: "8px 0",
                }}>
                  {f.evidence}
                </pre>
              )}

              {f.suggested_fix && (
                <div style={{ marginTop: 8 }}>
                  <div style={{ color: "#22d3ee", fontSize: 12, fontWeight: 600 }}>
                    Suggested Fix:
                  </div>
                  <pre style={{
                    background: "#0f172a", color: "#86efac",
                    padding: 12, borderRadius: 8,
                    fontSize: 12, overflowX: "auto",
                  }}>
                    {f.suggested_fix}
                  </pre>
                </div>
              )}

              {/* HITL Actions */}
              {f.status === "open" && (
                <div style={{ display: "flex", gap: 8, marginTop: 12 }}>
                  <button
                    onClick={(e) => { e.stopPropagation(); handleAction(f.id, "approve"); }}
                    disabled={loading === f.id + "approve"}
                    style={{
                      background: "#166534", color: "#fff",
                      border: "none", borderRadius: 6,
                      padding: "6px 16px", cursor: "pointer",
                      fontSize: 13, fontWeight: 600,
                    }}
                  >
                    ✓ Approve
                  </button>
                  <button
                    onClick={(e) => { e.stopPropagation(); handleAction(f.id, "reject"); }}
                    disabled={loading === f.id + "reject"}
                    style={{
                      background: "#7f1d1d", color: "#fff",
                      border: "none", borderRadius: 6,
                      padding: "6px 16px", cursor: "pointer",
                      fontSize: 13, fontWeight: 600,
                    }}
                  >
                    ✗ Reject
                  </button>
                  <button
                    onClick={(e) => { e.stopPropagation(); handleAction(f.id, "snooze"); }}
                    style={{
                      background: "#1e3a5f", color: "#fff",
                      border: "none", borderRadius: 6,
                      padding: "6px 16px", cursor: "pointer",
                      fontSize: 13,
                    }}
                  >
                    ⏸ Snooze 7d
                  </button>
                </div>
              )}

              <div style={{ color: "#475569", fontSize: 11, marginTop: 8 }}>
                ID: {f.id.substring(0, 8)} · Agent: {f.agent_source} · PR #{f.pr_number}
                {f.cwe_id && ` · ${f.cwe_id}`}
              </div>
            </div>
          )}
        </div>
      ))}
    </div>
  );
};
