import { describe, expect, it, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import CoveragePage from './CoveragePage'

const shareholdingResponse = {
  universe: 'NIFTY TOTAL MKT',
  total: 1,
  results: [
    {
      symbol: 'RELIANCE',
      active: true,
      last_status: 'complete',
      last_attempt: '2026-08-01T00:00:00+00:00',
      completed_at: '2026-08-01T00:00:00+00:00',
      periods: 5,
      queued: false,
    },
  ],
}

const disclosuresResponse = {
  system: 'disclosures',
  ttl_hours: 336,
  total: 1,
  results: [
    { symbol: 'TCS', fetched_at: '2026-07-01T00:00:00+00:00', age_hours: 800, fresh: false },
  ],
}

beforeEach(() => {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo) => {
      const url = typeof input === 'string' ? input : input.url
      if (url.startsWith('/api/coverage/shareholding')) {
        return new Response(JSON.stringify(shareholdingResponse), { status: 200 })
      }
      if (url.startsWith('/api/coverage/cache/disclosures')) {
        return new Response(JSON.stringify(disclosuresResponse), { status: 200 })
      }
      return new Response(null, { status: 404 })
    }),
  )
})

describe('CoveragePage', () => {
  it('shows shareholding coverage by default', async () => {
    render(<CoveragePage />)
    await waitFor(() => expect(screen.getByText('RELIANCE')).toBeInTheDocument())
    expect(screen.getByText('complete')).toBeInTheDocument()
  })

  it('switches to a cache-based system and shows its freshness view', async () => {
    render(<CoveragePage />)
    await waitFor(() => expect(screen.getByText('RELIANCE')).toBeInTheDocument())
    await userEvent.click(screen.getByRole('button', { name: /disclosures/i }))
    await waitFor(() => expect(screen.getByText('TCS')).toBeInTheDocument())
  })
})
