import React, { useEffect, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import {
  api,
  BASE_URL,
  EnrollRepositoryRequest,
  EphemeralFinding,
  EphemeralScanStatus,
  Repository,
} from "../api/client";
import { T } from "../theme";

// ── URL parser ────────────────────────────────────────────────────────────────

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
  A: T.success, B: "#84cc16", C: T.warning, D: "#f97316", F: T.error,
};

function ScoreBadge({ repoId }: { repoId: string }) {
  const [score, setScore] = useState<{ overall_score: number; grade: string } | null>(null);
  useEffect(() => { api.health.score(repoId).then(setScore).catch(() => {}); }, [repoId]);
  if (!score) return null;
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 6, background: T.bg, borderRadius: 8, padding: "2px 10px" }}>
      <span style={{ color: gradeColor[score.grade] ?? T.textMuted, fontWeight: 700, fontSize: 15 }}>{score.grade}</span>
      <span style={{ color: T.textDim, fontSize: 13 }}>{Math.round(score.overall_score)}</span>
    </span>
  );
}

// ── Severity colours ──────────────────────────────────────────────────────────

const sevColor: Record<string, string> = {
  CRITICAL: T.critical, HIGH: T.high, MEDIUM: T.medium, LOW: T.low, INFO: T.info,
};

// ── Agent status dot ─────────────────────────────────────────────────────────

function AgentDot({ status }: { status: string }) {
  const color =
    status === "complete" ? T.success :
    status === "failed"   ? T.error :
    status === "skipped"  ? T.textDim :
    status === "running"  ? T.warning : T.border;

  return (
    <span style={{
      display: "inline-block", width: 10, height: 10, borderRadius: "50%",
      background: color,
      animation: status === "running" ? "pulse-dot 1s ease-in-out infinite" : "none",
      flexShrink: 0,
    }} />
  );
}

const AGENT_LABELS: Record<string, string> = {
  security_scanner:   "Security Scanner",
  code_quality:       "Code Quality",
  dependency_auditor: "Dependency Auditor",
  doc_verifier:       "Doc Verifier",
};

// ── Ephemeral finding row ─────────────────────────────────────────────────────

function EphemeralFindingRow({ f }: { f: EphemeralFinding }) {
  const [open, setOpen] = useState(false);
  return (
    <div style={{
      background: T.bg, border: `1px solid ${T.border}`, borderRadius: 10,
      marginBottom: 8, overflow: "hidden",
    }}>
      <div
        onClick={() => setOpen(!open)}
        style={{ display: "flex", alignItems: "center", gap: 12, padding: "12px 16px", cursor: "pointer" }}
      >
        <span style={{
          background: (sevColor[f.severity] ?? T.info) + "22",
          color: sevColor[f.severity] ?? T.info,
          border: `1px solid ${(sevColor[f.severity] ?? T.info)}44`,
          borderRadius: 6, padding: "2px 8px", fontSize: 11, fontWeight: 700, whiteSpace: "nowrap",
        }}>{f.severity}</span>
        <div style={{ flex: 1, minWidth: 0 }}>
          <p style={{ margin: 0, color: T.text, fontSize: 14, fontWeight: 600 }}>{f.title}</p>
          {f.file_path && (
            <p style={{ margin: "2px 0 0", color: T.textDim, fontSize: 12, fontFamily: "monospace" }}>
              {f.file_path}{f.line_start ? `:${f.line_start}` : ""}
            </p>
          )}
        </div>
        <span style={{ color: T.textDim, fontSize: 13 }}>{open ? "▲" : "▼"}</span>
      </div>
      {open && (
        <div style={{ padding: "0 16px 16px", borderTop: `1px solid ${T.border}` }}>
          <p style={{ margin: "12px 0 8px", color: T.textMuted, fontSize: 13, lineHeight: 1.6 }}>{f.description}</p>
          {f.evidence && (
            <pre style={{
              background: T.surface, border: `1px solid ${T.border}`, borderRadius: 8,
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
          <p style={{ margin: "8px 0 0", color: T.textDim, fontSize: 11 }}>
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
  const [currentStep, setCurrentStep] = useState<string | null>(null);
  const [agentStatuses, setAgentStatuses] = useState<Record<string, string>>({});
  const [flashUrlError, setFlashUrlError] = useState<string | null>(null);
  const esRef = useRef<EventSource | null>(null);

  // Existing repos
  const [repos, setRepos] = useState<Repository[]>([]);

  useEffect(() => {
    api.repositories.list().then(setRepos).catch(() => {});
    return () => { esRef.current?.close(); };
  }, []);

  // ── Monitor handlers ──────────────────────────────────────────────────────

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

  // ── Flash audit handlers — SSE ────────────────────────────────────────────

  function stopFlash() {
    esRef.current?.close();
    esRef.current = null;
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
    setCurrentStep("Starting analysis…");
    setAgentStatuses({});

    let sessionId: string;
    try {
      const res = await api.scan.startEphemeral(flashUrl.trim());
      sessionId = res.session_id;
    } catch (err: unknown) {
      setFlashError(err instanceof Error ? err.message : String(err));
      setFlashStep("error");
      return;
    }

    // Open SSE stream
    const es = new EventSource(`${BASE_URL}/api/scan/ephemeral/${sessionId}/stream`);
    esRef.current = es;

    es.onmessage = (event) => {
      try {
        const status: EphemeralScanStatus = JSON.parse(event.data);
        setFlashProgress(status.progress_percent ?? 0);
        setCurrentStep(status.current_step ?? null);
        setAgentStatuses(status.agent_statuses ?? {});

        if (status.status === "complete") {
          setFlashProgress(100);
          setFlashResult(status);
          setFlashStep("results");
          es.close();
        } else if (status.status === "failed") {
          setFlashError(status.error || "Analysis failed.");
          setFlashStep("error");
          es.close();
        }
      } catch {}
    };

    es.addEventListener("done", () => { es.close(); });
    es.addEventListener("timeout", () => {
      es.close();
      setFlashError("Analysis timed out after 3 minutes. Please try again.");
      setFlashStep("error");
    });
    es.addEventListener("error", (e: Event) => {
      const data = (e as MessageEvent).data;
      try {
        const parsed = JSON.parse(data);
        if (parsed.error === "session_not_found") {
          setFlashError("Scan session not found or expired.");
          setFlashStep("error");
        }
      } catch {}
      es.close();
    });

    es.onerror = () => {
      // Connection dropped — don't show error if scan already complete
      if (flashStep !== "results") {
        es.close();
      }
    };
  }

  // ── Render ────────────────────────────────────────────────────────────────

  return (
    <div style={{ minHeight: "100vh", background: T.bg, display: "flex", flexDirection: "column", alignItems: "center", padding: "48px 24px 80px" }}>

      {/* Branding */}
      <div style={{ textAlign: "center", marginBottom: 40 }}>
        <div style={{ fontSize: 44, marginBottom: 10 }}>🛡️</div>
        <h1 style={{ margin: 0, fontSize: 34, fontWeight: 800, color: T.text, letterSpacing: -1 }}>RepoGuardian</h1>
        <p style={{ margin: "8px 0 0", color: T.textDim, fontSize: 15 }}>Repository Control Center</p>
      </div>

      {/* Tab selector */}
      <div style={{ display: "flex", gap: 8, marginBottom: 24, background: T.surface, borderRadius: 12, padding: 6, border: `1px solid ${T.border}` }}>
        {(["monitor", "flash"] as ActiveTab[]).map((tab) => (
          <button
            key={tab}
            onClick={() => setActiveTab(tab)}
            style={{
              padding: "10px 28px", borderRadius: 8, border: "none", cursor: "pointer",
              fontWeight: 600, fontSize: 14,
              background: activeTab === tab ? T.accent : "transparent",
              color: activeTab === tab ? "#fff" : T.textDim,
              transition: "all 0.15s",
            }}
          >
            {tab === "monitor" ? "🔍 Active Sentry" : "⚡ Flash Audit"}
          </button>
        ))}
      </div>

      {/* Panel */}
      <div style={{ width: "100%", maxWidth: 600, background: T.surface, borderRadius: 16, padding: 32, border: `1px solid ${T.border}` }}>

        {/* ── Monitor Tab ── */}
        {activeTab === "monitor" && (
          <>
            {enrollStep === "form" && (
              <>
                <h2 style={{ margin: "0 0 6px", color: T.text, fontSize: 20, fontWeight: 700 }}>Enroll a Repository</h2>
                <p style={{ margin: "0 0 24px", color: T.textDim, fontSize: 14 }}>
                  Set up continuous monitoring. RepoGuardian will analyze every PR, push, or merge event automatically.
                </p>

                <label style={{ display: "block", color: T.textMuted, fontSize: 13, fontWeight: 600, marginBottom: 6 }}>Repository URL</label>
                <input
                  autoFocus
                  type="text"
                  value={repoUrl}
                  onChange={(e) => { setRepoUrl(e.target.value); setRepoUrlError(null); }}
                  onKeyDown={(e) => e.key === "Enter" && handleEnroll()}
                  placeholder="https://github.com/owner/repo"
                  style={{
                    width: "100%", boxSizing: "border-box",
                    background: T.bg, border: `1.5px solid ${repoUrlError ? T.error : T.border}`,
                    borderRadius: 8, padding: "11px 14px", color: T.text, fontSize: 15, outline: "none", marginBottom: 4,
                  }}
                />
                {repoUrlError && <p style={{ margin: "0 0 12px", color: T.error, fontSize: 12 }}>{repoUrlError}</p>}

                <label style={{ display: "block", color: T.textMuted, fontSize: 13, fontWeight: 600, marginBottom: 6, marginTop: 16 }}>Webhook Secret <span style={{ color: T.textDim, fontWeight: 400 }}>(optional)</span></label>
                <input
                  type="password"
                  value={webhookSecret}
                  onChange={(e) => setWebhookSecret(e.target.value)}
                  placeholder="Your GitHub webhook secret"
                  style={{
                    width: "100%", boxSizing: "border-box",
                    background: T.bg, border: `1.5px solid ${T.border}`,
                    borderRadius: 8, padding: "11px 14px", color: T.text, fontSize: 15, outline: "none",
                  }}
                />

                <div style={{ marginTop: 24, padding: "16px 18px", background: T.bg, borderRadius: 10, border: `1px solid ${T.border}` }}>
                  <p style={{ margin: "0 0 12px", color: T.text, fontSize: 14, fontWeight: 600 }}>Real-Time Triggers</p>
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
                        style={{ width: 16, height: 16, accentColor: T.accent }}
                      />
                      <span style={{ color: T.textMuted, fontSize: 14 }}>{label}</span>
                    </label>
                  ))}
                </div>

                <div style={{ marginTop: 16, padding: "16px 18px", background: T.bg, borderRadius: 10, border: `1px solid ${T.border}` }}>
                  <p style={{ margin: "0 0 12px", color: T.text, fontSize: 14, fontWeight: 600 }}>Periodic Audits</p>
                  <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
                    {(["none", "hourly", "daily", "weekly", "custom"] as AuditSchedule[]).map((s) => (
                      <button
                        key={s}
                        onClick={() => setAuditSchedule(s)}
                        style={{
                          padding: "6px 14px", borderRadius: 8, cursor: "pointer",
                          fontSize: 13, fontWeight: 600,
                          background: auditSchedule === s ? T.accentGlow : T.surface,
                          color: auditSchedule === s ? T.accent : T.textDim,
                          border: `1.5px solid ${auditSchedule === s ? T.accent : T.border}`,
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
                        background: T.surface, border: `1.5px solid ${T.border}`,
                        borderRadius: 8, padding: "9px 14px", color: T.text, fontSize: 13, outline: "none",
                        fontFamily: "monospace",
                      }}
                    />
                  )}
                </div>

                <button
                  onClick={handleEnroll}
                  style={{
                    width: "100%", marginTop: 24,
                    background: T.accent, color: "#fff",
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
                <div style={{ width: 40, height: 40, borderRadius: "50%", border: `3px solid ${T.border}`, borderTopColor: T.accent, animation: "spin 0.8s linear infinite", margin: "0 auto 20px" }} />
                <p style={{ color: T.textMuted, margin: 0, fontSize: 15 }}>Enrolling repository…</p>
              </div>
            )}

            {enrollStep === "success" && enrolledRepo && (
              <div style={{ textAlign: "center" }}>
                <div style={{ fontSize: 48, marginBottom: 16 }}>✅</div>
                <h3 style={{ margin: "0 0 8px", color: T.text, fontSize: 20 }}>Repository Enrolled!</h3>
                <p style={{ margin: "0 0 24px", color: T.textDim, fontSize: 14 }}>{enrolledRepo.full_name} is now under continuous monitoring.</p>
                <div style={{ display: "flex", gap: 10 }}>
                  <button
                    onClick={() => navigate(`/repo/${enrolledRepo.id}`)}
                    style={{ flex: 1, background: T.accent, color: "#fff", border: "none", borderRadius: 8, padding: "12px 0", fontSize: 15, fontWeight: 600, cursor: "pointer" }}
                  >
                    View Dashboard →
                  </button>
                  <button
                    onClick={() => { setEnrollStep("form"); setRepoUrl(""); setWebhookSecret(""); setAuditSchedule("none"); }}
                    style={{ flex: 1, background: T.border, color: T.textMuted, border: "none", borderRadius: 8, padding: "12px 0", fontSize: 15, fontWeight: 600, cursor: "pointer" }}
                  >
                    Enroll Another
                  </button>
                </div>
              </div>
            )}

            {enrollStep === "error" && (
              <>
                <div style={{ background: "#450a0a", border: `1px solid ${T.error}`, borderRadius: 10, padding: "16px 20px", marginBottom: 20 }}>
                  <p style={{ margin: 0, color: T.error, fontSize: 14 }}>{enrollError}</p>
                </div>
                <button
                  onClick={() => setEnrollStep("form")}
                  style={{ width: "100%", background: T.border, color: T.text, border: "none", borderRadius: 8, padding: "12px 0", fontSize: 15, fontWeight: 600, cursor: "pointer" }}
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
            <h2 style={{ margin: "0 0 6px", color: T.text, fontSize: 20, fontWeight: 700 }}>Flash Audit</h2>
            <p style={{ margin: "0 0 24px", color: T.textDim, fontSize: 14 }}>
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
                  flex: 1, background: T.bg,
                  border: `1.5px solid ${flashUrlError ? T.error : T.border}`,
                  borderRadius: 8, padding: "11px 14px", color: T.text, fontSize: 15, outline: "none",
                  opacity: flashStep === "scanning" ? 0.6 : 1,
                }}
              />
              <button
                onClick={handleFlashScan}
                disabled={flashStep === "scanning"}
                style={{
                  background: flashStep === "scanning" ? T.accentDim : T.accent,
                  color: "#fff", border: "none",
                  borderRadius: 8, padding: "11px 22px", fontSize: 15,
                  fontWeight: 600, cursor: flashStep === "scanning" ? "not-allowed" : "pointer",
                  whiteSpace: "nowrap",
                }}
              >
                {flashStep === "scanning" ? "Analyzing…" : "Analyze Now →"}
              </button>
            </div>
            {flashUrlError && <p style={{ margin: "8px 0 0", color: T.error, fontSize: 12 }}>{flashUrlError}</p>}

            {/* Live SSE progress panel */}
            {flashStep === "scanning" && (
              <div style={{ marginTop: 24, background: T.bg, borderRadius: 12, border: `1px solid ${T.border}`, padding: "20px 22px" }}>
                {/* Header */}
                <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 16 }}>
                  <div style={{ width: 8, height: 8, borderRadius: "50%", background: T.accent, animation: "pulse-dot 1s ease-in-out infinite" }} />
                  <span style={{ color: T.text, fontWeight: 600, fontSize: 14 }}>Flash Audit in Progress</span>
                </div>

                {/* Current step label */}
                {currentStep && (
                  <p style={{ margin: "0 0 10px", color: T.textMuted, fontSize: 13 }}>{currentStep}</p>
                )}

                {/* Progress bar */}
                <div style={{ marginBottom: 18 }}>
                  <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 6 }}>
                    <span style={{ color: T.textDim, fontSize: 12 }}>Analyzing…</span>
                    <span style={{ color: T.accent, fontSize: 12, fontWeight: 700 }}>{Math.round(flashProgress)}%</span>
                  </div>
                  <div style={{ background: T.surface, borderRadius: 8, height: 8, overflow: "hidden", border: `1px solid ${T.border}` }}>
                    <div style={{
                      height: "100%", borderRadius: 8,
                      background: `linear-gradient(90deg, ${T.accent}, ${T.accentDim})`,
                      width: `${flashProgress}%`,
                      transition: "width 0.6s ease",
                    }} />
                  </div>
                </div>

                {/* Agent status rows */}
                {Object.keys(agentStatuses).length > 0 && (
                  <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                    {Object.entries(agentStatuses).map(([agent, status]) => (
                      <div key={agent} style={{ display: "flex", alignItems: "center", gap: 10 }}>
                        <AgentDot status={status} />
                        <span style={{ color: T.textMuted, fontSize: 13, flex: 1 }}>
                          {AGENT_LABELS[agent] ?? agent}
                        </span>
                        <span style={{
                          fontSize: 11, fontWeight: 600,
                          color: status === "complete" ? T.success : status === "failed" ? T.error : status === "skipped" ? T.textDim : T.warning,
                          textTransform: "capitalize",
                        }}>
                          {status}
                        </span>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )}

            {/* Error */}
            {flashStep === "error" && (
              <div style={{ marginTop: 20 }}>
                <div style={{ background: "#450a0a", border: `1px solid ${T.error}`, borderRadius: 10, padding: "16px 20px", marginBottom: 16 }}>
                  <p style={{ margin: 0, color: T.error, fontSize: 14 }}>{flashError}</p>
                </div>
                <button
                  onClick={() => { setFlashStep("idle"); setFlashError(""); }}
                  style={{ width: "100%", background: T.border, color: T.text, border: "none", borderRadius: 8, padding: "11px 0", fontSize: 14, fontWeight: 600, cursor: "pointer" }}
                >
                  Try Again
                </button>
              </div>
            )}

            {/* Results */}
            {flashStep === "results" && flashResult && (
              <div style={{ marginTop: 24 }}>
                <div style={{ background: T.bg, borderRadius: 12, padding: 20, border: `1px solid ${T.border}`, marginBottom: 20 }}>
                  <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", flexWrap: "wrap", gap: 12 }}>
                    <div>
                      <p style={{ margin: "0 0 4px", color: T.textDim, fontSize: 12, textTransform: "uppercase", letterSpacing: 1 }}>Verdict</p>
                      <p style={{ margin: 0, color: flashResult.overall_verdict === "APPROVE" ? T.success : T.warning, fontWeight: 700, fontSize: 18 }}>
                        {flashResult.overall_verdict ?? "—"}
                      </p>
                    </div>
                    <div style={{ textAlign: "right" }}>
                      <p style={{ margin: "0 0 4px", color: T.textDim, fontSize: 12, textTransform: "uppercase", letterSpacing: 1 }}>Score Impact</p>
                      <p style={{ margin: 0, fontWeight: 700, fontSize: 18, color: (flashResult.health_score_delta ?? 0) >= 0 ? T.success : T.error }}>
                        {flashResult.health_score_delta !== null
                          ? `${flashResult.health_score_delta >= 0 ? "+" : ""}${flashResult.health_score_delta.toFixed(1)}`
                          : "—"}
                      </p>
                    </div>
                    <div style={{ textAlign: "right" }}>
                      <p style={{ margin: "0 0 4px", color: T.textDim, fontSize: 12, textTransform: "uppercase", letterSpacing: 1 }}>Findings</p>
                      <p style={{ margin: 0, color: T.text, fontWeight: 700, fontSize: 18 }}>{flashResult.finding_count}</p>
                    </div>
                  </div>
                  {flashResult.pr_summary && (
                    <p style={{ margin: "16px 0 0", color: T.textMuted, fontSize: 13, lineHeight: 1.6, borderTop: `1px solid ${T.border}`, paddingTop: 12 }}>
                      {flashResult.pr_summary.slice(0, 400)}{flashResult.pr_summary.length > 400 ? "…" : ""}
                    </p>
                  )}
                </div>

                {flashResult.findings.length > 0 ? (
                  <>
                    <p style={{ margin: "0 0 12px", color: T.textMuted, fontSize: 13, fontWeight: 600 }}>
                      Findings ({flashResult.findings.length})
                    </p>
                    {flashResult.findings.map((f, i) => <EphemeralFindingRow key={i} f={f} />)}
                  </>
                ) : (
                  <div style={{ textAlign: "center", padding: "24px 0", color: T.textDim }}>
                    <p style={{ fontSize: 32, margin: "0 0 8px" }}>✅</p>
                    <p style={{ margin: 0, fontSize: 14 }}>No issues found!</p>
                  </div>
                )}

                <button
                  onClick={() => { setFlashStep("idle"); setFlashResult(null); setFlashUrl(""); setFlashProgress(0); setAgentStatuses({}); }}
                  style={{ width: "100%", marginTop: 16, background: T.border, color: T.textMuted, border: "none", borderRadius: 8, padding: "11px 0", fontSize: 14, fontWeight: 600, cursor: "pointer" }}
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
            <div style={{ flex: 1, height: 1, background: T.surface }} />
            <span style={{ color: T.textDim, fontSize: 13, whiteSpace: "nowrap" }}>or pick an existing repository</span>
            <div style={{ flex: 1, height: 1, background: T.surface }} />
          </div>
          <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
            {repos.map((repo) => (
              <div
                key={repo.id}
                onClick={() => navigate(`/repo/${repo.id}`)}
                style={{ background: T.surface, border: `1px solid ${T.border}`, borderRadius: 12, padding: "16px 20px", cursor: "pointer", display: "flex", alignItems: "center", justifyContent: "space-between", transition: "border-color 0.15s" }}
                onMouseEnter={(e) => (e.currentTarget.style.borderColor = T.accent)}
                onMouseLeave={(e) => (e.currentTarget.style.borderColor = T.border)}
              >
                <div>
                  <p style={{ margin: 0, color: T.text, fontWeight: 600, fontSize: 15 }}>{repo.full_name}</p>
                  <p style={{ margin: "3px 0 0", color: T.textDim, fontSize: 13 }}>
                    {repo.platform} · {repo.default_branch}
                    {repo.primary_language ? ` · ${repo.primary_language}` : ""}
                  </p>
                </div>
                <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
                  <ScoreBadge repoId={repo.id} />
                  <span style={{ color: T.textDim, fontSize: 18 }}>›</span>
                </div>
              </div>
            ))}
          </div>
          <p style={{ textAlign: "center", marginTop: 16 }}>
            <Link to="/repos" style={{ color: T.textDim, fontSize: 13, textDecoration: "none" }}>View all repositories →</Link>
          </p>
        </div>
      )}
    </div>
  );
};
