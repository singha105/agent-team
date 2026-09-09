/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** "1" when started with `npm run demo`: replay a recording, no backend. */
  readonly VITE_DEMO?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
