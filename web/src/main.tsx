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

// Derive the React-Router basename from the Vite base path so the SPA
// works under any mount point:
//   /v2/            → FastAPI serves at /v2 (dev / Fly.io)
//   /Forex-wws2277/ → GitHub Pages project site
// React-Router wants the basename without the trailing slash.
const rawBase =
  (import.meta.env.VITE_BASE_PATH as string | undefined) ?? "/v2/";
const routerBasename = rawBase.replace(/\/$/, "") || "/";

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
  { basename: routerBasename },
);

ReactDOM.createRoot(document.getElementById("root") as HTMLElement).render(
  <React.StrictMode>
    <RouterProvider router={router} />
  </React.StrictMode>,
);
