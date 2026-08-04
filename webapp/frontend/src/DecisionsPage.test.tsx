import { describe, expect, it, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import DecisionsPage from './DecisionsPage'

const listResponse = {
  total: 1,
  results: [
    {
      decision_id: 'id1',
      decision_timestamp: '2026-08-01T09:00:00+05:30',
      symbol: 'RELIANCE',
      disposition: 'PROPOSE',
      status: 'ok',
    },
  ],
}

const detailResponse = {
  decision_id: 'id1',
  symbol: 'RELIANCE',
  disposition: 'PROPOSE',
  evidence: { note: 'first' },
}

beforeEach(() => {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo) => {
      const url = typeof input === 'string' ? input : input.url
      if (url.startsWith('/api/decisions/id1')) {
        return new Response(JSON.stringify(detailResponse), { status: 200 })
      }
      if (url.startsWith('/api/decisions')) {
        return new Response(JSON.stringify(listResponse), { status: 200 })
      }
      return new Response(null, { status: 404 })
    }),
  )
})

describe('DecisionsPage', () => {
  it('lists decisions and shows evidence on row click', async () => {
    render(<DecisionsPage />)
    await waitFor(() => expect(screen.getByText('RELIANCE')).toBeInTheDocument())
    await userEvent.click(screen.getByText('RELIANCE'))
    await waitFor(() => expect(screen.getByText(/"note": "first"/)).toBeInTheDocument())
  })
})
