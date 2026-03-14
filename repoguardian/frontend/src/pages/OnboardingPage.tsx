import React, { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api, Repository, RepositoryCreateBody } from "../api/client";

// ── URL parser ───────────────────────────────────────────────────────────────

interface ParsedRepo {
  owner: string;
  name: string;
  full_name: string;
  clone_url: string;
}

function parseGitHubUrl(raw: string): ParsedRepo | null {
  try {
    const cleaned = raw.trim().replace(/\.git$/, "");
    const url = new URL(
      cleaned.startsWith("http") ? cleaned : `https://${cleaned}`
    );
    if (!url.hostname.includes("github.com")) return null;
    const parts = url.pathname.replace(/^\//, "").split("/").filter(Boolean);
    if (parts.length < 2) return null;
    const [owner, name] = parts;
    return {
      owner,
      name,
      full_name: `${owner}/${name}`,
      clone_url: `https://github.com/${owner}/${name}.git`,
    };
  } catch {
    return null;
  }
}

// ── Score badge ──────────────────────────────────────────────────────────────

const gradeColor: Record<string, string> = {
  A: "#22c55e",
  B: "#84cc16",
  C: "#eab308",
  D: "#f97316",
  F: "#ef4444",
};

function ScoreBadge({ repoId }: { repoId: string }) {
  const [score, setScore] = useState<{ overall_score: number; grade: string } | null>(null);

  useEffect(() => {
    api.health.score(repoId).then(setScore).catch(() => {});
  }, [repoId]);

  if (!score) return null;

  return (
    <span style={{
      display: "inline-flex", alignItems: "center", gap: 6,
      background: "#0f172a", borderRadius: 8, padding: "2px 10px",
    }}>
      <span style={{ color: gradeColor[score.grade] ?? "#94a3b8", fontWeight: 700, fontSize: 15 }}>
        {score.grade}
      </span>
      <span style={{ color: "#64748b", fontSize: 13 }}>{Math.round(score.overall_score)}</span>
    </span>
  );
}

// ── Main component ───────────────────────────────────────────────────────────

type Step = "input" | "confirm" | "loading" | "error";

export const OnboardingPage: React.FC = () => {
  const navigate = useNavigate();

  // URL input state
  const [urlInput, setUrlInput] = useState("");
  const [urlError, setUrlError] = useState<string | null>(null);
  const [parsed, setParsed] = useState<ParsedRepo | null>(null);
  const [step, setStep] = useState<Step>("input");
  const [errorMsg, setErrorMsg] = useState("");

  // Existing repos
  const [repos, setRepos] = useState<Repository[]>([]);

  useEffect(() => {
    api.repositories.list().then(setRepos).catch(() => {});
  }, []);

  // ── Handlers ────────────────────────────────────────────────────────────────

  function handleAnalyze() {
    setUrlError(null);
    if (!urlInput.trim()) {
      setUrlError("Please enter a GitHub repository URL.");
      return;
    }
    const p = parseGitHubUrl(urlInput);
    if (!p) {
      setUrlError("Please enter a valid GitHub URL (e.g. https://github.com/owner/repo).");
      return;
    }
    setParsed(p);
    setStep("confirm");
  }

  async function handleConfirm() {
    if (!parsed) return;
    setStep("loading");
    try {
      const body: RepositoryCreateBody = {
        full_name: parsed.full_name,
        owner: parsed.owner,
        name: parsed.name,
        platform: "github",
        clone_url: parsed.clone_url,
        github_token: "",
      };
      const repo = await api.repositories.create(body);
      navigate(`/repo/${repo.id}`);
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      // 409 = already registered — find it in the list and navigate
      if (msg.includes("409")) {
        try {
          const all = await api.repositories.list();
          const existing = all.find((r) => r.full_name === parsed.full_name);
          if (existing) {
            navigate(`/repo/${existing.id}`);
            return;
          }
        } catch {}
        setErrorMsg("This repository is already registered but could not be found. Refresh and try again.");
      } else {
        setErrorMsg(msg);
      }
      setStep("error");
    }
  }

  function handleCancel() {
    setStep("input");
    setParsed(null);
    setUrlError(null);
  }

  function handleRetry() {
    setStep("input");
    setErrorMsg("");
  }

  // ── Render ──────────────────────────────────────────────────────────────────

  return (
    <div style={{
      minHeight: "100vh",
      background: "#0f172a",
      display: "flex",
      flexDirection: "column",
      alignItems: "center",
      padding: "60px 24px 80px",
    }}>
      {/* Branding */}
      <div style={{ textAlign: "center", marginBottom: 48 }}>
        <div style={{ fontSize: 48, marginBottom: 12 }}>🛡️</div>
        <h1 style={{ margin: 0, fontSize: 36, fontWeight: 800, color: "#f1f5f9", letterSpacing: -1 }}>
          RepoGuardian
        </h1>
        <p style={{ margin: "10px 0 0", color: "#64748b", fontSize: 16 }}>
          Autonomous AI code review for every pull request
        </p>
      </div>

      {/* Main card */}
      <div style={{
        width: "100%", maxWidth: 560,
        background: "#1e293b",
        borderRadius: 16,
        padding: 32,
        border: "1px solid #334155",
      }}>

        {/* ── input step ── */}
        {step === "input" && (
          <>
            <p style={{ margin: "0 0 20px", color: "#94a3b8", fontSize: 15 }}>
              Paste a GitHub repository URL to start analyzing it.
            </p>
            <div style={{ display: "flex", gap: 10 }}>
              <input
                autoFocus
                type="text"
                value={urlInput}
                onChange={(e) => { setUrlInput(e.target.value); setUrlError(null); }}
                onKeyDown={(e) => e.key === "Enter" && handleAnalyze()}
                placeholder="https://github.com/owner/repo"
                style={{
                  flex: 1,
                  background: "#0f172a",
                  border: `1.5px solid ${urlError ? "#ef4444" : "#334155"}`,
                  borderRadius: 8,
                  padding: "11px 14px",
                  color: "#f1f5f9",
                  fontSize: 15,
                  outline: "none",
                }}
              />
              <button
                onClick={handleAnalyze}
                style={{
                  background: "#6366f1",
                  color: "#fff",
                  border: "none",
                  borderRadius: 8,
                  padding: "11px 22px",
                  fontSize: 15,
                  fontWeight: 600,
                  cursor: "pointer",
                  whiteSpace: "nowrap",
                }}
              >
                Analyze →
              </button>
            </div>
            {urlError && (
              <p style={{ margin: "10px 0 0", color: "#ef4444", fontSize: 13 }}>{urlError}</p>
            )}
          </>
        )}

        {/* ── confirm step ── */}
        {step === "confirm" && parsed && (
          <>
            <div style={{
              background: "#0f172a", borderRadius: 10, padding: "18px 20px",
              marginBottom: 24, border: "1px solid #334155",
            }}>
              <p style={{ margin: "0 0 4px", color: "#64748b", fontSize: 12, textTransform: "uppercase", letterSpacing: 1 }}>
                Repository
              </p>
              <p style={{ margin: 0, color: "#f1f5f9", fontSize: 18, fontWeight: 700 }}>
                {parsed.full_name}
              </p>
              <p style={{ margin: "4px 0 0", color: "#64748b", fontSize: 13 }}>
                {parsed.clone_url}
              </p>
            </div>
            <p style={{ margin: "0 0 24px", color: "#94a3b8", fontSize: 15 }}>
              RepoGuardian will register this repository and analyze every pull request using 10 AI agents. Allow access?
            </p>
            <div style={{ display: "flex", gap: 10 }}>
              <button
                onClick={handleConfirm}
                style={{
                  flex: 1, background: "#6366f1", color: "#fff",
                  border: "none", borderRadius: 8, padding: "12px 0",
                  fontSize: 15, fontWeight: 600, cursor: "pointer",
                }}
              >
                Yes, Analyze
              </button>
              <button
                onClick={handleCancel}
                style={{
                  flex: 1, background: "#334155", color: "#94a3b8",
                  border: "none", borderRadius: 8, padding: "12px 0",
                  fontSize: 15, fontWeight: 600, cursor: "pointer",
                }}
              >
                Cancel
              </button>
            </div>
          </>
        )}

        {/* ── loading step ── */}
        {step === "loading" && (
          <div style={{ textAlign: "center", padding: "20px 0" }}>
            <div style={{
              width: 40, height: 40, borderRadius: "50%",
              border: "3px solid #334155", borderTopColor: "#6366f1",
              animation: "spin 0.8s linear infinite",
              margin: "0 auto 20px",
            }} />
            <p style={{ color: "#94a3b8", margin: 0, fontSize: 15 }}>
              Registering <strong style={{ color: "#f1f5f9" }}>{parsed?.full_name}</strong>…
            </p>
          </div>
        )}

        {/* ── error step ── */}
        {step === "error" && (
          <>
            <div style={{
              background: "#450a0a", border: "1px solid #ef4444",
              borderRadius: 10, padding: "16px 20px", marginBottom: 24,
            }}>
              <p style={{ margin: 0, color: "#ef4444", fontSize: 14 }}>{errorMsg}</p>
            </div>
            <button
              onClick={handleRetry}
              style={{
                width: "100%", background: "#334155", color: "#f1f5f9",
                border: "none", borderRadius: 8, padding: "12px 0",
                fontSize: 15, fontWeight: 600, cursor: "pointer",
              }}
            >
              Try Again
            </button>
          </>
        )}
      </div>

      {/* ── Existing repos ── */}
      {repos.length > 0 && (
        <div style={{ width: "100%", maxWidth: 560, marginTop: 48 }}>
          <div style={{
            display: "flex", alignItems: "center", gap: 12, marginBottom: 20,
          }}>
            <div style={{ flex: 1, height: 1, background: "#1e293b" }} />
            <span style={{ color: "#475569", fontSize: 13, whiteSpace: "nowrap" }}>
              or pick an existing repository
            </span>
            <div style={{ flex: 1, height: 1, background: "#1e293b" }} />
          </div>

          <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
            {repos.map((repo) => (
              <div
                key={repo.id}
                onClick={() => navigate(`/repo/${repo.id}`)}
                style={{
                  background: "#1e293b",
                  border: "1px solid #334155",
                  borderRadius: 12,
                  padding: "16px 20px",
                  cursor: "pointer",
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "space-between",
                  transition: "border-color 0.15s",
                }}
                onMouseEnter={(e) => (e.currentTarget.style.borderColor = "#6366f1")}
                onMouseLeave={(e) => (e.currentTarget.style.borderColor = "#334155")}
              >
                <div>
                  <p style={{ margin: 0, color: "#f1f5f9", fontWeight: 600, fontSize: 15 }}>
                    {repo.full_name}
                  </p>
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
            <Link to="/repos" style={{ color: "#475569", fontSize: 13, textDecoration: "none" }}>
              View all repositories →
            </Link>
          </p>
        </div>
      )}
    </div>
  );
};
