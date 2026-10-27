"""Generate the provisioned Grafana dashboards.

    python scripts/gen_dashboards.py

Edit here rather than in the JSON: the output in
deploy/observability/grafana/dashboards/ is overwritten.
"""

import json
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "deploy" / "observability" / "grafana" / "dashboards"
PROM = {"type": "prometheus", "uid": "prometheus"}
LOKI = {"type": "loki", "uid": "loki"}


class Board:
    def __init__(self):
        self.panels: list[dict] = []
        self._id = 0
        self.y = 0

    def panel(self, title, kind, targets, w, h, x, unit="short", ds=PROM, stack=False, legend=None, extra=None):
        self._id += 1
        p = {
            "id": self._id, "title": title, "type": kind, "datasource": ds,
            "gridPos": {"x": x, "y": self.y, "w": w, "h": h},
            "targets": [
                {"datasource": ds, "expr": e, "refId": chr(65 + i), "legendFormat": (legend or [""] * len(targets))[i]}
                for i, e in enumerate(targets)
            ],
            "fieldConfig": {"defaults": {"unit": unit}, "overrides": []},
            "options": {},
        }
        if kind == "timeseries":
            p["fieldConfig"]["defaults"]["custom"] = {
                "lineWidth": 2, "fillOpacity": 12, "showPoints": "never",
                "stacking": {"mode": "normal" if stack else "none"},
            }
            p["options"] = {"legend": {"displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "multi"}}
        elif kind == "stat":
            p["options"] = {"reduceOptions": {"calcs": ["lastNotNull"]}, "colorMode": "value", "graphMode": "area"}
        if extra:
            p.update(extra)
        self.panels.append(p)
        return p

    def row(self, h):
        self.y += h


def services_board() -> dict:
    b = Board()
    sel = 'service=~"$service"'
    b.panel("Customer success rate", "stat",
            ['sum(rate(loadgen_requests_total{outcome="ok"}[1m])) / sum(rate(loadgen_requests_total[1m]))'],
            6, 4, 0, unit="percentunit",
            extra={"fieldConfig": {"defaults": {"unit": "percentunit", "min": 0, "max": 1, "thresholds": {
                "mode": "absolute", "steps": [{"color": "red", "value": None}, {"color": "orange", "value": 0.95},
                                              {"color": "green", "value": 0.99}]}}, "overrides": []}})
    b.panel("Requests / s", "stat", [f"sum(rate(http_server_requests_total{{{sel}}}[1m]))"], 6, 4, 6, unit="reqps")
    b.panel("Error rate", "stat",
            [f'sum(rate(http_server_requests_total{{{sel},status=~"5.."}}[1m])) / sum(rate(http_server_requests_total{{{sel}}}[1m]))'],
            6, 4, 12, unit="percentunit")
    b.panel("p95 latency", "stat",
            [f"histogram_quantile(0.95, sum by (le) (rate(http_server_request_duration_seconds_bucket{{{sel}}}[1m])))"],
            6, 4, 18, unit="s")
    b.row(4)
    b.panel("Rate by service", "timeseries", [f"sum by (service) (rate(http_server_requests_total{{{sel}}}[1m]))"],
            8, 8, 0, unit="reqps", legend=["{{service}}"])
    b.panel("Errors by service", "timeseries",
            [f'sum by (service) (rate(http_server_requests_total{{{sel},status=~"5.."}}[1m])) / sum by (service) (rate(http_server_requests_total{{{sel}}}[1m]))'],
            8, 8, 8, unit="percentunit", legend=["{{service}}"])
    b.panel("Duration p50 / p95 / p99", "timeseries", [
        f"histogram_quantile({q}, sum by (le, service) (rate(http_server_request_duration_seconds_bucket{{{sel}}}[1m])))"
        for q in (0.5, 0.95, 0.99)], 8, 8, 16, unit="s",
        legend=["p50 {{service}}", "p95 {{service}}", "p99 {{service}}"])
    b.row(8)
    b.panel("Outbound calls by outcome", "timeseries",
            [f"sum by (service, target, outcome) (rate(http_client_requests_total{{{sel}}}[1m]))"],
            12, 8, 0, unit="reqps", legend=["{{service}} → {{target}} {{outcome}}"])
    b.panel("Circuit breakers (0 closed · 1 half-open · 2 open)", "timeseries",
            [f"max by (service, target) (circuit_breaker_state{{{sel}}})"], 12, 8, 12,
            legend=["{{service}} → {{target}}"])
    b.row(8)
    b.panel("Orders by outcome", "timeseries", ["sum by (status, reason) (rate(orders_total[1m]))"],
            12, 8, 0, unit="ops", legend=["{{status}} {{reason}}"], stack=True)
    b.panel("Logs (errors & warnings)", "logs",
            ['{service=~"$service", level=~"error|warning"}'], 12, 8, 12, ds=LOKI,
            extra={"options": {"showTime": True, "wrapLogMessage": True, "sortOrder": "Descending"}})
    return {
        "uid": "pulseops-services", "title": "PulseOps — Services (RED)", "tags": ["pulseops"],
        "schemaVersion": 39, "version": 1, "refresh": "5s", "time": {"from": "now-30m", "to": "now"},
        "templating": {"list": [{
            "name": "service", "type": "query", "datasource": PROM, "includeAll": True, "multi": True,
            "allValue": ".*", "current": {"text": "All", "value": "$__all"},
            "query": {"query": "label_values(http_server_requests_total, service)", "refId": "A"},
            "refresh": 2,
        }]},
        "panels": b.panels,
    }


def chaos_board() -> dict:
    b = Board()
    b.panel("Chaos experiments (1h)", "stat", ["sum(increase(pulseops_chaos_experiments_total[1h]))"], 6, 4, 0)
    b.panel("Mean time to recovery", "stat",
            ["sum(rate(pulseops_chaos_recovery_seconds_sum[1h])) / sum(rate(pulseops_chaos_recovery_seconds_count[1h]))"],
            6, 4, 6, unit="s")
    b.panel("Failed recoveries (1h)", "stat", ["sum(increase(pulseops_chaos_recovery_failures_total[1h])) or vector(0)"], 6, 4, 12)
    b.panel("Alerts firing", "stat", ["max(pulseops_alerts_firing) or vector(0)"], 6, 4, 18)
    b.row(4)
    b.panel("Ready replicas by service", "timeseries", ['max by (service) (pulseops_service_replicas{state="ready"})'],
            12, 9, 0, legend=["{{service}}"])
    b.panel("Customer success rate during experiments", "timeseries",
            ['sum(rate(loadgen_requests_total{outcome="ok"}[30s])) / sum(rate(loadgen_requests_total[30s]))'],
            12, 9, 12, unit="percentunit", legend=["success"])
    b.row(9)
    b.panel("Experiments started", "timeseries", ["sum by (kind, service) (increase(pulseops_chaos_experiments_total[1m]))"],
            8, 8, 0, legend=["{{kind}} {{service}}"], stack=True)
    b.panel("Recovery time p50 / p95", "timeseries", [
        f"histogram_quantile({q}, sum by (le, service) (rate(pulseops_chaos_recovery_seconds_bucket[15m])))" for q in (0.5, 0.95)],
        8, 8, 8, unit="s", legend=["p50 {{service}}", "p95 {{service}}"])
    b.panel("Retries absorbing the failures", "timeseries", ["sum by (service, target) (rate(http_client_retries_total[1m]))"],
            8, 8, 16, unit="ops", legend=["{{service}} → {{target}}"])
    b.row(8)
    b.panel("Chaos & recovery log", "logs", ['{service="controlplane"} |~ "pod killed|recovered|circuit"'], 24, 8, 0, ds=LOKI,
            extra={"options": {"showTime": True, "wrapLogMessage": True, "sortOrder": "Descending"}})
    return {
        "uid": "pulseops-chaos", "title": "PulseOps — Chaos & Self-healing", "tags": ["pulseops"],
        "schemaVersion": 39, "version": 1, "refresh": "5s", "time": {"from": "now-30m", "to": "now"},
        "panels": b.panels,
    }


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    for board in (services_board(), chaos_board()):
        path = OUT / f"{board['uid']}.json"
        path.write_text(json.dumps(board, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"wrote {path.name} ({len(board['panels'])} panels)")
