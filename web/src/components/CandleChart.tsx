import { useEffect, useRef } from "react";
import {
  ColorType,
  createChart,
  type IChartApi,
  type ISeriesApi,
  type UTCTimestamp,
} from "lightweight-charts";
import type { Bar } from "../lib/types";

/**
 * Candlestick chart using the official TradingView lightweight-charts
 * library.  The chart is mounted into a div we fully control; on
 * unmount we tear it down and null out the series refs so React strict
 * mode's double-invoke doesn't leak canvases.
 *
 * Mobile notes:
 *   - kineticScroll is on by default, which is what we want.
 *   - handleScale.mouseWheel is off so page scroll on touch devices
 *     doesn't get hijacked by the chart.
 *   - The chart auto-resizes via ResizeObserver so it always fills the
 *     container width, both on orientation change and on the transition
 *     from 1-col to 2-col layout on sm breakpoint.
 */
export default function CandleChart({
  bars,
  height = 320,
  priceFormatDigits = 5,
}: {
  bars: Bar[];
  height?: number;
  priceFormatDigits?: number;
}) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);

  // Create chart once.
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;

    const chart = createChart(el, {
      width: el.clientWidth,
      height,
      layout: {
        background: { type: ColorType.Solid, color: "#141b2d" },
        textColor: "#8892a4",
        fontSize: 11,
      },
      grid: {
        vertLines: { color: "#1e2a3a" },
        horzLines: { color: "#1e2a3a" },
      },
      rightPriceScale: {
        borderColor: "#1e2a3a",
      },
      timeScale: {
        borderColor: "#1e2a3a",
        timeVisible: true,
        secondsVisible: false,
      },
      crosshair: {
        mode: 1,
      },
      handleScroll: {
        vertTouchDrag: false,
      },
      handleScale: {
        mouseWheel: false,
      },
    });
    const series = chart.addCandlestickSeries({
      upColor: "#00e676",
      downColor: "#ff5252",
      borderUpColor: "#00e676",
      borderDownColor: "#ff5252",
      wickUpColor: "#00e676",
      wickDownColor: "#ff5252",
      priceFormat: {
        type: "price",
        precision: priceFormatDigits,
        minMove: Number(`1e-${priceFormatDigits}`),
      },
    });

    chartRef.current = chart;
    seriesRef.current = series;

    const ro = new ResizeObserver((entries) => {
      const rect = entries[0]?.contentRect;
      if (rect) chart.applyOptions({ width: rect.width });
    });
    ro.observe(el);

    return () => {
      ro.disconnect();
      chart.remove();
      chartRef.current = null;
      seriesRef.current = null;
    };
  }, [height, priceFormatDigits]);

  // Push data updates whenever the bar array changes.  setData is
  // idempotent and the lib handles zoom preservation for us.
  useEffect(() => {
    const series = seriesRef.current;
    const chart = chartRef.current;
    if (!series || !chart || bars.length === 0) return;

    series.setData(
      bars.map((b) => ({
        time: b.time as UTCTimestamp,
        open: b.open,
        high: b.high,
        low: b.low,
        close: b.close,
      })),
    );
    chart.timeScale().fitContent();
  }, [bars]);

  return (
    <div
      ref={containerRef}
      style={{ height }}
      className="w-full rounded-lg overflow-hidden border border-border"
    />
  );
}
