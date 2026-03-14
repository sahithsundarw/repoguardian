import React, { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, Repository } from "../api/client";

export const RepositoryList: React.FC = () => {
  const [repos, setRepos] = useState<Repository[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    api.repositories.list()
      .then(setRepos)
      .catch(console.error)
      .finally(() => setLoading(false));
  }, []);

  return (
    <div style={{ padding: 40, maxWidth: 900, margin: "0 auto" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 32 }}>
        <div>
          <h1 style={{ color: "#f1f5f9", margin: 0, fontSize: 28 }}>🛡️ RepoGuardian</h1>
          <p style={{ color: "#64748b", margin: "4px 0 0" }}>Autonomous AI Code Repository Manager</p>
        </div>
      </div>

      {loading && <div style={{ color: "#64748b" }}>Loading repositories...</div>}

      {!loading && repos.length === 0 && (
        <div style={{
          background: "#1e293b", borderRadius: 16, padding: 40,
          textAlign: "center", color: "#64748b",
        }}>
          <div style={{ fontSize: 48, marginBottom: 16 }}>📦</div>
          <div>No repositories registered yet.</div>
          <div style={{ marginTop: 8, fontSize: 14 }}>
            Register a repository via the API to start monitoring.
          </div>
        </div>
      )}

      <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        {repos.map((repo) => (
          <Link
            key={repo.id}
            to={`/repo/${repo.id}`}
            style={{ textDecoration: "none" }}
          >
            <div style={{
              background: "#1e293b", borderRadius: 12,
              padding: "20px 24px",
              display: "flex", justifyContent: "space-between", alignItems: "center",
              transition: "background 0.2s",
              cursor: "pointer",
              border: "1px solid transparent",
            }}
              onMouseEnter={(e) => (e.currentTarget.style.borderColor = "#6366f1")}
              onMouseLeave={(e) => (e.currentTarget.style.borderColor = "transparent")}
            >
              <div>
                <div style={{ color: "#f1f5f9", fontWeight: 600, fontSize: 16 }}>
                  {repo.full_name}
                </div>
                <div style={{ color: "#64748b", fontSize: 13, marginTop: 4 }}>
                  {repo.platform} · {repo.default_branch}
                  {repo.primary_language && ` · ${repo.primary_language}`}
                </div>
              </div>
              <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
                <span style={{
                  background: repo.is_active ? "#166534" : "#334155",
                  color: repo.is_active ? "#86efac" : "#94a3b8",
                  fontSize: 12, padding: "2px 10px", borderRadius: 9999,
                }}>
                  {repo.is_active ? "Active" : "Inactive"}
                </span>
                <span style={{ color: "#475569" }}>→</span>
              </div>
            </div>
          </Link>
        ))}
      </div>
    </div>
  );
};
