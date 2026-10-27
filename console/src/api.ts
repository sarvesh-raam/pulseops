export interface PodInfo {
  id: string
  name: string
  service: string
  status: string
  ready: boolean
  restarts: number
  started_at: string | null
  age_sec: number | null
  ip: string | null
  node: string | null
  killable: boolean
}

export interface ServiceInfo {
  name: string
  depends_on: string[]
  desired: number
  ready: number
  health: "healthy" | "degraded" | "down"
  rps: number | null
  error_rate: number | null
  p95_ms: number | null
  chaos_target: boolean
  pods: PodInfo[]
}

export interface Overview {
  runtime: string | null
  runtime_error: string | null
  prometheus_error: string | null
  chaos_enabled: boolean
  token_required: boolean
  user_success_rate: number | null
  services: ServiceInfo[]
  alerts_firing: number
  links: Record<"grafana" | "prometheus" | "jaeger" | "alertmanager", string>
}

export interface Experiment {
  id: string
  kind: string
  service: string
  target: string
  started_at: string
  ended_at: string | null
  status: "running" | "recovered" | "failed" | "active" | "expired"
  detail: string
  recovery_seconds: number | null
}

export interface Alert {
  fingerprint: string
  status: "firing" | "resolved"
  name: string
  severity: string
  service: string | null
  summary: string
  starts_at: string | null
  received_at: string
}

export type Series = [number, number | null][]
export interface Timeseries {
  services: Record<string, Partial<Record<"rps" | "error_rate" | "p95_ms", Series>>>
  user_success: Series
}

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message)
  }
}

const TOKEN_KEY = "pulseops.token"

export function getToken(): string {
  try {
    return localStorage.getItem(TOKEN_KEY) ?? ""
  } catch {
    return ""
  }
}

export function setToken(token: string) {
  try {
    localStorage.setItem(TOKEN_KEY, token)
  } catch {
    /* storage unavailable: token lives for this page only */
  }
}

async function call<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers)
  headers.set("Content-Type", "application/json")
  const token = getToken()
  if (token) headers.set("X-PulseOps-Token", token)
  const res = await fetch(path, { ...init, headers })
  if (!res.ok) {
    let detail = res.statusText
    try {
      detail = (await res.json()).detail ?? detail
    } catch {
      /* not JSON */
    }
    throw new ApiError(res.status, String(detail))
  }
  return res.status === 204 ? (undefined as T) : res.json()
}

export const api = {
  overview: () => call<Overview>("/api/overview"),
  timeseries: (minutes = 15) => call<Timeseries>(`/api/timeseries?minutes=${minutes}`),
  experiments: () => call<Experiment[]>("/api/experiments"),
  alerts: () => call<Alert[]>("/api/alerts"),
  kill: (body: { pod?: string; service?: string }) =>
    call<Experiment>("/api/chaos/kill", { method: "POST", body: JSON.stringify(body) }),
  fault: (body: { service: string; latency_ms: number; error_rate: number; duration_sec: number }) =>
    call<Experiment>("/api/chaos/faults", { method: "POST", body: JSON.stringify(body) }),
  clearFaults: (service: string) => call<unknown>(`/api/chaos/faults/${service}`, { method: "DELETE" }),
}
