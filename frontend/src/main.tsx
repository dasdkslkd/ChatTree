import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import { initializeFrontendBootstrap } from './runtime/frontendBootstrap'

async function startFrontend(): Promise<void> {
  const root = createRoot(document.getElementById('root')!)
  try {
    const bootstrap = initializeFrontendBootstrap()
    const { default: App } = await import('./App')
    root.render(<StrictMode><App bootstrap={bootstrap} /></StrictMode>)
  } catch (error) {
    const message = error instanceof Error ? error.message : 'Frontend binding failed'
    root.render(<main role="alert" className="startup-error">{message}</main>)
  }
}

void startFrontend()
