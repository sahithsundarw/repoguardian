import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";

// Global reset
const style = document.createElement("style");
style.textContent = `
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #0f172a; }
  @keyframes spin { to { transform: rotate(360deg); } }
  ::-webkit-scrollbar { width: 6px; }
  ::-webkit-scrollbar-track { background: #0f172a; }
  ::-webkit-scrollbar-thumb { background: #334155; border-radius: 3px; }
`;
document.head.appendChild(style);

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);
