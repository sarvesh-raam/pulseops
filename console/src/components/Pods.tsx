import { useState } from "react"
import { Skull } from "lucide-react"
import type { ServiceInfo } from "../api"
import { fmt } from "../hooks"
import { Button, Card } from "./ui"

const STATUS_TONE: Record<string, string> = {
  Running: "text-ok",
  Pending: "text-warn",
  ContainerCreating: "text-warn",
  Restarting: "text-warn",
  Terminating: "text-bad",
  CrashLoopBackOff: "text-bad",
}

export function PodsTable({ services, chaosEnabled, onKill }: {
  services: ServiceInfo[]
  chaosEnabled: boolean
  onKill: (pod: string) => Promise<void>
}) {
  const [busy, setBusy] = useState<string | null>(null)
  const pods = services.flatMap((s) => s.pods)
  return (
    <Card title={`Pods (${pods.length})`}>
      <div className="-mx-4 -my-4 overflow-x-auto">
        <table className="w-full min-w-[640px] text-left text-sm">
          <thead className="text-[11px] uppercase tracking-wide text-muted">
            <tr className="border-b border-border">
              <th className="px-4 py-2 font-medium">Pod</th>
              <th className="px-2 py-2 font-medium">Service</th>
              <th className="px-2 py-2 font-medium">Status</th>
              <th className="px-2 py-2 font-medium">Restarts</th>
              <th className="px-2 py-2 font-medium">Age</th>
              <th className="px-4 py-2" />
            </tr>
          </thead>
          <tbody>
            {pods.map((p) => (
              <tr key={p.id} className="border-b border-border/60 last:border-0">
                <td className="max-w-[260px] truncate px-4 py-2 font-mono text-xs text-fg" title={p.name}>{p.name}</td>
                <td className="px-2 py-2 font-mono text-xs text-muted">{p.service}</td>
                <td className={`px-2 py-2 text-xs ${STATUS_TONE[p.status] ?? "text-muted"}`}>
                  {p.status}
                  {p.status === "Running" && !p.ready && <span className="text-warn"> (not ready)</span>}
                </td>
                <td className={`px-2 py-2 text-xs tabular ${p.restarts ? "text-warn" : "text-muted"}`}>{p.restarts}</td>
                <td className="px-2 py-2 text-xs text-muted tabular">{fmt.age(p.age_sec)}</td>
                <td className="px-4 py-2 text-right">
                  {p.killable && (
                    <Button
                      variant="danger"
                      disabled={!chaosEnabled || busy !== null}
                      onClick={async () => {
                        setBusy(p.id)
                        try {
                          await onKill(p.id)
                        } finally {
                          setBusy(null)
                        }
                      }}
                      title={`Kill ${p.name} now`}
                    >
                      <Skull className="h-3.5 w-3.5" /> Kill
                    </Button>
                  )}
                </td>
              </tr>
            ))}
            {pods.length === 0 && (
              <tr><td colSpan={6} className="px-4 py-6 text-center text-muted">No pods discovered yet.</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </Card>
  )
}
