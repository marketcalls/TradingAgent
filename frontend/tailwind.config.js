/** @type {import('tailwindcss').Config} */
export default {
  darkMode: "class",
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        background: "var(--background)",
        foreground: "var(--foreground)",
        muted: "var(--muted)",
        "muted-foreground": "var(--muted-foreground)",
        border: "var(--border)",
        input: "var(--input)",
        primary: "var(--primary)",
        "primary-foreground": "var(--primary-foreground)",
        sidebar: "var(--sidebar)",
        "chat-user": "var(--chat-user)",
        danger: "var(--danger)",
        "danger-soft": "var(--danger-soft)",
        "danger-border": "var(--danger-border)",
        success: "var(--success)",
        "success-soft": "var(--success-soft)",
        "success-border": "var(--success-border)",
        warn: "var(--warn)",
        "warn-soft": "var(--warn-soft)",
        "warn-border": "var(--warn-border)"
      },
      fontFamily: {
        sans: ["Inter", "ui-sans-serif", "system-ui", "sans-serif"],
        mono: ["SFMono-Regular", "Menlo", "Consolas", "monospace"]
      },
      maxWidth: { thread: "880px", composer: "768px" },
      boxShadow: { l: "var(--shadow-l)" }
    }
  },
  plugins: []
}
