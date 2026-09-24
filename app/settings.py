"""Runtime settings for the CIQ app (override with environment variables)."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _load_dotenv(path: Path) -> None:
    """Load KEY=VALUE lines from .env into the environment (real environment variables win)."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv(ROOT.parent / ".env")

STATIC_DIR = ROOT / "static"
CONVERSATION_DIR = ROOT.parent / "Data" / "conversations"

AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
AWS_PROFILE = os.getenv("AWS_PROFILE") or None          # None -> default credential chain
MODEL_ID = os.getenv("CIQ_MODEL", "us.anthropic.claude-sonnet-5")   # Bedrock inference profile
MODEL_LABEL = os.getenv("CIQ_MODEL_LABEL", "Claude Sonnet 5")
EFFORT = os.getenv("CIQ_EFFORT", "high")                # low | medium | high | xhigh | max
MAX_TOKENS = int(os.getenv("CIQ_MAX_TOKENS", "32000"))
MAX_AGENT_STEPS = int(os.getenv("CIQ_MAX_AGENT_STEPS", "12"))
