import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { applyTheme, initialTheme, type Theme } from "@/lib/theme";

/** Light/dark switch backed by token themes; synced with command palette. */
export function ThemeToggle() {
  const [theme, setTheme] = useState<Theme>(() => initialTheme());

  useEffect(() => {
    applyTheme(theme);
  }, [theme]);

  useEffect(() => {
    function onExternalToggle() {
      setTheme((prev) => (prev === "dark" ? "light" : "dark"));
    }
    window.addEventListener("optomind:toggle-theme", onExternalToggle);
    return () => window.removeEventListener("optomind:toggle-theme", onExternalToggle);
  }, []);

  return (
    <Button
      variant="ghost"
      size="sm"
      aria-label={theme === "dark" ? "切换到浅色主题" : "切换到深色主题"}
      onClick={() => setTheme(theme === "dark" ? "light" : "dark")}
    >
      {theme === "dark" ? "☀ 浅色" : "☾ 深色"}
    </Button>
  );
}
