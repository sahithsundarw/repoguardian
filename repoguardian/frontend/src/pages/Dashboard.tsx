import React, { useEffect, useState } from "react";
import { useParams, Link } from "react-router-dom";
import { api, HealthDashboard, Finding } from "../api/client";
import { HealthScoreCard } from "../components/HealthScoreCard";
import { SubScoreRadar } from "../components/SubScoreRadar";
import { FindingsTable } from "../components/FindingsTable";
import { TrendChart } from "../components/TrendChart";
import { HotZoneList } from "../components/HotZoneList";

export const Dashboard: React.FC = () => {
  const { repoId } = useParams<{ repoId: string }>();
  const [dashboard, setDashboard] = useState<HealthDashboard | null>(null);
  const [findings, setFindings] = useState<Finding[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [activeTab, setActiveTab] = useState<"all" | "CRITICAL" | "HIGH" | "MEDIUM" | "LOW">("all");

  const fetchData = async () => {
    if (!repoId) return;
    try {
      const [dash, finds] = await Promise.all([
        api.health.dashboard(repoId),
        api.findings.list({ repo_id: repoId, status: "open" }),
      ]);
      setDashboard(dash);
      setFindings(finds);
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { fetchData(); }, [repoId]);

  if (loading) return (
    <div style={styles.center}>
      <div style={styles.spinner} />
      <div style={{ color: "#64748b", marginTop: 16 }}>Loading dashboard...</div>
    </div>
  );

  if (error || !dashboard) return (
    <div style={styles.center}>
      <div style={{ color: "#ef4444", fontSize: 18 }}>⚠️ {error || "Failed to load dashboard"}</div>
    </div>
  );

  const filteredFindings = activeTab === "all"
    ? findings
    : findings.filter((f) => f.severity === activeTab);

  const findingCounts = {
    all: findings.length,
    CRITICAL: findings.filter((f) => f.severity === "CRITICAL").length,
    HIGH: findings.filter((f) => f.severity === "HIGH").length,
    MEDIUM: findings.filter((f) => f.severity === "MEDIUM").length,
    LOW: findings.filter((f) => f.severity === "LOW").length,
  };

  return (
    <div style={styles.page}>
      {/* Header */}
      <div style={styles.header}>
        <div>
          <Link to="/" style={{ color: "#64748b", textDecoration: "none", fontSize: 14 }}>
            ← All Repositories
          </Link>
          <h1 style={{ color: "#f1f5f9", margin: "8px 0 4px", fontSize: 24 }}>
            🛡️ {dashboard.repo_full_name}
          </h1>
          <div style={{ color: "#64748b", fontSize: 13 }}>
            Last updated: {new Date(dashboard.as_of).toLocaleString()}
          </div>
        </div>
        <button
          onClick={fetchData}
          style={styles.refreshBtn}
        >
          ↻ Refresh
        </button>
      </div>

      {/* Top row: Score card + Radar + Activity */}
      <div style={styles.grid3}>
        <HealthScoreCard
          score={dashboard.overall_score}
          grade={dashboard.grade}
          delta7d={dashboard.trend_delta_7d}
          velocity={dashboard.trend_velocity}
        />
        <SubScoreRadar subScores={dashboard.sub_scores} />
        <div style={{ background: "#1e293b", borderRadius: 16, padding: 24 }}>
          <h3 style={{ color: "#f1f5f9", margin: "0 0 16px", fontSize: 16 }}>
            Active Issues Summary
          </h3>
          {Object.entries(dashboard.active_findings).map(([sev, count]) =>
            count > 0 ? (
              <div key={sev} style={{
                display: "flex", justifyContent: "space-between",
                padding: "6px 0", borderBottom: "1px solid #334155",
              }}>
                <span style={{ color: severityColor[sev], fontWeight: 600 }}>{sev}</span>
                <span style={{ color: "#f1f5f9", fontWeight: 700 }}>{count}</span>
              </div>
            ) : null
          )}
          {Object.values(dashboard.active_findings).every((c) => c === 0) && (
            <div style={{ color: "#22c55e", fontSize: 14 }}>✨ No active issues!</div>
          )}

          <h4 style={{ color: "#94a3b8", marginTop: 20, marginBottom: 12, fontSize: 14 }}>
            Recent Activity
          </h4>
          {dashboard.recent_activity.slice(0, 5).map((a, i) => (
            <div key={i} style={{ color: "#64748b", fontSize: 12, padding: "3px 0" }}>
              <span style={{ color: "#94a3b8" }}>{a.actor}</span> {a.event.replace(/_/g, " ")}
            </div>
          ))}
        </div>
      </div>

      {/* Second row: Trend + Hot Zones */}
      <div style={styles.grid2}>
        <TrendChart data={dashboard.trend_30d} />
        <HotZoneList zones={dashboard.hot_zones} />
      </div>

      {/* Findings table */}
      <div>
        {/* Severity filter tabs */}
        <div style={{ display: "flex", gap: 8, marginBottom: 16 }}>
          {(["all", "CRITICAL", "HIGH", "MEDIUM", "LOW"] as const).map((tab) => (
            <button
              key={tab}
              onClick={() => setActiveTab(tab)}
              style={{
                ...styles.tabBtn,
                background: activeTab === tab ? "#6366f1" : "#1e293b",
                color: activeTab === tab ? "#fff" : "#94a3b8",
              }}
            >
              {tab === "all" ? "All" : tab}
              {findingCounts[tab] > 0 && (
                <span style={{
                  marginLeft: 6, background: "rgba(255,255,255,0.15)",
                  borderRadius: 9999, padding: "0 6px", fontSize: 11,
                }}>
                  {findingCounts[tab]}
                </span>
              )}
            </button>
          ))}
        </div>

        <FindingsTable findings={filteredFindings} onAction={fetchData} />
      </div>
    </div>
  );
};

// ── Styles ─────────────────────────────────────────────────────────────────────

const styles: Record<string, React.CSSProperties> = {
  page: {
    padding: 32,
    display: "flex",
    flexDirection: "column",
    gap: 24,
    maxWidth: 1400,
    margin: "0 auto",
  },
  header: {
    display: "flex",
    justifyContent: "space-between",
    alignItems: "flex-end",
  },
  grid3: {
    display: "grid",
    gridTemplateColumns: "200px 1fr 1fr",
    gap: 20,
    alignItems: "start",
  },
  grid2: {
    display: "grid",
    gridTemplateColumns: "1fr 1fr",
    gap: 20,
  },
  center: {
    display: "flex",
    flexDirection: "column",
    alignItems: "center",
    justifyContent: "center",
    height: "60vh",
  },
  spinner: {
    width: 48, height: 48,
    border: "4px solid #334155",
    borderTop: "4px solid #6366f1",
    borderRadius: "50%",
    animation: "spin 1s linear infinite",
  },
  refreshBtn: {
    background: "#334155", color: "#f1f5f9",
    border: "none", borderRadius: 8,
    padding: "8px 16px", cursor: "pointer",
    fontSize: 14,
  },
  tabBtn: {
    border: "none", borderRadius: 8,
    padding: "8px 14px", cursor: "pointer",
    fontSize: 13, fontWeight: 600,
    transition: "background 0.2s",
  },
};

const severityColor: Record<string, string> = {
  CRITICAL: "#ef4444", HIGH: "#f97316",
  MEDIUM: "#eab308", LOW: "#22c55e", INFO: "#94a3b8",
};
