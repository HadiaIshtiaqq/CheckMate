import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // Burgundy brand palette (600 = classic burgundy #800020)
        brand: {
          50: "#fcf2f4", 100: "#f9e2e7", 200: "#f1c3cd", 300: "#e494a6", 400: "#c95a74", 500: "#a8304f",
          600: "#800020", 700: "#6c001b", 800: "#580017", 900: "#440012", 950: "#27000a",
        },
        // Deep plum-wine partner used in gradients
        plum: {
          50: "#faf3f6", 100: "#f3e3ea", 200: "#e6c5d3", 300: "#d29ab2", 400: "#b96d8e", 500: "#9b4a6e",
          600: "#7e3155", 700: "#672646", 800: "#521e38", 900: "#3f172b", 950: "#240b18",
        },
      },
      fontFamily: { sans: ["var(--font-inter)", "Inter", "ui-sans-serif", "system-ui", "sans-serif"], mono: ["ui-monospace", "SFMono-Regular", "monospace"] },
    },
  },
  plugins: [],
};
export default config;
