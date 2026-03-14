/**
 * Typed API client for RepoGuardian backend.
 */

const BASE_URL = import.meta.env.VITE_API_URL || "http://localhost:8000";

export interface HealthDashboard {
  repo_id: string;
  repo_full_name: string;
  as_of: string;
  overall_score: number;
  grade: "A" | "B" | "C" | "D" | "F";
  sub_scores: {
    code_quality: number;
    security: number;
    dependencies: number;
    documentation: number;
    test_coverage: number;
  };
  active_findings: Record<string, number>;
  hot_zones: HotZone[];
  trend_30d: TrendPoint[];
  trend_delta_7d: number;
  trend_velocity: "IMPROVING" | "STABLE" | "DEGRADING";
  recent_activity: ActivityItem[];
}

export interface HotZone {
  file_path: string;
  risk_score: number;
  finding_count: number;
  critical_count: number;
  high_count: number;
}

export interface TrendPoint {
  timestamp: string;
  overall_score: number;
  grade: string;
}

export interface ActivityItem {
  timestamp: string;
  event: string;
  actor: string;
}

export interface Repository {
  id: string;
  platform: string;
  full_name: string;
  clone_url: string;
  default_branch: string;
  primary_language: string | null;
  is_active: boolean;
  created_at: string;
}

export interface Finding {
  id: string;
  repository_id: string;
  file_path: string | null;
  line_start: number | null;
  line_end: number | null;
  category: string;
  severity: "CRITICAL" | "HIGH" | "MEDIUM" | "LOW" | "INFO";
  title: string;
  description: string;
  evidence: string | null;
  suggested_fix: string | null;
  reasoning: string | null;
  cwe_id: string | null;
  confidence: number;
  agent_source: string;
  status: string;
  pr_number: number | null;
  created_at: string;
  resolved_at: string | null;
}

async function get<T>(path: string): Promise<T> {
  const res = await fetch(`${BASE_URL}${path}`);
  if (!res.ok) throw new Error(`API error ${res.status}: ${await res.text()}`);
  return res.json();
}

async function post<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${BASE_URL}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`API error ${res.status}: ${await res.text()}`);
  return res.json();
}

export const api = {
  repositories: {
    list: () => get<Repository[]>("/api/repositories"),
    get: (id: string) => get<Repository>(`/api/repositories/${id}`),
    create: (data: Partial<Repository>) => post<Repository>("/api/repositories", data),
  },
  health: {
    dashboard: (repoId: string) => get<HealthDashboard>(`/api/health/${repoId}`),
    score: (repoId: string) => get<{ overall_score: number; grade: string }>(`/api/health/${repoId}/score`),
  },
  findings: {
    list: (params?: { repo_id?: string; severity?: string; status?: string; pr_number?: number }) => {
      const q = new URLSearchParams();
      if (params?.repo_id) q.set("repo_id", params.repo_id);
      if (params?.severity) q.set("severity", params.severity);
      if (params?.status) q.set("status", params.status);
      if (params?.pr_number) q.set("pr_number", String(params.pr_number));
      return get<Finding[]>(`/api/findings?${q.toString()}`);
    },
    get: (id: string) => get<Finding>(`/api/findings/${id}`),
  },
  hitl: {
    action: (findingId: string, action: string, reasonCode?: string) =>
      post(`/api/hitl/${findingId}/action`, { action, reason_code: reasonCode }),
  },
};
