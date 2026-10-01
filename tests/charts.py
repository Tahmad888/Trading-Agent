"""Made-up daily charts for the setup tests: straight lines between waypoints."""
import numpy as np
import pandas as pd

from desk.indicators import daily_features
from tests.basis_support import volume_evidence


def line(*points: tuple[int, float]) -> np.ndarray:
    """Closes through (bar, price) waypoints, the first at bar 0."""
    xs, ys = zip(*points)
    return np.interp(np.arange(xs[-1] + 1), xs, ys)


def chart(close, spread=0.005, volume=2e6, start="2023-01-02") -> pd.DataFrame:
    close = np.asarray(close, dtype=float)
    open_ = (np.r_[close[0], close[:-1]] + close) / 2    # opens halfway from the last close
    vol = np.broadcast_to(np.asarray(volume, dtype=float), close.shape).copy()
    idx = pd.date_range(start, periods=len(close), freq="B", tz="UTC")
    out = pd.DataFrame({"open": open_, "high": np.maximum(open_, close) * (1 + spread),
                         "low": np.minimum(open_, close) * (1 - spread), "close": close,
                         "volume": vol}, index=idx)
    out.attrs["volume_basis"] = volume_evidence()
    return out


def features(close, **kw) -> pd.DataFrame:
    return daily_features(chart(close, **kw))
