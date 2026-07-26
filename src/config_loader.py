from pathlib import Path
from typing import Any

import yaml


PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"


def load_yaml(filename: str) -> dict[str, Any]:
    file_path = CONFIG_DIR / filename

    if not file_path.exists():
        raise FileNotFoundError(f"Configuration file not found: {file_path}")

    with file_path.open("r", encoding="utf-8") as file:
        config = yaml.safe_load(file)

    if not isinstance(config, dict):
        raise ValueError(f"Invalid YAML configuration: {file_path}")

    return config


def load_sources() -> list[dict[str, Any]]:
    config = load_yaml("sources.yml")

    return [
        source
        for source in config.get("sources", [])
        if source.get("enabled", True)
    ]


def load_searches() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    config = load_yaml("searches.yml")

    defaults = config.get("defaults", {})
    searches = [
        search
        for search in config.get("searches", [])
        if search.get("enabled", True)
    ]

    return defaults, searches