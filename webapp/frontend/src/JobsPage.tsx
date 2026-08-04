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

export default function JobsPage() {
  const [jobs, setJobs] = useState<Job[]>([])

  const refresh = useCallback(async () => {
    const response = await fetch('/api/jobs')
    if (response.ok) {
      setJobs(await response.json())
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
            <td>{job.enabled ? 'enabled' : 'disabled'}</td>
            <td>{job.last_record?.status ?? 'never run'}</td>
            <td>{job.last_record?.occurrence ?? job.current_occurrence ?? '-'}</td>
            <td>
              <button onClick={() => toggle(job)}>
                {job.enabled ? 'Disable' : 'Enable'}
              </button>{' '}
              <button onClick={() => runNow(job)} disabled={job.force_run_requested}>
                {job.force_run_requested ? 'Queued...' : 'Run now'}
              </button>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}
