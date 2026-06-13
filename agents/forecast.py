"""Forecast agent — Chronos-Bolt on AWS Lambda (eu-north-1), zero-shot.

POST {"series": [floats oldest->newest], "horizon": N}
  -> {"quantiles": {"0.1": [...], "0.5": [...], "0.9": [...]}}
"""

import json
import urllib.request

CHRONOS_URL = "https://3juzm47gye.execute-api.eu-north-1.amazonaws.com/"


def predict(series: list[float], horizon: int = 24, timeout: int = 30) -> dict:
    """Returns {"low": [...], "median": [...], "high": [...]} clipped at 0."""
    body = json.dumps({"series": [round(v, 2) for v in series],
                       "horizon": horizon}).encode()
    req = urllib.request.Request(CHRONOS_URL, data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        q = json.loads(r.read())["quantiles"]
    clip = lambda xs: [max(0.0, round(v, 2)) for v in xs]
    return {"low": clip(q["0.1"]), "median": clip(q["0.5"]), "high": clip(q["0.9"])}
