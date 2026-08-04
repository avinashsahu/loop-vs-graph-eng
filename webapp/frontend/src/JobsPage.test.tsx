import { describe, expect, it, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import JobsPage from './JobsPage'

const sampleJobs = [
  {
    name: 'cleanup',
    base_enabled: true,
    override: null,
    enabled: true,
    due_now: false,
    current_occurrence: null,
    force_run_requested: false,
    last_record: { status: 'success', occurrence: '2026-08-03', finished_at: '2026-08-03T02:00:11+05:30', return_code: 0 },
  },
]

beforeEach(() => {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo) => {
      const url = typeof input === 'string' ? input : input.url
      if (url === '/api/jobs') {
        return new Response(JSON.stringify(sampleJobs), { status: 200 })
      }
      return new Response(null, { status: 204 })
    }),
  )
})

describe('JobsPage', () => {
  it('renders a row per job with its last status', async () => {
    render(<JobsPage />)
    await waitFor(() => expect(screen.getByText('cleanup')).toBeInTheDocument())
    expect(screen.getByText('success')).toBeInTheDocument()
  })

  it('posts a toggle request when Disable is clicked', async () => {
    render(<JobsPage />)
    await waitFor(() => expect(screen.getByText('cleanup')).toBeInTheDocument())
    await userEvent.click(screen.getByRole('button', { name: /disable/i }))
    await waitFor(() =>
      expect(fetch).toHaveBeenCalledWith(
        '/api/jobs/toggle',
        expect.objectContaining({ method: 'POST' }),
      ),
    )
  })
})
