import { useCallback, useEffect, useRef, useState } from "react";

export type PollingState<T> = {
  data: T | undefined;
  error: Error | null;
  loading: boolean;
  lastUpdated: number | null;
  refresh: () => void;
};

/**
 * Tiny data-fetching hook with built-in polling.
 *
 * - Runs `fetcher` immediately and then every `intervalMs`.
 * - Pauses automatically when the tab is hidden (saves battery on mobile).
 * - Cancels in-flight requests with AbortController when the dep list
 *   changes or the component unmounts, so we never commit state after
 *   unmount and never race a stale request against a fresh one.
 * - `refresh()` forces an out-of-band refetch (used by pull-to-refresh /
 *   the manual refresh button in the header).
 *
 * `deps` is passed straight through to useEffect so callers can refetch
 * when e.g. the selected pair changes on the Pair Detail page.
 */
export function usePolling<T>(
  fetcher: (signal: AbortSignal) => Promise<T>,
  intervalMs: number,
  deps: ReadonlyArray<unknown> = [],
): PollingState<T> {
  const [data, setData] = useState<T | undefined>(undefined);
  const [error, setError] = useState<Error | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [lastUpdated, setLastUpdated] = useState<number | null>(null);

  const mountedRef = useRef(true);
  const tickRef = useRef(0);

  const run = useCallback(
    async (signal: AbortSignal) => {
      const ticket = ++tickRef.current;
      try {
        const result = await fetcher(signal);
        if (!mountedRef.current || signal.aborted || ticket !== tickRef.current) {
          return;
        }
        setData(result);
        setError(null);
        setLastUpdated(Date.now());
      } catch (err) {
        if (signal.aborted) return;
        if (!mountedRef.current || ticket !== tickRef.current) return;
        setError(err instanceof Error ? err : new Error(String(err)));
      } finally {
        if (mountedRef.current && ticket === tickRef.current) {
          setLoading(false);
        }
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [fetcher, ...deps],
  );

  useEffect(() => {
    mountedRef.current = true;
    const controller = new AbortController();
    void run(controller.signal);

    let timer: ReturnType<typeof setInterval> | null = null;
    const start = () => {
      if (timer) return;
      timer = setInterval(() => {
        if (document.visibilityState === "hidden") return;
        const c = new AbortController();
        void run(c.signal);
      }, intervalMs);
    };
    const stop = () => {
      if (timer) {
        clearInterval(timer);
        timer = null;
      }
    };
    start();

    const onVisibility = () => {
      if (document.visibilityState === "visible") {
        // Immediately refresh when the user returns to the tab.
        const c = new AbortController();
        void run(c.signal);
        start();
      } else {
        stop();
      }
    };
    document.addEventListener("visibilitychange", onVisibility);

    return () => {
      mountedRef.current = false;
      stop();
      controller.abort();
      document.removeEventListener("visibilitychange", onVisibility);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [intervalMs, run]);

  const refresh = useCallback(() => {
    const c = new AbortController();
    void run(c.signal);
  }, [run]);

  return { data, error, loading, lastUpdated, refresh };
}
