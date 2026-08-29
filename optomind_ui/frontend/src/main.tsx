import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import App from "./App";
import { ErrorBoundary } from "@/components/states/ErrorBoundary";
import { ToastProvider } from "@/components/toast/Toast";
import { DemoProvider } from "@/lib/demo";
import "./styles/tokens.css";
import "./styles/replay.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <ErrorBoundary>
      <DemoProvider>
        <ToastProvider>
          <BrowserRouter>
            <App />
          </BrowserRouter>
        </ToastProvider>
      </DemoProvider>
    </ErrorBoundary>
  </React.StrictMode>,
);
