import { createContext, useCallback, useContext, useState, type ReactNode } from "react"

export function Card({ title, actions, children, className = "" }: {
  title?: ReactNode
  actions?: ReactNode
  children: ReactNode
  className?: string
}) {
  return (
    <section className={`rounded-xl border border-border bg-surface ${className}`}>
      {(title || actions) && (
        <header className="flex items-center justify-between gap-3 border-b border-border px-4 py-3">
          <h2 className="text-sm font-semibold text-fg">{title}</h2>
          {actions}
        </header>
      )}
      <div className="p-4">{children}</div>
    </section>
  )
}

const HEALTH = {
  healthy: { dot: "bg-ok text-ok", label: "Healthy" },
  degraded: { dot: "bg-warn text-warn", label: "Degraded" },
  down: { dot: "bg-bad text-bad", label: "Down" },
} as const

export function HealthDot({ health, pulse = false }: { health: keyof typeof HEALTH; pulse?: boolean }) {
  const h = HEALTH[health]
  return (
    <span className="inline-flex items-center gap-1.5 text-xs text-muted">
      <span className={`relative inline-block h-2 w-2 rounded-full ${h.dot} ${pulse && health !== "healthy" ? "pulse-ring" : ""}`} />
      {h.label}
    </span>
  )
}

export function Button({ children, onClick, variant = "default", disabled, title, className = "", type = "button" }: {
  children: ReactNode
  onClick?: () => void
  variant?: "default" | "danger" | "ghost"
  disabled?: boolean
  title?: string
  className?: string
  type?: "button" | "submit"
}) {
  const styles = {
    default: "border border-border bg-surface-2 text-fg hover:border-muted",
    danger: "border border-bad/40 bg-bad/15 text-bad hover:bg-bad/25",
    ghost: "text-muted hover:text-fg",
  }[variant]
  return (
    <button
      type={type}
      onClick={onClick}
      disabled={disabled}
      title={title}
      className={`inline-flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-xs font-medium transition-colors focus-visible:outline-2 focus-visible:outline-accent disabled:cursor-not-allowed disabled:opacity-40 ${styles} ${className}`}
    >
      {children}
    </button>
  )
}

interface Toast { id: number; kind: "ok" | "bad" | "info"; text: string }
const ToastCtx = createContext<(kind: Toast["kind"], text: string) => void>(() => {})
export const useToast = () => useContext(ToastCtx)

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([])
  const push = useCallback((kind: Toast["kind"], text: string) => {
    const id = Date.now() + Math.random()
    setToasts((t) => [...t, { id, kind, text }])
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), 5000)
  }, [])
  const color = { ok: "border-ok/50", bad: "border-bad/60", info: "border-info/50" }
  return (
    <ToastCtx.Provider value={push}>
      {children}
      <div className="fixed right-4 bottom-4 z-50 flex max-w-sm flex-col gap-2" role="status" aria-live="polite">
        {toasts.map((t) => (
          <div key={t.id} className={`rounded-lg border bg-surface-2 px-3 py-2 text-sm shadow-lg ${color[t.kind]}`}>
            {t.text}
          </div>
        ))}
      </div>
    </ToastCtx.Provider>
  )
}
