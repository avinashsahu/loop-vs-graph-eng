import CoveragePage from './CoveragePage'
import DecisionsPage from './DecisionsPage'
import JobsPage from './JobsPage'
import ScanPage from './ScanPage'

function App() {
  return (
    <main>
      <header className="app-header">
        <h1>NSE Stock Picker — Control</h1>
      </header>
      <div className="panel">
        <ScanPage />
      </div>
      <div className="panel">
        <DecisionsPage />
      </div>
      <div className="panel">
        <CoveragePage />
      </div>
      <div className="panel">
        <JobsPage />
      </div>
    </main>
  )
}

export default App
