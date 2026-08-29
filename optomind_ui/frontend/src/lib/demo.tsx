import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from "react";

const DEMO_KEY = "optomind-demo";

interface DemoApi {
  demo: boolean;
  setDemo: (value: boolean) => void;
}

const DemoContext = createContext<DemoApi>({ demo: false, setDemo: () => undefined });

/** Recording-friendly mode: bigger type, forced dark, masked local details. */
export function DemoProvider({ children }: { children: ReactNode }) {
  const [demo, setDemoState] = useState<boolean>(() => localStorage.getItem(DEMO_KEY) === "1");

  useEffect(() => {
    const root = document.documentElement;
    if (demo) {
      root.dataset.demo = "1";
      root.classList.add("dark"); // videos look better dark; user can still toggle
      localStorage.setItem(DEMO_KEY, "1");
    } else {
      delete root.dataset.demo;
      localStorage.setItem(DEMO_KEY, "0");
    }
  }, [demo]);

  const value = useMemo(() => ({ demo, setDemo: setDemoState }), [demo]);
  return <DemoContext.Provider value={value}>{children}</DemoContext.Provider>;
}

export function useDemo(): DemoApi {
  return useContext(DemoContext);
}

const PATH_RE = /(?:[A-Za-z]:\\[^\s"'，。；]+|\/home\/[^\s"'，。；]+|\/Users\/[^\s"'，。；]+)/g;
const MACHINE_RE = /Administrator|DESKTOP-[A-Z0-9]+/g;
const KEY_RE = /sk-[A-Za-z0-9]{8,}/g;

/** Hide absolute paths / machine names / key-like strings during demos. */
export function maskSensitive(text: string, enabled: boolean): string {
  if (!enabled) return text;
  return text
    .replace(PATH_RE, "〔本地路径〕")
    .replace(MACHINE_RE, "〔本机〕")
    .replace(KEY_RE, "〔密钥已隐藏〕");
}
