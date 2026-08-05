import { useEffect, useState } from 'react'

type ReasonCode = { stage: string | null; code: string | null; count: number }
type ModelConfig = { backend: string | null; name: string | null; max_tokens: number | null; count: number }
type PolicyVersion = { version: string | null; count: number }

type Report = {
  decisions: {
    total: number
    status_counts: Record<string, number>
    evaluable: number
    raw_evaluable: number
    repeated_evaluable: number
    reason_codes: ReasonCode[]
    model_configs: ModelConfig[]
    policy_versions: PolicyVersion[]
  }
  horizons: unknown
  technical_score_bands: unknown
  model_performance: unknown
  decision_graph_performance: unknown
  methodology_performance: unknown
  methodology: unknown
}

export default function CalibrationPage() {
  const [report, setReport] = useState<Report | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    fetch('/api/calibration-report')
      .then((response) => {
        if (!response.ok) throw new Error(`HTTP ${response.status}`)
        return response.json()
      })
      .then((body) => setReport(body))
      .catch(() => setError('Could not load the calibration report. Is the control API running?'))
      .finally(() => setLoading(false))
  }, [])

  if (loading) {
    return (
      <section>
        <h2>Calibration report</h2>
        <div className="status-line">
          <span className="spinner" />
          <span>Loading…</span>
        </div>
      </section>
    )
  }

  if (error || !report || !report.decisions) {
    return (
      <section>
        <h2>Calibration report</h2>
        <p className="error-text">{error ?? 'No report available.'}</p>
      </section>
    )
  }

  const { decisions } = report
  const analytical = {
    horizons: report.horizons,
    technical_score_bands: report.technical_score_bands,
    model_performance: report.model_performance,
    decision_graph_performance: report.decision_graph_performance,
    methodology_performance: report.methodology_performance,
    methodology: report.methodology,
  }

  return (
    <section>
      <h2>Calibration report</h2>

      <p className="hint-text">
        <strong>{decisions.total}</strong> total decisions,{' '}
        <strong>{decisions.evaluable}</strong> evaluable (canonical signal with
        a validated risk plan and a completed outcome).
      </p>

      <div className="row-buttons">
        {Object.entries(decisions.status_counts).map(([status, count]) => (
          <span key={status} className="badge badge-neutral">
            {status}: {count}
          </span>
        ))}
      </div>

      {decisions.reason_codes.length > 0 && (
        <div className="table-scroll">
          <h2>Reason codes</h2>
          <table>
            <thead>
              <tr>
                <th>Stage</th>
                <th>Code</th>
                <th>Count</th>
              </tr>
            </thead>
            <tbody>
              {decisions.reason_codes.map((row) => (
                <tr key={`${row.stage}-${row.code}`}>
                  <td>{row.stage}</td>
                  <td>{row.code}</td>
                  <td>{row.count}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {decisions.model_configs.length > 0 && (
        <div className="table-scroll">
          <h2>Model configs</h2>
          <table>
            <thead>
              <tr>
                <th>Backend</th>
                <th>Model</th>
                <th>Max tokens</th>
                <th>Count</th>
              </tr>
            </thead>
            <tbody>
              {decisions.model_configs.map((row, index) => (
                <tr key={index}>
                  <td>{row.backend}</td>
                  <td>{row.name}</td>
                  <td>{row.max_tokens ?? '-'}</td>
                  <td>{row.count}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {decisions.policy_versions.length > 0 && (
        <div className="table-scroll">
          <h2>Policy versions</h2>
          <table>
            <thead>
              <tr>
                <th>Version</th>
                <th>Count</th>
              </tr>
            </thead>
            <tbody>
              {decisions.policy_versions.map((row) => (
                <tr key={row.version}>
                  <td>{row.version}</td>
                  <td>{row.count}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div className="evidence-detail">
        <p className="hint-text">
          Outcome performance, score bands, and methodology (nested/variable shape --
          shown as-is):
        </p>
        <pre>{JSON.stringify(analytical, null, 2)}</pre>
      </div>
    </section>
  )
}
