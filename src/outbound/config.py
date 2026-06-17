"""Configuration layer.

Loads non-secret, editable settings from ``config.toml`` and overlays secrets plus a few
overridable values from the environment (via a local ``.env``). Everything is validated into a
typed :class:`Settings` object, so misconfiguration fails fast and loudly instead of surfacing
as a confusing error deep in a run.
"""

from __future__ import annotations

import os
import tomllib
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, Field, model_validator

DEFAULT_MODEL = "claude-sonnet-4-6"


class Paths(BaseModel):
    """Resolved filesystem locations the engine reads from and writes to."""

    root: Path
    context_dir: Path
    data_dir: Path
    outputs_dir: Path
    db_path: Path

    def ensure(self) -> None:
        """Create the runtime directories if they do not yet exist."""
        for directory in (self.context_dir, self.data_dir, self.outputs_dir):
            directory.mkdir(parents=True, exist_ok=True)


class SequenceConfig(BaseModel):
    """Shape of the email sequence the agent writes per prospect."""

    steps: int = Field(default=3, ge=1, le=8)
    days_between: list[int] | None = None
    tone: str = "warm, concrete, low-pressure; craftsmanship-led, not salesy"
    max_words_per_email: int = Field(default=120, ge=40, le=400)

    @model_validator(mode="after")
    def _resolve_cadence(self) -> SequenceConfig:
        if self.days_between is None:
            # First touch at day 0, follow-ups spaced a few days apart.
            self.days_between = [0] + [3 * i for i in range(1, self.steps)]
        elif len(self.days_between) != self.steps:
            raise ValueError(
                f"days_between has {len(self.days_between)} entries but steps={self.steps}"
            )
        return self


class ICPSignals(BaseModel):
    """Keyword signals used (in a later step) to score a prospect's fit."""

    title_keywords: list[str] = Field(default_factory=list)
    industry_keywords: list[str] = Field(default_factory=list)
    preferred_regions: list[str] = Field(default_factory=list)
    exclude_keywords: list[str] = Field(default_factory=list)


class ICPWeights(BaseModel):
    """Relative importance of each signal in the fit score. Must sum to 1.0."""

    title: float = 0.35
    industry: float = 0.35
    geography: float = 0.15
    seniority: float = 0.15

    @model_validator(mode="after")
    def _weights_sum_to_one(self) -> ICPWeights:
        total = self.title + self.industry + self.geography + self.seniority
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"ICP weights must sum to 1.0 (got {total:.3f})")
        return self


class ICPConfig(BaseModel):
    """The ideal-customer profile the engine targets and scores against."""

    description: str = ""
    audiences: list[str] = Field(default_factory=list)
    signals: ICPSignals = Field(default_factory=ICPSignals)
    weights: ICPWeights = Field(default_factory=ICPWeights)


class BudgetConfig(BaseModel):
    """Per-opportunity spend and the fit threshold that unlocks it."""

    per_opportunity_usd: float = Field(default=0.0, ge=0)
    priority_tier_threshold: float = Field(default=0.7, ge=0, le=1)


class Settings(BaseModel):
    """Fully resolved, validated configuration for a run."""

    model: str = DEFAULT_MODEL
    anthropic_api_key: str | None = None
    paths: Paths
    sequence: SequenceConfig = Field(default_factory=SequenceConfig)
    icp: ICPConfig = Field(default_factory=ICPConfig)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)

    def require_api_key(self) -> str:
        """Return the API key or raise a clear error if it is missing."""
        if not self.anthropic_api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set. Copy .env.example to .env and add your key."
            )
        return self.anthropic_api_key


def load_settings(config_path: str | Path = "config.toml") -> Settings:
    """Load and validate settings from ``config.toml`` plus the environment.

    Resolution order: TOML provides editable defaults; ``OUTBOUND_MODEL`` from the environment
    overrides the model; secrets come only from the environment (never the TOML file).
    """
    config_path = Path(config_path)
    load_dotenv()  # Populate os.environ from a local .env if one exists.

    raw: dict = {}
    if config_path.exists():
        with config_path.open("rb") as handle:
            raw = tomllib.load(handle)
        root = config_path.resolve().parent
    else:
        root = Path.cwd()

    paths_raw = raw.get("paths", {})
    paths = Paths(
        root=root,
        context_dir=root / paths_raw.get("context_dir", "context"),
        data_dir=root / paths_raw.get("data_dir", "data"),
        outputs_dir=root / paths_raw.get("outputs_dir", "outputs"),
        db_path=root / paths_raw.get("db_path", "data/engine.sqlite"),
    )

    return Settings(
        model=os.environ.get("OUTBOUND_MODEL", raw.get("model", DEFAULT_MODEL)),
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY"),
        paths=paths,
        sequence=SequenceConfig(**raw.get("sequence", {})),
        icp=ICPConfig(**raw.get("icp", {})),
        budget=BudgetConfig(**raw.get("budget", {})),
    )


@lru_cache
def get_settings() -> Settings:
    """Cached accessor so settings are loaded once per process."""
    return load_settings()
