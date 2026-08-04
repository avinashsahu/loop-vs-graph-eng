import { useRef, useState } from 'react'

type Decision = {
  symbol: string
  disposition: string | null
  status: string
}

type ScanStatus = {
  status: string
  scan_label: string
  finished_at: string | null
  decisions: Decision[]
}

const DEFAULT_POLL_INTERVAL_MS = 3000

function dispositionBadgeClass(disposition: string | null): string {
  switch (disposition) {
    case 'PROPOSE':
      return 'badge badge-success'
    case 'REJECT':
      return 'badge badge-danger'
    case 'REVIEW':
      return 'badge badge-warning'
    default:
      return 'badge badge-neutral'
  }
}

export default function ScanPage({
  pollIntervalMs = DEFAULT_POLL_INTERVAL_MS,
}: {
  pollIntervalMs?: number
}) {
  const [symbolsInput, setSymbolsInput] = useState('')
  const [scan, setScan] = useState<ScanStatus | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const pollHandle = useRef<ReturnType<typeof setInterval> | null>(null)

  const stopPolling = () => {
    if (pollHandle.current !== null) {
      clearInterval(pollHandle.current)
      pollHandle.current = null
    }
  }

  const pollScan = (requestId: string) => {
    pollHandle.current = setInterval(async () => {
      try {
        const response = await fetch(`/api/scans/${requestId}`)
        if (!response.ok) {
          setError(`Could not check scan status (HTTP ${response.status}).`)
          stopPolling()
          return
        }
        const body: ScanStatus = await response.json()
        setScan(body)
        if (body.status !== 'queued' && body.status !== 'running') {
          stopPolling()
        }
      } catch {
        setError('Lost connection while checking scan status.')
        stopPolling()
      }
    }, pollIntervalMs)
  }

  const submit = async (event: React.FormEvent) => {
    event.preventDefault()
    stopPolling()
    setError(null)
    const symbols = symbolsInput
      .split(/[\s,]+/)
      .map((s) => s.trim())
      .filter(Boolean)
    if (symbols.length === 0) {
      setError('Enter at least one symbol.')
      return
    }
    setSubmitting(true)
    setScan(null)
    try {
      const response = await fetch('/api/scans', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ symbols }),
      })
      if (!response.ok) {
        const body = await response.json().catch(() => null)
        setError(body?.detail ?? `Scan request failed (HTTP ${response.status}).`)
        return
      }
      const { request_id: requestId } = await response.json()
      setScan({ status: 'queued', scan_label: '', finished_at: null, decisions: [] })
      pollScan(requestId)
    } catch {
      setError('Could not reach the control API. Is it running?')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <section>
      <h2>Trigger a scan</h2>
      <form className="inline-form" onSubmit={submit}>
        <label htmlFor="symbols">Symbols</label>
        <input
          id="symbols"
          type="text"
          value={symbolsInput}
          onChange={(event) => setSymbolsInput(event.target.value)}
          placeholder="RELIANCE HDFCBANK"
        />
        <button type="submit" className="primary" disabled={submitting}>
          {submitting ? 'Starting…' : 'Run scan'}
        </button>
      </form>

      {error && <p className="error-text">{error}</p>}

      {scan && (scan.status === 'queued' || scan.status === 'running') && (
        <div className="status-line">
          <span className="spinner" />
          <span>Scan {scan.status} — checking every {Math.round(pollIntervalMs / 1000)}s…</span>
        </div>
      )}

      {scan && scan.status === 'failed' && (
        <p className="error-text">Scan failed. Check cron.log for details.</p>
      )}

      {scan && scan.status === 'done' && scan.decisions.length === 0 && (
        <p className="empty-text">Scan completed, but produced no decision rows.</p>
      )}

      {scan && scan.status === 'done' && scan.decisions.length > 0 && (
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>Symbol</th>
                <th>Disposition</th>
              </tr>
            </thead>
            <tbody>
              {scan.decisions.map((decision) => (
                <tr key={decision.symbol}>
                  <td>{decision.symbol}</td>
                  <td>
                    <span className={dispositionBadgeClass(decision.disposition)}>
                      {decision.disposition ?? decision.status}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}
