import "@testing-library/jest-dom/vitest";

// jsdom implements neither, and both are used by the room: ResizeObserver to
// remeasure desk anchors when the grid reflows, matchMedia by Framer Motion's
// useReducedMotion.
class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}
globalThis.ResizeObserver ??= ResizeObserverStub as unknown as typeof ResizeObserver;

if (!window.matchMedia) {
  window.matchMedia = ((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia;
}

/*
  jsdom does not implement PointerEvent. Framer Motion's keyboard press handling
  constructs one when a motion element is activated with Enter or Space, which
  is exactly what the keyboard-accessibility tests do — without this the run
  reports an unhandled error and Vitest warns that results may be false
  positives.
*/
if (typeof window.PointerEvent === "undefined") {
  class PointerEventStub extends MouseEvent {
    readonly pointerId: number;
    readonly pointerType: string;
    readonly isPrimary: boolean;

    constructor(type: string, params: PointerEventInit = {}) {
      super(type, params);
      this.pointerId = params.pointerId ?? 0;
      this.pointerType = params.pointerType ?? "mouse";
      this.isPrimary = params.isPrimary ?? true;
    }
  }
  window.PointerEvent = PointerEventStub as unknown as typeof PointerEvent;
  globalThis.PointerEvent = window.PointerEvent;
}
