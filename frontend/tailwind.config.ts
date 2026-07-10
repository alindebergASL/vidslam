import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        ink: {
          950: "#05060B",
          900: "#0A0C14",
          800: "#12141F",
          700: "#1B1E2D",
          600: "#272B3F",
          500: "#3B4058",
          // ink-400 kept at ≥4.5:1 on ink-900 for WCAG AA small text.
          400: "#7E869B",
          300: "#8B92A4",
          200: "#B5BAC8",
          100: "#E9EAF2",
        },
        // Signature pair: iris (violet) → accent (pink). The gradient
        // between them is the product's visual identity — used on the
        // wordmark, primary actions, active states, and progress.
        iris: {
          DEFAULT: "#7C5CFF",
          soft: "#C9B8FF",
        },
        accent: {
          DEFAULT: "#FF5C8A",
          soft: "#FFD0DE",
        },
      },
      fontFamily: {
        sans: ["ui-sans-serif", "system-ui", "-apple-system", "Segoe UI", "Inter", "sans-serif"],
      },
      boxShadow: {
        // Soft ambient glow for hover/active surfaces — reads as depth on
        // the near-black base without a heavy drop shadow.
        glow: "0 0 0 1px rgba(124,92,255,0.35), 0 8px 40px -12px rgba(124,92,255,0.45)",
        "glow-pink": "0 0 0 1px rgba(255,92,138,0.35), 0 8px 40px -12px rgba(255,92,138,0.4)",
        lift: "0 12px 32px -16px rgba(0,0,0,0.8)",
      },
      keyframes: {
        "fade-up": {
          "0%": { opacity: "0", transform: "translateY(6px)" },
          "100%": { opacity: "1", transform: "translateY(0)" },
        },
        shimmer: {
          "0%": { backgroundPosition: "-200% 0" },
          "100%": { backgroundPosition: "200% 0" },
        },
      },
      animation: {
        "fade-up": "fade-up 0.35s cubic-bezier(0.16, 1, 0.3, 1) both",
        shimmer: "shimmer 2.2s linear infinite",
      },
    },
  },
  plugins: [],
};
export default config;
