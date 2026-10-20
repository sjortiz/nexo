"""Hardware profiles for running Nexo's models locally with Ollama.

A profile names a machine and the memory its models can use:

  m1-max-64, m5-pro-24, m1-pro-16   Apple Silicon: chip and unified memory
  gpu-24, gpu-48                    Linux/Windows with an NVIDIA or AMD GPU: VRAM
  cpu-64                            CPU only: system RAM (works, but slowly)

Memory picks the model tier (the largest tier that fits); the chip is
informational. One catalog serves both Nexo (settings) and
`deploy/local/ollama-setup.sh`, so the models Nexo asks for are the models the
script pulls.

Role sets:
  single - one machine runs every role
  heavy  - the agent and extraction/resolution models (the large machine)
  light  - router, judge and embeddings (the machine that also runs Neo4j and Nexo)

Standard library only: the setup script runs this file directly with python3.

    python3 src/nexo/local_profiles.py plan m1-max-64 heavy
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass

APPLE_RE = re.compile(
    r"^m(?P<generation>[1-9])(?:-(?P<variant>pro|max|ultra))?-(?P<memory>\d{1,3})$"
)
ACCELERATOR_RE = re.compile(r"^(?P<kind>gpu|cpu)-(?P<memory>\d{1,4})$")
# Apple Silicon generations and unified-memory sizes Apple has shipped (M1 to M5).
APPLE_GENERATIONS = range(1, 6)
APPLE_MEMORY_GB = (8, 16, 18, 24, 32, 36, 48, 64, 96, 128, 192, 256, 512)

AGENT_ROLES = ("agent", "extraction")
LIGHT_ROLES = ("router", "judge", "embedding")
ROLE_SETS = {
    "single": AGENT_ROLES + LIGHT_ROLES,
    "heavy": AGENT_ROLES,
    "light": LIGHT_ROLES,
}

# Models that reason before answering; their JSON roles get reasoning_effort=none
# because the schema is only enforced after the reasoning section.
THINKING_MODELS = frozenset({"qwen3.5:9b-q4_K_M", "qwen3.6:35b-a3b-q4_K_M"})

# Approximate download sizes (GB) from the Ollama library, for memory planning.
MODEL_SIZES_GB = {
    "qwen3.5:9b-q4_K_M": 6.6,
    "qwen3:4b-instruct-2507-q4_K_M": 2.5,
    "qwen3:30b-a3b-instruct-2507-q4_K_M": 19.0,
    "qwen3.6:35b-a3b-q4_K_M": 24.0,
    "llama3.1:8b-instruct-q4_K_M": 4.9,
    "bge-m3": 1.2,
}

SMALL, MID, LARGE, ROUTER, JUDGE, EMBED = (
    "qwen3.5:9b-q4_K_M",
    "qwen3:30b-a3b-instruct-2507-q4_K_M",
    "qwen3.6:35b-a3b-q4_K_M",
    "qwen3:4b-instruct-2507-q4_K_M",
    "llama3.1:8b-instruct-q4_K_M",
    "bge-m3",
)

# Per tier: models for each role set (None = role disabled), Ollama context length
# and parallel requests. A single Mac reuses its chat model for routing and skips
# the judge where memory is tight; enable one explicitly with JUDGE_MODEL.
TIERS: dict[int, dict] = {
    16: {
        "context_length": 8192,
        "num_parallel": 1,
        "single": {
            "agent": SMALL,
            "extraction": SMALL,
            "router": SMALL,
            "judge": None,
            "embedding": EMBED,
        },
        "heavy": {"agent": SMALL, "extraction": SMALL},
        "light": {"router": ROUTER, "judge": None, "embedding": EMBED},
    },
    24: {
        "context_length": 16384,
        "num_parallel": 1,
        "single": {
            "agent": SMALL,
            "extraction": SMALL,
            "router": SMALL,
            "judge": None,
            "embedding": EMBED,
        },
        "heavy": {"agent": SMALL, "extraction": SMALL},
        "light": {"router": ROUTER, "judge": JUDGE, "embedding": EMBED},
    },
    32: {
        "context_length": 16384,
        "num_parallel": 1,
        "single": {
            "agent": MID,
            "extraction": MID,
            "router": MID,
            "judge": None,
            "embedding": EMBED,
        },
        "heavy": {"agent": MID, "extraction": MID},
        "light": {"router": ROUTER, "judge": JUDGE, "embedding": EMBED},
    },
    48: {
        "context_length": 32768,
        "num_parallel": 1,
        "single": {
            "agent": MID,
            "extraction": MID,
            "router": ROUTER,
            "judge": JUDGE,
            "embedding": EMBED,
        },
        "heavy": {"agent": MID, "extraction": MID},
        "light": {"router": ROUTER, "judge": JUDGE, "embedding": EMBED},
    },
    64: {
        "context_length": 32768,
        "num_parallel": 2,
        "single": {
            "agent": LARGE,
            "extraction": LARGE,
            "router": ROUTER,
            "judge": JUDGE,
            "embedding": EMBED,
        },
        "heavy": {"agent": LARGE, "extraction": LARGE},
        "light": {"router": ROUTER, "judge": JUDGE, "embedding": EMBED},
    },
}


class ProfileError(ValueError):
    """An invalid profile name or role set."""


@dataclass(frozen=True)
class HostProfile:
    """A parsed host profile: chip, accelerator kind, memory and catalog tier."""

    name: str
    chip: str  # "M1 Max", "GPU", "CPU"
    kind: str  # apple | gpu | cpu
    memory_gb: int
    tier_gb: int


@dataclass(frozen=True)
class Plan:
    """What one Mac serves: role -> model, plus Ollama server settings."""

    profile: HostProfile
    role_set: str
    roles: dict[str, str | None]
    context_length: int
    num_parallel: int

    @property
    def models(self) -> list[str]:
        """Distinct models this plan serves, sorted."""
        return sorted({m for m in self.roles.values() if m})

    @property
    def weights_gb(self) -> float:
        """Total size of the plan's model weights in GB."""
        return round(sum(MODEL_SIZES_GB[m] for m in self.models), 1)


def parse_profile(name: str) -> HostProfile:
    """Parse a name like m1-max-64 or gpu-24 and pick the largest tier that fits."""
    normalized = name.strip().lower()
    if match := APPLE_RE.fullmatch(normalized):
        kind = "apple"
        if int(match["generation"]) not in APPLE_GENERATIONS:
            raise ProfileError(
                f"invalid profile {name!r}: no Apple M{match['generation']} chip;"
                f" use M{APPLE_GENERATIONS[0]} to M{APPLE_GENERATIONS[-1]}"
            )
        if int(match["memory"]) not in APPLE_MEMORY_GB:
            raise ProfileError(
                f"invalid profile {name!r}: Apple Silicon ships"
                f" {', '.join(map(str, APPLE_MEMORY_GB))} GB, not {match['memory']}"
            )
        chip = f"M{match['generation']}" + (
            f" {match['variant'].capitalize()}" if match["variant"] else ""
        )
    elif match := ACCELERATOR_RE.fullmatch(normalized):
        kind = match["kind"]
        chip = kind.upper()
    else:
        raise ProfileError(
            f"invalid profile {name!r}: expected e.g. m1-max-64, m5-pro-24, gpu-24, cpu-64"
        )
    memory = int(match["memory"])
    eligible = [tier for tier in TIERS if tier <= memory]
    if not eligible:
        unit = {"apple": "unified memory", "gpu": "VRAM", "cpu": "RAM"}[kind]
        raise ProfileError(f"{name}: at least {min(TIERS)} GB of {unit} is required")
    return HostProfile(
        name=normalized, chip=chip, kind=kind, memory_gb=memory, tier_gb=max(eligible)
    )


def plan(profile: str, role_set: str = "single") -> Plan:
    """Return the models and Ollama settings for a host profile and role set."""
    if role_set not in ROLE_SETS:
        raise ProfileError(f"role set must be one of {sorted(ROLE_SETS)}")
    host = parse_profile(profile)
    tier = TIERS[host.tier_gb]
    return Plan(
        profile=host,
        role_set=role_set,
        roles=dict(tier[role_set]),
        context_length=tier["context_length"],
        num_parallel=tier["num_parallel"],
    )


def needs_reasoning_off(model: str | None) -> bool:
    """Whether the model thinks before answering and needs reasoning off for JSON roles."""
    return model in THINKING_MODELS


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Nexo local model profiles")
    sub = parser.add_subparsers(dest="command", required=True)
    show = sub.add_parser("plan", help="models and Ollama settings for one Mac")
    show.add_argument("profile")
    show.add_argument(
        "role_set", nargs="?", default="single", choices=sorted(ROLE_SETS)
    )
    show.add_argument("--format", choices=["json", "shell"], default="json")
    sub.add_parser("tiers", help="the whole catalog as JSON")
    args = parser.parse_args(argv)

    if args.command == "tiers":
        sys.stdout.write(json.dumps(TIERS, indent=2) + "\n")
        return 0
    try:
        result = plan(args.profile, args.role_set)
    except ProfileError as exc:
        sys.stderr.write(f"{exc}\n")
        return 2
    if args.format == "shell":  # consumed by deploy/local/ollama-setup.sh
        sys.stdout.write(
            f"MODELS='{' '.join(result.models)}'\n"
            f"EMBEDDING_MODELS='{result.roles.get('embedding') or ''}'\n"
            f"CONTEXT_LENGTH={result.context_length}\n"
            f"NUM_PARALLEL={result.num_parallel}\n"
            f"MAX_LOADED_MODELS={max(1, len(result.models))}\n"
            f"WEIGHTS_GB={result.weights_gb}\n"
        )
    else:
        sys.stdout.write(
            json.dumps(
                {
                    **asdict(result),
                    "models": result.models,
                    "weights_gb": result.weights_gb,
                },
                indent=2,
            )
            + "\n"
        )
    return 0


if __name__ == "__main__":
    sys.exit(_main())
