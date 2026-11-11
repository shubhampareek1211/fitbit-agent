"""Central configuration: paths, models, and hard caps (caps live in code, not prompts)."""
import json
import logging
import os
import threading
import time
from pathlib import Path

log = logging.getLogger("pulse")

ROOT = Path(__file__).resolve().parent.parent
SNAPSHOTS = ROOT / "data" / "snapshots"
METRICS_YAML = ROOT / "data" / "metrics.yaml"


BUCKET = os.environ.get("PULSE_BUCKET")           # if set, the newest snapshot is loaded from Cloud Storage
REFRESH_CHECK_S = int(os.environ.get("PULSE_REFRESH_CHECK_S", "600"))  # how often to look for a newer one
CACHE = Path(os.environ.get("PULSE_CACHE", "/tmp/pulse-snapshots"))
_remote = {"checked": 0.0, "path": None, "generation": None}
_lock = threading.Lock()


def _remote_snapshot() -> Path | None:
    """The newest snapshot in the bucket, downloaded once per version. The scheduled refresh uploads
    snapshots/<day>/pulse.duckdb and then rewrites latest.json, so readers never see a half-written file.
    Any failure (no network, no permission, bad pointer) keeps the snapshot already in use."""
    now = time.monotonic()
    with _lock:
        if _remote["path"] and now - _remote["checked"] < REFRESH_CHECK_S:
            return _remote["path"]
        _remote["checked"] = now
        try:
            from google.cloud import storage
            bucket = storage.Client().bucket(BUCKET)
            latest = json.loads(bucket.blob("latest.json").download_as_text())
            blob = bucket.get_blob(latest["object"])
            if blob is None:
                raise FileNotFoundError(latest["object"])
            if blob.generation != _remote["generation"]:
                CACHE.mkdir(parents=True, exist_ok=True)
                target = CACHE / f"{latest['day']}-{blob.generation}.duckdb"
                if not target.exists():
                    tmp = target.with_suffix(".part")
                    blob.download_to_filename(str(tmp))
                    tmp.rename(target)
                _remote.update(path=target, generation=blob.generation)
                log.info("snapshot %s (as of %s, fetched %s) loaded from gs://%s", latest["day"],
                         latest.get("asof"), latest.get("fetched_at"), BUCKET)
        except Exception as e:  # keep serving what we have
            log.warning("snapshot refresh from gs://%s failed: %s: %s", BUCKET, type(e).__name__, str(e)[:200])
        return _remote["path"]


def latest_snapshot() -> Path:
    override = os.environ.get("PULSE_SNAPSHOT")
    if override:
        return Path(override)
    if BUCKET and (remote := _remote_snapshot()):
        return remote
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
AGENT_TIMEOUT_S = {"gatekeeper": 30, "lead": 60, "researcher": 90, "writer": 90}
GLOBAL_DEADLINE_S = 200   # graders wait up to 300 s; leave headroom
GATEKEEPER_RETRIES = 1    # one more try after a hang or an unparseable reply
WRITER_RETRIES = 1        # Verifier feedback rounds
