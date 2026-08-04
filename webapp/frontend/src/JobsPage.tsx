import { useCallback, useEffect, useState } from 'react'

type JobRecord = {
  status: string
  occurrence: string
  finished_at: string | null
  return_code: number | null
} | null

type Job = {
  name: string
  base_enabled: boolean
  override: boolean | null
  enabled: boolean
  due_now: boolean
  current_occurrence: string | null
  force_run_requested: boolean
  last_record: JobRecord
}

const POLL_INTERVAL_MS = 5000

function statusBadgeClass(status: string | undefined): string {
  switch (status) {
    case 'success':
      return 'badge badge-success'
    case 'failed':
      return 'badge badge-danger'
    case 'running':
      return 'badge badge-warning'
    default:
      return 'badge badge-neutral'
  }
}

export default function JobsPage() {
  const [jobs, setJobs] = useState<Job[]>([])
  const [error, setError] = useState<string | null>(null)

  const refresh = useCallback(async () => {
    try {
      const response = await fetch('/api/jobs')
      if (!response.ok) throw new Error(`HTTP ${response.status}`)
      setJobs(await response.json())
      setError(null)
    } catch {
      setError('Could not load jobs. Is the control API running?')
    }
  }, [])

  useEffect(() => {
    refresh()
    const interval = setInterval(refresh, POLL_INTERVAL_MS)
    return () => clearInterval(interval)
  }, [refresh])

  const toggle = async (job: Job) => {
    await fetch('/api/jobs/toggle', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ job: job.name, enabled: !job.enabled }),
    })
    refresh()
  }

  const runNow = async (job: Job) => {
    await fetch('/api/jobs/run-now', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ job: job.name }),
    })
    refresh()
  }

  return (
    <section>
      <h2>Scheduled jobs</h2>
      {error && <p className="error-text">{error}</p>}
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <th>Job</th>
              <th>Enabled</th>
              <th>Last status</th>
              <th>Occurrence</th>
              <th>Controls</th>
            </tr>
          </thead>
          <tbody>
            {jobs.map((job) => (
              <tr key={job.name}>
                <td>{job.name}</td>
                <td>
                  <span className={job.enabled ? 'badge badge-success' : 'badge badge-neutral'}>
                    {job.enabled ? 'enabled' : 'disabled'}
                  </span>
                </td>
                <td>
                  <span className={statusBadgeClass(job.last_record?.status)}>
                    {job.last_record?.status ?? 'never run'}
                  </span>
                </td>
                <td>{job.last_record?.occurrence ?? job.current_occurrence ?? '-'}</td>
                <td>
                  <div className="row-buttons">
                    <button onClick={() => toggle(job)}>
                      {job.enabled ? 'Disable' : 'Enable'}
                    </button>
                    <button onClick={() => runNow(job)} disabled={job.force_run_requested}>
                      {job.force_run_requested ? 'Queued…' : 'Run now'}
                    </button>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  )
}
