/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        bg: "#0a0e17",
        card: "#141b2d",
        cardAlt: "#1a2235",
        border: "#1e2a3a",
        text: "#e2e8f0",
        muted: "#8892a4",
        buy: "#00e676",
        sell: "#ff5252",
        accent: "#4fc3f7",
        accent2: "#7c4dff",
      },
      fontFamily: {
        sans: [
          "-apple-system",
          "BlinkMacSystemFont",
          "Segoe UI",
          "Roboto",
          "sans-serif",
        ],
        mono: ["ui-monospace", "SFMono-Regular", "Menlo", "monospace"],
      },
      animation: {
        pulseDot: "pulseDot 2s ease-in-out infinite",
        fadeIn: "fadeIn 0.2s ease-out",
      },
      keyframes: {
        pulseDot: {
          "0%,100%": { opacity: "1" },
          "50%": { opacity: "0.4" },
        },
        fadeIn: {
          from: { opacity: "0", transform: "translateY(4px)" },
          to: { opacity: "1", transform: "translateY(0)" },
        },
      },
    },
  },
  plugins: [],
};
