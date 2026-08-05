import { useEffect, useState } from 'react'

type ShareholdingRow = {
  symbol: string
  active: boolean
  last_status: string | null
  last_attempt: string | null
  completed_at: string | null
  periods: number | null
  queued: boolean
}

type CacheRow = {
  symbol: string
  fetched_at: string | null
  age_hours: number | null
  fresh: boolean
}

type System = 'shareholding' | 'disclosures' | 'governance' | 'document_research'

const SYSTEM_LABELS: Record<System, string> = {
  shareholding: 'Shareholding',
  disclosures: 'Disclosures',
  governance: 'Governance',
  document_research: 'Document Research',
}

function endpointFor(system: System): string {
  return system === 'shareholding'
    ? '/api/coverage/shareholding'
    : `/api/coverage/cache/${system}`
}

export default function CoveragePage() {
  const [system, setSystem] = useState<System>('shareholding')
  const [rows, setRows] = useState<(ShareholdingRow | CacheRow)[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    setLoading(true)
    setError(null)
    fetch(endpointFor(system))
      .then((response) => {
        if (!response.ok) throw new Error(`HTTP ${response.status}`)
        return response.json()
      })
      .then((body) => setRows(body.results ?? []))
      .catch(() => setError('Could not load coverage. Is the control API running?'))
      .finally(() => setLoading(false))
  }, [system])

  return (
    <section>
      <h2>Data-warm coverage</h2>
      <div className="row-buttons">
        {(Object.keys(SYSTEM_LABELS) as System[]).map((key) => (
          <button
            key={key}
            className={key === system ? 'primary' : undefined}
            onClick={() => setSystem(key)}
          >
            {SYSTEM_LABELS[key]}
          </button>
        ))}
      </div>

      {error && <p className="error-text">{error}</p>}
      {!error && loading && (
        <div className="status-line">
          <span className="spinner" />
          <span>Loading…</span>
        </div>
      )}
      {!error && !loading && rows.length === 0 && (
        <p className="empty-text">No coverage data for {SYSTEM_LABELS[system]} yet.</p>
      )}

      {!error && !loading && rows.length > 0 && (
        <div className="table-scroll">
          {system === 'shareholding' ? (
            <table>
              <thead>
                <tr>
                  <th>Symbol</th>
                  <th>Status</th>
                  <th>Completed</th>
                  <th>Periods</th>
                  <th>Queued</th>
                </tr>
              </thead>
              <tbody>
                {(rows as ShareholdingRow[]).map((row) => (
                  <tr key={row.symbol}>
                    <td>{row.symbol}</td>
                    <td>
                      <span
                        className={
                          row.last_status === 'complete'
                            ? 'badge badge-success'
                            : row.last_status === 'incomplete'
                              ? 'badge badge-warning'
                              : 'badge badge-neutral'
                        }
                      >
                        {row.last_status ?? 'pending'}
                      </span>
                    </td>
                    <td>{row.completed_at ?? '-'}</td>
                    <td>{row.periods ?? '-'}</td>
                    <td>{row.queued ? 'yes' : ''}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <table>
              <thead>
                <tr>
                  <th>Symbol</th>
                  <th>Freshness</th>
                  <th>Age (hours)</th>
                  <th>Last fetched</th>
                </tr>
              </thead>
              <tbody>
                {(rows as CacheRow[]).map((row) => (
                  <tr key={row.symbol}>
                    <td>{row.symbol}</td>
                    <td>
                      <span className={row.fresh ? 'badge badge-success' : 'badge badge-warning'}>
                        {row.fresh ? 'fresh' : 'stale'}
                      </span>
                    </td>
                    <td>{row.age_hours ?? '-'}</td>
                    <td>{row.fetched_at ?? '-'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}
    </section>
  )
}
