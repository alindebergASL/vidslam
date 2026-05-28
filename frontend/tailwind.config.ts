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
          400: "#5E6678",
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
