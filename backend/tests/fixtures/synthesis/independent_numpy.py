"""Independent SMD (Hedges' g) + DerSimonian-Laird implementation (GOO-311).

Test-only second implementation used to prove the recorded gold values are
not self-referential. Vectorised numpy; never imports ``src``. Runnable
offline against a ``/synthesis/export`` package's inputs.
"""

from __future__ import annotations

from typing import Any, Sequence

import numpy as np

try:  # pragma: no cover - depends on the environment
    from scipy.stats import norm

    Z = float(norm.ppf(0.975))
except ImportError:  # pragma: no cover
    Z = 1.959963984540054


def compute(arms: Sequence[Sequence[float]]) -> dict[str, Any]:
    """``arms`` rows are ``(m1, s1, n1, m2, s2, n2)``; intervention minus control."""
    a = np.asarray(arms, dtype=np.float64)
    m1, s1, n1, m2, s2, n2 = (a[:, i] for i in range(6))
    df = n1 + n2 - 2
    sp = np.sqrt(((n1 - 1) * s1**2 + (n2 - 1) * s2**2) / df)
    d = (m1 - m2) / sp
    j = 1 - 3 / (4 * df - 1)
    g = j * d
    v = j**2 * ((n1 + n2) / (n1 * n2) + d**2 / (2 * (n1 + n2)))
    w = 1 / v
    mu_f = np.sum(w * g) / np.sum(w)
    q = float(np.sum(w * (g - mu_f) ** 2))
    k = len(g)
    c = np.sum(w) - np.sum(w**2) / np.sum(w)
    tau2 = max(0.0, float((q - (k - 1)) / c))
    i2 = 0.0 if q == 0 else max(0.0, (q - (k - 1)) / q)
    ws = 1 / (v + tau2)
    mu = float(np.sum(ws * g) / np.sum(ws))
    se = float(np.sqrt(1 / np.sum(ws)))
    return {
        "g": [float(x) for x in g],
        "v": [float(x) for x in v],
        "q": q,
        "df": k - 1,
        "tau2": tau2,
        "i2": float(i2),
        "estimate": mu,
        "se": se,
        "ci_low": mu - Z * se,
        "ci_high": mu + Z * se,
    }


if __name__ == "__main__":  # pragma: no cover
    import json
    import sys

    print(json.dumps(compute(json.load(sys.stdin)), indent=2))
