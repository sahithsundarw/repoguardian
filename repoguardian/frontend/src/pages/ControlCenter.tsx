import React, { useEffect, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import {
  api,
  EnrollRepositoryRequest,
  EphemeralFinding,
  EphemeralScanStatus,
  Repository,
} from "../api/client";

// ── URL parser (shared between both paths) ────────────────────────────────────

interface ParsedRepo {
  owner: string;
  name: string;
  full_name: string;
  clone_url: string;
}

function parseGitHubUrl(raw: string): ParsedRepo | null {
  try {
    const cleaned = raw.trim().replace(/\.git$/, "");
    const url = new URL(cleaned.startsWith("http") ? cleaned : `https://${cleaned}`);
    if (!url.hostname.includes("github.com")) return null;
    const parts = url.pathname.replace(/^\//, "").split("/").filter(Boolean);
    if (parts.length < 2) return null;
    const [owner, name] = parts;
    return { owner, name, full_name: `${owner}/${name}`, clone_url: `https://github.com/${owner}/${name}.git` };
  } catch {
    return null;
  }
}

// ── Score badge ───────────────────────────────────────────────────────────────

const gradeColor: Record<string, string> = {
  A: "#22c55e", B: "#84cc16", C: "#eab308", D: "#f97316", F: "#ef4444",
};

function ScoreBadge({ repoId }: { repoId: string }) {
  const [score, setScore] = useState<{ overall_score: number; grade: string } | null>(null);
  useEffect(() => { api.health.score(repoId).then(setScore).catch(() => {}); }, [repoId]);
  if (!score) return null;
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 6, background: "#0f172a", borderRadius: 8, padding: "2px 10px" }}>
      <span style={{ color: gradeColor[score.grade] ?? "#94a3b8", fontWeight: 700, fontSize: 15 }}>{score.grade}</span>
      <span style={{ color: "#64748b", fontSize: 13 }}>{Math.round(score.overall_score)}</span>
    </span>
  );
}

// ── Severity colours ──────────────────────────────────────────────────────────

const sevColor: Record<string, string> = {
  CRITICAL: "#ef4444", HIGH: "#f97316", MEDIUM: "#eab308", LOW: "#22c55e", INFO: "#6366f1",
};

// ── Ephemeral finding row ─────────────────────────────────────────────────────

function EphemeralFindingRow({ f }: { f: EphemeralFinding }) {
  const [open, setOpen] = useState(false);
  return (
    <div style={{
      background: "#0f172a", border: "1px solid #334155", borderRadius: 10,
      marginBottom: 8, overflow: "hidden",
    }}>
      <div
        onClick={() => setOpen(!open)}
        style={{
          display: "flex", alignItems: "center", gap: 12,
          padding: "12px 16px", cursor: "pointer",
        }}
      >
        <span style={{
          background: sevColor[f.severity] + "22",
          color: sevColor[f.severity],
          border: `1px solid ${sevColor[f.severity]}44`,
          borderRadius: 6, padding: "2px 8px", fontSize: 11, fontWeight: 700,
          whiteSpace: "nowrap",
        }}>{f.severity}</span>
        <div style={{ flex: 1, minWidth: 0 }}>
          <p style={{ margin: 0, color: "#f1f5f9", fontSize: 14, fontWeight: 600 }}>{f.title}</p>
          {f.file_path && (
            <p style={{ margin: "2px 0 0", color: "#64748b", fontSize: 12, fontFamily: "monospace" }}>
              {f.file_path}{f.line_start ? `:${f.line_start}` : ""}
            </p>
          )}
        </div>
        <span style={{ color: "#475569", fontSize: 13 }}>{open ? "▲" : "▼"}</span>
      </div>
      {open && (
        <div style={{ padding: "0 16px 16px", borderTop: "1px solid #334155" }}>
          <p style={{ margin: "12px 0 8px", color: "#94a3b8", fontSize: 13, lineHeight: 1.6 }}>{f.description}</p>
          {f.evidence && (
            <pre style={{
              background: "#1e293b", border: "1px solid #334155", borderRadius: 8,
              padding: "10px 14px", fontSize: 12, color: "#e2e8f0",
              margin: "8px 0", overflowX: "auto", whiteSpace: "pre-wrap",
            }}>{f.evidence}</pre>
          )}
          {f.suggested_fix && (
            <div style={{ background: "#052e16", border: "1px solid #166534", borderRadius: 8, padding: "10px 14px", marginTop: 8 }}>
              <p style={{ margin: "0 0 4px", color: "#86efac", fontSize: 12, fontWeight: 600 }}>Suggested Fix</p>
              <p style={{ margin: 0, color: "#bbf7d0", fontSize: 12, lineHeight: 1.5 }}>{f.suggested_fix}</p>
            </div>
          )}
          <p style={{ margin: "8px 0 0", color: "#475569", fontSize: 11 }}>
            Confidence: {Math.round(f.confidence * 100)}% · Source: {f.agent_source}
          </p>
        </div>
      )}
    </div>
  );
}

// ── Types ─────────────────────────────────────────────────────────────────────

type ActiveTab = "monitor" | "flash";
type AuditSchedule = "none" | "hourly" | "daily" | "weekly" | "custom";
type EnrollStep = "form" | "loading" | "success" | "error";
type FlashStep = "idle" | "scanning" | "results" | "error";

// ── Main component ────────────────────────────────────────────────────────────

export const ControlCenter: React.FC = () => {
  const navigate = useNavigate();

  // Tab
  const [activeTab, setActiveTab] = useState<ActiveTab>("monitor");

  // Monitor enrollment state
  const [repoUrl, setRepoUrl] = useState("");
  const [webhookSecret, setWebhookSecret] = useState("");
  const [triggerPR, setTriggerPR] = useState(true);
  const [triggerPush, setTriggerPush] = useState(true);
  const [triggerMerge, setTriggerMerge] = useState(true);
  const [auditSchedule, setAuditSchedule] = useState<AuditSchedule>("none");
  const [customCron, setCustomCron] = useState("");
  const [enrollStep, setEnrollStep] = useState<EnrollStep>("form");
  const [enrollError, setEnrollError] = useState("");
  const [enrolledRepo, setEnrolledRepo] = useState<Repository | null>(null);
  const [repoUrlError, setRepoUrlError] = useState<string | null>(null);

  // Flash audit state
  const [flashUrl, setFlashUrl] = useState("");
  const [flashStep, setFlashStep] = useState<FlashStep>("idle");
  const [flashResult, setFlashResult] = useState<EphemeralScanStatus | null>(null);
  const [flashError, setFlashError] = useState("");
  const [flashProgress, setFlashProgress] = useState(0);
  const [flashUrlError, setFlashUrlError] = useState<string | null>(null);
  const flashPollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const flashProgressRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // Existing repos
  const [repos, setRepos] = useState<Repository[]>([]);

  useEffect(() => {
    api.repositories.list().then(setRepos).catch(() => {});
    return () => {
      if (flashPollRef.current) clearInterval(flashPollRef.current);
      if (flashProgressRef.current) clearInterval(flashProgressRef.current);
    };
  }, []);

  // ── Monitor handlers ─────────────────────────────────────────────────────────

  function resolvedSchedule(): string | null {
    if (auditSchedule === "none") return null;
    if (auditSchedule === "custom") return customCron.trim() || null;
    return auditSchedule;
  }

  async function handleEnroll() {
    setRepoUrlError(null);
    if (!repoUrl.trim()) { setRepoUrlError("Please enter a GitHub repository URL."); return; }
    const parsed = parseGitHubUrl(repoUrl);
    if (!parsed) { setRepoUrlError("Please enter a valid GitHub URL (e.g. https://github.com/owner/repo)."); return; }

    setEnrollStep("loading");
    setEnrollError("");

    const body: EnrollRepositoryRequest = {
      repo_url: repoUrl.trim(),
      webhook_secret: webhookSecret,
      trigger_config: { pull_requests: triggerPR, pushes: triggerPush, merges: triggerMerge },
      audit_schedule: resolvedSchedule(),
      default_branch: "main",
    };

    try {
      const repo = await api.repositories.enroll(body);
      setEnrolledRepo(repo);
      setEnrollStep("success");
      setRepos((prev) => prev.some((r) => r.id === repo.id) ? prev : [repo, ...prev]);
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      if (msg.includes("409")) {
        try {
          const all = await api.repositories.list();
          const existing = all.find((r) => r.full_name === parsed.full_name);
          if (existing) { navigate(`/repo/${existing.id}`); return; }
        } catch {}
        setEnrollError("Repository already registered. Try clicking the card below.");
      } else {
        setEnrollError(msg);
      }
      setEnrollStep("error");
    }
  }

  // ── Flash audit handlers ──────────────────────────────────────────────────────

  function stopFlash() {
    if (flashPollRef.current) { clearInterval(flashPollRef.current); flashPollRef.current = null; }
    if (flashProgressRef.current) { clearInterval(flashProgressRef.current); flashProgressRef.current = null; }
  }

  async function handleFlashScan() {
    setFlashUrlError(null);
    if (!flashUrl.trim()) { setFlashUrlError("Please enter a GitHub repository URL."); return; }
    if (!parseGitHubUrl(flashUrl)) { setFlashUrlError("Please enter a valid GitHub URL."); return; }

    stopFlash();
    setFlashStep("scanning");
    setFlashResult(null);
    setFlashError("");
    setFlashProgress(0);

    // Animate progress bar to 92% over 90s
    let prog = 0;
    flashProgressRef.current = setInterval(() => {
      prog = Math.min(prog + (92 / 90), 92);
      setFlashProgress(prog);
    }, 1000);

    let sessionId: string;
    try {
      const res = await api.scan.startEphemeral(flashUrl.trim());
      sessionId = res.session_id;
    } catch (err: unknown) {
      stopFlash();
      setFlashError(err instanceof Error ? err.message : String(err));
      setFlashStep("error");
      return;
    }

    let attempts = 0;
    flashPollRef.current = setInterval(async () => {
      attempts++;
      if (attempts > 36) { // 3 min timeout
        stopFlash();
        setFlashError("Analysis timed out after 3 minutes. Please try again.");
        setFlashStep("error");
        return;
      }
      try {
        const result = await api.scan.getEphemeralResult(sessionId);
        if (result.status === "complete") {
          stopFlash();
          setFlashProgress(100);
          setFlashResult(result);
          setFlashStep("results");
        } else if (result.status === "failed") {
          stopFlash();
          setFlashError(result.error || "Analysis failed.");
          setFlashStep("error");
        }
      } catch {}
    }, 5000);
  }

  // ── Render ───────────────────────────────────────────────────────────────────

  return (
    <div style={{ minHeight: "100vh", background: "#0f172a", display: "flex", flexDirection: "column", alignItems: "center", padding: "48px 24px 80px" }}>

      {/* Branding */}
      <div style={{ textAlign: "center", marginBottom: 40 }}>
        <div style={{ fontSize: 44, marginBottom: 10 }}>🛡️</div>
        <h1 style={{ margin: 0, fontSize: 34, fontWeight: 800, color: "#f1f5f9", letterSpacing: -1 }}>RepoGuardian</h1>
        <p style={{ margin: "8px 0 0", color: "#64748b", fontSize: 15 }}>Repository Control Center</p>
      </div>

      {/* Tab selector */}
      <div style={{ display: "flex", gap: 8, marginBottom: 24, background: "#1e293b", borderRadius: 12, padding: 6, border: "1px solid #334155" }}>
        {(["monitor", "flash"] as ActiveTab[]).map((tab) => (
          <button
            key={tab}
            onClick={() => setActiveTab(tab)}
            style={{
              padding: "10px 28px", borderRadius: 8, border: "none", cursor: "pointer",
              fontWeight: 600, fontSize: 14,
              background: activeTab === tab ? "#6366f1" : "transparent",
              color: activeTab === tab ? "#fff" : "#64748b",
              transition: "all 0.15s",
            }}
          >
            {tab === "monitor" ? "🔍 Active Sentry" : "⚡ Flash Audit"}
          </button>
        ))}
      </div>

      {/* Panel */}
      <div style={{ width: "100%", maxWidth: 600, background: "#1e293b", borderRadius: 16, padding: 32, border: "1px solid #334155" }}>

        {/* ── Monitor Tab ── */}
        {activeTab === "monitor" && (
          <>
            {enrollStep === "form" && (
              <>
                <h2 style={{ margin: "0 0 6px", color: "#f1f5f9", fontSize: 20, fontWeight: 700 }}>Enroll a Repository</h2>
                <p style={{ margin: "0 0 24px", color: "#64748b", fontSize: 14 }}>
                  Set up continuous monitoring. RepoGuardian will analyze every PR, push, or merge event automatically.
                </p>

                {/* Repo URL */}
                <label style={{ display: "block", color: "#94a3b8", fontSize: 13, fontWeight: 600, marginBottom: 6 }}>Repository URL</label>
                <input
                  autoFocus
                  type="text"
                  value={repoUrl}
                  onChange={(e) => { setRepoUrl(e.target.value); setRepoUrlError(null); }}
                  onKeyDown={(e) => e.key === "Enter" && handleEnroll()}
                  placeholder="https://github.com/owner/repo"
                  style={{
                    width: "100%", boxSizing: "border-box",
                    background: "#0f172a", border: `1.5px solid ${repoUrlError ? "#ef4444" : "#334155"}`,
                    borderRadius: 8, padding: "11px 14px", color: "#f1f5f9", fontSize: 15, outline: "none", marginBottom: 4,
                  }}
                />
                {repoUrlError && <p style={{ margin: "0 0 12px", color: "#ef4444", fontSize: 12 }}>{repoUrlError}</p>}

                {/* Webhook Secret */}
                <label style={{ display: "block", color: "#94a3b8", fontSize: 13, fontWeight: 600, marginBottom: 6, marginTop: 16 }}>Webhook Secret <span style={{ color: "#475569", fontWeight: 400 }}>(optional)</span></label>
                <input
                  type="password"
                  value={webhookSecret}
                  onChange={(e) => setWebhookSecret(e.target.value)}
                  placeholder="Your GitHub webhook secret"
                  style={{
                    width: "100%", boxSizing: "border-box",
                    background: "#0f172a", border: "1.5px solid #334155",
                    borderRadius: 8, padding: "11px 14px", color: "#f1f5f9", fontSize: 15, outline: "none",
                  }}
                />

                {/* Triggers */}
                <div style={{ marginTop: 24, padding: "16px 18px", background: "#0f172a", borderRadius: 10, border: "1px solid #334155" }}>
                  <p style={{ margin: "0 0 12px", color: "#f1f5f9", fontSize: 14, fontWeight: 600 }}>Real-Time Triggers</p>
                  {[
                    ["Pull Request Events", triggerPR, setTriggerPR] as const,
                    ["Push Events", triggerPush, setTriggerPush] as const,
                    ["Merge Events", triggerMerge, setTriggerMerge] as const,
                  ].map(([label, val, setter]) => (
                    <label key={label} style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 10, cursor: "pointer" }}>
                      <input
                        type="checkbox"
                        checked={val}
                        onChange={(e) => setter(e.target.checked)}
                        style={{ width: 16, height: 16, accentColor: "#6366f1" }}
                      />
                      <span style={{ color: "#94a3b8", fontSize: 14 }}>{label}</span>
                    </label>
                  ))}
                </div>

                {/* Schedule */}
                <div style={{ marginTop: 16, padding: "16px 18px", background: "#0f172a", borderRadius: 10, border: "1px solid #334155" }}>
                  <p style={{ margin: "0 0 12px", color: "#f1f5f9", fontSize: 14, fontWeight: 600 }}>Periodic Audits</p>
                  <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
                    {(["none", "hourly", "daily", "weekly", "custom"] as AuditSchedule[]).map((s) => (
                      <button
                        key={s}
                        onClick={() => setAuditSchedule(s)}
                        style={{
                          padding: "6px 14px", borderRadius: 8, border: "none", cursor: "pointer",
                          fontSize: 13, fontWeight: 600,
                          background: auditSchedule === s ? "#6366f1" : "#1e293b",
                          color: auditSchedule === s ? "#fff" : "#64748b",
                          border: `1.5px solid ${auditSchedule === s ? "#6366f1" : "#334155"}`,
                        }}
                      >
                        {s.charAt(0).toUpperCase() + s.slice(1)}
                      </button>
                    ))}
                  </div>
                  {auditSchedule === "custom" && (
                    <input
                      type="text"
                      value={customCron}
                      onChange={(e) => setCustomCron(e.target.value)}
                      placeholder="0 9 * * 1  (min hour dom month dow)"
                      style={{
                        width: "100%", boxSizing: "border-box", marginTop: 12,
                        background: "#1e293b", border: "1.5px solid #334155",
                        borderRadius: 8, padding: "9px 14px", color: "#f1f5f9", fontSize: 13, outline: "none",
                        fontFamily: "monospace",
                      }}
                    />
                  )}
                </div>

                <button
                  onClick={handleEnroll}
                  style={{
                    width: "100%", marginTop: 24,
                    background: "#6366f1", color: "#fff",
                    border: "none", borderRadius: 10, padding: "13px 0",
                    fontSize: 15, fontWeight: 700, cursor: "pointer",
                  }}
                >
                  Enroll Repository →
                </button>
              </>
            )}

            {enrollStep === "loading" && (
              <div style={{ textAlign: "center", padding: "32px 0" }}>
                <div style={{ width: 40, height: 40, borderRadius: "50%", border: "3px solid #334155", borderTopColor: "#6366f1", animation: "spin 0.8s linear infinite", margin: "0 auto 20px" }} />
                <p style={{ color: "#94a3b8", margin: 0, fontSize: 15 }}>Enrolling repository…</p>
              </div>
            )}

            {enrollStep === "success" && enrolledRepo && (
              <div style={{ textAlign: "center" }}>
                <div style={{ fontSize: 48, marginBottom: 16 }}>✅</div>
                <h3 style={{ margin: "0 0 8px", color: "#f1f5f9", fontSize: 20 }}>Repository Enrolled!</h3>
                <p style={{ margin: "0 0 24px", color: "#64748b", fontSize: 14 }}>{enrolledRepo.full_name} is now under continuous monitoring.</p>
                <div style={{ display: "flex", gap: 10 }}>
                  <button
                    onClick={() => navigate(`/repo/${enrolledRepo.id}`)}
                    style={{ flex: 1, background: "#6366f1", color: "#fff", border: "none", borderRadius: 8, padding: "12px 0", fontSize: 15, fontWeight: 600, cursor: "pointer" }}
                  >
                    View Dashboard →
                  </button>
                  <button
                    onClick={() => { setEnrollStep("form"); setRepoUrl(""); setWebhookSecret(""); setAuditSchedule("none"); }}
                    style={{ flex: 1, background: "#334155", color: "#94a3b8", border: "none", borderRadius: 8, padding: "12px 0", fontSize: 15, fontWeight: 600, cursor: "pointer" }}
                  >
                    Enroll Another
                  </button>
                </div>
              </div>
            )}

            {enrollStep === "error" && (
              <>
                <div style={{ background: "#450a0a", border: "1px solid #ef4444", borderRadius: 10, padding: "16px 20px", marginBottom: 20 }}>
                  <p style={{ margin: 0, color: "#ef4444", fontSize: 14 }}>{enrollError}</p>
                </div>
                <button
                  onClick={() => setEnrollStep("form")}
                  style={{ width: "100%", background: "#334155", color: "#f1f5f9", border: "none", borderRadius: 8, padding: "12px 0", fontSize: 15, fontWeight: 600, cursor: "pointer" }}
                >
                  Try Again
                </button>
              </>
            )}
          </>
        )}

        {/* ── Flash Audit Tab ── */}
        {activeTab === "flash" && (
          <>
            <h2 style={{ margin: "0 0 6px", color: "#f1f5f9", fontSize: 20, fontWeight: 700 }}>Flash Audit</h2>
            <p style={{ margin: "0 0 24px", color: "#64748b", fontSize: 14 }}>
              Scan any public GitHub repo on-demand. No registration required — results are ephemeral.
            </p>

            <div style={{ display: "flex", gap: 10 }}>
              <input
                type="text"
                value={flashUrl}
                onChange={(e) => { setFlashUrl(e.target.value); setFlashUrlError(null); }}
                onKeyDown={(e) => e.key === "Enter" && handleFlashScan()}
                placeholder="https://github.com/owner/repo"
                disabled={flashStep === "scanning"}
                style={{
                  flex: 1, background: "#0f172a",
                  border: `1.5px solid ${flashUrlError ? "#ef4444" : "#334155"}`,
                  borderRadius: 8, padding: "11px 14px", color: "#f1f5f9", fontSize: 15, outline: "none",
                  opacity: flashStep === "scanning" ? 0.6 : 1,
                }}
              />
              <button
                onClick={handleFlashScan}
                disabled={flashStep === "scanning"}
                style={{
                  background: "#6366f1", color: "#fff", border: "none",
                  borderRadius: 8, padding: "11px 22px", fontSize: 15,
                  fontWeight: 600, cursor: flashStep === "scanning" ? "not-allowed" : "pointer",
                  whiteSpace: "nowrap", opacity: flashStep === "scanning" ? 0.7 : 1,
                }}
              >
                {flashStep === "scanning" ? "Analyzing…" : "Analyze Now →"}
              </button>
            </div>
            {flashUrlError && <p style={{ margin: "8px 0 0", color: "#ef4444", fontSize: 12 }}>{flashUrlError}</p>}

            {/* Progress bar */}
            {flashStep === "scanning" && (
              <div style={{ marginTop: 24 }}>
                <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 6 }}>
                  <span style={{ color: "#94a3b8", fontSize: 13 }}>Running AI analysis across 10 agents…</span>
                  <span style={{ color: "#6366f1", fontSize: 13, fontWeight: 600 }}>{Math.round(flashProgress)}%</span>
                </div>
                <div style={{ background: "#0f172a", borderRadius: 8, height: 8, overflow: "hidden" }}>
                  <div style={{
                    height: "100%", borderRadius: 8,
                    background: "linear-gradient(90deg, #6366f1, #818cf8)",
                    width: `${flashProgress}%`,
                    transition: "width 0.8s ease",
                  }} />
                </div>
                <p style={{ margin: "8px 0 0", color: "#475569", fontSize: 12, textAlign: "center" }}>
                  Dashboard will update automatically when complete
                </p>
              </div>
            )}

            {/* Error */}
            {flashStep === "error" && (
              <div style={{ marginTop: 20 }}>
                <div style={{ background: "#450a0a", border: "1px solid #ef4444", borderRadius: 10, padding: "16px 20px", marginBottom: 16 }}>
                  <p style={{ margin: 0, color: "#ef4444", fontSize: 14 }}>{flashError}</p>
                </div>
                <button
                  onClick={() => { setFlashStep("idle"); setFlashError(""); }}
                  style={{ width: "100%", background: "#334155", color: "#f1f5f9", border: "none", borderRadius: 8, padding: "11px 0", fontSize: 14, fontWeight: 600, cursor: "pointer" }}
                >
                  Try Again
                </button>
              </div>
            )}

            {/* Results */}
            {flashStep === "results" && flashResult && (
              <div style={{ marginTop: 24 }}>
                {/* Summary card */}
                <div style={{ background: "#0f172a", borderRadius: 12, padding: "20px", border: "1px solid #334155", marginBottom: 20 }}>
                  <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", flexWrap: "wrap", gap: 12 }}>
                    <div>
                      <p style={{ margin: "0 0 4px", color: "#64748b", fontSize: 12, textTransform: "uppercase", letterSpacing: 1 }}>Verdict</p>
                      <p style={{ margin: 0, color: flashResult.overall_verdict === "APPROVE" ? "#22c55e" : "#f97316", fontWeight: 700, fontSize: 18 }}>
                        {flashResult.overall_verdict ?? "—"}
                      </p>
                    </div>
                    <div style={{ textAlign: "right" }}>
                      <p style={{ margin: "0 0 4px", color: "#64748b", fontSize: 12, textTransform: "uppercase", letterSpacing: 1 }}>Score Impact</p>
                      <p style={{ margin: 0, fontWeight: 700, fontSize: 18, color: (flashResult.health_score_delta ?? 0) >= 0 ? "#22c55e" : "#ef4444" }}>
                        {flashResult.health_score_delta !== null
                          ? `${flashResult.health_score_delta >= 0 ? "+" : ""}${flashResult.health_score_delta.toFixed(1)}`
                          : "—"}
                      </p>
                    </div>
                    <div style={{ textAlign: "right" }}>
                      <p style={{ margin: "0 0 4px", color: "#64748b", fontSize: 12, textTransform: "uppercase", letterSpacing: 1 }}>Findings</p>
                      <p style={{ margin: 0, color: "#f1f5f9", fontWeight: 700, fontSize: 18 }}>{flashResult.finding_count}</p>
                    </div>
                  </div>
                  {flashResult.pr_summary && (
                    <p style={{ margin: "16px 0 0", color: "#94a3b8", fontSize: 13, lineHeight: 1.6, borderTop: "1px solid #334155", paddingTop: 12 }}>
                      {flashResult.pr_summary.slice(0, 400)}{flashResult.pr_summary.length > 400 ? "…" : ""}
                    </p>
                  )}
                </div>

                {/* Findings list */}
                {flashResult.findings.length > 0 ? (
                  <>
                    <p style={{ margin: "0 0 12px", color: "#94a3b8", fontSize: 13, fontWeight: 600 }}>
                      Active Findings ({flashResult.findings.length})
                    </p>
                    {flashResult.findings.map((f, i) => (
                      <EphemeralFindingRow key={i} f={f} />
                    ))}
                  </>
                ) : (
                  <div style={{ textAlign: "center", padding: "24px 0", color: "#64748b" }}>
                    <p style={{ fontSize: 32, margin: "0 0 8px" }}>✅</p>
                    <p style={{ margin: 0, fontSize: 14 }}>No issues found!</p>
                  </div>
                )}

                <button
                  onClick={() => { setFlashStep("idle"); setFlashResult(null); setFlashUrl(""); setFlashProgress(0); }}
                  style={{ width: "100%", marginTop: 16, background: "#334155", color: "#94a3b8", border: "none", borderRadius: 8, padding: "11px 0", fontSize: 14, fontWeight: 600, cursor: "pointer" }}
                >
                  Scan Another Repo
                </button>
              </div>
            )}
          </>
        )}
      </div>

      {/* Existing repos */}
      {repos.length > 0 && (
        <div style={{ width: "100%", maxWidth: 600, marginTop: 48 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 20 }}>
            <div style={{ flex: 1, height: 1, background: "#1e293b" }} />
            <span style={{ color: "#475569", fontSize: 13, whiteSpace: "nowrap" }}>or pick an existing repository</span>
            <div style={{ flex: 1, height: 1, background: "#1e293b" }} />
          </div>
          <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
            {repos.map((repo) => (
              <div
                key={repo.id}
                onClick={() => navigate(`/repo/${repo.id}`)}
                style={{ background: "#1e293b", border: "1px solid #334155", borderRadius: 12, padding: "16px 20px", cursor: "pointer", display: "flex", alignItems: "center", justifyContent: "space-between", transition: "border-color 0.15s" }}
                onMouseEnter={(e) => (e.currentTarget.style.borderColor = "#6366f1")}
                onMouseLeave={(e) => (e.currentTarget.style.borderColor = "#334155")}
              >
                <div>
                  <p style={{ margin: 0, color: "#f1f5f9", fontWeight: 600, fontSize: 15 }}>{repo.full_name}</p>
                  <p style={{ margin: "3px 0 0", color: "#64748b", fontSize: 13 }}>
                    {repo.platform} · {repo.default_branch}
                    {repo.primary_language ? ` · ${repo.primary_language}` : ""}
                  </p>
                </div>
                <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
                  <ScoreBadge repoId={repo.id} />
                  <span style={{ color: "#475569", fontSize: 18 }}>›</span>
                </div>
              </div>
            ))}
          </div>
          <p style={{ textAlign: "center", marginTop: 16 }}>
            <Link to="/repos" style={{ color: "#475569", fontSize: 13, textDecoration: "none" }}>View all repositories →</Link>
          </p>
        </div>
      )}

      <style>{`@keyframes spin { to { transform: rotate(360deg); } }`}</style>
    </div>
  );
};
