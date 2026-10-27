"""Settings loaded from environment variables."""
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    NVIDIA_API_KEY: str = ""
    NVIDIA_BASE_URL: str = "https://integrate.api.nvidia.com/v1"
    MODEL_NAME: str = "nvidia/nemotron-3.5-lightning-30b-a3b"
    NVIDIA_EXTRA_BODY_JSON: str = '{"chat_template_kwargs": {"enable_thinking": false}}'
    LLM_MODE: str = "mock"
    ADMIN_TOKEN: str = "change-me-admin-token"
    GATEWAY_URL: str = "http://gateway:8000"
    TOOLS_URL: str = "http://tools:8001"
    GATEWAY_DB_PATH: str = "/data/gateway.db"
    TOOLS_DB_PATH: str = "/data/tools.db"
    GATEWAY_KEY_PATH: str = "/data/gateway_ed25519.pem"
    RESULTS_DIR: str = "results"
    GUARD_BACKEND: str = "mock"
    NVIDIA_GUARD: str = "off"
    NVIDIA_GUARD_FAIL: str = "open"
    SAFETY_MODEL: str = "nvidia/nemotron-3.5-content-safety"
    JUDGE_MODEL: str = "nvidia/nemotron-3.5-lightning-30b-a3b"
    NVIDIA_GUARD_MIN_INTERVAL: str = "1.6"
    GUARD_BASE_URL: str = ""           # empty = use NVIDIA_BASE_URL


def load_settings() -> Settings:
    """Build Settings from os.environ, falling back to the defaults."""
    defaults = Settings()
    values = {f: os.environ.get(f, getattr(defaults, f)) for f in Settings.__dataclass_fields__}
    values["GUARD_BASE_URL"] = values["GUARD_BASE_URL"] or values["NVIDIA_BASE_URL"]
    return Settings(**values)
