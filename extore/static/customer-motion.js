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
    const animate = (element, frames, duration, easing = "cubic-bezier(0.16, 1, 0.3, 1)") => {
      const animation = element.animate(frames, {
        duration,
        easing,
        fill: "forwards",
      });
      animations.push(animation);
      return animation.finished?.catch(() => {});
    };
    const tearFrames = (lift) => [
      {
        transform: "translate3d(0, 0, 0) rotateX(0deg) rotateZ(0deg)",
        opacity: 1,
        easing: "cubic-bezier(0.33, 0, 0.2, 1)",
      },
      {
        transform: `translate3d(${lift ? -4 : 5}px, ${lift ? -8 : 9}px, 0) rotateX(${lift ? 8 : -7}deg) rotateZ(${lift ? -0.6 : 0.7}deg)`,
        opacity: 1,
        offset: 0.36,
        easing: "cubic-bezier(0.16, 1, 0.3, 1)",
      },
      {
        transform: `translate3d(${lift ? -16 : 18}px, ${lift ? -48 : 56}px, 0) rotateX(${lift ? 24 : -20}deg) rotateZ(${lift ? -2.6 : 3}deg)`,
        opacity: 0,
        offset: 1,
      },
    ];
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
            halves.map((half, index) => animate(half, tearFrames(index === 0), 350, "linear")),
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
              { transform: "translate3d(0, 16px, 0) scale(0.985)", opacity: 0 },
              { transform: "translate3d(0, 0, 0) scale(1)", opacity: 1 },
            ],
            240,
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
    let active = "";
    let disposed = false;
    let slipTimer = 0;
    let slipNode = null;
    let slipMotion = null;
    let slipCard = null;
    const slipTimers = [];
    const clearSlip = () => {
      if (slipTimer) window.clearTimeout(slipTimer);
      slipTimer = 0;
      for (const timer of slipTimers) window.clearTimeout(timer);
      slipTimers.length = 0;
      slipMotion?.cancel();
      slipMotion = null;
      slipNode?.remove();
      slipNode = null;
      slipCard?.classList.remove("paper-caught");
      slipCard = null;
    };
    const scheduleSlip = () => {
      clearSlip();
      if (
        disposed ||
        document.hidden ||
        reducedMotion?.matches ||
        typeof document.querySelector !== "function" ||
        typeof document.createElement !== "function"
      ) return;
      slipTimer = window.setTimeout(() => {
        slipTimer = 0;
        if (disposed || document.hidden || reducedMotion?.matches || !stack?.isConnected) return;
        const logo = document.querySelector(".brand img");
        const card = cards.find((node) => node.dataset.homePaper === "redeem");
        if (!logo || !card || !inViewport(card)) return;
        const from = logo.getBoundingClientRect();
        const to = card.getBoundingClientRect();
        const startX = from.left + from.width * 0.68;
        const startY = from.top + from.height * 0.68;
        const dx = to.right - 22 - startX;
        const dy = to.bottom + 4 - startY;
        const above = to.top - 18 - startY;
        slipCard = card;
        slipNode = document.createElement("i");
        slipNode.className = "counter-slip";
        slipNode.setAttribute("aria-hidden", "true");
        Object.assign(slipNode.style, { left: `${startX}px`, top: `${startY}px` });
        document.body.append(slipNode);
        if (typeof slipNode.animate === "function") {
          slipMotion = slipNode.animate(
            [
              { transform: "translate3d(0, 0, 0) rotate(-16deg) scale(0.2)", opacity: 0 },
              { transform: "translate3d(8px, -22px, 0) rotate(7deg) scale(0.86)", opacity: 1, offset: 0.14 },
              { transform: `translate3d(${dx}px, ${above}px, 0) rotate(-8deg) scale(1)`, opacity: 1, offset: 0.52 },
              { transform: `translate3d(${dx}px, ${dy}px, 0) rotate(2deg) scale(0.78)`, opacity: 0 },
            ],
            { duration: 760, easing: "linear", fill: "forwards" },
          );
          slipMotion.finished?.then(() => {
            if (slipNode?.isConnected) slipNode.remove();
            if (slipMotion) slipMotion = null;
            slipNode = null;
          }).catch(() => {});
        }
        slipTimers.push(window.setTimeout(() => {
          if (slipCard?.isConnected) slipCard.classList.add("paper-caught");
        }, 560));
        slipTimers.push(window.setTimeout(() => slipCard?.classList.remove("paper-caught"), 980));
      }, 460);
    };
    const mobile = (() => {
      try {
        return window.matchMedia?.("(max-width: 760px)") || null;
      } catch {
        return null;
      }
    })();
    const isMobile = () => mobile ? mobile.matches : window.innerWidth <= 760;
    const show = (name, { scroll = true, animate = true } = {}) => {
      clearSlip();
      if (disposed || app.isConnected === false || !stack || stack.isConnected === false) return false;
      const card = cards.find((node) => node.dataset.homePaper === name);
      if (!card) return false;
      if (active !== name) {
        active = name;
        stack.classList.toggle("home-focus-products", name === "products");
        stack.classList.toggle("home-focus-redeem", name === "redeem");
        for (const node of cards)
          node.setAttribute("aria-current", String(node === card));
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
      clearSlip();
      if (event.defaultPrevented || event.target?.closest?.("button, a, input, select, textarea, label, summary, [contenteditable], [role=button]")) return;
      const card = event.target?.closest?.("[data-home-paper]");
      if (card && cards.includes(card) && show(card.dataset.homePaper))
        card.focus?.({ preventScroll: true });
    };
    const focus = (event) => {
      const card = event.target?.closest?.("[data-home-paper]");
      if (card && cards.includes(card)) show(card.dataset.homePaper, { scroll: false });
    };
    const keydown = (event) => {
      if (!cards.includes(event.target) || event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
      const index = event.key === "ArrowLeft" ? 0 : event.key === "ArrowRight" ? 1 : -1;
      const card = cards[index];
      if (!card || !show(card.dataset.homePaper)) return;
      event.preventDefault();
      card.focus?.({ preventScroll: true });
    };
    const scrolled = (event) => {
      if (event?.target && event.target !== stack) return;
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
    const slipInterrupted = () => {
      if (document.hidden || reducedMotion?.matches) clearSlip();
    };
    const removeSlipMedia = mediaListener(slipInterrupted);
    document.addEventListener("visibilitychange", slipInterrupted);
    const detach = () => {
      stack?.removeEventListener("click", click);
      stack?.removeEventListener("focusin", focus);
      stack?.removeEventListener("keydown", keydown);
      stack?.removeEventListener("scroll", scrolled);
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
        clearSlip();
        stack = next;
        active = "";
        cards = stack ? [...stack.querySelectorAll("[data-home-paper]")] : [];
        for (const card of cards) card.setAttribute("aria-current", "false");
        stack?.addEventListener("click", click);
        stack?.addEventListener("focusin", focus);
        stack?.addEventListener("keydown", keydown);
        stack?.addEventListener("scroll", scrolled, { passive: true });
        if (isMobile()) show("redeem", { animate: false });
        scheduleSlip();
      },
      dispose() {
        if (disposed) return;
        disposed = true;
        detach();
        clearSlip();
        removeSlipMedia();
        document.removeEventListener("visibilitychange", slipInterrupted);
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
