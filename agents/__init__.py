"""O&M Command Crew — one module per agent.

Triage  -> finds + classifies underperformance (PR cascade, IEC 61724)
Impact  -> prices incidents in EUR (per-inverter feed-in tariff)
Drafter -> drafts human-approvable actions (work order / EEG claim / setpoint)
Forecast-> Chronos-Bolt Lambda client (day-ahead, quantiles)

Advisory only: every outbound action stops at the approval queue.
"""

from . import drafter, forecast, impact, triage  # noqa: F401
