import React, { useState } from "react";
import type { Finding } from "../api/client";
import { api } from "../api/client";
import { T } from "../theme";

interface Props {
  findings: Finding[];
  onAction?: () => void;
}

const severityColor: Record<string, string> = {
  CRITICAL: T.critical, HIGH: T.high,
  MEDIUM: T.medium, LOW: T.low, INFO: T.info,
};

const statusBadge: Record<string, { bg: string; label: string }> = {
  open:     { bg: T.border, label: "Open" },
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
    <div style={{ background: T.surface, borderRadius: 16, overflow: "hidden" }}>
      <div style={{ padding: "16px 24px", borderBottom: `1px solid ${T.border}` }}>
        <h3 style={{ color: T.text, margin: 0, fontSize: 16 }}>
          Active Findings ({findings.length})
        </h3>
      </div>

      {findings.length === 0 && (
        <div style={{ padding: 32, textAlign: "center", color: T.textDim }}>
          ✨ No active findings
        </div>
      )}

      {findings.map((f) => (
        <div key={f.id} style={{ borderBottom: `1px solid ${T.border}` }}>
          <div
            style={{ padding: "12px 24px", display: "flex", alignItems: "center", gap: 12, cursor: "pointer" }}
            onClick={() => setExpanded(expanded === f.id ? null : f.id)}
          >
            <span style={{
              background: severityColor[f.severity],
              color: "#fff", fontSize: 11, fontWeight: 700,
              padding: "2px 8px", borderRadius: 4, minWidth: 64, textAlign: "center",
            }}>
              {f.severity}
            </span>
            <div style={{ flex: 1, color: T.text, fontSize: 14 }}>
              {f.title}
              {f.file_path && (
                <span style={{ color: T.textDim, marginLeft: 8, fontSize: 12 }}>
                  {f.file_path}{f.line_start ? `:${f.line_start}` : ""}
                </span>
              )}
            </div>
            <span style={{
              background: statusBadge[f.status]?.bg ?? T.border,
              color: "#cbd5e1", fontSize: 11, padding: "2px 8px", borderRadius: 4,
            }}>
              {statusBadge[f.status]?.label ?? f.status}
            </span>
            <span style={{ color: T.textDim, fontSize: 12, minWidth: 40 }}>
              {Math.round(f.confidence * 100)}%
            </span>
            <span style={{ color: T.textDim }}>{expanded === f.id ? "▲" : "▼"}</span>
          </div>

          {expanded === f.id && (
            <div style={{
              padding: "0 24px 16px",
              borderTop: `1px solid ${T.bg}`,
              background: T.surfaceHi,
            }}>
              <p style={{ color: T.textMuted, fontSize: 14, margin: "12px 0 8px" }}>
                {f.description}
              </p>
              {f.evidence && (
                <pre style={{
                  background: T.bg, color: "#e2e8f0",
                  padding: 12, borderRadius: 8, fontSize: 12, overflowX: "auto", margin: "8px 0",
                }}>
                  {f.evidence}
                </pre>
              )}
              {f.suggested_fix && (
                <div style={{ marginTop: 8 }}>
                  <div style={{ color: T.accent, fontSize: 12, fontWeight: 600 }}>Suggested Fix:</div>
                  <pre style={{
                    background: T.bg, color: "#86efac",
                    padding: 12, borderRadius: 8, fontSize: 12, overflowX: "auto",
                  }}>
                    {f.suggested_fix}
                  </pre>
                </div>
              )}
              {f.status === "open" && (
                <div style={{ display: "flex", gap: 8, marginTop: 12 }}>
                  <button
                    onClick={(e) => { e.stopPropagation(); handleAction(f.id, "approve"); }}
                    disabled={loading === f.id + "approve"}
                    style={{ background: "#166534", color: "#fff", border: "none", borderRadius: 6, padding: "6px 16px", cursor: "pointer", fontSize: 13, fontWeight: 600 }}
                  >✓ Approve</button>
                  <button
                    onClick={(e) => { e.stopPropagation(); handleAction(f.id, "reject"); }}
                    disabled={loading === f.id + "reject"}
                    style={{ background: "#7f1d1d", color: "#fff", border: "none", borderRadius: 6, padding: "6px 16px", cursor: "pointer", fontSize: 13, fontWeight: 600 }}
                  >✗ Reject</button>
                  <button
                    onClick={(e) => { e.stopPropagation(); handleAction(f.id, "snooze"); }}
                    style={{ background: "#1e3a5f", color: "#fff", border: "none", borderRadius: 6, padding: "6px 16px", cursor: "pointer", fontSize: 13 }}
                  >⏸ Snooze 7d</button>
                </div>
              )}
              <div style={{ color: T.textDim, fontSize: 11, marginTop: 8 }}>
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
