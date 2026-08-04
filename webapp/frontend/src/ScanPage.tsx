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

export default function ScanPage({
  pollIntervalMs = DEFAULT_POLL_INTERVAL_MS,
}: {
  pollIntervalMs?: number
}) {
  const [symbolsInput, setSymbolsInput] = useState('')
  const [scan, setScan] = useState<ScanStatus | null>(null)
  const pollHandle = useRef<ReturnType<typeof setInterval> | null>(null)

  const stopPolling = () => {
    if (pollHandle.current !== null) {
      clearInterval(pollHandle.current)
      pollHandle.current = null
    }
  }

  const pollScan = (requestId: string) => {
    pollHandle.current = setInterval(async () => {
      const response = await fetch(`/api/scans/${requestId}`)
      if (!response.ok) return
      const body: ScanStatus = await response.json()
      setScan(body)
      if (body.status !== 'queued' && body.status !== 'running') {
        stopPolling()
      }
    }, pollIntervalMs)
  }

  const submit = async (event: React.FormEvent) => {
    event.preventDefault()
    stopPolling()
    const symbols = symbolsInput
      .split(/[\s,]+/)
      .map((s) => s.trim())
      .filter(Boolean)
    if (symbols.length === 0) return
    const response = await fetch('/api/scans', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ symbols }),
    })
    if (!response.ok) return
    const { request_id: requestId } = await response.json()
    setScan({ status: 'queued', scan_label: '', finished_at: null, decisions: [] })
    pollScan(requestId)
  }

  return (
    <section>
      <h2>Trigger a scan</h2>
      <form onSubmit={submit}>
        <label htmlFor="symbols">Symbols</label>
        <input
          id="symbols"
          value={symbolsInput}
          onChange={(event) => setSymbolsInput(event.target.value)}
          placeholder="RELIANCE HDFCBANK"
        />
        <button type="submit">Run scan</button>
      </form>
      {scan && (
        <div>
          <p>Status: {scan.status}</p>
          <ul>
            {scan.decisions.map((decision) => (
              <li key={decision.symbol}>
                {decision.symbol}: {decision.disposition ?? decision.status}
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  )
}
