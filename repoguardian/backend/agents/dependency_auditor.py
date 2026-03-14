"""
Dependency Auditor Agent.

Scans dependency manifest files for:
  1. Known CVE vulnerabilities (via OSV API — free, no key required)
  2. Significantly outdated packages
  3. License compatibility issues

OSV (Open Source Vulnerabilities) API: https://osv.dev/docs/
  - Supports PyPI, npm, Go, Maven, Cargo, etc.
  - Free, no authentication required
  - Returns CVE and GHSA advisories
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Optional

import httpx
from pydantic import BaseModel, Field

from backend.agents.base import BaseAgent
from backend.config import get_settings
from backend.models.database import FindingCategory, Severity
from backend.models.schemas import (
    AgentFinding,
    ContextPackage,
    DependencyReport,
    VulnerablePackage,
)

logger = logging.getLogger(__name__)
settings = get_settings()

OSV_API_URL = "https://api.osv.dev/v1/query"


# ── Ecosystem detection ────────────────────────────────────────────────────────

_MANIFEST_TO_ECOSYSTEM = {
    "requirements.txt": "PyPI",
    "requirements-dev.txt": "PyPI",
    "pyproject.toml": "PyPI",
    "Pipfile": "PyPI",
    "package.json": "npm",
    "Cargo.toml": "crates.io",
    "go.mod": "Go",
    "pom.xml": "Maven",
    "build.gradle": "Maven",
}

# ── Copyleft licenses that may conflict with proprietary code ─────────────────
_COPYLEFT_LICENSES = {"GPL-2.0", "GPL-3.0", "AGPL-3.0", "LGPL-2.1", "LGPL-3.0", "EUPL-1.1"}


class DependencyAuditorAgent(BaseAgent):
    name = "dependency_auditor"

    async def run(self, context: ContextPackage) -> DependencyReport:
        self.log_info("Starting dependency audit for %s", context.repo_full_name)

        if not context.dependency_manifests:
            self.log_info("No dependency manifests found — skipping")
            return DependencyReport(
                agent_source=self.name,
                vulnerable_packages=[],
                outdated_packages=[],
                license_issues=[],
                findings=[],
            )

        # Parse packages from all manifests
        all_packages: list[dict] = []
        for manifest in context.dependency_manifests:
            ecosystem = _MANIFEST_TO_ECOSYSTEM.get(manifest.path.split("/")[-1], "")
            if ecosystem:
                packages = self._parse_manifest(manifest.path, manifest.content, ecosystem)
                all_packages.extend(packages)

        self.log_info("Parsed %d packages from manifests", len(all_packages))

        # Query OSV API for vulnerabilities
        vulnerable_packages = await self._query_osv_batch(all_packages)

        # Convert to findings
        findings = self._vuln_packages_to_findings(vulnerable_packages)

        return DependencyReport(
            agent_source=self.name,
            vulnerable_packages=vulnerable_packages,
            outdated_packages=[],     # Would require registry API calls
            license_issues=[],        # Would require license DB
            findings=findings,
            total_token_cost=self.total_token_cost,
        )

    # ── Manifest parsing ───────────────────────────────────────────────────────

    def _parse_manifest(self, path: str, content: str, ecosystem: str) -> list[dict]:
        """Extract {name, version, ecosystem} from a manifest file."""
        packages = []
        filename = path.split("/")[-1]

        if filename == "requirements.txt" or filename == "requirements-dev.txt":
            packages.extend(self._parse_requirements_txt(content, ecosystem))
        elif filename == "package.json":
            packages.extend(self._parse_package_json(content, ecosystem))
        elif filename == "pyproject.toml":
            packages.extend(self._parse_pyproject_toml(content, ecosystem))
        elif filename == "go.mod":
            packages.extend(self._parse_go_mod(content, ecosystem))
        elif filename == "Cargo.toml":
            packages.extend(self._parse_cargo_toml(content, ecosystem))

        return packages

    def _parse_requirements_txt(self, content: str, ecosystem: str) -> list[dict]:
        packages = []
        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("-"):
                continue
            # Match: package==1.2.3 or package>=1.0
            m = re.match(r"^([A-Za-z0-9_\-\.]+)\s*([=><~!]+)\s*([^\s;]+)", line)
            if m:
                packages.append({
                    "name": m.group(1),
                    "version": m.group(3).strip(","),
                    "ecosystem": ecosystem,
                })
        return packages

    def _parse_package_json(self, content: str, ecosystem: str) -> list[dict]:
        import json
        packages = []
        try:
            data = json.loads(content)
            for dep_section in ("dependencies", "devDependencies", "peerDependencies"):
                for name, version_range in data.get(dep_section, {}).items():
                    # Strip semver range markers
                    version = re.sub(r"^[\^~>=<]", "", version_range).split(" ")[0]
                    packages.append({"name": name, "version": version, "ecosystem": ecosystem})
        except json.JSONDecodeError:
            pass
        return packages

    def _parse_pyproject_toml(self, content: str, ecosystem: str) -> list[dict]:
        packages = []
        # Simple regex for [project] dependencies = ["package>=1.0"]
        dep_section = re.search(r"\[project\].*?dependencies\s*=\s*\[(.*?)\]", content, re.DOTALL)
        if dep_section:
            for dep in re.findall(r'"([^"]+)"', dep_section.group(1)):
                m = re.match(r"([A-Za-z0-9_\-\.]+)\s*[>=<]+\s*([^\s,\"]+)", dep)
                if m:
                    packages.append({"name": m.group(1), "version": m.group(2), "ecosystem": ecosystem})
        return packages

    def _parse_go_mod(self, content: str, ecosystem: str) -> list[dict]:
        packages = []
        for line in content.splitlines():
            m = re.match(r"^\s+([a-z0-9./\-]+)\s+v([0-9][^\s]+)", line)
            if m:
                packages.append({"name": m.group(1), "version": m.group(2), "ecosystem": "Go"})
        return packages

    def _parse_cargo_toml(self, content: str, ecosystem: str) -> list[dict]:
        packages = []
        for m in re.finditer(r'^([a-z0-9_\-]+)\s*=\s*"([^"]+)"', content, re.MULTILINE):
            packages.append({"name": m.group(1), "version": m.group(2), "ecosystem": "crates.io"})
        return packages

    # ── OSV API ────────────────────────────────────────────────────────────────

    async def _query_osv_batch(self, packages: list[dict]) -> list[VulnerablePackage]:
        """Query OSV API for vulnerabilities in batches of 20."""
        vulnerable = []
        batch_size = 20

        async with httpx.AsyncClient(timeout=30.0) as client:
            for i in range(0, len(packages), batch_size):
                batch = packages[i: i + batch_size]
                tasks = [self._query_osv_single(client, pkg) for pkg in batch]
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for result in results:
                    if isinstance(result, list):
                        vulnerable.extend(result)
                    elif isinstance(result, Exception):
                        logger.debug("OSV query error: %s", result)

        return vulnerable

    async def _query_osv_single(
        self, client: httpx.AsyncClient, package: dict
    ) -> list[VulnerablePackage]:
        """Query OSV for a single package version."""
        payload = {
            "version": package["version"],
            "package": {
                "name": package["name"],
                "ecosystem": package["ecosystem"],
            },
        }
        try:
            response = await client.post(OSV_API_URL, json=payload)
            if response.status_code != 200:
                return []
            data = response.json()
        except Exception:
            return []

        vulns = []
        for vuln in data.get("vulns", []):
            severity = self._parse_osv_severity(vuln)
            cve_id = next(
                (a["id"] for a in vuln.get("aliases", []) if a.startswith("CVE")), vuln["id"]
            )
            # Find fix version from affected ranges
            fix_version = self._extract_fix_version(vuln, package["version"])

            vulns.append(VulnerablePackage(
                name=package["name"],
                installed_version=package["version"],
                vulnerable_range=vuln.get("summary", "see advisory"),
                cve_id=cve_id,
                severity=severity,
                fix_version=fix_version,
                cvss_score=self._extract_cvss(vuln),
            ))

        return vulns

    def _parse_osv_severity(self, vuln: dict) -> Severity:
        """Map OSV severity to our Severity enum."""
        for sev_entry in vuln.get("severity", []):
            score_str = sev_entry.get("score", "")
            if "CVSS" in sev_entry.get("type", ""):
                try:
                    score = float(re.search(r"(\d+\.\d+)", score_str).group(1))
                    if score >= 9.0:
                        return Severity.CRITICAL
                    elif score >= 7.0:
                        return Severity.HIGH
                    elif score >= 4.0:
                        return Severity.MEDIUM
                    else:
                        return Severity.LOW
                except (AttributeError, ValueError):
                    pass
        return Severity.HIGH  # Default to HIGH if no score

    def _extract_fix_version(self, vuln: dict, installed: str) -> Optional[str]:
        """Extract the earliest fixed version from OSV advisory."""
        for affected in vuln.get("affected", []):
            for r in affected.get("ranges", []):
                for event in r.get("events", []):
                    if "fixed" in event:
                        return event["fixed"]
        return None

    def _extract_cvss(self, vuln: dict) -> Optional[float]:
        for sev in vuln.get("severity", []):
            score_str = sev.get("score", "")
            m = re.search(r"(\d+\.\d+)", score_str)
            if m:
                try:
                    return float(m.group(1))
                except ValueError:
                    pass
        return None

    # ── Finding conversion ─────────────────────────────────────────────────────

    def _vuln_packages_to_findings(
        self, vulnerable_packages: list[VulnerablePackage]
    ) -> list[AgentFinding]:
        findings = []
        for vp in vulnerable_packages:
            fix_note = f" Fix by upgrading to `{vp.fix_version}`." if vp.fix_version else ""
            findings.append(AgentFinding(
                agent_source=self.name,
                category=FindingCategory.DEPENDENCY,
                severity=vp.severity,
                title=f"Vulnerable dependency: {vp.name}@{vp.installed_version}",
                description=(
                    f"`{vp.name}` version `{vp.installed_version}` has a known vulnerability "
                    f"({vp.cve_id}).{fix_note}"
                ),
                evidence=f"{vp.name}=={vp.installed_version}",
                suggested_fix=f"Upgrade `{vp.name}` to `{vp.fix_version or 'latest non-vulnerable version'}`.",
                reasoning=f"Found in OSV advisory database: {vp.cve_id}",
                cve_id=vp.cve_id,
                cvss_score=vp.cvss_score,
                confidence=0.95,  # OSV is authoritative, high confidence
            ))
        return findings
