"""Bounded Fleet Comms gauges for the existing Monitor Prometheus scrape."""

from prometheus_client.core import GaugeMetricFamily

from lu_machinery.api.comms_router import (
    _FAILURE_METRICS,
    _authority_metrics_payload,
    _public_comms_metrics,
)
from lu_machinery.api.monitor_context import MonitorContext
from lu_machinery.fleet_comms.efficiency_metrics import (
    collect_efficiency_metrics_authority,
    resolve_metrics_source,
)


class CommsCollector:
    """Read authority events on every scrape, independently of dashboard visits."""

    def __init__(self, context: MonitorContext) -> None:
        self.context = context

    def collect(self):
        payload = {}
        if resolve_metrics_source() == "authority":
            payload = _public_comms_metrics(
                _authority_metrics_payload(collect_efficiency_metrics_authority, self.context)
            )
        windows = payload.get("failure_windows", {})
        healthy = all(
            windows.get(label, {}).get(metric, {}).get("total") is not None
            for label in ("24h", "7d") for metric in _FAILURE_METRICS
        ) and payload.get("store", {}).get("reachable") is True
        health = GaugeMetricFamily("lu_fleet_comms_collection_success", "Whether authority failure metrics were collected.")
        health.add_metric([], int(healthy))
        yield health
        if not healthy:
            return
        for metric in _FAILURE_METRICS:
            total = GaugeMetricFamily(
                f"lu_fleet_comms_{metric}_recent", "Terminal events in the rolling window.", labels=["window"],
            )
            grouped = GaugeMetricFamily(
                f"lu_fleet_comms_{metric}_recent_by_recipient", "Terminal events by bounded recipient bucket.",
                labels=["window", "recipient"],
            )
            for label in ("24h", "7d"):
                row = windows.get(label, {}).get(metric, {})
                if row.get("total") is None:
                    continue
                total.add_metric([label], row["total"])
                for recipient, count in row["by_recipient"].items():
                    grouped.add_metric([label, recipient], count)
            yield total
            yield grouped
