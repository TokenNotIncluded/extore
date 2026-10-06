"use strict";

(() => {
  const transitions = new WeakMap();
  const mounts = new WeakMap();
  const waitingRoots = new WeakMap();
  const homeMounts = new WeakMap();
  const reducedMotion = (() => {
    try {
      return window.matchMedia?.("(prefers-reduced-motion: reduce)") || null;
    } catch {
      return null;
    }
  })();
  const mediaListener = (callback) => {
    if (reducedMotion?.addEventListener) {
      reducedMotion.addEventListener("change", callback);
      return () => reducedMotion.removeEventListener("change", callback);
    }
    reducedMotion?.addListener?.(callback);
    return () => reducedMotion?.removeListener?.(callback);
  };
  const inViewport = (element) => {
    const rect = element.getBoundingClientRect();
    return (
      rect.width > 0 &&
      rect.height > 0 &&
      rect.bottom > 0 &&
      rect.right > 0 &&
      rect.top < window.innerHeight &&
      rect.left < window.innerWidth
    );
  };

  async function transition(app, render, isCurrent = () => true) {
    transitions.get(app)?.cancel();
    const animations = [];
    let aborted = false;
    let early = false;
    let cleaned = false;
    let layer = null;
    let source = null;
    let previousVisibility = "";
    const valid = () => !aborted && app.isConnected !== false && isCurrent();
    const stopAnimations = () => {
      for (const animation of animations) animation.cancel();
    };
    const restoreSource = () => {
      layer?.remove();
      layer = null;
      if (source) source.style.visibility = previousVisibility;
      source = null;
    };
    const cleanup = () => {
      if (cleaned) return;
      cleaned = true;
      restoreSource();
      stopAnimations();
      document.removeEventListener("visibilitychange", visibilityChanged);
      removeMediaListener();
      if (transitions.get(app) === run) {
        transitions.delete(app);
        app.classList.remove("motion-transition");
      }
    };
    const run = {
      cancel() {
        aborted = true;
        cleanup();
      },
    };
    const finishEarly = () => {
      early = true;
      stopAnimations();
    };
    const visibilityChanged = () => {
      if (document.hidden) finishEarly();
    };
    const removeMediaListener = mediaListener(() => {
      if (reducedMotion?.matches) finishEarly();
    });
    document.addEventListener("visibilitychange", visibilityChanged);
    transitions.set(app, run);
    const animate = (element, frames, duration) => {
      const animation = element.animate(frames, {
        duration,
        easing: "cubic-bezier(0.16, 1, 0.3, 1)",
        fill: "forwards",
      });
      animations.push(animation);
      return animation.finished?.catch(() => {});
    };
    try {
      if (!valid()) return false;
      source = app.querySelector(".exchange");
      if (source) previousVisibility = source.style.visibility;
      const spatial =
        !document.hidden &&
        !reducedMotion?.matches &&
        source &&
        typeof source.animate === "function" &&
        inViewport(source);
      if (spatial) {
        const rect = source.getBoundingClientRect();
        layer = document.createElement("div");
        layer.className = "motion-tear-layer";
        layer.setAttribute("aria-hidden", "true");
        layer.inert = true;
        Object.assign(layer.style, {
          left: `${rect.left}px`,
          top: `${rect.top}px`,
          width: `${rect.width}px`,
          height: `${rect.height + 12}px`,
        });
        const halves = ["top", "bottom"].map((position) => {
          const half = document.createElement("div");
          half.className = `motion-tear-half motion-tear-${position}`;
          const copy = source.cloneNode(true);
          copy.removeAttribute("id");
          copy.classList.add("motion-tear-copy");
          copy.style.visibility = "visible";
          copy.style.height = `${rect.height}px`;
          copy.querySelectorAll("[id]").forEach((node) => node.removeAttribute("id"));
          copy.querySelectorAll("[name]").forEach((node) => node.removeAttribute("name"));
          copy.querySelectorAll("input, select, textarea, button, a").forEach((node) => {
            node.setAttribute("tabindex", "-1");
          });
          half.append(copy);
          layer.append(half);
          return half;
        });
        app.append(layer);
        app.classList.add("motion-transition");
        source.style.visibility = "hidden";
        try {
          await Promise.all(
            halves.map((half, index) =>
              animate(
                half,
                [
                  { transform: "translateY(0) rotate(0deg)", opacity: 1 },
                  {
                    transform: `translateY(${index ? 34 : -28}px) rotate(${index ? 1.1 : -0.8}deg)`,
                    opacity: 0,
                  },
                ],
                350,
              ),
            ),
          );
        } catch {
          // Rendering remains available when a browser declines WAAPI effects.
          finishEarly();
        }
      }
      restoreSource();
      if (!valid()) return false;
      await render();
      if (!valid()) return false;
      const arriving = app.querySelector(".narrow") || app.firstElementChild;
      if (
        spatial &&
        !early &&
        !document.hidden &&
        !reducedMotion?.matches &&
        arriving?.animate
      ) {
        try {
          await animate(
            arriving,
            [
              { transform: "translateY(8px)", opacity: 0.65 },
              { transform: "translateY(0)", opacity: 1 },
            ],
            180,
          );
        } catch {
          // The new page is already visible without an arrival effect.
        }
      }
      return valid();
    } finally {
      cleanup();
    }
  }

  function waitingMarkup(lang) {
    const group = '<span class="waiting-paper-group"><i class="waiting-slip"></i><i class="waiting-slip"></i><i class="waiting-slip"></i></span>';
    return `<div class="waiting-conveyor" aria-hidden="true" data-motion-language="${lang === "en" ? "en" : "zh-CN"}"><div class="waiting-conveyor-track">${group}${group}</div></div>`;
  }

  function mount(app) {
    const existing = mounts.get(app);
    if (existing) {
      existing.refresh();
      return existing;
    }
    let target = null;
    let visible = false;
    let disposed = false;
    let manualPaused = app.classList.contains("motion-user-paused");
    const observer = window.IntersectionObserver
      ? new window.IntersectionObserver((entries) => {
          for (const entry of entries)
            if (entry.target === target)
              visible = entry.isIntersecting && entry.intersectionRatio > 0;
          update();
        })
      : null;
    const update = () => {
      if (disposed) return;
      if (!observer) visible = target ? inViewport(target) : false;
      app.classList.toggle(
        "motion-paused",
        manualPaused || document.hidden || !visible || !!reducedMotion?.matches,
      );
    };
    const removeMediaListener = mediaListener(update);
    document.addEventListener("visibilitychange", update);
    if (!observer) {
      window.addEventListener("scroll", update, { passive: true });
      window.addEventListener("resize", update, { passive: true });
    }
    const controller = {
      get disposed() {
        return disposed;
      },
      get paused() {
        return manualPaused;
      },
      refresh() {
        const next = app.classList.contains("receipt-waiting")
          ? app
          : app.querySelector(".receipt-waiting");
        if (next !== target) {
          if (target) waitingRoots.delete(target);
          observer?.disconnect();
          target = next;
          visible = false;
          if (target) {
            waitingRoots.set(target, controller);
            observer?.observe(target);
          }
        }
        update();
      },
      setPaused(value) {
        manualPaused = !!value;
        app.classList.toggle("motion-user-paused", manualPaused);
        update();
      },
      dispose() {
        if (disposed) return;
        disposed = true;
        observer?.disconnect();
        removeMediaListener();
        document.removeEventListener("visibilitychange", update);
        window.removeEventListener("scroll", update);
        window.removeEventListener("resize", update);
        if (target) waitingRoots.delete(target);
        mounts.delete(app);
        app.classList.add("motion-paused");
      },
    };
    mounts.set(app, controller);
    controller.refresh();
    return controller;
  }

  function setPaused(root, value) {
    const controller = mounts.get(root) || waitingRoots.get(root);
    if (controller) controller.setPaused(value);
    else {
      root.classList.toggle("motion-user-paused", !!value);
      root.classList.toggle("motion-paused", !!value);
    }
  }

  function mountHome(app) {
    const existing = homeMounts.get(app);
    if (existing) {
      existing.refresh();
      return existing;
    }
    let stack = null;
    let cards = [];
    let buttons = [];
    let active = "";
    let disposed = false;
    const mobile = (() => {
      try {
        return window.matchMedia?.("(max-width: 760px)") || null;
      } catch {
        return null;
      }
    })();
    const isMobile = () => mobile ? mobile.matches : window.innerWidth <= 760;
    const show = (name, { scroll = true, animate = true } = {}) => {
      if (disposed || app.isConnected === false || !stack) return false;
      const card = cards.find((node) => node.dataset.homePaper === name);
      if (!card) return false;
      if (active !== name) {
        active = name;
        stack.classList.toggle("home-focus-products", name === "products");
        stack.classList.toggle("home-focus-redeem", name === "redeem");
        for (const button of buttons)
          button.node.setAttribute("aria-current", String(button.name === name));
      }
      if (scroll && isMobile()) {
        const left = Math.max(0, card.offsetLeft - 18);
        if (typeof stack.scrollTo === "function")
          stack.scrollTo({
            left,
            behavior: animate && !reducedMotion?.matches ? "smooth" : "auto",
          });
        else stack.scrollLeft = left;
      }
      return true;
    };
    const click = (event) => {
      const card = event.target.closest?.("[data-home-paper]");
      if (card && cards.includes(card)) show(card.dataset.homePaper);
    };
    const focus = (event) => {
      const card = event.target.closest?.("[data-home-paper]");
      if (card && cards.includes(card)) show(card.dataset.homePaper);
    };
    const scrolled = () => {
      if (!isMobile() || !cards.length) return;
      const center = stack.scrollLeft + stack.clientWidth / 2;
      const nearest = cards.reduce((previous, card) => {
        const distance = (node) => Math.abs(node.offsetLeft + node.offsetWidth / 2 - center);
        return distance(card) < distance(previous) ? card : previous;
      });
      if (nearest.dataset.homePaper !== active)
        show(nearest.dataset.homePaper, { scroll: false });
    };
    const modeChanged = () => {
      if (isMobile()) show(active || "redeem", { animate: false });
    };
    const detach = () => {
      stack?.removeEventListener("click", click);
      stack?.removeEventListener("focusin", focus);
      stack?.removeEventListener("scroll", scrolled);
      for (const button of buttons)
        button.node.removeEventListener("click", button.handler);
    };
    const controller = {
      get disposed() {
        return disposed;
      },
      show,
      refresh() {
        const next = app.querySelector(".home-paper-stack");
        if (next === stack) return;
        detach();
        stack = next;
        active = "";
        cards = stack ? [...stack.querySelectorAll("[data-home-paper]")] : [];
        buttons = ["products", "redeem"].flatMap((name) => {
          const node = app.querySelector(`#home-show-${name}`);
          if (!node) return [];
          const handler = () => show(name);
          node.addEventListener("click", handler);
          node.setAttribute("aria-current", "false");
          return [{ name, node, handler }];
        });
        stack?.addEventListener("click", click);
        stack?.addEventListener("focusin", focus);
        stack?.addEventListener("scroll", scrolled, { passive: true });
        if (isMobile()) show("redeem", { animate: false });
      },
      dispose() {
        if (disposed) return;
        disposed = true;
        detach();
        if (mobile?.removeEventListener) mobile.removeEventListener("change", modeChanged);
        else mobile?.removeListener?.(modeChanged);
        homeMounts.delete(app);
      },
    };
    if (mobile?.addEventListener) mobile.addEventListener("change", modeChanged);
    else mobile?.addListener?.(modeChanged);
    homeMounts.set(app, controller);
    controller.refresh();
    return controller;
  }

  window.ExtoreMotion = Object.freeze({
    transition,
    waitingMarkup,
    mount,
    mountHome,
    setPaused,
    cancel: (app) => transitions.get(app)?.cancel(),
  });
})();
