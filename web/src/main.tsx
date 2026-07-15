import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App'
// 全局样式：深色主题基础 + Tailwind
import './index.css'
// @xyflow/react 画布组件所需的基础样式
import '@xyflow/react/dist/style.css'

// 挂载 React 应用到 index.html 中的 #root 节点
ReactDOM.createRoot(document.getElementById('root') as HTMLElement).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
)
