/** @type {import('tailwindcss').Config} */
export default {
  darkMode: "class",
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        background: "var(--color-background)",
        surface: "var(--color-surface)",
        "surface-raised": "var(--color-surface-raised)",
        line: "var(--color-border)",
        foreground: "var(--color-text-primary)",
        muted: "var(--color-text-muted)",
        accent: "var(--color-accent)",
        success: "var(--color-success)",
        warning: "var(--color-warning)",
        danger: "var(--color-danger)",
        running: "var(--color-running)",
      },
      fontSize: {
        display: ["var(--font-size-display)", "var(--line-height-display)"],
        title: ["var(--font-size-title)", "var(--line-height-title)"],
        body: ["var(--font-size-body)", "var(--line-height-body)"],
        small: ["var(--font-size-small)", "var(--line-height-small)"],
        mono: ["var(--font-size-mono)", "var(--line-height-mono)"],
      },
      spacing: {
        1: "var(--spacing-1)",
        2: "var(--spacing-2)",
        3: "var(--spacing-3)",
        4: "var(--spacing-4)",
        5: "var(--spacing-5)",
        6: "var(--spacing-6)",
      },
      borderRadius: {
        sm: "var(--radius-sm)",
        md: "var(--radius-md)",
        lg: "var(--radius-lg)",
      },
      boxShadow: {
        sm: "var(--shadow-sm)",
        md: "var(--shadow-md)",
        lg: "var(--shadow-lg)",
      },
      transitionTimingFunction: {
        out: "var(--ease-out)",
        inout: "var(--ease-in-out)",
      },
      transitionDuration: {
        fast: "var(--duration-fast)",
        normal: "var(--duration-normal)",
      },
    },
  },
  plugins: [],
};
