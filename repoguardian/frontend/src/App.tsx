import React from "react";
import { BrowserRouter, Routes, Route } from "react-router-dom";
import { RepositoryList } from "./pages/RepositoryList";
import { Dashboard } from "./pages/Dashboard";

export const App: React.FC = () => (
  <BrowserRouter>
    <div style={{
      minHeight: "100vh",
      background: "#0f172a",
      fontFamily: "-apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif",
    }}>
      <Routes>
        <Route path="/" element={<RepositoryList />} />
        <Route path="/repo/:repoId" element={<Dashboard />} />
      </Routes>
    </div>
  </BrowserRouter>
);

export default App;
