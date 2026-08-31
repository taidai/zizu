import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App'
import './index.css'

// 在非 HTTPS（如 e606 现网 http://...）环境下，crypto.randomUUID 不可用，
// jdm-editor 的决策表编辑器依赖它生成行/列 ID，因此提供 polyfill。
if (typeof crypto !== 'undefined' && !('randomUUID' in crypto)) {
  (crypto as any).randomUUID = () => {
    return '10000000-1000-4000-8000-100000000000'.replace(/[018]/g, (c: string) => {
      const n = +c
      return (n ^ (crypto.getRandomValues(new Uint8Array(1))[0] & (15 >> (n / 4)))).toString(16)
    })
  }
}

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
)
