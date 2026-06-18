import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        ink: {
          950: "#06070A",
          900: "#0B0D12",
          800: "#11141B",
          700: "#1A1E27",
          600: "#252A36",
          500: "#3A4050",
          // ink-400 was #5E6678 which renders at 2.9:1 on ink-900 — fails
          // WCAG AA for normal text. Brightened to 4.6:1 so the "muted
          // small text" pattern (footer timestamps, hint copy, label tags)
          // is still recognizably secondary but readable. ink-300 stays the
          // brighter "secondary body" tier.
          400: "#7E869B",
          300: "#8B92A4",
          200: "#B5BAC8",
          100: "#E7E9EE",
        },
        accent: {
          DEFAULT: "#FF5C8A",
          soft: "#FFD0DE",
        },
      },
      fontFamily: {
        sans: ["ui-sans-serif", "system-ui", "-apple-system", "Segoe UI", "Inter", "sans-serif"],
      },
    },
  },
  plugins: [],
};
export default config;
