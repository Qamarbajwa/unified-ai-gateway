from __future__ import annotations

import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def default_root_dir() -> Path:
    configured = os.environ.get("GATEWAY_ROOT")
    if configured:
        return Path(configured)
    current = Path(__file__).resolve()
    return current.parents[2] if len(current.parents) > 2 else current.parent


ROOT_DIR = default_root_dir()
DEFAULT_REGISTRY_PATH = ROOT_DIR / ".harness" / "connected_tools.json"
DEFAULT_SNAPSHOT_PATH = ROOT_DIR / ".harness" / "tool_inventory.json"

SKIP_DIRS = {
    ".git",
    "__pycache__",
    ".pytest_cache",
    "node_modules",
    "dist",
    "build",
    ".next",
    ".venv",
    "venv",
}

MANIFEST_FILES = [
    "package.json",
    "pnpm-workspace.yaml",
    "docker-compose.yml",
    "pyproject.toml",
    "requirements.txt",
    "CMakeLists.txt",
    "vcpkg.json",
    "README.md",
    "MASTER_STATUS.md",
    "progress_report.md",
]

GOVERNANCE_FILES = [
    "AGENTS.md",
    ".cursorrules",
    ".roocoderrules",
    ".harness/harness_state.json",
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_registry(registry_path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    path = Path(registry_path or os.environ.get("CONNECTED_TOOLS_REGISTRY") or DEFAULT_REGISTRY_PATH)
    with path.open("r", encoding="utf-8") as handle:
        registry = json.load(handle)
    registry["tools"] = dedupe_tools(registry.get("tools", []))
    return registry


def dedupe_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for tool in tools:
        key = str(Path(tool.get("path", "")).resolve()).casefold()
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(tool)
    return deduped


def scan_connected_tools(
    registry_path: str | os.PathLike[str] | None = None,
    snapshot_path: str | os.PathLike[str] | None = None,
    persist: bool = False,
) -> dict[str, Any]:
    registry = load_registry(registry_path)
    previous = load_snapshot(snapshot_path)
    tools = [scan_tool(tool, previous.get("tools_by_id", {})) for tool in registry["tools"]]
    result = {
        "generated_at": utc_now(),
        "registry_version": registry.get("version", "unknown"),
        "tool_count": len(tools),
        "changed_count": sum(1 for tool in tools if tool["change_status"] != "unchanged"),
        "tools": tools,
    }
    if persist:
        save_snapshot(result, snapshot_path)
    return result


def load_snapshot(snapshot_path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    path = Path(snapshot_path or os.environ.get("CONNECTED_TOOLS_SNAPSHOT") or DEFAULT_SNAPSHOT_PATH)
    if not path.exists():
        return {"tools_by_id": {}}
    with path.open("r", encoding="utf-8") as handle:
        snapshot = json.load(handle)
    return {
        "generated_at": snapshot.get("generated_at"),
        "tools_by_id": {tool.get("id"): tool for tool in snapshot.get("tools", [])},
    }


def save_snapshot(result: dict[str, Any], snapshot_path: str | os.PathLike[str] | None = None) -> None:
    path = Path(snapshot_path or os.environ.get("CONNECTED_TOOLS_SNAPSHOT") or DEFAULT_SNAPSHOT_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2)
        handle.write("\n")


def scan_tool(tool: dict[str, Any], previous_by_id: dict[str, Any]) -> dict[str, Any]:
    path = resolve_tool_path(tool)
    exists = path.exists()
    inventory = {
        "id": tool["id"],
        "name": tool["name"],
        "configured_path": tool["path"],
        "container_path": tool.get("container_path"),
        "path": str(path),
        "domain": tool.get("domain"),
        "quality_profile": tool.get("quality_profile"),
        "exists": exists,
        "git": {},
        "manifests": [],
        "governance": [],
        "file_count": 0,
        "total_bytes": 0,
        "latest_write_utc": None,
        "fingerprint": None,
        "change_status": "missing",
        "recommendations": [],
    }
    if not exists:
        inventory["recommendations"].append("Verify the configured path or remove this tool from the registry.")
        return inventory

    stats = collect_tree_stats(path)
    inventory.update(stats)
    inventory["manifests"] = existing_relative_files(path, MANIFEST_FILES)
    inventory["governance"] = existing_relative_files(path, GOVERNANCE_FILES)
    inventory["git"] = git_status(path)
    inventory["fingerprint"] = make_fingerprint(inventory)
    inventory["change_status"] = classify_change(inventory, previous_by_id.get(tool["id"]))
    inventory["recommendations"] = recommendations_for(inventory)
    return inventory


def resolve_tool_path(tool: dict[str, Any]) -> Path:
    host_path = Path(tool["path"])
    if host_path.exists():
        return host_path
    container_path = tool.get("container_path")
    if container_path and Path(container_path).exists():
        return Path(container_path)
    return host_path


def collect_tree_stats(root: Path) -> dict[str, Any]:
    digest = hashlib.sha256()
    count = 0
    total_bytes = 0
    latest_ns = 0
    for current_root, dirs, files in os.walk(root):
        dirs[:] = [name for name in dirs if name not in SKIP_DIRS]
        rel_root = Path(current_root).relative_to(root)
        for name in sorted(files):
            file_path = Path(current_root) / name
            try:
                stat = file_path.stat()
            except OSError:
                continue
            rel = (rel_root / name).as_posix()
            digest.update(f"{rel}|{stat.st_size}|{stat.st_mtime_ns}\n".encode("utf-8", "replace"))
            count += 1
            total_bytes += stat.st_size
            latest_ns = max(latest_ns, stat.st_mtime_ns)
    latest = None
    if latest_ns:
        latest = datetime.fromtimestamp(latest_ns / 1_000_000_000, tz=timezone.utc).isoformat(timespec="seconds")
    return {
        "file_count": count,
        "total_bytes": total_bytes,
        "latest_write_utc": latest,
        "tree_digest": digest.hexdigest(),
    }


def existing_relative_files(root: Path, candidates: list[str]) -> list[str]:
    return [candidate for candidate in candidates if (root / candidate).exists()]


def git_status(root: Path) -> dict[str, Any]:
    if not (root / ".git").exists():
        return {"present": False}
    result = {"present": True, "head": None, "branch": None, "dirty": None, "status_lines": []}
    result["head"] = run_git(root, "rev-parse", "--short", "HEAD")
    result["branch"] = run_git(root, "branch", "--show-current")
    status = run_git(root, "status", "--short")
    lines = [line for line in status.splitlines() if line.strip()] if status is not None else []
    result["dirty"] = bool(lines)
    result["status_lines"] = lines[:50]
    return result


def run_git(root: Path, *args: str) -> str | None:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def make_fingerprint(inventory: dict[str, Any]) -> str:
    parts = [
        inventory.get("tree_digest") or "",
        inventory.get("git", {}).get("head") or "",
        "\n".join(inventory.get("git", {}).get("status_lines", [])),
    ]
    return hashlib.sha256("\n".join(parts).encode("utf-8", "replace")).hexdigest()


def classify_change(current: dict[str, Any], previous: dict[str, Any] | None) -> str:
    if not previous:
        return "new"
    if current.get("fingerprint") != previous.get("fingerprint"):
        return "changed"
    return "unchanged"


def recommendations_for(inventory: dict[str, Any]) -> list[str]:
    recommendations: list[str] = []
    manifests = set(inventory["manifests"])
    governance = set(inventory["governance"])
    domain = inventory.get("domain")

    if not governance:
        recommendations.append("Add synchronized governance instructions so agents inherit this tool's constraints.")
    if "README.md" not in manifests:
        recommendations.append("Add a README with run, test, and integration contracts.")
    if not inventory.get("git", {}).get("present"):
        recommendations.append("Place the tool under git so changes can be tracked reliably.")
    elif inventory.get("git", {}).get("dirty"):
        recommendations.append("Review uncommitted changes before automated fixes are dispatched.")
    if domain == "health_assistant":
        recommendations.append("Require medical safety checks, PHI handling rules, and human escalation paths.")
    if domain == "bim_hbim_architecture":
        recommendations.append("Keep ISO 19650/DIN checks and geometry/topology validation in the quality gate.")
    if domain in {"commerce_assistant", "social_media_automation"} and "package.json" not in manifests:
        recommendations.append("Expose frontend/backend manifests so the gateway can run dependency and test audits.")
    if inventory["change_status"] == "changed":
        recommendations.append("Run the tool's local tests and refresh the unified gateway snapshot after review.")
    return recommendations
