// @vitest-environment jsdom
import { render, screen, waitFor } from "@testing-library/react";
import { beforeAll, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";

vi.mock("@/api/client", () => ({
  apiGet: vi.fn(),
}));

import { apiGet } from "@/api/client";
import { OnboardingPage } from "./OnboardingPage";
import { ToastProvider } from "@/components/toast/Toast";

beforeAll(() => {
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    value: vi.fn().mockReturnValue({
      matches: false,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    }),
  });
});

function renderPage() {
  return render(
    <ToastProvider>
      <MemoryRouter initialEntries={["/onboarding"]}>
        <OnboardingPage />
      </MemoryRouter>
    </ToastProvider>
  );
}

function check(overrides: Record<string, unknown>) {
  return {
    key: "x", label: "X", status: "ok", detail: "", fix_hint: "", blocking: false,
    ...overrides,
  };
}

describe("OnboardingPage gate behaviour (F6 G5)", () => {
  it("blocks entry while a required item is missing", async () => {
    vi.mocked(apiGet).mockResolvedValueOnce({
      checks: [
        check({ key: "python", label: "Python 版本" }),
        check({
          key: "api_key",
          label: "DashScope API key（api_keys/qwen-api-key.txt）",
          status: "missing",
          detail: "未找到 api_keys/qwen-api-key.txt",
          fix_hint: "将我们私发的 DashScope key 放入 api_keys/qwen-api-key.txt",
          blocking: true,
        }),
      ],
      ready: false,
      blocking_missing: ["api_key"],
    } as never);
    renderPage();
    await screen.findByText("存在必需项未就绪，暂不能进入");
    expect(
      screen.getByRole("button", { name: "存在必需项未就绪，暂不能进入" }),
    ).toHaveProperty("disabled", true);
    expect(screen.getByRole("alert").textContent).toContain("api_key");
  });

  it("allows entry with only non-blocking degradation and states the consequence", async () => {
    vi.mocked(apiGet).mockResolvedValueOnce({
      checks: [
        check({ key: "python", label: "Python 版本" }),
        check({
          key: "latex",
          label: "LaTeX 工具链（latexmk/xelatex）",
          status: "degraded",
          detail: "缺 latexmk；将跳过 PDF 编译，仍生成 .tex/.md",
          blocking: false,
        }),
      ],
      ready: true,
      blocking_missing: [],
    } as never);
    renderPage();
    await waitFor(() => {
      expect(screen.getByRole("button", { name: "进入 OptoMind" })).toHaveProperty("disabled", false);
    });
    expect(screen.getAllByText(/跳过后果：将跳过 PDF 编译/).length).toBeGreaterThan(0);
  });
});
