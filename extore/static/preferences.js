/* Resolve display preferences before the page stylesheet is loaded. */
(function (root) {
  "use strict";

  const themes = new Set(["auto", "light", "dark"]);
  const accents = new Set(["green", "blue", "violet", "rose", "amber", "graphite"]);
  const languages = new Set(["auto", "zh-CN", "en"]);
  const listeners = new Set();
  let storage = null;
  let darkMedia = null;
  try {
    storage = root.localStorage;
  } catch {
    // Privacy settings can deny storage while display preferences still work.
  }
  try {
    darkMedia = root.matchMedia?.("(prefers-color-scheme: dark)") || null;
  } catch {
    // Browsers without media queries use the light appearance.
  }

  function read(key) {
    try {
      return storage?.getItem(key);
    } catch {
      return null;
    }
  }
  function write(key, value) {
    try {
      storage?.setItem(key, value);
    } catch {
      // Retain the selection in memory when persistence is unavailable.
    }
  }
  const themeValue = (value) => (themes.has(value) ? value : "auto");
  const accentValue = (value) => (accents.has(value) ? value : "green");
  const languageValue = (value) =>
    languages.has(value) ? value : "auto";
  let settings = {
    theme: themeValue(read("extore_theme")),
    accent: accentValue(read("extore_accent")),
    language: languageValue(read("extore_language")),
  };

  function browserLanguage() {
    const preferred = root.navigator?.languages;
    const ordered =
      preferred?.length > 0
        ? preferred
        : [root.navigator?.language || ""];
    for (const language of ordered) {
      if (typeof language !== "string") continue;
      if (/^zh(?:-|$)/i.test(language)) return "zh-CN";
      if (/^en(?:-|$)/i.test(language)) return "en";
    }
    return "zh-CN";
  }
  function resolve() {
    return {
      theme:
        settings.theme === "auto"
          ? darkMedia?.matches
            ? "dark"
            : "light"
          : settings.theme,
      language:
        settings.language === "auto"
          ? browserLanguage()
          : settings.language,
    };
  }
  let resolved = resolve();
  let previous = {
    theme: settings.theme,
    accent: settings.accent,
    language: settings.language,
    resolvedTheme: resolved.theme,
    resolvedLanguage: resolved.language,
  };

  function apply() {
    const element = root.document?.documentElement;
    if (!element) return;
    element.dataset.theme = resolved.theme;
    element.dataset.accent = settings.accent;
    element.lang = resolved.language;
  }
  function snapshot() {
    return { settings: { ...settings }, resolved: { ...resolved } };
  }
  function refresh() {
    resolved = resolve();
    apply();
    const next = {
      theme: settings.theme,
      accent: settings.accent,
      language: settings.language,
      resolvedTheme: resolved.theme,
      resolvedLanguage: resolved.language,
    };
    if (
      previous.theme === next.theme &&
      previous.accent === next.accent &&
      previous.language === next.language &&
      previous.resolvedTheme === next.resolvedTheme &&
      previous.resolvedLanguage === next.resolvedLanguage
    )
      return;
    previous = next;
    for (const listener of [...listeners]) {
      try {
        listener(snapshot());
      } catch {
        // One rendering callback must not prevent other views from updating.
      }
    }
  }

  apply();
  root.addEventListener("storage", (event) => {
    if (event.storageArea && event.storageArea !== storage) return;
    if (event.key === "extore_theme")
      settings.theme = themeValue(event.newValue);
    else if (event.key === "extore_accent")
      settings.accent = accentValue(event.newValue);
    else if (event.key === "extore_language")
      settings.language = languageValue(event.newValue);
    else if (event.key === null)
      settings = {
        theme: themeValue(read("extore_theme")),
        accent: accentValue(read("extore_accent")),
        language: languageValue(read("extore_language")),
      };
    else return;
    refresh();
  });
  root.addEventListener("languagechange", refresh);
  if (typeof darkMedia?.addEventListener === "function")
    darkMedia.addEventListener("change", refresh);
  else if (typeof darkMedia?.addListener === "function")
    darkMedia.addListener(refresh);

  root.ExtorePreferences = Object.freeze({
    get settings() {
      return { ...settings };
    },
    get resolved() {
      return { ...resolved };
    },
    setTheme(value) {
      if (!themes.has(value)) throw new TypeError("Invalid theme preference");
      settings.theme = value;
      write("extore_theme", value);
      refresh();
    },
    setAccent(value) {
      if (!accents.has(value)) throw new TypeError("Invalid accent preference");
      settings.accent = value;
      write("extore_accent", value);
      refresh();
    },
    setLanguage(value) {
      if (!languages.has(value))
        throw new TypeError("Invalid language preference");
      settings.language = value;
      write("extore_language", value);
      refresh();
    },
    subscribe(callback) {
      if (typeof callback !== "function")
        throw new TypeError("Preference subscriber must be a function");
      listeners.add(callback);
      return () => listeners.delete(callback);
    },
  });
})(window);
