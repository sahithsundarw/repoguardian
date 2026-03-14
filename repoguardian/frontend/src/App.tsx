import React from "react";
import { BrowserRouter, Routes, Route } from "react-router-dom";
import { ControlCenter } from "./pages/ControlCenter";
import { RepositoryList } from "./pages/RepositoryList";
import { Dashboard } from "./pages/Dashboard";
import { T } from "./theme";

export const App: React.FC = () => (
  <BrowserRouter>
    <div style={{
      minHeight: "100vh",
      background: T.bg,
      fontFamily: "-apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif",
    }}>
      <Routes>
        <Route path="/" element={<ControlCenter />} />
        <Route path="/repos" element={<RepositoryList />} />
        <Route path="/repo/:repoId" element={<Dashboard />} />
      </Routes>
    </div>
  </BrowserRouter>
);

export default App;
