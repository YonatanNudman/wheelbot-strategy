"""YAML config loader with environment variable interpolation."""

import os
import re
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

load_dotenv()

_CONFIG: dict | None = None
_CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"


def _interpolate_env_vars(value: Any) -> Any:
    """Replace ${ENV_VAR} patterns with actual environment variable values."""
    if isinstance(value, str):
        pattern = re.compile(r"\$\{([^}]+)\}")
        match = pattern.search(value)
        if match:
            env_var = match.group(1)
            env_value = os.getenv(env_var, "")
            return pattern.sub(env_value, value)
        return value
    if isinstance(value, dict):
        return {k: _interpolate_env_vars(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate_env_vars(item) for item in value]
    return value


def load_config(path: Path | None = None) -> dict:
    """Load and cache the YAML config file."""
    global _CONFIG
    if _CONFIG is not None and path is None:
        return _CONFIG

    config_path = path or _CONFIG_PATH
    with open(config_path) as f:
        raw = yaml.safe_load(f)

    _CONFIG = _interpolate_env_vars(raw)
    return _CONFIG


def get(key: str, default: Any = None) -> Any:
    """Get a config value by dot-notation key. E.g. get('wheel.target_delta')."""
    config = load_config()
    keys = key.split(".")
    value = config
    for k in keys:
        if isinstance(value, dict) and k in value:
            value = value[k]
        else:
            return default
    return value
