from dataclasses import dataclass, field


@dataclass(frozen=True)
class Candle:
    open_time: int
    close_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class Snapshot:
    symbol: str
    candles: list[Candle]
    bid: float
    ask: float
    quote_volume: float
    fetched_at: float

    @property
    def price(self) -> float:
        return (self.bid + self.ask) / 2


@dataclass(frozen=True)
class Advice:
    agent: str
    score: float
    veto: bool = False
    reason: str = ""
    details: dict = field(default_factory=dict)
