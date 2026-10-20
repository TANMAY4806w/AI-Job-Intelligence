"""In-process task scheduling layer.

APScheduler cron and interval triggers for high-frequency harvesting and freshness sweeps.
Boundary: Scheduling dispatches to orchestration; it never implements pipeline or persistence logic.
"""
