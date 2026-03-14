import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";

// Global reset + Deep Space theme
const style = document.createElement("style");
style.textContent = `
  :root {
    --bg: #0B0E14;
    --surface: #111827;
    --border: #1E293B;
    --accent: #10B981;
    --accent-dim: #059669;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); }
  @keyframes spin { to { transform: rotate(360deg); } }
  @keyframes pulse-dot {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.3; }
  }
  @keyframes shimmer {
    0% { background-position: -600px 0; }
    100% { background-position: 600px 0; }
  }
  .skeleton {
    background: linear-gradient(90deg, #1E293B 25%, #1a2235 50%, #1E293B 75%);
    background-size: 600px 100%;
    animation: shimmer 1.6s infinite linear;
    border-radius: 6px;
  }
  ::-webkit-scrollbar { width: 6px; }
  ::-webkit-scrollbar-track { background: #0B0E14; }
  ::-webkit-scrollbar-thumb { background: #1E293B; border-radius: 3px; }
`;
document.head.appendChild(style);

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);
