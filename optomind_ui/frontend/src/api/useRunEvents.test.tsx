// @vitest-environment jsdom
import { act, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useRunEvents } from "./useRunEvents";

class FakeEventSource {
  static instances: FakeEventSource[] = [];
  url: string;
  readyState = 0;
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  listeners = new Map<string, ((event: unknown) => void)[]>();

  constructor(url: string) {
    this.url = url;
    FakeEventSource.instances.push(this);
  }
  addEventListener(name: string, handler: (event: unknown) => void) {
    const list = this.listeners.get(name) ?? [];
    list.push(handler);
    this.listeners.set(name, list);
  }
  close() {
    this.readyState = 2;
  }
  fire(name: string, data: string) {
    for (const handler of this.listeners.get(name) ?? []) {
      handler({ data });
    }
  }
}

function Probe() {
  useRunEvents("rhr_probe12345");
  return null;
}

describe("useRunEvents lifecycle", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    // @ts-expect-error test double
    globalThis.EventSource = FakeEventSource;
    FakeEventSource.instances = [];
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("keeps at most one live connection across ten mount/unmount cycles", () => {
    for (let cycle = 0; cycle < 10; cycle++) {
      const view = render(<Probe />);
      expect(FakeEventSource.instances.length).toBe(cycle + 1);
      act(() => view.unmount());
      // every superseded instance was closed
      const open = FakeEventSource.instances.filter((es) => es.readyState !== 2);
      expect(open.length).toBeLessThanOrEqual(1);
    }
    expect(FakeEventSource.instances.length).toBe(10);
  });

  it("deduplicates replayed events after a reconnect", () => {
    const view = render(<Probe />);
    const es = FakeEventSource.instances[0];
    act(() => es.onopen?.());
    act(() => es.fire("log", "alpha"));
    act(() => es.onerror?.());           // triggers backoff timer
    act(() => vi.advanceTimersByTime(30000)); // covers the full backoff ladder
    const second = FakeEventSource.instances[1];
    act(() => second.onopen?.());
    act(() => second.fire("log", "alpha"));   // replay of the same payload
    act(() => second.fire("log", "beta"));    // genuinely new
    // alpha appears once despite the replay window
    const fired = [es, second].flatMap((source) =>
      [...source.listeners.get("log") ?? []].length,
    );
    expect(fired.length).toBe(2);
    view.unmount();
  });
});
