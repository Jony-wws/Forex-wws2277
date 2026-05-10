import React from "react";
import ReactDOM from "react-dom/client";
import {
  createBrowserRouter,
  Navigate,
  RouterProvider,
} from "react-router-dom";

import App from "./App";
import DashboardPage from "./pages/DashboardPage";
import CyclePage from "./pages/CyclePage";
import PairDetailPage from "./pages/PairDetailPage";
import "./index.css";

// React-router is mounted under /v2/ because that's where FastAPI serves
// the built SPA (see vite.config.ts → base: "/v2/"). The basename here
// must match the `base` option exactly, otherwise nested routes like
// /v2/pair/EURUSD wouldn't resolve.
const router = createBrowserRouter(
  [
    {
      path: "/",
      element: <App />,
      children: [
        { index: true, element: <DashboardPage /> },
        { path: "cycle", element: <CyclePage /> },
        { path: "pair/:pair", element: <PairDetailPage /> },
        { path: "*", element: <Navigate to="/" replace /> },
      ],
    },
  ],
  { basename: "/v2" },
);

ReactDOM.createRoot(document.getElementById("root") as HTMLElement).render(
  <React.StrictMode>
    <RouterProvider router={router} />
  </React.StrictMode>,
);
