"""Settings and logging.

Two conventions this project inherits from equity-research-agent:
  - Logging is plain ASCII on stdout. Agno installs a ColoredRichHandler that emits
    box-drawing glyphs, so handlers are replaced before any Agent is constructed.
  - stdout is reconfigured to UTF-8 with errors="replace" because the model emits
    currency signs and cp1252 consoles raise on them.
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]

load_dotenv(PROJECT_ROOT / ".env")


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(_env(name, str(default))))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except ValueError:
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = _env(name, "true" if default else "false").lower()
    return raw in ("1", "true", "yes", "on")


def _env_list(name: str, default: str = "") -> list[str]:
    return [p.strip().upper() for p in _env(name, default).split(",") if p.strip()]


def _resolve(path_value: str) -> Path:
    p = Path(path_value)
    return p if p.is_absolute() else PROJECT_ROOT / p


@dataclass
class Settings:
    # OpenAlgo
    openalgo_api_key: str = ""
    openalgo_host: str = "http://127.0.0.1:5000"
    openalgo_ws_url: str = "ws://127.0.0.1:8765"
    openalgo_api_version: str = "v1"
    openalgo_timeout: float = 15.0
    openalgo_instruments_timeout: float = 30.0

    # Model. LiteLLM fronts every provider, so there is ONE key and ONE optional base
    # URL; the provider is decided by the prefix on litellm_model.
    litellm_api_key: str = ""
    litellm_model: str = "baseten/deepseek-ai/DeepSeek-V4-Flash-0731"
    litellm_api_base: str = ""
    # "full" (43 tools), "lean" (a small high-value subset), or "" to choose
    # automatically: lean for local models, full otherwise.
    tool_profile: str = ""
    litellm_temperature: float = 0.2
    litellm_top_p: float = 1.0
    # This model reasons before it answers. A low cap returns EMPTY content, not short
    # content, because the whole budget goes to reasoning tokens. Do not lower this.
    litellm_max_tokens: int = 4096
    litellm_include_usage: bool = True
    # "" (provider default), or none | minimal | low | medium | high.
    # "none" switches thinking OFF where the provider supports it.
    litellm_reasoning_effort: str = ""

    # Risk
    trading_enabled: bool = False
    require_analyzer_mode: bool = True
    default_strategy_name: str = "TradingAgent"
    max_order_value: float = 100_000.0
    max_order_quantity: int = 1000
    max_orders_per_session: int = 25
    max_price_deviation_pct: float = 20.0
    duplicate_order_window_sec: int = 10
    allowed_exchanges: list[str] = field(default_factory=list)
    allowed_products: list[str] = field(default_factory=list)
    symbol_denylist: list[str] = field(default_factory=list)
    kill_switch_file: Path = PROJECT_ROOT / "data" / "KILL"

    # App
    backend_host: str = "127.0.0.1"
    backend_port: int = 8088
    cors_origins: list[str] = field(default_factory=list)
    log_level: str = "INFO"
    db_path: Path = PROJECT_ROOT / "data" / "trading.db"
    audit_dir: Path = PROJECT_ROOT / "data" / "audit"
    timezone: str = "Asia/Kolkata"

    # Alerts
    telegram_username: str = ""
    whatsapp_default_to: str = ""

    @classmethod
    def load(cls) -> "Settings":
        return cls(
            openalgo_api_key=_env("OPENALGO_API_KEY"),
            openalgo_host=_env("OPENALGO_HOST", "http://127.0.0.1:5000").rstrip("/"),
            openalgo_ws_url=_env("OPENALGO_WS_URL", "ws://127.0.0.1:8765"),
            openalgo_api_version=_env("OPENALGO_API_VERSION", "v1"),
            openalgo_timeout=_env_float("OPENALGO_TIMEOUT", 15.0),
            openalgo_instruments_timeout=_env_float("OPENALGO_INSTRUMENTS_TIMEOUT", 30.0),
            litellm_api_key=_env("LITELLM_API_KEY"),
            litellm_model=_env("LITELLM_MODEL", "baseten/deepseek-ai/DeepSeek-V4-Flash-0731"),
            tool_profile=_env("TOOL_PROFILE").lower(),
            # Empty means "let LiteLLM resolve the provider's own default endpoint".
            litellm_api_base=_env("LITELLM_API_BASE"),
            litellm_temperature=_env_float("LITELLM_TEMPERATURE", 0.2),
            litellm_top_p=_env_float("LITELLM_TOP_P", 1.0),
            litellm_max_tokens=_env_int("LITELLM_MAX_TOKENS", 4096),
            litellm_include_usage=_env_bool("LITELLM_INCLUDE_USAGE", True),
            litellm_reasoning_effort=_env("LITELLM_REASONING_EFFORT").lower(),
            trading_enabled=_env_bool("TRADING_ENABLED", False),
            require_analyzer_mode=_env_bool("REQUIRE_ANALYZER_MODE", True),
            default_strategy_name=_env("DEFAULT_STRATEGY_NAME", "TradingAgent"),
            max_order_value=_env_float("MAX_ORDER_VALUE", 100_000.0),
            max_order_quantity=_env_int("MAX_ORDER_QUANTITY", 1000),
            max_orders_per_session=_env_int("MAX_ORDERS_PER_SESSION", 25),
            max_price_deviation_pct=_env_float("MAX_PRICE_DEVIATION_PCT", 20.0),
            duplicate_order_window_sec=_env_int("DUPLICATE_ORDER_WINDOW_SEC", 10),
            allowed_exchanges=_env_list("ALLOWED_EXCHANGES", "NSE,NFO,BSE,BFO,MCX,CDS"),
            allowed_products=_env_list("ALLOWED_PRODUCTS", "MIS,CNC,NRML"),
            symbol_denylist=_env_list("SYMBOL_DENYLIST", ""),
            kill_switch_file=_resolve(_env("KILL_SWITCH_FILE", "data/KILL")),
            backend_host=_env("BACKEND_HOST", "127.0.0.1"),
            backend_port=_env_int("BACKEND_PORT", 8088),
            cors_origins=[o.strip() for o in _env(
                "CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"
            ).split(",") if o.strip()],
            log_level=_env("LOG_LEVEL", "INFO").upper(),
            db_path=_resolve(_env("DB_PATH", "data/trading.db")),
            audit_dir=_resolve(_env("AUDIT_DIR", "data/audit")),
            timezone=_env("TIMEZONE", "Asia/Kolkata"),
            telegram_username=_env("OPENALGO_TELEGRAM_USERNAME"),
            whatsapp_default_to=_env("WHATSAPP_DEFAULT_TO"),
        )

    # Providers that run on your own machine and need no credential at all.
    LOCAL_PROVIDERS = ("ollama", "ollama_chat", "lm_studio", "llamafile", "vllm")

    @property
    def model_provider(self) -> str:
        """The LiteLLM provider prefix, e.g. 'ollama' from 'ollama_chat/gemma4:e4b'."""
        return self.litellm_model.split("/", 1)[0].strip().lower() if "/" in self.litellm_model else ""

    @property
    def is_local_model(self) -> bool:
        return self.model_provider in self.LOCAL_PROVIDERS

    def resolve_model_api_key(self) -> str | None:
        """The single credential for the configured provider, or None if none is needed.

        There is deliberately only one key setting. Whichever provider LITELLM_MODEL
        names, LITELLM_API_KEY is its key. Local providers get None, because passing an
        empty string makes some LiteLLM paths send an Authorization header that a local
        server does not expect.
        """
        if self.is_local_model:
            return None
        return self.litellm_api_key or None

    def resolve_model_api_base(self) -> str | None:
        """Explicit base URL, or None to let LiteLLM use the provider default."""
        return self.litellm_api_base or None

    REASONING_EFFORTS = ("none", "minimal", "low", "medium", "high")

    def resolve_reasoning_effort(self) -> str | None:
        """Validated reasoning effort, or None to leave the provider's default alone.

        Measured behaviour differs sharply by provider, so this is a request, not a
        guarantee:

          Ollama (gemma4:e4b) - LiteLLM maps reasoning_effort onto Ollama's `think`
            flag, which is a BOOLEAN. "none" genuinely turns thinking off (measured:
            1869 thinking characters and 18.4s, down to 0 and 7.8s). Any of
            low/medium/high simply means "on"; there is no gradation.

          Baseten (DeepSeek V4 Flash) - the value is passed through and does scale the
            reasoning budget (low -> 1283 reasoning tokens, high -> 1498), but "none"
            does NOT disable it. Raising effort without also raising max_tokens can
            consume the whole budget and return EMPTY content; see the warning in
            build_model.
        """
        value = (self.litellm_reasoning_effort or "").strip().lower()
        if not value:
            return None
        if value not in self.REASONING_EFFORTS:
            import logging
            logging.getLogger(__name__).warning(
                "LITELLM_REASONING_EFFORT=%r is not one of %s; ignoring it.",
                value, ", ".join(self.REASONING_EFFORTS))
            return None
        return value

    @property
    def thinking_disabled(self) -> bool:
        return self.resolve_reasoning_effort() == "none"

    def reasoning_capability(self) -> dict:
        """What thinking control THIS model actually offers, detected per model.

        Detection is dynamic: openai/gpt-4o cannot reason while openai/o3 can, so the
        answer depends on the exact model, not the provider. See
        model_providers.detect_reasoning_support.
        """
        from .model_providers import detect_reasoning_support

        found = detect_reasoning_support(self.litellm_model,
                                         self.resolve_model_api_base())
        current = self.resolve_reasoning_effort() or ""

        if not found["thinks"]:
            options: list[str] = []          # the UI hides the control entirely
        elif found["graded"]:
            options = ["none", "minimal", "low", "medium", "high"]
            if not found["can_disable"]:
                options.remove("none")
        else:
            options = ["none", "low"]        # on/off only

        return {
            "model": self.litellm_model,
            "provider": self.model_provider or "unknown",
            "model_thinks": found["thinks"],
            "supported": options,
            "labels": {"none": "Off", "minimal": "Minimal", "low": "Low" if found["graded"]
                       else "On", "medium": "Medium", "high": "High"},
            "can_disable": found["can_disable"],
            "graded": found["graded"],
            "verified": found["verified"],
            "detected_by": found["detected_by"],
            "current": current,
            "note": found.get("note", ""),
        }

    @property
    def effective_tool_profile(self) -> str:
        """'lean' or 'full'.

        Measured on gemma4:e4b (8B) with the full 26-tool read-only agent: it looped
        through 15 tool calls on a single quote request, and separately claimed it had
        no way to fetch funds while get_funds was in scope. Small models cannot choose
        reliably among that many tools, so local defaults to lean. Override with
        TOOL_PROFILE=full.
        """
        if self.tool_profile in ("lean", "full"):
            return self.tool_profile
        return "lean" if self.is_local_model else "full"

    def missing(self) -> list[str]:
        names = []
        if not self.openalgo_api_key:
            names.append("OPENALGO_API_KEY")
        # A local model needs no credential; every hosted provider needs LITELLM_API_KEY.
        if not self.is_local_model and not self.resolve_model_api_key():
            names.append("LITELLM_API_KEY")
        return names

    def kill_switch_engaged(self) -> bool:
        return self.kill_switch_file.exists()

    def redacted(self) -> dict:
        def mask(v: str) -> str:
            return f"{v[:4]}...{v[-4:]}" if len(v) > 12 else ("set" if v else "(missing)")
        return {
            "openalgo_host": self.openalgo_host,
            "openalgo_api_key": mask(self.openalgo_api_key),
            "litellm_model": self.litellm_model,
            "model_provider": self.model_provider or "(none, LiteLLM will infer)",
            "is_local_model": self.is_local_model,
            "model_api_key": mask(self.resolve_model_api_key() or ""),
            "model_api_base": self.resolve_model_api_base() or "(provider default)",
            "litellm_max_tokens": self.litellm_max_tokens,
            "trading_enabled": self.trading_enabled,
            "require_analyzer_mode": self.require_analyzer_mode,
            "max_order_value": self.max_order_value,
            "allowed_exchanges": self.allowed_exchanges,
            "db_path": str(self.db_path),
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    s = Settings.load()
    s.db_path.parent.mkdir(parents=True, exist_ok=True)
    s.audit_dir.mkdir(parents=True, exist_ok=True)
    return s


def setup_logging(level: str | int | None = None) -> None:
    """Plain ASCII logging. Must run before any Agent is constructed."""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    resolved = level if level is not None else get_settings().log_level
    if isinstance(resolved, str):
        resolved = getattr(logging, resolved.upper(), logging.INFO)

    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s %(message)s", "%H:%M:%S")
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(fmt)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(resolved)

    for name in ("agno", "agno-team", "agno-workflow"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = False
        h = logging.StreamHandler(sys.stdout)
        h.setFormatter(fmt)
        lg.addHandler(h)
        lg.setLevel(logging.WARNING)

    for noisy in ("httpx", "httpcore", "LiteLLM", "litellm", "openai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


if __name__ == "__main__":
    setup_logging()
    import json
    print(json.dumps(get_settings().redacted(), indent=2, default=str))
    print("missing:", get_settings().missing() or "none")
