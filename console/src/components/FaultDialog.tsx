import { useEffect, useRef, useState } from "react"
import { Button } from "./ui"

export function FaultDialog({ service, onClose, onSubmit, onClear }: {
  service: string | null
  onClose: () => void
  onSubmit: (spec: { latency_ms: number; error_rate: number; duration_sec: number }) => Promise<void>
  onClear: () => Promise<void>
}) {
  const ref = useRef<HTMLDialogElement>(null)
  const [latency, setLatency] = useState(500)
  const [errors, setErrors] = useState(0)
  const [duration, setDuration] = useState(60)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    const d = ref.current
    if (!d) return
    if (service && !d.open) d.showModal()
    if (!service && d.open) d.close()
  }, [service])

  const run = async (fn: () => Promise<void>) => {
    setBusy(true)
    try {
      await fn()
      onClose()
    } finally {
      setBusy(false)
    }
  }

  return (
    <dialog
      ref={ref}
      onClose={onClose}
      className="m-auto w-[min(420px,calc(100vw-32px))] rounded-xl border border-border bg-surface p-0 text-fg backdrop:bg-black/60"
    >
      <form
        method="dialog"
        className="space-y-4 p-5"
        onSubmit={(e) => {
          e.preventDefault()
          run(() => onSubmit({ latency_ms: latency, error_rate: errors / 100, duration_sec: duration }))
        }}
      >
        <div>
          <h2 className="text-base font-semibold">Inject fault into <span className="font-mono">{service}</span></h2>
          <p className="mt-1 text-xs text-muted">
            Applied to every replica, expires automatically. Probes stay green, so this tests retries and circuit
            breakers, not Kubernetes restarts.
          </p>
        </div>
        <label className="block text-sm">
          Added latency: <strong className="tabular">{latency}ms</strong>
          <input className="mt-1 w-full accent-[var(--accent)]" type="range" min={0} max={3000} step={50} value={latency} onChange={(e) => setLatency(+e.target.value)} />
        </label>
        <label className="block text-sm">
          Error rate: <strong className="tabular">{errors}%</strong>
          <input className="mt-1 w-full accent-[var(--accent)]" type="range" min={0} max={100} step={5} value={errors} onChange={(e) => setErrors(+e.target.value)} />
        </label>
        <label className="block text-sm">
          Duration: <strong className="tabular">{duration}s</strong>
          <input className="mt-1 w-full accent-[var(--accent)]" type="range" min={10} max={600} step={10} value={duration} onChange={(e) => setDuration(+e.target.value)} />
        </label>
        <div className="flex flex-wrap justify-between gap-2 pt-2">
          <Button variant="ghost" onClick={() => run(onClear)} disabled={busy}>Clear active faults</Button>
          <div className="flex gap-2">
            <Button onClick={onClose}>Cancel</Button>
            <Button type="submit" variant="danger" disabled={busy || (!latency && !errors)}>Inject</Button>
          </div>
        </div>
      </form>
    </dialog>
  )
}
