import { NavLink, Outlet, useLocation } from "react-router-dom";
import { useEffect } from "react";
import HealthBadge from "./components/HealthBadge";

export default function App() {
  const location = useLocation();

  // Scroll to top when the route changes — mobile users expect a fresh
  // page to start at the top, not wherever the previous page scrolled to.
  useEffect(() => {
    window.scrollTo(0, 0);
  }, [location.pathname]);

  return (
    <div className="min-h-screen flex flex-col">
      <header className="sticky top-0 z-40 bg-bg/90 backdrop-blur border-b border-border">
        <div className="max-w-6xl mx-auto px-3 sm:px-4 py-3 flex items-center justify-between gap-3">
          <NavLink to="/" className="flex items-center gap-2 group">
            <span className="inline-block w-8 h-8 rounded-lg bg-gradient-to-br from-accent to-accent2 grid place-items-center font-black text-bg">
              ₣
            </span>
            <div className="leading-tight">
              <div className="font-bold text-text text-sm sm:text-base">
                FOREX <span className="text-accent">v2</span>
              </div>
              <div className="text-[10px] sm:text-xs text-muted">
                28 пар · живые сигналы
              </div>
            </div>
          </NavLink>
          <HealthBadge />
        </div>
        <nav className="max-w-6xl mx-auto px-3 sm:px-4 flex gap-1 overflow-x-auto">
          <Tab to="/" label="Сигналы" />
          <Tab to="/cycle" label="Цикл 5ч" />
        </nav>
      </header>

      <main className="flex-1 max-w-6xl w-full mx-auto px-3 sm:px-4 py-4 sm:py-6">
        <Outlet />
      </main>

      <footer className="border-t border-border py-4 text-center text-xs text-muted">
        Данные: Yahoo Finance ·{" "}
        {import.meta.env.VITE_STATIC_DATA === "1"
          ? "обновление каждые 15 минут (GitHub Actions cron)"
          : "обновление каждые 10с"}
      </footer>
    </div>
  );
}

function Tab({ to, label }: { to: string; label: string }) {
  return (
    <NavLink
      to={to}
      end={to === "/"}
      className={({ isActive }) =>
        [
          "px-4 py-2.5 text-sm font-semibold whitespace-nowrap border-b-2 transition-colors",
          isActive
            ? "text-accent border-accent"
            : "text-muted border-transparent hover:text-text",
        ].join(" ")
      }
    >
      {label}
    </NavLink>
  );
}
