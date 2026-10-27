import { useState } from "react"
import { ArrowRight, Gauge, Skull, Zap } from "lucide-react"
import type { ServiceInfo } from "../api"
import { fmt } from "../hooks"
import { Button, HealthDot } from "./ui"

function Replicas({ s }: { s: ServiceInfo }) {
  // one block per desired replica: filled = ready
  const blocks = Array.from({ length: Math.max(s.desired, s.pods.length) }, (_, i) => i < s.ready)
  return (
    <div className="flex items-center gap-1" aria-label={`${s.ready} of ${s.desired} replicas ready`}>
      {blocks.map((ready, i) => (
        <span key={i} className={`h-2.5 w-4 rounded-sm ${ready ? "bg-ok" : "bg-bad/70"}`} />
      ))}
      <span className="ml-1 text-xs text-muted tabular">{s.ready}/{s.desired}</span>
    </div>
  )
}

function Stat({ label, value, bad }: { label: string; value: string; bad?: boolean }) {
  return (
    <div>
      <div className="text-[11px] uppercase tracking-wide text-muted">{label}</div>
      <div className={`text-sm font-semibold tabular ${bad ? "text-bad" : "text-fg"}`}>{value}</div>
    </div>
  )
}

export function ServiceCard({ s, chaosEnabled, onKill, onFault }: {
  s: ServiceInfo
  chaosEnabled: boolean
  onKill: (service: string) => void
  onFault: (service: string) => void
}) {
  const [busy, setBusy] = useState(false)
  const errorHigh = (s.error_rate ?? 0) > 0.05
  return (
    <article
      className={`flex flex-col gap-3 rounded-xl border bg-surface p-4 ${
        s.health === "down" ? "border-bad/60" : s.health === "degraded" ? "border-warn/50" : "border-border"
      }`}
    >
      <div className="flex items-start justify-between gap-2">
        <div>
          <h3 className="font-mono text-sm font-semibold text-fg">{s.name}</h3>
          <HealthDot health={s.health} pulse />
        </div>
        <Replicas s={s} />
      </div>

      <div className="grid grid-cols-3 gap-2">
        <Stat label="req/s" value={fmt.num(s.rps)} />
        <Stat label="errors" value={fmt.pct(s.error_rate)} bad={errorHigh} />
        <Stat label="p95" value={fmt.ms(s.p95_ms)} />
      </div>

      {s.depends_on.length > 0 && (
        <div className="flex flex-wrap items-center gap-1 text-[11px] text-muted">
          <ArrowRight className="h-3 w-3" aria-hidden /> {s.depends_on.join(", ")}
        </div>
      )}

      {s.chaos_target && (
        <div className="mt-auto flex flex-wrap gap-2 border-t border-border pt-3">
          <Button
            variant="danger"
            disabled={!chaosEnabled || busy || s.ready === 0}
            onClick={async () => {
              setBusy(true)
              try {
                await onKill(s.name)
              } finally {
                setBusy(false)
              }
            }}
            title="Kill a random ready pod of this service"
          >
            <Skull className="h-3.5 w-3.5" /> Kill a pod
          </Button>
          {s.name !== "postgres" && (
            <Button disabled={!chaosEnabled} onClick={() => onFault(s.name)} title="Inject latency or errors">
              <Zap className="h-3.5 w-3.5" /> Inject fault
            </Button>
          )}
        </div>
      )}
    </article>
  )
}

export function UserImpact({ value }: { value: number | null }) {
  const tone = value == null ? "text-muted" : value >= 0.99 ? "text-ok" : value >= 0.95 ? "text-warn" : "text-bad"
  return (
    <div className="flex items-center gap-3 rounded-xl border border-border bg-surface px-4 py-3">
      <Gauge className={`h-5 w-5 ${tone}`} aria-hidden />
      <div>
        <div className="text-[11px] uppercase tracking-wide text-muted">Customer success rate (1m)</div>
        <div className={`text-2xl font-semibold tabular ${tone}`}>{fmt.pct(value, 2)}</div>
      </div>
    </div>
  )
}
