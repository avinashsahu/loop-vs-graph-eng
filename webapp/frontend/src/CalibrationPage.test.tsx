import { describe, expect, it, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import CalibrationPage from './CalibrationPage'

const reportResponse = {
  decisions: {
    total: 2226,
    status_counts: { proposed: 185, aborted: 1879, flagged_for_review: 161, failed: 1 },
    evaluable: 97,
    raw_evaluable: 100,
    repeated_evaluable: 3,
    canonical: [],
    reason_codes: [{ stage: 'technical', code: 'TECHNICAL_CONFLUENCE_FAILED', count: 900 }],
    model_configs: [{ backend: 'openai_compatible_local', name: 'phi4:14b-q4_K_M', max_tokens: 2048, count: 300 }],
    policy_versions: [{ version: 'technical-relative-participation-v2', count: 2226 }],
  },
  horizons: { '1': { count: 97 } },
  technical_score_bands: {},
  model_performance: [],
  decision_graph_performance: [],
  methodology_performance: [],
  methodology: { scope: 'selected_candidate_evaluation' },
}

beforeEach(() => {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => new Response(JSON.stringify(reportResponse), { status: 200 })),
  )
})

describe('CalibrationPage', () => {
  it('shows decision totals, status counts, and reason codes', async () => {
    render(<CalibrationPage />)
    await waitFor(() =>
      expect(screen.getByText('TECHNICAL_CONFLUENCE_FAILED')).toBeInTheDocument(),
    )
    expect(
      screen.getByText(
        (_, element) =>
          element?.tagName === 'P' &&
          element.textContent ===
            '2226 total decisions, 97 evaluable (canonical signal with a validated risk plan and a completed outcome).',
      ),
    ).toBeInTheDocument()
    expect(screen.getByText(/"scope": "selected_candidate_evaluation"/)).toBeInTheDocument()
  })
})
