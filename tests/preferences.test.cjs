const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(
  path.join(__dirname, "../extore/static/preferences.js"),
  "utf8",
);
const plain = (value) => JSON.parse(JSON.stringify(value));

function browser(options = {}) {
  const values = new Map(Object.entries(options.stored || {}));
  const events = new Map();
  const mediaListeners = new Set();
  const media = { matches: options.dark || false };
  if (options.legacyMedia)
    media.addListener = (callback) => mediaListeners.add(callback);
  else
    media.addEventListener = (type, callback) => {
      assert.equal(type, "change");
      mediaListeners.add(callback);
    };
  const storage = {
    getItem(key) {
      if (options.denyRead) throw new Error("Storage read denied");
      return values.get(key) ?? null;
    },
    setItem(key, value) {
      if (options.denyWrite) throw new Error("Storage write denied");
      values.set(key, value);
    },
  };
  const element = { dataset: {}, lang: "" };
  Object.defineProperty(element, "style", {
    get() {
      throw new Error("Inline styles are blocked by CSP");
    },
    set() {
      throw new Error("Inline styles are blocked by CSP");
    },
  });
  const window = {
    navigator: {
      languages: options.languages ?? ["zh-CN"],
      language: options.language ?? "zh-CN",
    },
    document: { documentElement: element },
    matchMedia(query) {
      assert.equal(query, "(prefers-color-scheme: dark)");
      if (options.noMedia) throw new Error("Media queries unavailable");
      return media;
    },
    addEventListener(type, callback) {
      if (!events.has(type)) events.set(type, new Set());
      events.get(type).add(callback);
    },
  };
  Object.defineProperty(window, "localStorage", {
    get() {
      if (options.denyStorage) throw new Error("Storage access denied");
      return storage;
    },
  });
  vm.runInNewContext(source, { window });
  const emit = (type, event = {}) => {
    for (const listener of events.get(type) || []) listener(event);
  };
  return {
    preferences: window.ExtorePreferences,
    element,
    values,
    languageChanged(languages) {
      window.navigator.languages = languages;
      emit("languagechange");
    },
    themeChanged(dark) {
      media.matches = dark;
      for (const listener of mediaListeners) listener({ matches: dark });
    },
    storageChanged(key, value, storageArea = storage) {
      if (value === null) values.delete(key);
      else values.set(key, value);
      emit("storage", { key, newValue: value, storageArea });
    },
    storageCleared() {
      values.clear();
      emit("storage", { key: null, newValue: null, storageArea: storage });
    },
  };
}

test("defaults follow browser appearance and the first supported preferred language", () => {
  const b = browser({ dark: true, languages: ["fr-FR", "en-GB", "zh-CN"] });
  assert.deepEqual(plain(b.preferences.settings), {
    theme: "auto",
    language: "auto",
  });
  assert.deepEqual(plain(b.preferences.resolved), {
    theme: "dark",
    language: "en",
  });
  assert.equal(b.element.dataset.theme, "dark");
  assert.equal(b.element.lang, "en");
  assert.equal(b.values.size, 0);

  for (const [languages, expected] of [
    [["zh-TW", "en-US"], "zh-CN"],
    [["ja-JP", "ZH-hans"], "zh-CN"],
    [["fr-FR", "EN-us"], "en"],
    [["enochian", "zhang"], "zh-CN"],
    [["ja-JP", "de-DE"], "zh-CN"],
  ]) {
    assert.equal(browser({ languages }).preferences.resolved.language, expected);
  }
  assert.equal(
    browser({ languages: [], language: "en-US" }).preferences.resolved.language,
    "en",
  );
});

test("automatic settings update live and notify only when preference state changes", () => {
  const b = browser({ languages: ["en-US"] });
  const notifications = [];
  const unsubscribe = b.preferences.subscribe((value) =>
    notifications.push(plain(value)),
  );
  assert.equal(notifications.length, 0);
  b.themeChanged(false);
  b.languageChanged(["en-GB"]);
  assert.equal(notifications.length, 0);
  b.themeChanged(true);
  assert.equal(notifications.length, 1);
  assert.equal(b.element.dataset.theme, "dark");
  b.languageChanged(["zh-TW", "en-US"]);
  assert.equal(notifications.length, 2);
  assert.equal(b.element.lang, "zh-CN");
  assert.deepEqual(notifications[1], {
    settings: { theme: "auto", language: "auto" },
    resolved: { theme: "dark", language: "zh-CN" },
  });
  b.preferences.setTheme("auto");
  b.preferences.setLanguage("auto");
  assert.equal(notifications.length, 2);
  unsubscribe();
  b.themeChanged(false);
  assert.equal(b.element.dataset.theme, "light");
  assert.equal(notifications.length, 2);
});

test("explicit selections persist and remain stable across browser changes", () => {
  const b = browser({ languages: ["en-US"] });
  let notified = 0;
  b.preferences.subscribe(() => notified++);
  b.preferences.setTheme("dark");
  b.preferences.setLanguage("zh-CN");
  assert.equal(b.values.get("extore_theme"), "dark");
  assert.equal(b.values.get("extore_language"), "zh-CN");
  assert.equal(notified, 2);
  b.themeChanged(false);
  b.languageChanged(["en-US"]);
  assert.equal(notified, 2);
  assert.deepEqual(plain(b.preferences.resolved), {
    theme: "dark",
    language: "zh-CN",
  });
  b.preferences.setTheme("auto");
  b.preferences.setLanguage("auto");
  assert.equal(notified, 4);
  assert.deepEqual(plain(b.preferences.resolved), {
    theme: "light",
    language: "en",
  });
});

test("existing explicit language selections are preserved and invalid stored values fall back to auto", () => {
  for (const language of ["zh-CN", "en"]) {
    const b = browser({
      stored: { extore_language: language, extore_theme: "dark" },
      languages: [language === "en" ? "zh-TW" : "en-US"],
    });
    assert.equal(b.preferences.settings.language, language);
    assert.equal(b.preferences.resolved.language, language);
    assert.equal(b.preferences.settings.theme, "dark");
    b.languageChanged([language === "en" ? "zh-CN" : "en-GB"]);
    assert.equal(b.preferences.resolved.language, language);
  }
  const b = browser({
    stored: { extore_language: "fr", extore_theme: "sepia" },
    dark: true,
    languages: ["en-US"],
  });
  assert.deepEqual(plain(b.preferences.settings), {
    theme: "auto",
    language: "auto",
  });
  assert.deepEqual(plain(b.preferences.resolved), {
    theme: "dark",
    language: "en",
  });
});

test("changes from another tab resolve immediately and unrelated storage is ignored", () => {
  const b = browser({ languages: ["en-US"] });
  let notified = 0;
  b.preferences.subscribe(() => notified++);
  b.storageChanged("extore_theme", "dark");
  b.storageChanged("extore_language", "zh-CN");
  assert.equal(notified, 2);
  assert.equal(b.element.dataset.theme, "dark");
  assert.equal(b.element.lang, "zh-CN");
  b.storageChanged("extore_theme", "dark");
  b.storageChanged("unrelated", "value");
  b.storageChanged("extore_theme", "light", {});
  assert.equal(notified, 2);
  assert.equal(b.preferences.settings.theme, "dark");
  b.storageChanged("extore_language", null);
  assert.equal(b.preferences.settings.language, "auto");
  assert.equal(b.element.lang, "en");
  b.storageCleared();
  assert.equal(notified, 4);
  assert.deepEqual(plain(b.preferences.settings), {
    theme: "auto",
    language: "auto",
  });
  assert.equal(b.element.dataset.theme, "light");
});

test("blocked storage does not prevent selecting preferences for the current page", () => {
  for (const options of [
    { denyStorage: true },
    { denyRead: true, denyWrite: true },
    { denyWrite: true },
  ]) {
    const b = browser({ ...options, dark: true, languages: ["en-US"] });
    assert.equal(b.preferences.settings.theme, "auto");
    b.preferences.setTheme("light");
    b.preferences.setLanguage("zh-CN");
    assert.deepEqual(plain(b.preferences.settings), {
      theme: "light",
      language: "zh-CN",
    });
    assert.equal(b.element.dataset.theme, "light");
    assert.equal(b.element.lang, "zh-CN");
    b.themeChanged(true);
    b.languageChanged(["en-GB"]);
    assert.equal(b.element.dataset.theme, "light");
    assert.equal(b.element.lang, "zh-CN");
  }
});

test("legacy media listeners and unavailable media queries remain usable", () => {
  const b = browser({ legacyMedia: true });
  b.themeChanged(true);
  assert.equal(b.element.dataset.theme, "dark");
  const unsupported = browser({ noMedia: true });
  assert.equal(unsupported.preferences.resolved.theme, "light");
  unsupported.preferences.setTheme("dark");
  assert.equal(unsupported.element.dataset.theme, "dark");
});

test("getter snapshots cannot mutate preferences and invalid choices are rejected", () => {
  const b = browser();
  b.preferences.settings.theme = "dark";
  b.preferences.resolved.language = "en";
  assert.equal(b.preferences.settings.theme, "auto");
  assert.equal(b.preferences.resolved.language, "zh-CN");
  assert.throws(() => b.preferences.setTheme("sepia"), /Invalid theme/);
  assert.throws(() => b.preferences.setLanguage("fr"), /Invalid language/);
  assert.throws(() => b.preferences.subscribe(null), /must be a function/);
  let notified = 0;
  b.preferences.subscribe(() => {
    throw new Error("A view failed");
  });
  b.preferences.subscribe(() => notified++);
  assert.doesNotThrow(() => b.preferences.setTheme("dark"));
  assert.equal(notified, 1);
});
