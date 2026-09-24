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
            v = v.strip().strip('"').strip("'")
            if v:   # an empty value means "unset"; boto3 treats AWS_PROFILE="" as a profile name
                os.environ.setdefault(k.strip(), v)


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


def _default_user_name() -> str:
    try:
        import getpass
        return getpass.getuser().replace(".", " ").replace("_", " ").title()
    except Exception:
        return "Analyst"


# Shown in the sidebar profile row (no sign-in yet: set these per user in .env)
USER_NAME = os.getenv("CIQ_USER_NAME") or _default_user_name()
USER_EMAIL = os.getenv("CIQ_USER_EMAIL", "")
USER_ROLE = os.getenv("CIQ_USER_ROLE", "Brand analytics")
