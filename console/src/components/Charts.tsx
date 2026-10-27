import { useMemo, useState } from "react"
import { CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts"
import type { Series, Timeseries } from "../api"
import { Card } from "./ui"

// one stable color per service (legend order = topology order)
const COLORS: Record<string, string> = {
  gateway: "#60a5fa",
  orders: "#34d399",
  inventory: "#fbbf24",
  payments: "#f472b6",
  controlplane: "#a78bfa",
}
const METRICS = {
  rps: { label: "Requests / s", fmt: (v: number) => v.toFixed(1) },
  error_rate: { label: "Error rate", fmt: (v: number) => `${(v * 100).toFixed(1)}%` },
  p95_ms: { label: "p95 latency", fmt: (v: number) => (v >= 1000 ? `${(v / 1000).toFixed(1)}s` : `${Math.round(v)}ms`) },
} as const
type Metric = keyof typeof METRICS

function merge(byService: Record<string, Series | undefined>) {
  const rows = new Map<number, Record<string, number | null>>()
  for (const [service, series] of Object.entries(byService)) {
    for (const [t, v] of series ?? []) {
      const row = rows.get(t) ?? { t }
      row[service] = v
      rows.set(t, row)
    }
  }
  return [...rows.values()].sort((a, b) => (a.t as number) - (b.t as number))
}

const time = (t: number) => new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })

export function SignalsChart({ data }: { data: Timeseries | null }) {
  const [metric, setMetric] = useState<Metric>("rps")
  const services = useMemo(
    () => Object.keys(data?.services ?? {}).filter((s) => s in COLORS).sort((a, b) => Object.keys(COLORS).indexOf(a) - Object.keys(COLORS).indexOf(b)),
    [data],
  )
  const rows = useMemo(
    () => merge(Object.fromEntries(services.map((s) => [s, data?.services[s]?.[metric]]))),
    [data, services, metric],
  )
  const m = METRICS[metric]

  return (
    <Card
      title="Golden signals (last 15 min)"
      actions={
        <div className="flex rounded-lg border border-border p-0.5" role="tablist">
          {(Object.keys(METRICS) as Metric[]).map((k) => (
            <button
              key={k}
              role="tab"
              aria-selected={metric === k}
              onClick={() => setMetric(k)}
              className={`rounded-md px-2 py-1 text-xs ${metric === k ? "bg-surface-2 text-fg" : "text-muted hover:text-fg"}`}
            >
              {METRICS[k].label}
            </button>
          ))}
        </div>
      }
    >
      <div className="h-64">
        {rows.length === 0 ? (
          <div className="flex h-full items-center justify-center text-sm text-muted">Waiting for Prometheus data…</div>
        ) : (
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={rows} margin={{ top: 4, right: 8, bottom: 0, left: 0 }}>
              <CartesianGrid stroke="var(--border)" strokeDasharray="3 3" vertical={false} />
              <XAxis dataKey="t" tickFormatter={time} stroke="var(--muted)" fontSize={11} tickLine={false} axisLine={false} minTickGap={40} />
              <YAxis stroke="var(--muted)" fontSize={11} tickLine={false} axisLine={false} width={52} tickFormatter={(v) => m.fmt(v)} />
              <Tooltip
                labelFormatter={(t) => time(Number(t))}
                formatter={(v) => (typeof v === "number" ? m.fmt(v) : "—")}
                contentStyle={{ background: "var(--surface-2)", border: "1px solid var(--border)", borderRadius: 8, fontSize: 12 }}
                labelStyle={{ color: "var(--fg)" }}
              />
              <Legend wrapperStyle={{ fontSize: 12 }} />
              {services.map((s) => (
                <Line key={s} type="monotone" dataKey={s} stroke={COLORS[s]} strokeWidth={2} dot={false} isAnimationActive={false} connectNulls={false} />
              ))}
            </LineChart>
          </ResponsiveContainer>
        )}
      </div>
    </Card>
  )
}
