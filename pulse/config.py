"""Central configuration: paths, models, and hard caps (caps live in code, not prompts)."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SNAPSHOTS = ROOT / "data" / "snapshots"
METRICS_YAML = ROOT / "data" / "metrics.yaml"


def latest_snapshot() -> Path:
    override = os.environ.get("PULSE_SNAPSHOT")
    if override:
        return Path(override)
    dirs = sorted(p for p in SNAPSHOTS.iterdir() if (p / "pulse.duckdb").exists())
    if not dirs:
        raise SystemExit("No snapshot found. Run: uv run build/normalize.py")
    return dirs[-1] / "pulse.duckdb"


# Vertex AI (Gemini) via Application Default Credentials, same as the class examples.
os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "TRUE")
os.environ.setdefault("GOOGLE_CLOUD_LOCATION", "global")
if not os.environ.get("GOOGLE_CLOUD_PROJECT"):
    try:
        import google.auth
        _, project = google.auth.default()
        if project:
            os.environ["GOOGLE_CLOUD_PROJECT"] = project
    except Exception:
        pass

MODEL_LEAD = os.environ.get("PULSE_MODEL_LEAD", "gemini-3.5-flash")
MODEL_RESEARCHER = os.environ.get("PULSE_MODEL_RESEARCHER", "gemini-3.5-flash-lite")

# Hard caps.
MAX_ROUNDS = 3            # Lead plan + up to 2 gap rounds
MAX_RESEARCHERS = 8       # total researcher runs per question
MAX_PARALLEL = 4          # concurrent researcher runs (Vertex rate limits)
MAX_LLM_CALLS = {"gatekeeper": 6, "lead": 4, "researcher": 12, "writer": 3}
AGENT_TIMEOUT_S = {"gatekeeper": 45, "lead": 60, "researcher": 90, "writer": 90}
GLOBAL_DEADLINE_S = 200   # graders wait up to 300 s; leave headroom
WRITER_RETRIES = 1        # Verifier feedback rounds
