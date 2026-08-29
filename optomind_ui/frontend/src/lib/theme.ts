export type Theme = "light" | "dark";

const STORAGE_KEY = "optomind-theme";

export function initialTheme(): Theme {
  const saved = localStorage.getItem(STORAGE_KEY);
  if (saved === "light" || saved === "dark") return saved;
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

export function applyTheme(theme: Theme) {
  document.documentElement.classList.toggle("dark", theme === "dark");
  document.documentElement.dataset.demo = document.documentElement.classList.contains("dark")
    ? document.documentElement.dataset.demo ?? ""
    : document.documentElement.dataset.demo ?? "";
  localStorage.setItem(STORAGE_KEY, theme);
}
