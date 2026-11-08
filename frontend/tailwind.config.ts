import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // Burgundy brand palette (600 = classic burgundy #800020)
        brand: {
          50: "#fdf6f8", 100: "#fbecf0", 200: "#f6d8e0", 300: "#eebdc9", 400: "#de97a9", 500: "#c47186",
          600: "#a8506a", 700: "#8f4259", 800: "#76374a", 900: "#5f2d3c", 950: "#371820",
        },
        // Deep plum-wine partner used in gradients
        plum: {
          50: "#fcf6f9", 100: "#f7eaf1", 200: "#efd5e2", 300: "#e2b5ca", 400: "#cc8daa", 500: "#b36d8d",
          600: "#975673", 700: "#7f475f", 800: "#683b4f", 900: "#553141", 950: "#331b26",
        },
      },
      fontFamily: { sans: ["var(--font-inter)", "Inter", "ui-sans-serif", "system-ui", "sans-serif"], mono: ["ui-monospace", "SFMono-Regular", "monospace"] },
    },
  },
  plugins: [],
};
export default config;
