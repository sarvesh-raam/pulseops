import { useState } from "react"
import { Activity, ExternalLink, KeyRound, Shuffle } from "lucide-react"
import { api, ApiError, getToken, setToken } from "./api"
import { usePoll } from "./hooks"
import { ServiceCard, UserImpact } from "./components/Services"
import { PodsTable } from "./components/Pods"
import { AlertsPanel, ExperimentsPanel } from "./components/Panels"
import { SignalsChart } from "./components/Charts"
import { FaultDialog } from "./components/FaultDialog"
import { Button, useToast } from "./components/ui"

export default function App() {
  const toast = useToast()
  const overview = usePoll(api.overview, 2000)
  const experiments = usePoll(api.experiments, 2000)
  const alerts = usePoll(api.alerts, 5000)
  const series = usePoll(() => api.timeseries(15), 10000)
  const [faultTarget, setFaultTarget] = useState<string | null>(null)

  const o = overview.data
  const chaosEnabled = !!o?.chaos_enabled

  // run a chaos call; on 401 ask for the control-plane token once and retry
  const guarded = async <T,>(fn: () => Promise<T>, ok: (r: T) => string) => {
    try {
      const r = await fn()
      toast("ok", ok(r))
      experiments.refresh()
      overview.refresh()
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) {
        const token = window.prompt("This control plane requires a token (CONTROL_PLANE_TOKEN):", getToken())
        if (token) {
          setToken(token)
          return guarded(fn, ok)
        }
      }
      toast("bad", e instanceof Error ? e.message : "request failed")
    }
  }

  const killService = (service?: string) =>
    guarded(() => api.kill(service ? { service } : {}), (r) => `Killed ${r.target} — watching it heal…`)
  const killPod = (pod: string) => guarded(() => api.kill({ pod }), (r) => `Killed ${r.target} — watching it heal…`)

  return (
    <div className="mx-auto max-w-[1400px] px-4 py-5 sm:px-6">
      <header className="mb-5 flex flex-wrap items-center justify-between gap-4">
        <div className="flex items-center gap-3">
          <Activity className="h-6 w-6 text-accent" aria-hidden />
          <div>
            <h1 className="text-lg font-semibold">PulseOps</h1>
            <p className="text-xs text-muted">
              Observability & chaos console ·{" "}
              {o ? (o.runtime ? <>runtime <strong className="text-fg">{o.runtime}</strong></> : <span className="text-warn">no runtime (read-only)</span>) : "connecting…"}
            </p>
          </div>
        </div>
        <nav className="flex flex-wrap items-center gap-2 text-xs">
          {o &&
            (["grafana", "prometheus", "jaeger", "alertmanager"] as const).map((k) => (
              <a key={k} href={o.links[k]} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 rounded-lg border border-border px-2.5 py-1.5 capitalize text-muted hover:text-fg">
                {k} <ExternalLink className="h-3 w-3" aria-hidden />
              </a>
            ))}
          {o?.token_required && (
            <Button variant="ghost" onClick={() => { const t = window.prompt("Control-plane token:", getToken()); if (t !== null) setToken(t) }}>
              <KeyRound className="h-3.5 w-3.5" /> Token
            </Button>
          )}
        </nav>
      </header>

      {overview.error && !o && (
        <div className="mb-4 rounded-lg border border-bad/50 bg-bad/10 px-4 py-3 text-sm text-bad">
          Cannot reach the control plane: {overview.error.message}
        </div>
      )}
      {(o?.runtime_error || o?.prometheus_error) && (
        <div className="mb-4 rounded-lg border border-warn/50 bg-warn/10 px-4 py-3 text-sm text-warn">
          {o.runtime_error && <>Runtime: {o.runtime_error}. </>}
          {o.prometheus_error && <>Prometheus unreachable ({o.prometheus_error}) — metrics unavailable.</>}
        </div>
      )}

      <div className="mb-5 grid gap-4 md:grid-cols-[minmax(0,1fr)_auto]">
        <UserImpact value={o?.user_success_rate ?? null} />
        <div className="flex items-center justify-center rounded-xl border border-bad/40 bg-bad/5 px-5 py-3">
          <Button variant="danger" className="px-4 py-2 text-sm" disabled={!chaosEnabled} onClick={() => killService()}>
            <Shuffle className="h-4 w-4" /> Kill a random pod
          </Button>
        </div>
      </div>

      <section aria-label="Services" className="mb-5 grid gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-6">
        {o?.services.map((s) => (
          <ServiceCard key={s.name} s={s} chaosEnabled={chaosEnabled} onKill={(svc) => killService(svc)} onFault={setFaultTarget} />
        ))}
      </section>

      <div className="mb-5 grid gap-4 lg:grid-cols-3">
        <div className="lg:col-span-2"><SignalsChart data={series.data} /></div>
        <ExperimentsPanel experiments={experiments.data ?? []} />
      </div>

      <div className="grid gap-4 lg:grid-cols-3">
        <div className="lg:col-span-2"><PodsTable services={o?.services ?? []} chaosEnabled={chaosEnabled} onKill={killPod} /></div>
        <AlertsPanel alerts={alerts.data ?? []} alertmanagerUrl={o?.links.alertmanager ?? "#"} />
      </div>

      <FaultDialog
        service={faultTarget}
        onClose={() => setFaultTarget(null)}
        onSubmit={(spec) => guarded(() => api.fault({ service: faultTarget!, ...spec }), (r) => `Fault active on ${r.service}: ${r.detail}`)}
        onClear={() => guarded(() => api.clearFaults(faultTarget!), () => `Faults cleared on ${faultTarget}`)}
      />
    </div>
  )
}
