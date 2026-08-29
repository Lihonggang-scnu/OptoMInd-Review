import { Link, useNavigate } from "react-router-dom";
import type { ReactNode } from "react";
import { Button } from "@/components/ui/button";
import { ThemeToggle } from "@/components/theme/ThemeToggle";
import { useDemo } from "@/lib/demo";
import { cn } from "@/lib/cn";

export interface Crumb {
  label: string;
  to?: string;
}

/**
 * Every page renders inside PageFrame: breadcrumbs + back are mandatory so no
 * view can strand the user (F3 problem 4/6).
 */
export function PageFrame({
  crumbs,
  children,
  actions,
  immersive = false,
}: {
  crumbs: Crumb[];
  children: ReactNode;
  actions?: ReactNode;
  immersive?: boolean;
}) {
  const navigate = useNavigate();
  const { demo, setDemo } = useDemo();
  return (
    <div className="min-h-screen bg-background text-foreground">
      {!immersive && (
        <header className="border-b border-line bg-surface">
          <div className="mx-auto flex max-w-6xl flex-wrap items-center gap-spacing-3 px-spacing-5 py-spacing-3">
            <Button variant="ghost" size="sm" onClick={() => navigate(-1)} aria-label="返回上一页">
              ← 返回
            </Button>
            <nav aria-label="面包屑" className="flex flex-wrap items-center gap-spacing-2 text-small text-muted">
              {crumbs.map((crumb, index) => (
                <span key={index} className="flex items-center gap-spacing-2">
                  {index > 0 && <span aria-hidden>/</span>}
                  {crumb.to ? (
                    <Link to={crumb.to} className="hover:text-accent">{crumb.label}</Link>
                  ) : (
                    <span className="text-foreground">{crumb.label}</span>
                  )}
                </span>
              ))}
            </nav>
            <div className={cn("ml-auto flex flex-wrap items-center gap-spacing-2")}>
              {actions}
              <Button
                variant={demo ? "primary" : "ghost"}
                size="sm"
                aria-pressed={demo}
                aria-label={demo ? "退出演示模式" : "进入演示模式"}
                onClick={() => setDemo(!demo)}
              >
                🎬 演示{demo ? "开" : "关"}
              </Button>
              <ThemeToggle />
            </div>
          </div>
        </header>
      )}
      <main className={immersive ? "min-h-screen" : "mx-auto max-w-6xl px-spacing-5 py-spacing-5"}>
        {children}
      </main>
    </div>
  );
}
