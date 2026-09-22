"""Predictions describe evidence; decisions describe requested position changes."""
from dataclasses import asdict, dataclass
import math
from system_data import daily_cutoff, digest


@dataclass(frozen=True)
class Prediction:
    symbol: str
    at: str
    horizon: int
    value: float | None
    model: str
    direction: int
    reasons: list
    refs: list
    error: str | None = None

    def record(self):
        return asdict(self)


def decide(prediction, held):
    if prediction.error or prediction.direction not in (-1, 0, 1) or (
            prediction.value is not None and not math.isfinite(prediction.value)):
        action = 'ERROR'
    elif prediction.direction > 0 and not held:
        action = 'BUY'
    elif prediction.direction < 0 and held:
        action = 'SELL'
    else:
        action = 'HOLD' if held else 'NO_TRADE'
    value = dict(action=action, prediction=prediction.record(), policy='decision-v1')
    return dict(id=digest(value), **value)


class MovingAverage:
    name = 'ma-cross-5-20-v1'

    def predict(self, history, at=None):
        closes, relations = [], []
        for b in history:
            if b.split_factor != 1:
                closes = [c * b.split_factor for c in closes]
            closes.append(b.close)
            if len(closes) >= 20:
                fast, slow = sum(closes[-5:])/5, sum(closes[-20:])/20
                relations.append((fast > slow) - (fast < slow))
        direction = 0
        if len(relations) >= 2:
            if relations[-2] <= 0 < relations[-1]:
                direction = 1
            elif relations[-2] >= 0 > relations[-1]:
                direction = -1
        return Prediction(history[-1].symbol, at or daily_cutoff(history[-1].day),
                          1, None, self.name, direction, ['5/20 MA crossing; not a probability'],
                          [history[-1].id, 'history:'+digest([b.id for b in history])])
