# /harness/bin/harness_manager.py
import json
import shutil
import asyncio
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Literal, Any
from pydantic import BaseModel, Field, field_validator

ModelType = Literal["codex", "deepseek", "claude", "unknown"]

class NegativeConstraint(BaseModel):
    model: ModelType
    rule: str
    created_at: datetime
    last_triggered: Optional[datetime] = None
    pinned: bool = False

    @field_validator("created_at", mode="before")
    @classmethod
    def parse_datetime(cls, v):
        if isinstance(v, str):
            try:
                return datetime.fromisoformat(v)
            except ValueError:
                return datetime.now()
        return v

class HandoverRecord(BaseModel):
    from_model: ModelType
    to_model: ModelType
    timestamp: datetime
    reasoning_depth: Literal["low", "medium", "high"] = "medium"

    @field_validator("timestamp", mode="before")
    @classmethod
    def parse_timestamp(cls, v):
        if isinstance(v, str):
            try:
                return datetime.fromisoformat(v)
            except ValueError:
                return datetime.now()
        return v

class HarnessState(BaseModel):
    version: str = "33.0"
    last_audit_iso: datetime
    legacy_artifacts: Dict[str, Any] = Field(default_factory=dict)
    active_rules: Dict[str, Any] = Field(default_factory=dict)
    negative_constraints: List[NegativeConstraint] = Field(default_factory=list)
    handover_chain: List[HandoverRecord] = Field(default_factory=list)
    model_fingerprint_cache: Dict[str, ModelType] = Field(default_factory=dict)  # filepath -> model

    @field_validator("last_audit_iso", mode="before")
    @classmethod
    def parse_last_audit(cls, v):
        if isinstance(v, str):
            try:
                return datetime.fromisoformat(v)
            except ValueError:
                return datetime.now()
        return v

    @field_validator("negative_constraints", mode="before")
    @classmethod
    def prune_old_constraints(cls, v):
        if not isinstance(v, list):
            return v
        cutoff = datetime.now() - timedelta(days=30)
        pruned = []
        for c in v:
            created_at = None
            pinned = False
            if isinstance(c, dict):
                created_at_str = c.get("created_at")
                if isinstance(created_at_str, str):
                    try:
                        created_at = datetime.fromisoformat(created_at_str)
                    except ValueError:
                        created_at = datetime.now()
                else:
                    created_at = datetime.now()
                pinned = c.get("pinned", False)
            elif isinstance(c, NegativeConstraint):
                created_at = c.created_at
                pinned = c.pinned
            else:
                continue

            if pinned or created_at > cutoff:
                pruned.append(c)
        return pruned

class HarnessManager:
    def __init__(self, project_root: Path):
        self.root = project_root
        self.base_dir = self.root / ".harness"
        self.state_path = self.base_dir / "harness_state.json"
        self.event_dir = self.base_dir / "events"
        self.history_dir = self.base_dir / "history"
        self.state = self._load_or_bootstrap()

    def _load_or_bootstrap(self) -> HarnessState:
        if self.state_path.exists():
            with open(self.state_path, encoding="utf-8") as f:
                data = json.load(f)
                # handle if the format has harness_version instead of version (Ver 30.0 compatibility)
                if "harness_version" in data and "version" not in data:
                    data["version"] = data.pop("harness_version")
                if "last_audit_iso" not in data:
                    data["last_audit_iso"] = datetime.now().isoformat()
                return HarnessState(**data)
        else:
            # Bootstrap new harness
            self._create_directories()
            default_state = HarnessState(last_audit_iso=datetime.now())
            self._save_state(default_state)
            self._create_default_templates()
            return default_state

    def _create_directories(self):
        self.history_dir.mkdir(parents=True, exist_ok=True)
        self.event_dir.mkdir(parents=True, exist_ok=True)
        (self.base_dir / "bin").mkdir(parents=True, exist_ok=True)

    def _create_default_templates(self):
        # Create default MASTER_STATUS.md if missing
        status_file = self.root / "MASTER_STATUS.md"
        if not status_file.exists():
            status_file.write_text(DEFAULT_STATUS_MD, encoding="utf-8")

        # Create .cursorrules, AGENTS.md, and .roocoderrules with Ver 33.0 prompt
        for fname in [".cursorrules", "AGENTS.md", ".roocoderrules"]:
            fpath = self.root / fname
            if not fpath.exists():
                fpath.write_text(MASTER_PROMPT_VER_33, encoding="utf-8")

    def _save_state(self, state: HarnessState):
        with open(self.state_path, "w", encoding="utf-8") as f:
            # Dump to JSON using model_dump in pydantic v2
            json.dump(state.model_dump(), f, indent=2, default=str)

    async def upgrade_from_legacy(self):
        """Detects and upgrades Ver 30.0 or 31.0 state."""
        self._create_directories()
        
        # Safe backup: Copy all files except history and events to history dir
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_dir = self.history_dir / f"pre_upgrade_{timestamp}"
        backup_dir.mkdir(parents=True, exist_ok=True)
        for path in self.base_dir.glob("*"):
            if path.name not in ("history", "events"):
                if path.is_file():
                    shutil.copy2(path, backup_dir)
                elif path.is_dir():
                    shutil.copytree(path, backup_dir / path.name, dirs_exist_ok=True)

        # Parse old state if exists
        if self.state_path.exists():
            with open(self.state_path, encoding="utf-8") as f:
                old = json.load(f)
            
            # Migrate fields
            self.state.legacy_artifacts = old.get("legacy_artifacts", old.get("context", {}))
            self.state.active_rules = old.get("active_rules", old.get("pillars_of_execution", {}))
            
            # Convert old constraints to model-specific (default unknown)
            old_constraints = old.get("negative_constraints", [])
            for constr in old_constraints:
                if isinstance(constr, str):
                    self.state.negative_constraints.append(
                        NegativeConstraint(model="unknown", rule=constr, created_at=datetime.now())
                    )
                elif isinstance(constr, dict):
                    # check if format matches NegativeConstraint
                    if "model" not in constr:
                        constr["model"] = "unknown"
                    if "created_at" not in constr:
                        constr["created_at"] = datetime.now().isoformat()
                    self.state.negative_constraints.append(NegativeConstraint(**constr))
            
            self.state.version = "33.0"
            self.state.last_audit_iso = datetime.now()
            self._save_state(self.state)

        # Overwrite rule files with Ver 33.0 prompt
        for fname in [".cursorrules", "AGENTS.md", ".roocoderrules"]:
            fpath = self.root / fname
            fpath.write_text(MASTER_PROMPT_VER_33, encoding="utf-8")

        # Update .vscode/settings.json
        vscode_dir = self.root / ".vscode"
        vscode_dir.mkdir(exist_ok=True)
        settings_file = vscode_dir / "settings.json"
        settings_data = {}
        if settings_file.exists():
            try:
                settings_data = json.loads(settings_file.read_text(encoding="utf-8"))
            except Exception:
                pass
        settings_data["deepseek.rules"] = "/.harness/harness_state.json"
        settings_file.write_text(json.dumps(settings_data, indent=2), encoding="utf-8")

        # Touch event bus
        (self.event_dir / "rule_changed").touch()
        print("Upgraded to Ver 33.0, history preserved. Event rule_changed touched.")

    async def add_negative_constraint(self, model: ModelType, rule: str):
        self.state.negative_constraints.append(
            NegativeConstraint(model=model, rule=rule, created_at=datetime.now())
        )
        self._save_state(self.state)
        (self.event_dir / "constraint_added").touch()

    async def record_handover(self, from_model: ModelType, to_model: ModelType):
        record = HandoverRecord(from_model=from_model, to_model=to_model, timestamp=datetime.now())
        self.state.handover_chain.append(record)
        self._save_state(self.state)

    def get_constraints_for_model(self, model: ModelType) -> List[str]:
        return [c.rule for c in self.state.negative_constraints if c.model == model or c.model == "unknown"]

# Default templates
DEFAULT_STATUS_MD = """# MASTER STATUS – Sovereign Factory (Ver 33.0)
## Historical Timeline
(empty)
## DeepSeek Reasoning Traces
(empty)
## Active Constraints
(managed by harness)
"""

MASTER_PROMPT_VER_33 = """# 🔱 THE SOVEREIGN FACTORY: UNIVERSAL RESEARCH HARNESS (VER 33.0)

## 🤖 GLOBAL OPERATIONAL IDENTITY & TARGETS
You are the **Meta‑Orchestrator** operating across:
- **Cursor (Codex / Sonnet)**
- **VS Code (DeepSeek‑R1 / any LLM)**
- **Antigravity (any agent)**
- **Standalone Codex** (GitHub Copilot CLI, API)

**Core directive:** Autonomously detect if this project already has a harness (Ver 30.0 or later). If yes, perform an **in‑place upgrade** preserving all history. If no, **bootstrap** a fresh Ver 33.0 harness.

---

## 🔍 PILLAR 33: UNIVERSAL DEPLOYMENT & RETROACTIVE ASSIMILATION

### Step 0 – Self‑Detection (Run First)
Check for the existence of `/.harness/harness_state.json` and any of `.cursorrules`, `AGENTS.md`.  
- If **none exist** → Branch A (Bootstrap)  
- If **any exist** → Branch B (Upgrade)

### Branch A: Bootstrap (New Project)
1. Create `/.harness/`, `/.harness/history/`, `/.harness/events/`, `/.harness/bin/`.
2. Create `/.harness/harness_state.json` with Ver 33.0 schema (see Appendix).
3. Create `MASTER_STATUS.md` from template.
4. Write this exact prompt into `.cursorrules` and `AGENTS.md`.
5. Touch `/.harness/events/rule_changed`.
6. Output: `✅ Bootstrapped Ver 33.0 harness.`

### Branch B: Upgrade (Existing Harness)
1. Backup `/.harness/` to `/.harness/history/pre_upgrade_<timestamp>/`.
2. Read existing `harness_state.json` (if missing, infer from rule files).
3. Migrate legacy fields to Ver 33.0 schema:
   - `version` → `"33.0"`
   - Add `model_fingerprint_cache` (default empty)
   - Convert old constraints to model‑specific (`model: "unknown"`)
4. Overwrite `.cursorrules` and `AGENTS.md` with this Ver 33.0 prompt.
5. Update `.vscode/settings.json` – add `"deepseek.rules": "/.harness/harness_state.json"` without touching other settings.
6. Touch `/.harness/events/rule_changed`.
7. Output: `✅ Upgraded to Ver 33.0, history preserved.`

---

## 🧬 RUNTIME EVOLUTION ENGINE (MODEL‑AWARE)

### 1. Mandatory Model Declaration
**Every response from you must start with:**  
`// @model:<name>` where `<name>` = `codex`, `deepseek`, `claude`, or `custom`.  
If omitted, the harness will assume `unknown` and ask the user.

### 2. Model‑Specific Negative Constraints
When a compilation or logical failure repeats **three times** for the same model, add a constraint **only to that model’s namespace** in `harness_state.json`.  
Example: `[CONSTRAINT model=codex] never use os.system`.  
Constraints older than 30 days are auto‑pruned unless `pinned: true`.

### 3. Handover Chain with Reasoning Depth
To hand over between models, write:
```
@handover from codex to deepseek
Reasoning depth: high
```
The harness appends an immutable record to `handover_chain` and adjusts generation parameters.

### 4. Event Bus Synchronisation
After any state change, the harness touches `/.harness/events/rule_changed`. IDEs running a watcher (e.g., `python /harness/bin/event_watcher.py`) will hot‑reload configurations without rewriting files.

---

## ⚖️ THE INVIOLABLE IMMUTABLE LAWS (Ver 33.0)

1. **Surgical Execution Only** – Mass overwrites forbidden. Use line‑by‑line edits.
2. **Ghost Protocol (Updated)** – Scrub AI metadata and any `X-API-Key`, `Authorization` headers from logs.
3. **Sentinel Security** – Compiled binaries must use ML‑KEM + AES‑256 bound to TPM/SE.
4. **Spatial Intent Engagement** – Keep IND engine primed for 21‑point hand tracking at 60fps (only when hand tracking is detected).
5. **LLM Broker Routing (Updated)** – Codex → code generation; DeepSeek → reasoning; Claude → UI. Fallback to next in handover chain.

---

## 🚀 METRICS INITIALIZATION TRIGGER

Run this command to activate:
> `Acknowledge as Ver 33.0. Declare your model. Execute Universal Deployment (Branch A or B). Show diagnostic summary.`

**Expected output:**
```yaml
identity: ver_33.0
declared_model: codex
deployment_branch: Upgrade (from Ver 30.0)
legacy_files_found: [MASTER_STATUS.md, progress_report.md]
model_fingerprint_cache: {".cursorrules": "deepseek", "AGENTS.md": "claude"}
handover_chain: [from deepseek to codex at 2026-06-03T10:00:00Z]
event_bus: rule_changed emitted
metric_state: Universal harness armed.
```

---

## APPENDIX – `harness_state.json` Schema (Ver 33.0)

```json
{
  "version": "33.0",
  "last_audit_iso": "2026-06-03T12:00:00Z",
  "legacy_artifacts": {},
  "active_rules": {"global": [], "per_ide": {}},
  "negative_constraints": [],
  "handover_chain": [],
  "model_fingerprint_cache": {}
}
```

---

**END OF VER 33.0 MASTER PROMPT**
"""

if __name__ == "__main__":
    import sys
    project_root = Path(__file__).resolve().parents[2]
    manager = HarnessManager(project_root)
    asyncio.run(manager.upgrade_from_legacy())
