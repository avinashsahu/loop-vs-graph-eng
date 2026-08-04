import { describe, expect, it, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import ScanPage from './ScanPage'

beforeEach(() => {
  let getCallCount = 0
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input.url
      if (url === '/api/scans' && init?.method === 'POST') {
        return new Response(JSON.stringify({ request_id: 'abc' }), { status: 201 })
      }
      if (url === '/api/scans/abc') {
        getCallCount += 1
        if (getCallCount === 1) {
          return new Response(
            JSON.stringify({ status: 'queued', scan_label: 'adhoc-abc', finished_at: null, decisions: [] }),
            { status: 200 },
          )
        }
        return new Response(
          JSON.stringify({
            status: 'done',
            scan_label: 'adhoc-abc',
            finished_at: '2026-08-04T10:00:05+05:30',
            decisions: [{ symbol: 'RELIANCE', disposition: 'PROPOSE', status: 'ok' }],
          }),
          { status: 200 },
        )
      }
      return new Response(null, { status: 404 })
    }),
  )
})

describe('ScanPage', () => {
  it('submits symbols and polls until the decision is shown', async () => {
    render(<ScanPage pollIntervalMs={10} />)
    await userEvent.type(screen.getByLabelText(/symbols/i), 'RELIANCE')
    await userEvent.click(screen.getByRole('button', { name: /run scan/i }))
    await waitFor(() => expect(screen.getByText(/PROPOSE/)).toBeInTheDocument())
  })

  it('shows an error instead of failing silently when the server rejects the request', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response(JSON.stringify({ detail: 'Method Not Allowed' }), { status: 405 })),
    )
    render(<ScanPage pollIntervalMs={10} />)
    await userEvent.type(screen.getByLabelText(/symbols/i), 'LODHA')
    await userEvent.click(screen.getByRole('button', { name: /run scan/i }))
    await waitFor(() => expect(screen.getByText(/Method Not Allowed/)).toBeInTheDocument())
  })

  it('shows an error when submitting with no symbols', async () => {
    render(<ScanPage pollIntervalMs={10} />)
    await userEvent.click(screen.getByRole('button', { name: /run scan/i }))
    await waitFor(() => expect(screen.getByText(/enter at least one symbol/i)).toBeInTheDocument())
  })
})
