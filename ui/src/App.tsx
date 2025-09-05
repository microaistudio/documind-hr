import { Routes, Route, Link } from "react-router-dom";
import OpsDashboard from "./pages/OpsDashboard";
import Ask from "./pages/Ask"; // NEW

export default function App() {
  return (
    <div className="min-h-screen bg-gray-50">
      <div className="p-3 border-b bg-white">
        <div className="max-w-7xl mx-auto flex items-center justify-between">
          <div className="font-semibold">DocuMind-HR</div>
          <nav className="text-sm space-x-2">
            <Link to="/ops" className="px-3 py-1 rounded hover:bg-gray-100">Ops</Link>
            <Link to="/ask" className="px-3 py-1 rounded hover:bg-gray-100">Ask</Link> {/* NEW */}
          </nav>
        </div>
      </div>
      <Routes>
        <Route path="/ops" element={<OpsDashboard />} />
        <Route path="/ask" element={<Ask />} /> {/* NEW */}
        <Route
          path="*"
          element={
            <div className="max-w-7xl mx-auto p-6 space-y-2">
              <div>
                Go to <Link className="text-indigo-600" to="/ops">/ops</Link>
              </div>
              <div>
                Or try <Link className="text-indigo-600" to="/ask">/ask</Link>
              </div>
            </div>
          }
        />
      </Routes>
    </div>
  );
}
