import { BellRing, CheckCircle2, CircleAlert, Clock, Loader2 } from "lucide-react"
import type { Alert, Experiment } from "../api"
import { fmt } from "../hooks"
import { Card } from "./ui"

const EXP_ICON = {
  running: <Loader2 className="h-4 w-4 animate-spin text-warn" aria-label="recovering" />,
  active: <Clock className="h-4 w-4 text-warn" aria-label="fault active" />,
  recovered: <CheckCircle2 className="h-4 w-4 text-ok" aria-label="recovered" />,
  expired: <CheckCircle2 className="h-4 w-4 text-muted" aria-label="fault ended" />,
  failed: <CircleAlert className="h-4 w-4 text-bad" aria-label="did not recover" />,
}

export function ExperimentsPanel({ experiments }: { experiments: Experiment[] }) {
  const recovered = experiments.filter((e) => e.recovery_seconds != null)
  const mttr = recovered.length
    ? recovered.reduce((a, e) => a + (e.recovery_seconds ?? 0), 0) / recovered.length
    : null
  return (
    <Card
      title="Chaos experiments"
      actions={<span className="text-xs text-muted">MTTR <strong className="text-fg tabular">{mttr == null ? "—" : `${mttr.toFixed(1)}s`}</strong></span>}
    >
      {experiments.length === 0 ? (
        <p className="text-sm text-muted">No experiments yet. Kill a pod and watch the platform heal.</p>
      ) : (
        <ol className="space-y-3">
          {experiments.slice(0, 12).map((e) => (
            <li key={e.id} className="flex gap-3">
              <span className="mt-0.5">{EXP_ICON[e.status]}</span>
              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-baseline justify-between gap-x-2">
                  <span className="text-sm text-fg">
                    <span className="font-medium">{e.kind}</span> <span className="font-mono text-xs text-muted">{e.service}</span>
                  </span>
                  <span className="text-xs text-muted">{fmt.ago(e.started_at)}</span>
                </div>
                <p className="truncate text-xs text-muted" title={e.detail}>{e.detail}</p>
                {e.recovery_seconds != null && (
                  <p className="text-xs text-ok tabular">healed in {e.recovery_seconds.toFixed(1)}s</p>
                )}
              </div>
            </li>
          ))}
        </ol>
      )}
    </Card>
  )
}

export function AlertsPanel({ alerts, alertmanagerUrl }: { alerts: Alert[]; alertmanagerUrl: string }) {
  const firing = alerts.filter((a) => a.status === "firing")
  return (
    <Card
      title={<span className="inline-flex items-center gap-2"><BellRing className="h-4 w-4" aria-hidden /> Alerts</span>}
      actions={<a className="text-xs text-info hover:underline" href={alertmanagerUrl} target="_blank" rel="noreferrer">Alertmanager ↗</a>}
    >
      {alerts.length === 0 ? (
        <p className="text-sm text-muted">No alerts received. Alertmanager delivers them here via webhook.</p>
      ) : (
        <ul className="space-y-2">
          {alerts.slice(0, 10).map((a) => (
            <li key={a.fingerprint} className="flex items-start gap-2 text-sm">
              <span className={`mt-1.5 h-2 w-2 shrink-0 rounded-full ${a.status !== "firing" ? "bg-muted" : a.severity === "critical" ? "bg-bad" : "bg-warn"}`} />
              <div className="min-w-0">
                <span className={a.status === "firing" ? "text-fg" : "text-muted line-through"}>{a.name}</span>
                {a.service && <span className="ml-1 font-mono text-xs text-muted">{a.service}</span>}
                <p className="text-xs text-muted">{a.summary}</p>
              </div>
            </li>
          ))}
        </ul>
      )}
      {firing.length > 0 && <p className="mt-3 text-xs text-bad">{firing.length} firing</p>}
    </Card>
  )
}
