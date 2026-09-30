import math
import os
import re
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path


def load_env(path: str = ".env") -> None:
    if not Path(path).exists():
        return
    for line in Path(path).read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key.strip()):
            raise ValueError("Formato invalido en .env")
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


@dataclass(frozen=True)
class Config:
    mode: str = "paper"
    symbols: tuple[str, ...] = ("DOGEUSDT", "SOLUSDT", "ETHUSDT", "1000PEPEUSDT")
    interval: str = "5m"
    poll_seconds: int = 30
    initial_equity: float = 100.0
    leverage: int = 5
    risk_per_trade: float = 0.005
    max_daily_loss_pct: float = 0.05
    max_total_loss_usdt: float = 30.0
    max_drawdown_pct: float = 0.10
    max_margin_fraction: float = 0.10
    max_positions: int = 2
    entry_threshold: float = 0.45
    stop_atr_multiple: float = 2.0
    reward_risk_ratio: float = 2.0
    fee_rate: float = 0.0005
    slippage_bps: float = 5.0
    max_spread_bps: float = 12.0
    min_quote_volume: float = 10000000.0
    max_funding_rate: float = 0.001
    news_enabled: bool = True
    require_fresh_news: bool = False
    grok_enabled: bool = False
    news_refresh_seconds: int = 900
    adaptation_min_trades: int = 30
    max_holding_hours: float = 12.0
    min_trend_score: float = 0.0
    min_momentum_score: float = 0.0
    strategy_profile: str = "trend"
    allow_shorts: bool = True
    daily_review_enabled: bool = True
    daily_review_time: str = "00:10"

    def __post_init__(self):
        for name in ("leverage", "poll_seconds", "max_positions", "news_refresh_seconds", "adaptation_min_trades"):
            if isinstance(getattr(self, name), bool) or not isinstance(getattr(self, name), int):
                raise ValueError(f"Se requiere un entero: {name}")
        for name in ("news_enabled", "require_fresh_news", "grok_enabled", "allow_shorts", "daily_review_enabled"):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"Se requiere un booleano: {name}")
        if self.mode not in ("paper", "demo") or self.leverage != 5:
            raise ValueError("Solo paper/demo futuros con apalancamiento 5x")
        if self.interval not in ("1m", "5m", "15m", "30m", "1h"):
            raise ValueError("Intervalo no soportado")
        if self.strategy_profile not in ("trend", "swing", "scalping"):
            raise ValueError("Perfil de estrategia no soportado")
        if not isinstance(self.symbols, (list, tuple)) or not self.symbols or any(not isinstance(x, str) for x in self.symbols):
            raise ValueError("Se requiere una lista de simbolos")
        object.__setattr__(self, "symbols", tuple(self.symbols))
        if len(set(self.symbols)) != len(self.symbols):
            raise ValueError("Simbolos vacios o duplicados")
        if any(not re.fullmatch(r"[A-Z0-9]{3,20}USDT", x) for x in self.symbols):
            raise ValueError("Se requieren simbolos USDT de Binance Futures")
        positive = ("initial_equity", "risk_per_trade", "max_daily_loss_pct", "max_total_loss_usdt",
                    "max_drawdown_pct", "max_margin_fraction", "stop_atr_multiple", "reward_risk_ratio",
                    "max_spread_bps", "min_quote_volume", "max_funding_rate", "max_holding_hours")
        for name in positive:
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"Valor invalido: {name}")
        for name in ("fee_rate", "slippage_bps"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ValueError(f"Valor invalido: {name}")
        if isinstance(self.entry_threshold, bool) or not isinstance(self.entry_threshold, (int, float)) or not (0 < self.entry_threshold <= 1):
            raise ValueError("entry_threshold fuera de rango")
        if self.fee_rate >= 1 or self.slippage_bps >= 10000:
            raise ValueError("Comision y deslizamiento deben ser menores al 100%")
        if not (0 < self.risk_per_trade <= .01 and self.max_daily_loss_pct <= .05):
            raise ValueError("Riesgo por operacion <=1%; perdida diaria <=5%")
        if self.max_total_loss_usdt > 30 or self.max_total_loss_usdt >= self.initial_equity:
            raise ValueError("Perdida total debe ser menor al capital y <=30 USDT")
        if self.max_drawdown_pct > .30 or self.max_margin_fraction > .20:
            raise ValueError("Limites de drawdown/margen excedidos")
        if self.poll_seconds < 10 or not 1 <= self.max_positions <= 2:
            raise ValueError("Usar polling >=10s y maximo dos posiciones")
        if not 300 <= self.news_refresh_seconds <= 86400 or self.adaptation_min_trades < 30:
            raise ValueError("Noticias entre 300 y 86400s; ajuste con >=30 operaciones")
        for name in ("min_trend_score", "min_momentum_score"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= .95:
                raise ValueError(f"Umbral invalido: {name}")
        if not isinstance(self.daily_review_time, str) or not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", self.daily_review_time):
            raise ValueError("daily_review_time requiere HH:MM, hora de Colombia")


def load_config(path="config.toml", mode=None):
    load_env()
    raw = tomllib.loads(Path(path).read_text(encoding="utf-8-sig"))
    unknown = set(raw) - {f.name for f in fields(Config)}
    if unknown:
        raise ValueError(f"Opciones desconocidas: {sorted(unknown)}")
    if "symbols" in raw:
        raw["symbols"] = tuple(raw["symbols"])
    if mode:
        raw["mode"] = mode
    for key, name in (("BOT_GROK_ENABLED", "grok_enabled"), ("BOT_NEWS_ENABLED", "news_enabled")):
        if key in os.environ:
            value = os.environ[key].lower()
            if value not in ("true", "false"):
                raise ValueError(f"{key} debe ser true/false")
            raw[name] = value == "true"
    return Config(**raw)
