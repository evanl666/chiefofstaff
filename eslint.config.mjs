// Lint for the web UI (plain ES modules, no build step): npx eslint
const browser = Object.fromEntries(
  [
    "window", "document", "navigator", "location", "localStorage", "fetch", "EventSource",
    "URL", "URLSearchParams", "Blob", "ClipboardItem", "setTimeout", "clearTimeout",
    "setInterval", "clearInterval", "requestAnimationFrame", "Intl", "console", "HTMLElement",
    "Node", "getComputedStyle", "matchMedia", "CSS", "Notification", "Event", "history",
    "confirm", "HTMLDialogElement", "IntersectionObserver", "performance", "AbortController",
  ].map((name) => [name, "readonly"]),
);

// The share links viewer (site/s/): a page of its own, with WebCrypto.
const viewer = {
  ...browser,
  ...Object.fromEntries(["crypto", "TextEncoder", "TextDecoder", "atob", "btoa"].map((name) => [name, "readonly"])),
};
const rules = {
  "no-undef": "error",
  "no-unused-vars": ["error", { args: "none", caughtErrors: "none" }],
  "no-redeclare": "error",
  "no-var": "error",
  "prefer-const": "error",
  eqeqeq: ["error", "smart"],
  "no-implicit-globals": "error",
};

export default [
  {
    files: ["site/**/*.js"],
    languageOptions: { ecmaVersion: 2024, sourceType: "module", globals: viewer },
    rules,
  },
  {
    files: ["src/chiefofstaff/static/**/*.js"],
    languageOptions: { ecmaVersion: 2024, sourceType: "module", globals: browser },
    rules: {
      "no-undef": "error",
      "no-unused-vars": ["error", { args: "none", caughtErrors: "none" }],
      "no-redeclare": "error",
      "no-var": "error",
      "prefer-const": "error",
      eqeqeq: ["error", "smart"],
      "no-implicit-globals": "error",
    },
  },
];
