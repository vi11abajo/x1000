from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore[import-not-found]


def _load_dotenv(path: str | Path = ".env") -> dict[str, str]:
    p = Path(path)
    if not p.exists():
        return {}
    env: dict[str, str] = {}
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def _load_okx_config() -> dict[str, Any]:
    cfg_path = Path(os.environ.get("OKX_CONFIG", Path.home() / ".okx" / "config.toml"))
    if not cfg_path.exists():
        return {}
    with open(cfg_path, "rb") as f:
        return tomllib.load(f)


@dataclass(frozen=True)
class RiskLimits:
    max_position_usd: float = 100.0
    max_daily_loss_usd: float = 50.0
    max_leverage: int = 50
    max_margin_usd: float = 10.0  # own capital per isolated position
    kill_switch_enabled: bool = False
    tp_percent: float = 0.0  # 0 = disabled; e.g. 0.05 = 5% TP
    sl_percent: float = 0.0  # 0 = disabled; e.g. 0.02 = 2% SL
    trailing_callback: float = 0.0  # 0 = disabled; e.g. 0.02 = 2% trailing
    td_mode: str = "isolated"  # cross or isolated


@dataclass(frozen=True)
class TelegramConfig:
    bot_token: str = ""
    chat_id: str = ""
    enabled: bool = False


@dataclass(frozen=True)
class AgentConfig:
    profile: str = "live"
    inst_id: str = "BTC-USDT-SWAP"
    loop_interval_sec: int = 30
    risk: RiskLimits = field(default_factory=RiskLimits)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)

    @classmethod
    def from_env(cls, overrides: dict[str, str] | None = None) -> "AgentConfig":
        env = _load_dotenv()
        if overrides:
            env.update(overrides)

        risk = RiskLimits(
            max_position_usd=float(env.get("MAX_POSITION_USD", "100")),
            max_daily_loss_usd=float(env.get("MAX_DAILY_LOSS_USD", "50")),
            max_leverage=int(env.get("MAX_LEVERAGE", "50")),
            max_margin_usd=float(env.get("MAX_MARGIN_USD", "10")),
            kill_switch_enabled=env.get("KILL_SWITCH", "false").lower() == "true",
            tp_percent=float(env.get("TP_PERCENT", "0")),
            sl_percent=float(env.get("SL_PERCENT", "0")),
            trailing_callback=float(env.get("TRAILING_CALLBACK", "0")),
            td_mode=env.get("TD_MODE", "isolated"),
        )
        tg_token = env.get("TG_BOT_TOKEN", "")
        tg_chat = env.get("TG_CHAT_ID", "")
        telegram = TelegramConfig(
            bot_token=tg_token,
            chat_id=tg_chat,
            enabled=bool(tg_token and tg_chat),
        )
        return cls(
            profile=env.get("OKX_PROFILE", "live"),
            inst_id=env.get("INST_ID", "BTC-USDT-SWAP"),
            loop_interval_sec=int(env.get("LOOP_INTERVAL_SEC", "30")),
            risk=risk,
            telegram=telegram,
        )
