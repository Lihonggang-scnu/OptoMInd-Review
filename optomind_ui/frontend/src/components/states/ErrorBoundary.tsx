import { Component, type ErrorInfo, type ReactNode } from "react";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";

interface Props {
  children: ReactNode;
}

interface State {
  error: Error | null;
}

/** Global boundary: a render crash shows a readable card, NEVER a blank page. */
export class ErrorBoundary extends Component<Props, State> {
  override state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  override componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error("[ErrorBoundary]", error, info.componentStack);
  }

  override render(): ReactNode {
    if (this.state.error) {
      return (
        <div className="mx-auto max-w-xl p-spacing-6">
          <Card>
            <CardContent>
              <h1 className="text-title text-foreground">页面出错了</h1>
              <p className="mt-spacing-2 text-body text-muted">
                渲染发生异常：{this.state.error.message}
              </p>
              <Button
                className="mt-spacing-4"
                variant="primary"
                onClick={() => this.setState({ error: null })}
              >
                重试
              </Button>
              <details className="mt-spacing-4 text-small text-muted">
                <summary className="cursor-pointer">技术详情</summary>
                <pre className="mt-spacing-2 overflow-auto whitespace-pre-wrap font-mono-token">
                  {this.state.error.stack ?? String(this.state.error)}
                </pre>
              </details>
            </CardContent>
          </Card>
        </div>
      );
    }
    return this.props.children;
  }
}
