import { useEffect, useState } from 'react'

type DecisionSummary = {
  decision_id: string
  decision_timestamp: string
  symbol: string
  disposition: string | null
  status: string
}

type DecisionDetail = DecisionSummary & {
  evidence: unknown
}

const DEBOUNCE_MS = 350

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

export default function DecisionsPage() {
  const [symbolInput, setSymbolInput] = useState('')
  const [debouncedSymbol, setDebouncedSymbol] = useState('')
  const [results, setResults] = useState<DecisionSummary[]>([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [selected, setSelected] = useState<DecisionDetail | null>(null)
  const [detailError, setDetailError] = useState<string | null>(null)

  useEffect(() => {
    const timer = setTimeout(() => setDebouncedSymbol(symbolInput.trim()), DEBOUNCE_MS)
    return () => clearTimeout(timer)
  }, [symbolInput])

  useEffect(() => {
    setLoading(true)
    setError(null)
    const params = new URLSearchParams()
    if (debouncedSymbol) params.set('symbol', debouncedSymbol)
    fetch(`/api/decisions?${params.toString()}`)
      .then((response) => {
        if (!response.ok) throw new Error(`HTTP ${response.status}`)
        return response.json()
      })
      .then((body) => {
        setResults(body.results ?? [])
        setTotal(body.total ?? 0)
      })
      .catch(() => setError('Could not load decisions. Is the control API running?'))
      .finally(() => setLoading(false))
  }, [debouncedSymbol])

  const openDetail = async (decisionId: string) => {
    setDetailError(null)
    try {
      const response = await fetch(`/api/decisions/${decisionId}`)
      if (!response.ok) throw new Error(`HTTP ${response.status}`)
      setSelected(await response.json())
    } catch {
      setDetailError('Could not load that decision.')
    }
  }

  return (
    <section>
      <h2>Decisions</h2>
      <form className="inline-form" onSubmit={(event) => event.preventDefault()}>
        <label htmlFor="symbol-filter">Symbol</label>
        <input
          id="symbol-filter"
          type="text"
          aria-label="Filter by symbol"
          placeholder="Filter by symbol, e.g. RELIANCE"
          value={symbolInput}
          onChange={(event) => setSymbolInput(event.target.value)}
        />
      </form>

      {error && <p className="error-text">{error}</p>}

      {!error && loading && (
        <div className="status-line">
          <span className="spinner" />
          <span>Loading…</span>
        </div>
      )}

      {!error && !loading && results.length === 0 && (
        <p className="empty-text">
          {debouncedSymbol
            ? `No decisions found for "${debouncedSymbol}".`
            : 'No decisions recorded yet.'}
        </p>
      )}

      {!error && !loading && results.length > 0 && (
        <div className="table-scroll">
          <p className="hint-text">
            Showing {results.length} of {total} decision{total === 1 ? '' : 's'}
            {debouncedSymbol ? ` for ${debouncedSymbol}` : ''}. Click a row for detail.
          </p>
          <table>
            <thead>
              <tr>
                <th>Symbol</th>
                <th>Disposition</th>
                <th>Timestamp</th>
              </tr>
            </thead>
            <tbody>
              {results.map((decision) => (
                <tr
                  key={decision.decision_id}
                  className="clickable"
                  onClick={() => openDetail(decision.decision_id)}
                >
                  <td>{decision.symbol}</td>
                  <td>
                    <span className={dispositionBadgeClass(decision.disposition)}>
                      {decision.disposition ?? decision.status}
                    </span>
                  </td>
                  <td>{decision.decision_timestamp}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {detailError && <p className="error-text">{detailError}</p>}

      {selected && (
        <div className="evidence-detail">
          <p className="hint-text">
            {selected.symbol} — {selected.disposition ?? selected.status}
          </p>
          <pre>{JSON.stringify(selected.evidence, null, 2)}</pre>
        </div>
      )}
    </section>
  )
}
