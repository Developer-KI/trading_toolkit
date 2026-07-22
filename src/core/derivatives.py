"""
core/derivatives.py — Options analytics: pricing, implied vol, Greeks, IV surface.

Self-contained derivatives layer. Given an options chain for an underlying, this module
reconstructs the implied volatility of every contract, computes the full Greek set, and
builds an interpolable IV surface (smile + term structure + 3-D grid).

Design mirrors indicators.py / sizing.py: small stateless functions plus a few dataclasses,
importing only core.models + numpy. scipy is used when importable (Brent root-find and
grid interpolation) with numpy fallbacks, so it is not a hard dependency.

Pricing models:
  • Black-Scholes (European) — the fast, vectorized default.
  • Binomial CRR tree (American) — opt-in via model="american" / american=True.

Greek conventions (raw, per unit — scale in the UI as needed):
  • delta : ∂V/∂S            (per $1 of underlying)
  • gamma : ∂²V/∂S²
  • vega  : ∂V/∂σ            (per 1.00 = 100 vol points; divide by 100 for per 1%)
  • theta : ∂V/∂t            (per year; divide by 365 for per calendar day)
  • rho   : ∂V/∂r            (per 1.00 = 100 rate points; divide by 100 for per 1%)
"""

from __future__ import annotations

import enum
import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

# ── Optional scipy acceleration (guarded; numpy fallbacks below) ──────────────
try:
    from scipy.special import ndtr as _ndtr  # vectorized standard-normal CDF
    from scipy.optimize import brentq as _brentq
    from scipy.optimize import least_squares as _least_squares
    from scipy.interpolate import griddata as _griddata
    _HAVE_SCIPY = True
except Exception:  # pragma: no cover - exercised only when scipy is absent
    _ndtr = None
    _brentq = None
    _least_squares = None
    _griddata = None
    _HAVE_SCIPY = False

_SQRT_2PI = math.sqrt(2.0 * math.pi)
_YEAR_SECONDS = 365.25 * 24 * 3600  # calendar-year fraction denominator

# Upper end of the volatility search. Short-dated deep ITM/OTM contracts routinely imply
# 300-700% vol; a 500% cap (the old value) silently dropped ~11% of a live equity chain.
MAX_IV = 20.0
# Stale-quote tolerance: a last-traded price may sit a hair *below* intrinsic once spot has
# moved since the trade. Violations within this slack are clamped to the bound instead of
# being rejected as arbitrage. Expressed as a fraction of the intrinsic bound, floored at 1c.
ARB_SLACK_REL = 0.01
ARB_SLACK_ABS = 0.01


def _to_utc(ts) -> pd.Timestamp:
    """Coerce a timestamp to UTC-aware (assume UTC when tz-naive)."""
    ts = pd.Timestamp(ts)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


# ── Standard-normal helpers ───────────────────────────────────────────────────

def _norm_cdf(x):
    """Standard-normal CDF, vectorized. Uses scipy if available, else numpy erf approx."""
    if _HAVE_SCIPY:
        return _ndtr(x)
    x = np.asarray(x, dtype=float)
    # Abramowitz & Stegun 7.1.26 approximation of erf, applied to x/sqrt(2).
    z = x / math.sqrt(2.0)
    sign = np.sign(z)
    az = np.abs(z)
    t = 1.0 / (1.0 + 0.3275911 * az)
    poly = t * (0.254829592 + t * (-0.284496736 + t * (1.421413741
            + t * (-1.453152027 + t * 1.061405429))))
    erf = 1.0 - poly * np.exp(-az * az)
    return 0.5 * (1.0 + sign * erf)


def _norm_pdf(x):
    """Standard-normal PDF, vectorized (pure numpy)."""
    x = np.asarray(x, dtype=float)
    return np.exp(-0.5 * x * x) / _SQRT_2PI


# ── Data models ───────────────────────────────────────────────────────────────

class OptionType(enum.Enum):
    CALL = "call"
    PUT = "put"

    @classmethod
    def parse(cls, value: "OptionType | str") -> "OptionType":
        if isinstance(value, cls):
            return value
        s = str(value).strip().lower()
        if s in ("c", "call"):
            return cls.CALL
        if s in ("p", "put"):
            return cls.PUT
        raise ValueError(f"Unrecognized option type: {value!r}")


@dataclass
class OptionContract:
    """A single option quote/observation."""

    underlying: str
    expiry: pd.Timestamp
    strike: float
    option_type: OptionType
    bid: float = np.nan
    ask: float = np.nan
    last: float = np.nan
    volume: float = 0.0
    open_interest: float = 0.0
    underlying_price: float = np.nan
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def mid(self) -> float:
        """Mid of bid/ask; falls back to last when either side is missing."""
        if np.isfinite(self.bid) and np.isfinite(self.ask) and self.bid > 0 and self.ask > 0:
            return (self.bid + self.ask) / 2.0
        return self.last

    def price(self, basis: str = "mid") -> float:
        return self.last if basis == "last" else self.mid


# Common column-name aliases for provider-agnostic ingestion.
_FIELD_ALIASES = {
    "expiry": ["expiry", "expiration", "expiration_date", "expiry_date", "exp"],
    "strike": ["strike", "strike_price", "k"],
    "option_type": ["option_type", "contract_type", "type", "right", "call_put", "cp"],
    "bid": ["bid", "bid_price"],
    "ask": ["ask", "ask_price", "offer"],
    "last": ["last", "last_price", "price", "close", "mark"],
    "volume": ["volume", "volume_today", "vol"],
    "open_interest": ["open_interest", "oi", "openinterest"],
    # note: bare "underlying" is deliberately excluded — some feeds put the ticker there.
    "underlying_price": ["underlying_price", "underlying_last", "underlying_close",
                          "spot_price", "spot"],
}


def _pick(row: dict, keys: list[str], default=np.nan):
    for k in keys:
        if k in row and row[k] is not None:
            return row[k]
    return default


@dataclass
class OptionChain:
    """A snapshot of the option chain for one underlying at one point in time."""

    underlying: str
    asof: pd.Timestamp
    contracts: list[OptionContract] = field(default_factory=list)

    # ── construction ─────────────────────────────────────────────────────
    @classmethod
    def from_records(
        cls,
        records: list[dict],
        underlying: str,
        asof: pd.Timestamp | str | None = None,
    ) -> "OptionChain":
        """Build from a list of dicts (e.g. LSE `client.options()` rows). Tolerant of
        varying provider field names via `_FIELD_ALIASES`."""
        asof_ts = pd.Timestamp(asof) if asof is not None else pd.Timestamp.utcnow()
        contracts: list[OptionContract] = []
        for row in records:
            try:
                otype = OptionType.parse(_pick(row, _FIELD_ALIASES["option_type"]))
            except (ValueError, KeyError):
                continue
            expiry = _pick(row, _FIELD_ALIASES["expiry"])
            strike = _pick(row, _FIELD_ALIASES["strike"])
            if expiry is None or not np.isfinite(float(strike if strike is not None else np.nan)):
                continue
            contracts.append(OptionContract(
                underlying=underlying,
                expiry=pd.Timestamp(expiry),
                strike=float(strike),
                option_type=otype,
                bid=float(_pick(row, _FIELD_ALIASES["bid"])),
                ask=float(_pick(row, _FIELD_ALIASES["ask"])),
                last=float(_pick(row, _FIELD_ALIASES["last"])),
                volume=float(_pick(row, _FIELD_ALIASES["volume"], 0.0) or 0.0),
                open_interest=float(_pick(row, _FIELD_ALIASES["open_interest"], 0.0) or 0.0),
                underlying_price=float(_pick(row, _FIELD_ALIASES["underlying_price"])),
                meta={k: v for k, v in row.items()},
            ))
        return cls(underlying=underlying, asof=asof_ts, contracts=contracts)

    @classmethod
    def from_df(
        cls,
        df: pd.DataFrame,
        underlying: str,
        asof: pd.Timestamp | str | None = None,
    ) -> "OptionChain":
        return cls.from_records(df.to_dict("records"), underlying, asof)

    # ── views ────────────────────────────────────────────────────────────
    def to_df(self) -> pd.DataFrame:
        rows = []
        for c in self.contracts:
            rows.append({
                "underlying": c.underlying,
                "expiry": c.expiry,
                "strike": c.strike,
                "option_type": c.option_type.value,
                "bid": c.bid,
                "ask": c.ask,
                "last": c.last,
                "mid": c.mid,
                "volume": c.volume,
                "open_interest": c.open_interest,
                "underlying_price": c.underlying_price,
            })
        return pd.DataFrame(rows)

    @property
    def expiries(self) -> list[pd.Timestamp]:
        return sorted({c.expiry for c in self.contracts})

    @property
    def strikes(self) -> list[float]:
        return sorted({c.strike for c in self.contracts})

    @property
    def calls(self) -> list[OptionContract]:
        return [c for c in self.contracts if c.option_type is OptionType.CALL]

    @property
    def puts(self) -> list[OptionContract]:
        return [c for c in self.contracts if c.option_type is OptionType.PUT]

    def for_expiry(self, expiry: pd.Timestamp | str) -> list[OptionContract]:
        exp = pd.Timestamp(expiry)
        return [c for c in self.contracts if c.expiry == exp]

    @property
    def spot(self) -> float:
        """Best estimate of the underlying price (median of non-null quotes)."""
        vals = [c.underlying_price for c in self.contracts if np.isfinite(c.underlying_price)]
        return float(np.median(vals)) if vals else np.nan

    def drop_expired(self) -> "OptionChain":
        """Chain without already-expired contracts.

        Providers keep expired rows in the "current" chain until they age out, frozen at
        their last-traded state. Nothing can be reconstructed from them (T = 0 ⇒ no IV, no
        Greeks), so they are dropped rather than carried as blank rows.
        """
        asof = _to_utc(self.asof)
        live = [c for c in self.contracts if _to_utc(c.expiry) > asof]
        return OptionChain(underlying=self.underlying, asof=self.asof, contracts=live)

    @property
    def n_expired(self) -> int:
        asof = _to_utc(self.asof)
        return sum(1 for c in self.contracts if _to_utc(c.expiry) <= asof)

    def year_fractions(self) -> np.ndarray:
        """Time-to-expiry in calendar years for each contract, aligned to `contracts`.

        Normalizes tz-naive/aware mismatches (provider expiry dates are often naive while
        `asof` is UTC-aware) by coercing both operands to UTC before subtracting.
        """
        asof = _to_utc(self.asof)
        return np.array([
            max((_to_utc(c.expiry) - asof).total_seconds() / _YEAR_SECONDS, 0.0)
            for c in self.contracts
        ])


# ── Black-Scholes pricing ─────────────────────────────────────────────────────

def _d1_d2(S, K, T, r, sigma, q):
    S = np.asarray(S, dtype=float)
    K = np.asarray(K, dtype=float)
    T = np.asarray(T, dtype=float)
    sigma = np.asarray(sigma, dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        vsqrt = sigma * np.sqrt(T)
        d1 = (np.log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / vsqrt
        d2 = d1 - vsqrt
    return d1, d2


def black_scholes_price(S, K, T, r, sigma, option_type, q=0.0):
    """European option price under Black-Scholes with continuous dividend yield q.

    Scalars or numpy arrays (broadcast) are both accepted. Handles the T→0 limit by
    returning discounted intrinsic value.
    """
    is_call = OptionType.parse(option_type) is OptionType.CALL
    S = np.asarray(S, dtype=float)
    K = np.asarray(K, dtype=float)
    T = np.asarray(T, dtype=float)
    sigma = np.asarray(sigma, dtype=float)

    d1, d2 = _d1_d2(S, K, T, r, sigma, q)
    disc_r = np.exp(-r * T)
    disc_q = np.exp(-q * T)
    if is_call:
        price = S * disc_q * _norm_cdf(d1) - K * disc_r * _norm_cdf(d2)
        intrinsic = np.maximum(S - K, 0.0)
    else:
        price = K * disc_r * _norm_cdf(-d2) - S * disc_q * _norm_cdf(-d1)
        intrinsic = np.maximum(K - S, 0.0)

    # T→0 or σ→0: collapse to intrinsic value.
    degenerate = (T <= 0) | (sigma <= 0)
    price = np.where(degenerate, intrinsic, price)
    return float(price) if np.ndim(price) == 0 else price


def bs_vega(S, K, T, r, sigma, q=0.0):
    """Black-Scholes vega (∂price/∂σ, per 1.00 vol). Type-independent."""
    S = np.asarray(S, dtype=float)
    T = np.asarray(T, dtype=float)
    d1, _ = _d1_d2(S, K, T, r, sigma, q)
    vega = S * np.exp(-q * T) * _norm_pdf(d1) * np.sqrt(T)
    vega = np.where((T <= 0) | (np.asarray(sigma) <= 0), 0.0, vega)
    return float(vega) if np.ndim(vega) == 0 else vega


# ── Binomial (CRR) pricing — American exercise ────────────────────────────────

def binomial_price(S, K, T, r, sigma, option_type, q=0.0, steps=200, american=True):
    """Cox-Ross-Rubinstein binomial price. Scalar inputs only.

    american=True allows early exercise (equity-style); american=False is European
    and converges to Black-Scholes as `steps` grows.
    """
    is_call = OptionType.parse(option_type) is OptionType.CALL
    S = float(S); K = float(K); T = float(T); sigma = float(sigma)
    if T <= 0 or sigma <= 0:
        return max(S - K, 0.0) if is_call else max(K - S, 0.0)

    dt = T / steps
    u = math.exp(sigma * math.sqrt(dt))
    d = 1.0 / u
    disc = math.exp(-r * dt)
    p = (math.exp((r - q) * dt) - d) / (u - d)
    p = min(max(p, 0.0), 1.0)  # guard against degenerate params

    j = np.arange(steps + 1)
    prices = S * (u ** (steps - j)) * (d ** j)
    if is_call:
        values = np.maximum(prices - K, 0.0)
    else:
        values = np.maximum(K - prices, 0.0)

    for step in range(steps, 0, -1):
        prices = prices[: step] / u  # underlying prices one layer earlier
        values = disc * (p * values[: step] + (1.0 - p) * values[1: step + 1])
        if american:
            intrinsic = (prices - K) if is_call else (K - prices)
            values = np.maximum(values, intrinsic)
    return float(values[0])


# ── Implied volatility ────────────────────────────────────────────────────────

def _sanitize_iv_inputs(price, S, K, T, r, q, is_call, american=False):
    """Validate one contract's inputs and repair small no-arbitrage violations.

    Returns `(price, status)`. `status` is `"ok"` when `price` is usable, otherwise a short
    reason the contract cannot be inverted — surfaced as `iv_status` so blank IV/Greek cells
    are explainable rather than mysterious.
    """
    if not np.isfinite(T) or T <= 0:
        return np.nan, "expired"
    if not np.isfinite(S) or S <= 0 or not np.isfinite(K) or K <= 0:
        return np.nan, "no spot"
    if not np.isfinite(price) or price <= 0:
        return np.nan, "no price"

    # No-arbitrage bounds: price must sit between intrinsic and the underlying (call) /
    # discounted strike (put).
    disc_r = math.exp(-r * T)
    disc_q = math.exp(-q * T)
    lo_bound = max(S * disc_q - K * disc_r, 0.0) if is_call else max(K * disc_r - S * disc_q, 0.0)
    hi_bound = S * disc_q if is_call else K * disc_r
    if american:
        # Early exercise makes undiscounted intrinsic the binding floor, and an American
        # option is never worth more than the spot (call) or the strike (put).
        lo_bound = max(lo_bound, max(S - K, 0.0) if is_call else max(K - S, 0.0))
        hi_bound = S if is_call else K
    if price < lo_bound:
        slack = max(ARB_SLACK_REL * lo_bound, ARB_SLACK_ABS)
        if price < lo_bound - slack:
            return np.nan, "below intrinsic"
        # Stale last-trade vs. live spot: nudge onto the bound so the solve still yields a
        # (near-floor) vol instead of a hole in the table.
        price = lo_bound * (1.0 + 1e-6) + 1e-8
    if price > hi_bound:
        return np.nan, "above bound"
    return price, "ok"


def _solve_bracketed(f, tol, lo=1e-6, hi_max=MAX_IV):
    """Root-find `f` over vol, growing the upper bracket until it straddles the root.

    A fixed upper bound is the wrong shape here: ATM contracts root near 0.2 while 1-DTE
    deep ITM quotes can imply 7+. Doubling from 1.0 keeps the common case at one extra
    evaluation while still reaching `hi_max`.
    """
    flo = f(lo)
    if not np.isfinite(flo):
        return np.nan, "unsolvable"
    hi = 1.0
    while hi <= hi_max:
        fhi = f(hi)
        if np.isfinite(fhi) and flo * fhi <= 0:
            if _HAVE_SCIPY:
                try:
                    return float(_brentq(f, lo, hi, xtol=tol, maxiter=200)), "ok"
                except Exception:
                    return np.nan, "unsolvable"
            return _bisect(f, lo, hi, tol), "ok"
        hi *= 2.0
    return np.nan, "iv above cap"


def _bs_iv_scalar(price, S, K, T, r, q, is_call, tol=1e-8, max_iter=100):
    """Invert Black-Scholes for one contract. Newton with a bracketed fallback.

    Returns `(iv, status)`.
    """
    otype = OptionType.CALL if is_call else OptionType.PUT
    price, status = _sanitize_iv_inputs(price, S, K, T, r, q, is_call)
    if status != "ok":
        return np.nan, status

    # Brenner-Subrahmanyam seed for at-the-money-ish contracts.
    sigma = max(math.sqrt(2 * math.pi / T) * price / S, 1e-3)
    for _ in range(max_iter):
        model = black_scholes_price(S, K, T, r, sigma, otype, q)
        diff = model - price
        if abs(diff) < tol:
            return sigma, "ok"
        v = bs_vega(S, K, T, r, sigma, q)
        if v < 1e-10:
            break  # vega collapsed — hand off to the bracketed solver
        sigma -= diff / v
        if sigma <= 0 or sigma > MAX_IV or not np.isfinite(sigma):
            break

    return _solve_bracketed(
        lambda sig: black_scholes_price(S, K, T, r, sig, otype, q) - price, tol)


def _american_iv(price, S, K, T, r, q, is_call, steps=160, tol=1e-6):
    """Invert the American binomial price for one contract. Returns `(iv, status)`."""
    otype = OptionType.CALL if is_call else OptionType.PUT
    price, status = _sanitize_iv_inputs(price, S, K, T, r, q, is_call, american=True)
    if status != "ok":
        return np.nan, status

    return _solve_bracketed(
        lambda sig: binomial_price(S, K, T, r, sig, otype, q, steps=steps, american=True) - price,
        tol, lo=1e-4)


def _bisect(f, lo, hi, tol=1e-8, max_iter=200):
    flo = f(lo)
    for _ in range(max_iter):
        mid = 0.5 * (lo + hi)
        fmid = f(mid)
        if abs(fmid) < tol or (hi - lo) < tol:
            return mid
        if flo * fmid <= 0:
            hi = mid
        else:
            lo, flo = mid, fmid
    return 0.5 * (lo + hi)


def implied_vol_detail(price, S, K, T, r, option_type, q=0.0, model="bs") -> tuple[float, str]:
    """`(iv, status)` for one contract — `status` explains any NaN instead of raising."""
    is_call = OptionType.parse(option_type) is OptionType.CALL
    if model == "american":
        return _american_iv(price, S, K, T, r, q, is_call)
    return _bs_iv_scalar(price, S, K, T, r, q, is_call)


def implied_vol(price, S, K, T, r, option_type, q=0.0, model="bs") -> float:
    """Implied volatility for one contract. Returns np.nan for arbitrage-violating /
    unsolvable inputs rather than raising.

    model: "bs" (European closed form) or "american" (binomial inversion).
    Use `implied_vol_detail` when you also want the reason for a NaN.
    """
    return implied_vol_detail(price, S, K, T, r, option_type, q, model)[0]


def implied_vol_chain(chain: OptionChain, r: float, q: float = 0.0,
                      model: str = "bs", price: str = "mid") -> pd.DataFrame:
    """Return the chain as a DataFrame with added `T` (years), `iv` and `iv_status` columns."""
    df = chain.to_df()
    if df.empty:
        df["T"] = []
        df["iv"] = []
        df["iv_status"] = []
        return df
    T = chain.year_fractions()
    df["T"] = T
    ivs, statuses = [], []
    for c, t in zip(chain.contracts, T):
        px = c.price(price)
        S = c.underlying_price if np.isfinite(c.underlying_price) else chain.spot
        iv, status = implied_vol_detail(px, S, c.strike, t, r, c.option_type, q, model)
        ivs.append(iv)
        statuses.append(status)
    df["iv"] = ivs
    df["iv_status"] = statuses
    return df


# ── Greeks ────────────────────────────────────────────────────────────────────

@dataclass
class OptionGreeks:
    delta: float
    gamma: float
    vega: float
    theta: float
    rho: float

    def as_dict(self) -> dict[str, float]:
        return {"delta": self.delta, "gamma": self.gamma, "vega": self.vega,
                "theta": self.theta, "rho": self.rho}


def _bs_greeks(S, K, T, r, sigma, is_call, q=0.0) -> OptionGreeks:
    if T <= 0 or sigma <= 0:
        return OptionGreeks(np.nan, np.nan, np.nan, np.nan, np.nan)
    d1, d2 = _d1_d2(S, K, T, r, sigma, q)
    d1 = float(d1); d2 = float(d2)
    disc_r = math.exp(-r * T)
    disc_q = math.exp(-q * T)
    pdf = float(_norm_pdf(d1))
    gamma = disc_q * pdf / (S * sigma * math.sqrt(T))
    vega = S * disc_q * pdf * math.sqrt(T)
    if is_call:
        delta = disc_q * float(_norm_cdf(d1))
        theta = (-(S * disc_q * pdf * sigma) / (2 * math.sqrt(T))
                 - r * K * disc_r * float(_norm_cdf(d2))
                 + q * S * disc_q * float(_norm_cdf(d1)))
        rho = K * T * disc_r * float(_norm_cdf(d2))
    else:
        delta = -disc_q * float(_norm_cdf(-d1))
        theta = (-(S * disc_q * pdf * sigma) / (2 * math.sqrt(T))
                 + r * K * disc_r * float(_norm_cdf(-d2))
                 - q * S * disc_q * float(_norm_cdf(-d1)))
        rho = -K * T * disc_r * float(_norm_cdf(-d2))
    return OptionGreeks(delta, gamma, vega, theta, rho)


def _fd_greeks(S, K, T, r, sigma, is_call, q=0.0, steps=160) -> OptionGreeks:
    """Finite-difference Greeks on the American binomial tree (bump and reprice)."""
    otype = OptionType.CALL if is_call else OptionType.PUT

    def px(s=S, k=K, t=T, rr=r, sig=sigma):
        return binomial_price(s, k, t, rr, sig, otype, q, steps=steps, american=True)

    base = px()
    dS = S * 0.01
    dSig = 0.01
    dT = min(T * 0.01, 1.0 / 365.0)
    dr = 1e-4
    up, dn = px(s=S + dS), px(s=S - dS)
    delta = (up - dn) / (2 * dS)
    gamma = (up - 2 * base + dn) / (dS * dS)
    vega = (px(sig=sigma + dSig) - px(sig=sigma - dSig)) / (2 * dSig)
    theta = -(px(t=T + dT) - base) / dT if T > dT else np.nan  # per year, forward in time
    rho = (px(rr=r + dr) - px(rr=r - dr)) / (2 * dr)
    return OptionGreeks(delta, gamma, vega, theta, rho)


def greeks(S, K, T, r, sigma, option_type, q=0.0, model="bs") -> OptionGreeks:
    """Full Greek set for one contract. Analytic under Black-Scholes; finite-difference
    on the binomial tree for model="american"."""
    is_call = OptionType.parse(option_type) is OptionType.CALL
    if model == "american":
        return _fd_greeks(S, K, T, r, sigma, is_call, q)
    return _bs_greeks(S, K, T, r, sigma, is_call, q)


def greeks_chain(chain: OptionChain, r: float, q: float = 0.0,
                 model: str = "bs", price: str = "mid") -> pd.DataFrame:
    """Chain DataFrame with `T`, `iv`, and delta/gamma/vega/theta/rho columns."""
    df = implied_vol_chain(chain, r, q, model, price)
    cols = {"delta": [], "gamma": [], "vega": [], "theta": [], "rho": []}
    for c, t, iv in zip(chain.contracts, df["T"], df["iv"]):
        S = c.underlying_price if np.isfinite(c.underlying_price) else chain.spot
        if not np.isfinite(iv):
            g = OptionGreeks(np.nan, np.nan, np.nan, np.nan, np.nan)
        else:
            g = greeks(S, c.strike, t, r, iv, c.option_type, q, model)
        for k, v in g.as_dict().items():
            cols[k].append(v)
    for k, v in cols.items():
        df[k] = v
    return df


# ── Heston stochastic-volatility model ────────────────────────────────────────

@dataclass
class HestonParams:
    """Heston model parameters (variance dynamics under the risk-neutral measure)."""

    v0: float      # initial variance
    kappa: float   # mean-reversion speed
    theta: float   # long-run variance
    sigma: float   # vol-of-vol
    rho: float     # spot/variance correlation
    rmse: float = float("nan")  # calibration fit error, in IV units (set by calibrate_heston)

    def as_dict(self) -> dict[str, float]:
        return {"v0": self.v0, "kappa": self.kappa, "theta": self.theta,
                "sigma": self.sigma, "rho": self.rho}

    @property
    def feller(self) -> float:
        """Feller ratio 2·κ·θ / σ² (>1 keeps the variance process strictly positive)."""
        return (2.0 * self.kappa * self.theta / (self.sigma ** 2)
                if self.sigma > 0 else float("inf"))


# 64-node Gauss-Legendre quadrature on [-1, 1], reused for every Heston integral.
_HESTON_NODES, _HESTON_WEIGHTS = np.polynomial.legendre.leggauss(64)
_HESTON_UMAX = 120.0  # integration truncation for the characteristic-function integral


def _heston_integrand(phi, S, K, T, r, q, p: "HestonParams", j: int):
    """Vectorized integrand for probability P_j — Albrecher 'little trap' formulation
    (numerically stable branch of the Heston characteristic function)."""
    i = 1j
    if j == 1:
        u = 0.5
        b = p.kappa - p.rho * p.sigma
    else:
        u = -0.5
        b = p.kappa
    rho_sig_iphi = p.rho * p.sigma * i * phi
    d = np.sqrt((rho_sig_iphi - b) ** 2 - p.sigma ** 2 * (2 * u * i * phi - phi ** 2))
    g = (b - rho_sig_iphi - d) / (b - rho_sig_iphi + d)   # little-trap g2 = 1/g
    edt = np.exp(-d * T)
    C = ((r - q) * i * phi * T
         + (p.kappa * p.theta / p.sigma ** 2)
         * ((b - rho_sig_iphi - d) * T - 2.0 * np.log((1 - g * edt) / (1 - g))))
    D = ((b - rho_sig_iphi - d) / p.sigma ** 2) * ((1 - edt) / (1 - g * edt))
    f = np.exp(C + D * p.v0 + i * phi * np.log(S))
    return np.real(np.exp(-i * phi * np.log(K)) * f / (i * phi))


def heston_price(S, K, T, r, q, params: "HestonParams", option_type) -> float:
    """European option price under the Heston model via Gauss-Legendre quadrature."""
    is_call = OptionType.parse(option_type) is OptionType.CALL
    S = float(S); K = float(K); T = float(T)
    if T <= 0:
        return max(S - K, 0.0) if is_call else max(K - S, 0.0)
    phi = 0.5 * _HESTON_UMAX * (_HESTON_NODES + 1.0)   # map nodes to (0, UMAX]
    w = 0.5 * _HESTON_UMAX * _HESTON_WEIGHTS
    P1 = 0.5 + (1.0 / math.pi) * np.sum(w * _heston_integrand(phi, S, K, T, r, q, params, 1))
    P2 = 0.5 + (1.0 / math.pi) * np.sum(w * _heston_integrand(phi, S, K, T, r, q, params, 2))
    call = S * math.exp(-q * T) * P1 - K * math.exp(-r * T) * P2
    if is_call:
        price = call
    else:  # put-call parity
        price = call - S * math.exp(-q * T) + K * math.exp(-r * T)
    return float(max(price, 0.0))


def heston_iv(S, K, T, r, q, params: "HestonParams", option_type) -> float:
    """Black-Scholes implied vol of the Heston price — i.e. the model's own smile point."""
    px = heston_price(S, K, T, r, q, params, option_type)
    return implied_vol(px, S, K, T, r, option_type, q, model="bs")


def calibrate_heston(chain: OptionChain, r: float, q: float = 0.0, price: str = "mid",
                     max_contracts: int = 120,
                     init: "HestonParams | None" = None) -> "HestonParams":
    """Least-squares calibration of Heston parameters to the chain's market implied vols.

    Fitting IVs rather than raw prices is far better conditioned: it targets the surface
    directly and puts every maturity/strike on a comparable scale. Only **OTM** quotes are
    used (ITM last-trade prices are typically stale), restricted to moneyness 0.8–1.2 and
    T > 1 week. Requires scipy. Raises ValueError if too few usable quotes remain.
    """
    if not _HAVE_SCIPY:
        raise RuntimeError("Heston calibration requires scipy.")

    spot = chain.spot
    if not (np.isfinite(spot) and spot > 0):
        raise ValueError("Cannot calibrate Heston: underlying spot price is unavailable.")

    T = chain.year_fractions()
    recs = []
    for c, t in zip(chain.contracts, T):
        if t <= 7 / 365.25:
            continue
        m = c.strike / spot
        if not (0.8 <= m <= 1.2):
            continue
        is_c = c.option_type is OptionType.CALL
        if (is_c and c.strike < spot) or ((not is_c) and c.strike > spot):
            continue  # keep OTM only
        iv = implied_vol(c.price(price), spot, c.strike, t, r, c.option_type, q, "bs")
        if np.isfinite(iv) and 0.01 < iv <= 3.0:
            recs.append((c.strike, t, is_c, iv))
    if len(recs) < 5:
        raise ValueError("Not enough liquid OTM contracts with solvable IV to calibrate Heston.")

    recs.sort(key=lambda x: (x[1], x[0]))
    if len(recs) > max_contracts:  # even subsample to cap cost
        idx = np.linspace(0, len(recs) - 1, max_contracts).round().astype(int)
        recs = [recs[i] for i in idx]

    Ks = np.array([x[0] for x in recs])
    Ts = np.array([x[1] for x in recs])
    is_call = np.array([x[2] for x in recs])
    mkt_iv = np.array([x[3] for x in recs])

    if init is None:
        v_seed = float(np.clip(np.median(mkt_iv) ** 2, 1e-3, 1.0))
        init = HestonParams(v0=v_seed, kappa=2.0, theta=v_seed, sigma=0.5, rho=-0.6)
    x0 = [init.v0, init.kappa, init.theta, init.sigma, init.rho]
    lb = [1e-4, 1e-2, 1e-4, 1e-2, -0.99]
    ub = [1.0, 10.0, 1.0, 3.0, 0.99]

    def resid(x):
        p = HestonParams(*x)
        model = np.array([
            heston_iv(spot, k, t, r, q, p, OptionType.CALL if c else OptionType.PUT)
            for k, t, c in zip(Ks, Ts, is_call)
        ])
        # Penalize parameter sets whose price can't be inverted rather than failing.
        model = np.where(np.isfinite(model), model, mkt_iv + 1.0)
        return model - mkt_iv

    sol = _least_squares(resid, x0, bounds=(lb, ub), method="trf",
                         max_nfev=200, xtol=1e-8, ftol=1e-8)
    rmse = float(np.sqrt(np.mean(np.asarray(sol.fun) ** 2)))
    return HestonParams(*sol.x, rmse=rmse)


# ── Implied-volatility surface ────────────────────────────────────────────────

@dataclass
class IVSurface:
    """Reconstructed IV surface for one underlying.

    `points` is the finite-IV cloud used for smile / term-structure / grid views.
    `iv_df` is the full per-contract table (every contract, incl. IV + BS Greeks) used
    for display. In "heston" mode the IVs are the *model* IVs of the calibrated surface;
    in "bs" mode they are inverted directly from market prices.
    """

    underlying: str
    spot: float
    points: pd.DataFrame  # columns: expiry, T, strike, moneyness, option_type, iv
    use_moneyness: bool = True
    iv_df: pd.DataFrame | None = None
    heston_params: "HestonParams | None" = None

    @classmethod
    def from_chain(cls, chain: OptionChain, r: float, q: float = 0.0,
                   moneyness: bool = True, model: str = "bs",
                   price: str = "mid") -> "IVSurface":
        spot = chain.spot
        T = chain.year_fractions()
        base = chain.to_df()
        base["T"] = T

        n = len(chain.contracts)
        ivs = np.full(n, np.nan)
        statuses = ["ok"] * n
        heston_params = None

        if model == "heston":
            heston_params = calibrate_heston(chain, r, q, price)
            for i, (c, t) in enumerate(zip(chain.contracts, T)):
                if t <= 0:
                    statuses[i] = "expired"
                    continue
                hp = heston_price(spot, c.strike, t, r, q, heston_params, c.option_type)
                ivs[i], statuses[i] = implied_vol_detail(
                    hp, spot, c.strike, t, r, c.option_type, q, "bs")
        else:  # "bs": invert market prices contract by contract
            for i, (c, t) in enumerate(zip(chain.contracts, T)):
                S = c.underlying_price if np.isfinite(c.underlying_price) else spot
                ivs[i], statuses[i] = implied_vol_detail(
                    c.price(price), S, c.strike, t, r, c.option_type, q, "bs")

        base["iv"] = ivs
        base["iv_status"] = statuses
        base["moneyness"] = base["strike"] / spot if np.isfinite(spot) and spot > 0 else np.nan

        # BS Greeks at the reconstructed IV (consistent across both modes).
        gcols = {k: np.full(n, np.nan) for k in ("delta", "gamma", "vega", "theta", "rho")}
        for i, (c, t, iv) in enumerate(zip(chain.contracts, T, ivs)):
            if np.isfinite(iv) and t > 0:
                g = _bs_greeks(spot, c.strike, t, r, iv, c.option_type is OptionType.CALL, q)
                for k, v in g.as_dict().items():
                    gcols[k][i] = v
        for k, v in gcols.items():
            base[k] = v

        pts = base[np.isfinite(base["iv"]) & (base["T"] > 0)][
            ["expiry", "T", "strike", "moneyness", "option_type", "iv"]
        ].reset_index(drop=True)
        return cls(underlying=chain.underlying, spot=spot, points=pts,
                   use_moneyness=moneyness, iv_df=base, heston_params=heston_params)

    def _x(self) -> np.ndarray:
        return self.points["moneyness"].to_numpy() if self.use_moneyness \
            else self.points["strike"].to_numpy()

    @property
    def x_label(self) -> str:
        return "moneyness" if self.use_moneyness else "strike"

    @property
    def expiries(self) -> list[pd.Timestamp]:
        return sorted(self.points["expiry"].unique())

    def smile(self, expiry: pd.Timestamp | str) -> tuple[np.ndarray, np.ndarray]:
        """(x, iv) for one expiry, averaged across call/put duplicates at each strike."""
        exp = pd.Timestamp(expiry)
        sub = self.points[self.points["expiry"] == exp]
        xcol = "moneyness" if self.use_moneyness else "strike"
        grp = sub.groupby(xcol)["iv"].mean().sort_index()
        return grp.index.to_numpy(), grp.to_numpy()

    def term_structure(self) -> tuple[np.ndarray, np.ndarray]:
        """(T, ATM-iv) — one point per expiry, IV nearest to at-the-money."""
        Ts, ivs = [], []
        target = 1.0 if self.use_moneyness else self.spot
        for exp in self.expiries:
            x, iv = self.smile(exp)
            if len(x) == 0:
                continue
            atm_iv = float(np.interp(target, x, iv)) if len(x) > 1 \
                else float(iv[np.argmin(np.abs(x - target))])
            Ts.append(float(self.points[self.points["expiry"] == exp]["T"].iloc[0]))
            ivs.append(atm_iv)
        order = np.argsort(Ts)
        return np.array(Ts)[order], np.array(ivs)[order]

    def atm_vol(self, expiry: pd.Timestamp | str) -> float:
        x, iv = self.smile(expiry)
        if len(x) == 0:
            return np.nan
        target = 1.0 if self.use_moneyness else self.spot
        return float(np.interp(target, x, iv)) if len(x) > 1 else float(iv[0])

    def skew(self, expiry: pd.Timestamp | str, lo: float = 0.9, hi: float = 1.1) -> float:
        """IV(lo moneyness) − IV(hi moneyness) for one expiry (put-side minus call-side)."""
        x, iv = self.smile(expiry)
        if len(x) < 2:
            return np.nan
        if not self.use_moneyness:
            lo, hi = lo * self.spot, hi * self.spot
        return float(np.interp(lo, x, iv) - np.interp(hi, x, iv))

    def interpolate(self, x: float, T: float) -> float:
        """Interpolated IV at (strike-or-moneyness x, time-to-expiry T years)."""
        px = self._x()
        py = self.points["T"].to_numpy()
        pz = self.points["iv"].to_numpy()
        if len(pz) == 0:
            return np.nan
        if _HAVE_SCIPY:
            val = _griddata((px, py), pz, (x, T), method="linear")
            if val is None or not np.isfinite(val):
                val = _griddata((px, py), pz, (x, T), method="nearest")
            return float(val)
        # numpy fallback: inverse-distance weighting over normalized coords.
        xs = (px - px.min()) / (np.ptp(px) or 1.0)
        ys = (py - py.min()) / (np.ptp(py) or 1.0)
        xq = (x - px.min()) / (np.ptp(px) or 1.0)
        yq = (T - py.min()) / (np.ptp(py) or 1.0)
        d2 = (xs - xq) ** 2 + (ys - yq) ** 2
        if d2.min() < 1e-12:
            return float(pz[np.argmin(d2)])
        w = 1.0 / d2
        return float(np.sum(w * pz) / np.sum(w))

    def grid(self, n: int = 40) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Mesh (X, Y, Z) over the point cloud for 3-D surface plotting.

        X = strike/moneyness axis, Y = time-to-expiry (years), Z = interpolated IV.
        """
        px = self._x()
        py = self.points["T"].to_numpy()
        pz = self.points["iv"].to_numpy()
        if len(pz) == 0:
            empty = np.zeros((n, n))
            return empty, empty, empty
        xi = np.linspace(px.min(), px.max(), n)
        yi = np.linspace(py.min(), py.max(), n)
        X, Y = np.meshgrid(xi, yi)
        if _HAVE_SCIPY:
            Z = _griddata((px, py), pz, (X, Y), method="linear")
            Zn = _griddata((px, py), pz, (X, Y), method="nearest")
            Z = np.where(np.isfinite(Z), Z, Zn)
        else:
            Z = np.vectorize(lambda a, b: self.interpolate(a, b))(X, Y)
        return X, Y, Z
