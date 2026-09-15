"""Campaign configuration and application paths."""

import os
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_environment() -> None:
    """Load local secrets/configuration without overriding shell variables."""
    load_dotenv(PROJECT_ROOT / ".env.local")
    load_dotenv(PROJECT_ROOT / ".env")


class CampaignConfig(BaseModel):
    """Loaded campaign settings and paths derived from the input event."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    base: Path
    raw: dict

    @property
    def organization_id(self) -> str:
        """Return the organization allowed to reach this campaign."""
        return self.raw["campana"]["organization_id"]

    @property
    def timezone(self) -> str:
        """Return the IANA timezone used for all date arithmetic."""
        return self.raw["campana"]["zona_horaria"]

    @property
    def event_schema_path(self) -> Path:
        """Return the official event schema shipped with the challenge."""
        return self.base / "esquemas" / "evento.schema.json"

    @property
    def prompt_path(self) -> Path:
        """Return the versioned local classifier prompt."""
        return PROJECT_ROOT / "prompts" / "classification_v1.md"

    @classmethod
    def load(cls, event_path: str | Path) -> "CampaignConfig":
        load_environment()
        path = Path(event_path).resolve()
        configured_base = os.getenv("KONTAKTU_BASE")
        if configured_base:
            configured_path = Path(configured_base)
            base = (
                configured_path
                if configured_path.is_absolute()
                else PROJECT_ROOT / configured_path
            ).resolve()
        else:
            base = path.parent.parent
        config_path = base / "config" / "campana.yaml"
        if not config_path.is_file():
            raise ValueError(f"campaign configuration does not exist: {config_path}")
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or "campana" not in raw:
            raise ValueError("invalid campaign configuration")
        return cls(base=base, raw=raw)
