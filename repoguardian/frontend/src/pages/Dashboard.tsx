import React, { useEffect, useRef, useState } from "react";
import { useParams, Link } from "react-router-dom";
import { api, HealthDashboard, Finding } from "../api/client";
import { HealthScoreCard } from "../components/HealthScoreCard";
import { SubScoreRadar } from "../components/SubScoreRadar";
import { FindingsTable } from "../components/FindingsTable";
import { TrendChart } from "../components/TrendChart";
import { HotZoneList } from "../components/HotZoneList";
import { SkeletonDashboard } from "../components/Skeleton";
import { T } from "../theme";

export const Dashboard: React.FC = () => {
  const { repoId } = useParams<{ repoId: string }>();
  const [dashboard, setDashboard] = useState<HealthDashboard | null>(null);
  const [findings, setFindings] = useState<Finding[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [activeTab, setActiveTab] = useState<"all" | "CRITICAL" | "HIGH" | "MEDIUM" | "LOW">("all");
  const [scanning, setScanning] = useState(false);
  const [scanProgress, setScanProgress] = useState(0);   // 0-100
  const [scanError, setScanError] = useState<string | null>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const progressRef = useRef<ReturnType<typeof setInterval> | null>(null);
  // Ref to hold baseline timestamp — avoids stale closure in poll callback
  const baseTimestampRef = useRef<string>("");

  const stopPolling = () => {
    if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; }
    if (progressRef.current) { clearInterval(progressRef.current); progressRef.current = null; }
  };

  const triggerScan = async () => {
    if (!repoId || scanning) return;
    // Clear any existing poll/progress intervals before starting a new scan
    stopPolling();
    setScanning(true);
    setScanProgress(0);
    setScanError(null);

    try {
      await api.scan.trigger(repoId);
    } catch (e) {
      setScanError("Scan failed: " + String(e));
      setScanning(false);
      return;
    }

    // Animate progress bar over ~90s (never quite reaches 100 until done)
    const startTime = Date.now();
    const SCAN_ESTIMATE_MS = 90_000;
    progressRef.current = setInterval(() => {
      const elapsed = Date.now() - startTime;
      const pct = Math.min(92, (elapsed / SCAN_ESTIMATE_MS) * 100);
      setScanProgress(pct);
    }, 500);

    // Store baseline via ref to avoid stale closure inside poll callback
    baseTimestampRef.current = dashboard?.as_of ?? "";

    // Poll every 6s — refresh dashboard when as_of changes or after max attempts
    let attempts = 0;
    const MAX_ATTEMPTS = 20; // 20 × 6s = 120s max wait
    pollRef.current = setInterval(async () => {
      attempts++;
      try {
        const updated = await api.health.dashboard(repoId!);
        const hasNewData = updated.as_of !== baseTimestampRef.current;
        const timedOut = attempts >= MAX_ATTEMPTS;

        if (hasNewData || timedOut) {
          stopPolling();
          const finds = await api.findings.list({ repo_id: repoId!, status: "open" });
          setScanProgress(100);
          setDashboard(updated);
          setFindings(finds);
          setTimeout(() => { setScanning(false); setScanProgress(0); }, 600);
        }
      } catch {
        // network blip — keep polling
        if (attempts >= MAX_ATTEMPTS) {
          stopPolling();
          setScanning(false);
          setScanProgress(0);
          setScanError("Scan timed out. Check results manually.");
        }
      }
    }, 6_000);
  };

  // Cleanup on unmount
  useEffect(() => () => stopPolling(), []);

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

  if (loading) return <SkeletonDashboard />;

  if (error || !dashboard) return (
    <div style={styles.center}>
      <div style={{ color: T.error, fontSize: 18 }}>⚠️ {error || "Failed to load dashboard"}</div>
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

  // Compute live active findings from current findings state (stays in sync after HITL actions)
  const liveActiveFindings: Record<string, number> = {
    CRITICAL: findings.filter((f) => f.status === "open" && f.severity === "CRITICAL").length,
    HIGH:     findings.filter((f) => f.status === "open" && f.severity === "HIGH").length,
    MEDIUM:   findings.filter((f) => f.status === "open" && f.severity === "MEDIUM").length,
    LOW:      findings.filter((f) => f.status === "open" && f.severity === "LOW").length,
    INFO:     findings.filter((f) => f.status === "open" && f.severity === "INFO").length,
  };

  return (
    <div style={styles.page}>
      {/* Header */}
      <div style={styles.header}>
        <div>
          <Link to="/" style={{ color: T.textDim, textDecoration: "none", fontSize: 14 }}>
            ← All Repositories
          </Link>
          <h1 style={{ color: T.text, margin: "8px 0 4px", fontSize: 24 }}>
            🛡️ {dashboard.repo_full_name}
          </h1>
          <div style={{ color: T.textDim, fontSize: 13 }}>
            Last updated: {new Date(dashboard.as_of).toLocaleString()}
          </div>
        </div>
        <div style={{ display: "flex", flexDirection: "column", alignItems: "flex-end", gap: 10 }}>
          <div style={{ display: "flex", gap: 8 }}>
            <button onClick={fetchData} disabled={scanning} style={{ ...styles.refreshBtn, opacity: scanning ? 0.4 : 1 }}>↻ Refresh</button>
            <button
              onClick={triggerScan}
              disabled={scanning}
              style={{
                ...styles.refreshBtn,
                background: scanning ? T.accentDim : T.accent,
                color: "#fff",
                opacity: scanning ? 0.85 : 1,
                minWidth: 130,
              }}
            >
              {scanning ? "⚡ Scanning…" : "⚡ Scan Repo"}
            </button>
          </div>

          {/* Progress bar */}
          {scanning && (
            <div style={{ width: 260 }}>
              <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 4 }}>
                <span style={{ color: T.textMuted, fontSize: 12 }}>Analyzing codebase…</span>
                <span style={{ color: T.accent, fontSize: 12, fontWeight: 600 }}>{Math.round(scanProgress)}%</span>
              </div>
              <div style={{ background: T.surface, borderRadius: 999, height: 6, overflow: "hidden", border: `1px solid ${T.border}` }}>
                <div style={{
                  height: "100%",
                  width: `${scanProgress}%`,
                  background: `linear-gradient(90deg, ${T.accent}, ${T.accentDim})`,
                  borderRadius: 999,
                  transition: "width 0.4s ease",
                }} />
              </div>
              <div style={{ color: T.textDim, fontSize: 11, marginTop: 4, textAlign: "right" }}>
                Dashboard will update automatically
              </div>
            </div>
          )}

          {scanError && (
            <div style={{ fontSize: 12, color: T.error, maxWidth: 260, textAlign: "right" }}>
              {scanError}
            </div>
          )}
        </div>
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
        <div style={{ background: T.surface, borderRadius: 16, padding: 24 }}>
          <h3 style={{ color: T.text, margin: "0 0 16px", fontSize: 16 }}>
            Active Issues Summary
          </h3>
          {Object.entries(liveActiveFindings).map(([sev, count]) =>
            count > 0 ? (
              <div key={sev} style={{
                display: "flex", justifyContent: "space-between",
                padding: "6px 0", borderBottom: `1px solid ${T.border}`,
              }}>
                <span style={{ color: severityColor[sev], fontWeight: 600 }}>{sev}</span>
                <span style={{ color: T.text, fontWeight: 700 }}>{count}</span>
              </div>
            ) : null
          )}
          {Object.values(liveActiveFindings).every((c) => c === 0) && (
            <div style={{ color: T.success, fontSize: 14 }}>✨ No active issues!</div>
          )}

          <h4 style={{ color: T.textMuted, marginTop: 20, marginBottom: 12, fontSize: 14 }}>
            Recent Activity
          </h4>
          {dashboard.recent_activity.slice(0, 5).map((a, i) => (
            <div key={i} style={{ color: T.textDim, fontSize: 12, padding: "3px 0" }}>
              <span style={{ color: T.textMuted }}>{a.actor}</span> {a.event.replace(/_/g, " ")}
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
                background: activeTab === tab ? T.accent : T.surface,
                color: activeTab === tab ? "#fff" : T.textMuted,
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
  refreshBtn: {
    background: T.border, color: T.text,
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
  CRITICAL: T.critical, HIGH: T.high,
  MEDIUM: T.medium, LOW: T.low, INFO: T.info,
};
