import { useCallback, useEffect, useRef, useState } from "react"

/** Poll `fn` every `intervalMs`, pausing while the tab is hidden. */
export function usePoll<T>(fn: () => Promise<T>, intervalMs: number) {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<Error | null>(null)
  const fnRef = useRef(fn)
  fnRef.current = fn

  const refresh = useCallback(async () => {
    try {
      setData(await fnRef.current())
      setError(null)
    } catch (e) {
      setError(e as Error)
    }
  }, [])

  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | undefined
    let cancelled = false
    const tick = async () => {
      if (!document.hidden) await refresh()
      if (!cancelled) timer = setTimeout(tick, intervalMs)
    }
    tick()
    const onVisible = () => !document.hidden && refresh()
    document.addEventListener("visibilitychange", onVisible)
    return () => {
      cancelled = true
      clearTimeout(timer)
      document.removeEventListener("visibilitychange", onVisible)
    }
  }, [intervalMs, refresh])

  return { data, error, refresh }
}

export const fmt = {
  pct: (v: number | null | undefined, digits = 1) => (v == null ? "—" : `${(v * 100).toFixed(digits)}%`),
  num: (v: number | null | undefined, digits = 1) => (v == null ? "—" : v.toFixed(digits)),
  ms: (v: number | null | undefined) => (v == null ? "—" : v >= 1000 ? `${(v / 1000).toFixed(2)}s` : `${Math.round(v)}ms`),
  age: (sec: number | null | undefined) => {
    if (sec == null) return "—"
    if (sec < 60) return `${sec}s`
    if (sec < 3600) return `${Math.floor(sec / 60)}m`
    if (sec < 86400) return `${Math.floor(sec / 3600)}h`
    return `${Math.floor(sec / 86400)}d`
  },
  ago: (iso: string | null) => {
    if (!iso) return "—"
    const s = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 1000))
    return s < 5 ? "just now" : `${fmt.age(s)} ago`
  },
}
