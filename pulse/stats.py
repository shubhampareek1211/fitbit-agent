"""Small, deterministic statistics helpers. All derived numbers are computed here, never by the LLM."""
import math

import numpy as np
from scipy import stats

MIN_N_CORRELATION = 10


def bootstrap_diff_ci(a: list[float], b: list[float], iters: int = 4000, seed: int = 7) -> tuple[float, float]:
    """95% bootstrap CI for mean(b) - mean(a). Deterministic (fixed seed)."""
    rng = np.random.default_rng(seed)
    a_arr, b_arr = np.asarray(a, float), np.asarray(b, float)
    diffs = (rng.choice(b_arr, (iters, b_arr.size)).mean(1) - rng.choice(a_arr, (iters, a_arr.size)).mean(1))
    lo, hi = np.percentile(diffs, [2.5, 97.5])
    return float(lo), float(hi)


def compare(a: list[float], b: list[float]) -> dict:
    """Compare period A (baseline) to period B."""
    mean_a, mean_b = float(np.mean(a)), float(np.mean(b))
    out = {"mean_a": mean_a, "mean_b": mean_b, "n_a": len(a), "n_b": len(b),
           "diff": mean_b - mean_a, "pct_change": (mean_b - mean_a) / mean_a * 100 if mean_a else None}
    if len(a) < 3 or len(b) < 3:
        out.update(ci_low=None, ci_high=None, verdict="insufficient_data")
        return out
    lo, hi = bootstrap_diff_ci(a, b)
    out.update(ci_low=lo, ci_high=hi,
               verdict="higher" if lo > 0 else "lower" if hi < 0 else "no_clear_difference")
    return out


def spearman(x: list[float], y: list[float]) -> dict:
    """Spearman correlation with a Fisher-z 95% CI and a plain-language verdict."""
    n = len(x)
    if n < MIN_N_CORRELATION:
        return {"rho": None, "n": n, "ci_low": None, "ci_high": None, "p_value": None,
                "verdict": "insufficient_data"}
    rho, p = stats.spearmanr(x, y)
    rho = float(rho)
    if math.isnan(rho):
        return {"rho": None, "n": n, "ci_low": None, "ci_high": None, "p_value": None,
                "verdict": "insufficient_data"}
    z, se = math.atanh(max(min(rho, 0.999999), -0.999999)), 1 / math.sqrt(n - 3)
    lo, hi = math.tanh(z - 1.96 * se), math.tanh(z + 1.96 * se)
    verdict = "positive" if lo > 0 else "negative" if hi < 0 else "inconclusive"
    return {"rho": rho, "n": n, "ci_low": lo, "ci_high": hi, "p_value": float(p), "verdict": verdict}


def strength(rho: float | None) -> str | None:
    if rho is None:
        return None
    r = abs(rho)
    return "very weak" if r < 0.1 else "weak" if r < 0.3 else "moderate" if r < 0.5 else "strong"
