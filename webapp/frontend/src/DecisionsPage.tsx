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

export default function DecisionsPage() {
  const [symbolFilter, setSymbolFilter] = useState('')
  const [results, setResults] = useState<DecisionSummary[]>([])
  const [selected, setSelected] = useState<DecisionDetail | null>(null)

  useEffect(() => {
    const params = new URLSearchParams()
    if (symbolFilter.trim()) params.set('symbol', symbolFilter.trim())
    fetch(`/api/decisions?${params.toString()}`)
      .then((response) => (response.ok ? response.json() : { results: [] }))
      .then((body) => setResults(body.results ?? []))
  }, [symbolFilter])

  const openDetail = async (decisionId: string) => {
    const response = await fetch(`/api/decisions/${decisionId}`)
    if (response.ok) {
      setSelected(await response.json())
    }
  }

  return (
    <section>
      <h2>Decisions</h2>
      <input
        aria-label="Filter by symbol"
        placeholder="Filter by symbol"
        value={symbolFilter}
        onChange={(event) => setSymbolFilter(event.target.value)}
      />
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
              onClick={() => openDetail(decision.decision_id)}
              style={{ cursor: 'pointer' }}
            >
              <td>{decision.symbol}</td>
              <td>{decision.disposition ?? decision.status}</td>
              <td>{decision.decision_timestamp}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {selected && (
        <pre>{JSON.stringify(selected.evidence, null, 2)}</pre>
      )}
    </section>
  )
}
