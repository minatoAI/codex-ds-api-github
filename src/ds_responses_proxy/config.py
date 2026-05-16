from dataclasses import dataclass
import json
import os
from pathlib import Path
import tomllib


DEFAULT_MODEL_MAPPING = {
    "gpt-5.5": "deepseek-v4-pro",
    "gpt-5.4": "deepseek-v4-pro",
    "gpt-5.4-mini": "deepseek-v4-flash",
    "gpt-5.3-codex": "deepseek-v4-pro",
    "gpt-5.3-codex-spark": "deepseek-v4-flash",
    "gpt-5.2": "deepseek-v4-pro",
}


@dataclass(frozen=True)
class Settings:
    deepseek_api_key: str | None
    deepseek_base_url: str
    deepseek_model: str
    model_mapping: dict[str, str]
    model_mapping_strict: bool
    state_backend: str
    state_db_path: str
    state_ttl_seconds: int
    state_max_entries: int
    web_search_backend: str | None
    web_search_mcp_command: str
    web_search_mcp_args: list[str]
    web_search_mcp_tool: str
    web_search_mcp_framing: str
    web_search_max_results: int
    web_search_timeout_seconds: int
    image_preprocessor: str | None
    image_preprocessor_endpoint: str | None
    image_preprocessor_api_key: str | None
    image_preprocessor_model: str | None
    image_preprocessor_prompt: str | None

    def resolve_deepseek_model(self, client_model: str) -> str | None:
        mapped = self.model_mapping.get(client_model)
        if mapped is not None:
            return mapped
        if self.model_mapping_strict:
            return None
        return self.deepseek_model


def get_settings(config_path: str | None = None) -> Settings:
    config = _load_config(config_path)
    return Settings(
        deepseek_api_key=_setting(config, ("deepseek", "api_key"), "DEEPSEEK_API_KEY"),
        deepseek_base_url=_setting(
            config,
            ("deepseek", "base_url"),
            "DEEPSEEK_BASE_URL",
            "https://api.deepseek.com",
        ).rstrip("/"),
        deepseek_model=_setting(config, ("deepseek", "model"), "DEEPSEEK_MODEL", "deepseek-v4-pro"),
        model_mapping=_load_model_mapping(config),
        model_mapping_strict=_bool_setting(config, ("model", "mapping_strict"), "MODEL_MAPPING_STRICT", False),
        state_backend=_setting(config, ("state", "backend"), "STATE_BACKEND", "memory"),
        state_db_path=_setting(
            config,
            ("state", "db_path"),
            "STATE_DB_PATH",
            ".proxy-state/responses-state.sqlite3",
        ),
        state_ttl_seconds=_int_setting(
            config,
            ("state", "ttl_seconds"),
            ("STATE_TTL_SECONDS", "PROXY_STATE_TTL_SECONDS"),
            604800,
        ),
        state_max_entries=_int_setting(
            config,
            ("state", "max_entries"),
            ("STATE_MAX_ENTRIES", "PROXY_STATE_MAX_ENTRIES"),
            10000,
        ),
        web_search_backend=_setting(config, ("web_search", "backend"), "WEB_SEARCH_BACKEND"),
        web_search_mcp_command=_setting(config, ("web_search", "mcp_command"), "WEB_SEARCH_MCP_COMMAND", "uvx"),
        web_search_mcp_args=_string_list_setting(
            config,
            ("web_search", "mcp_args"),
            "WEB_SEARCH_MCP_ARGS",
            ["duckduckgo-mcp-server"],
        ),
        web_search_mcp_tool=_setting(config, ("web_search", "mcp_tool"), "WEB_SEARCH_MCP_TOOL", "search"),
        web_search_mcp_framing=_setting(config, ("web_search", "mcp_framing"), "WEB_SEARCH_MCP_FRAMING", "jsonl"),
        web_search_max_results=_int_setting(config, ("web_search", "max_results"), "WEB_SEARCH_MAX_RESULTS", 5),
        web_search_timeout_seconds=_int_setting(
            config,
            ("web_search", "timeout_seconds"),
            "WEB_SEARCH_TIMEOUT_SECONDS",
            30,
        ),
        image_preprocessor=_setting(config, ("image", "preprocessor"), "IMAGE_PREPROCESSOR"),
        image_preprocessor_endpoint=_setting(
            config,
            ("image", "preprocessor_endpoint"),
            "IMAGE_PREPROCESSOR_ENDPOINT",
        ),
        image_preprocessor_api_key=_setting(
            config,
            ("image", "preprocessor_api_key"),
            "IMAGE_PREPROCESSOR_API_KEY",
        ),
        image_preprocessor_model=_setting(
            config,
            ("image", "preprocessor_model"),
            "IMAGE_PREPROCESSOR_MODEL",
        ),
        image_preprocessor_prompt=_setting(
            config,
            ("image", "preprocessor_prompt"),
            "IMAGE_PREPROCESSOR_PROMPT",
        ),
    )


def _load_config(config_path: str | None) -> dict:
    raw_path = config_path or os.getenv("DS_RESPONSES_CONFIG") or "config.toml"
    path = Path(raw_path)
    if not path.exists():
        if config_path is not None or os.getenv("DS_RESPONSES_CONFIG"):
            raise ValueError(f"Config file does not exist: {path}")
        return {}
    if path.suffix == ".json":
        with path.open("r", encoding="utf-8") as file:
            config = json.load(file)
    else:
        with path.open("rb") as file:
            config = tomllib.load(file)
    if not isinstance(config, dict):
        raise ValueError("Config file must contain a top-level object.")
    return config


def _load_model_mapping(config: dict) -> dict[str, str]:
    mapping = dict(DEFAULT_MODEL_MAPPING)
    override = _config_value(config, ("model", "mapping"))
    raw = os.getenv("MODEL_MAPPING_JSON")
    if raw is not None and raw.strip() != "":
        try:
            override = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"MODEL_MAPPING_JSON must be valid JSON: {exc}") from exc
    if override is None:
        return mapping
    if not isinstance(override, dict):
        raise ValueError("Model mapping must be a JSON/TOML object.")
    for key, value in override.items():
        if not isinstance(key, str) or not isinstance(value, str) or not key or not value:
            raise ValueError("Model mapping entries must map non-empty strings to non-empty strings.")
        mapping[key] = value
    return mapping


def _setting(config: dict, path: tuple[str, ...], env_name: str, default: str | None = None) -> str | None:
    value = os.getenv(env_name)
    if value is None or value.strip() == "":
        value = _config_value(config, path)
    if value is None:
        return default
    if not isinstance(value, str):
        raise ValueError(f"{'.'.join(path)} must be a string.")
    return value


def _int_setting(
    config: dict,
    path: tuple[str, ...],
    env_names: str | tuple[str, ...],
    default: int,
) -> int:
    names = (env_names,) if isinstance(env_names, str) else env_names
    value = None
    for name in names:
        raw = os.getenv(name)
        if raw is not None and raw.strip() != "":
            value = raw
            break
    if value is None:
        value = _config_value(config, path)
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{'.'.join(path)} must be an integer.") from exc


def _string_list_setting(config: dict, path: tuple[str, ...], env_name: str, default: list[str]) -> list[str]:
    raw = os.getenv(env_name)
    if raw is not None and raw.strip() != "":
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{env_name} must be a JSON array of strings.") from exc
    else:
        value = _config_value(config, path)
    if value is None:
        return list(default)
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise ValueError(f"{'.'.join(path)} must be an array of non-empty strings.")
    return list(value)


def _bool_setting(config: dict, path: tuple[str, ...], env_name: str, default: bool) -> bool:
    raw = os.getenv(env_name)
    if raw is not None and raw.strip() != "":
        return _truthy(raw)
    value = _config_value(config, path)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return _truthy(value)
    raise ValueError(f"{'.'.join(path)} must be a boolean.")


def _config_value(config: dict, path: tuple[str, ...]) -> object:
    current: object = config
    for part in path:
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _truthy(value: str | None) -> bool:
    return value is not None and value.lower() in {"1", "true", "yes", "on"}
