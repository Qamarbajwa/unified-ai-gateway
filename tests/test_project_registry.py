import json
import os
import sys
from pathlib import Path


base_dir = Path(__file__).resolve().parents[1]
mcp_server_path = base_dir / "services" / "mcp-server"
sys.path.insert(0, str(mcp_server_path))

from project_registry import dedupe_tools, scan_connected_tools


def write_registry(path: Path, tools: list[dict]) -> None:
    path.write_text(json.dumps({"version": "test", "tools": tools}), encoding="utf-8")


def test_dedupe_tools_by_path(tmp_path):
    tool_dir = tmp_path / "tool"
    tool_dir.mkdir()
    tools = [
        {"id": "one", "name": "One", "path": str(tool_dir)},
        {"id": "two", "name": "Two", "path": str(tool_dir)},
    ]

    assert [tool["id"] for tool in dedupe_tools(tools)] == ["one"]


def test_scan_connected_tools_detects_new_and_changed(tmp_path):
    tool_dir = tmp_path / "tool"
    tool_dir.mkdir()
    (tool_dir / "README.md").write_text("run with care\n", encoding="utf-8")
    registry_path = tmp_path / "connected_tools.json"
    snapshot_path = tmp_path / "tool_inventory.json"
    write_registry(
        registry_path,
        [
            {
                "id": "tool",
                "name": "Tool",
                "path": str(tool_dir),
                "domain": "commerce_assistant",
                "quality_profile": "consumer_web_app",
            }
        ],
    )

    first = scan_connected_tools(registry_path, snapshot_path, persist=True)
    assert first["tool_count"] == 1
    assert first["tools"][0]["change_status"] == "new"

    second = scan_connected_tools(registry_path, snapshot_path, persist=False)
    assert second["tools"][0]["change_status"] == "unchanged"

    (tool_dir / "README.md").write_text("changed\n", encoding="utf-8")
    os.utime(tool_dir / "README.md", None)
    third = scan_connected_tools(registry_path, snapshot_path, persist=False)
    assert third["tools"][0]["change_status"] == "changed"


def test_scan_connected_tools_reports_missing_path(tmp_path):
    registry_path = tmp_path / "connected_tools.json"
    write_registry(
        registry_path,
        [
            {
                "id": "missing",
                "name": "Missing",
                "path": str(tmp_path / "missing"),
                "domain": "health_assistant",
                "quality_profile": "medical_safety_sensitive",
            }
        ],
    )

    result = scan_connected_tools(registry_path, tmp_path / "snapshot.json", persist=False)
    tool = result["tools"][0]
    assert tool["exists"] is False
    assert tool["change_status"] == "missing"
    assert tool["recommendations"]
