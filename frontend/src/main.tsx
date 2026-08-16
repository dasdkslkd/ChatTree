import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import { initializeProfileContext } from './runtime/profileContext'

async function startFrontend(): Promise<void> {
  const rootElement = document.getElementById('root')
  if (!rootElement) throw new Error('启动失败：未找到 #root 挂载点')
  const root = createRoot(rootElement)
  try {
    const profile = initializeProfileContext()
    const { default: App } = await import('./App')
    root.render(<StrictMode><App profile={profile} /></StrictMode>)
  } catch (error) {
    const message = error instanceof Error ? error.message : 'Frontend binding failed'
    root.render(<main role="alert" className="startup-error">{message}</main>)
  }
}

void startFrontend()
