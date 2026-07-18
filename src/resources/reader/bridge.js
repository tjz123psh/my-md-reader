(() => {
  "use strict";

  const mappedBlock = (node) => {
    if (!node) return null;
    const element = node.nodeType === Node.ELEMENT_NODE ? node : node.parentElement;
    return element ? element.closest("[data-source-start]") : null;
  };

  const headingFor = (element) => {
    let current = element;
    while (current) {
      let candidate = current;
      while (candidate) {
        if (/^H[1-6]$/.test(candidate.tagName || "")) {
          return { id: candidate.id || "", title: candidate.textContent.trim() };
        }
        candidate = candidate.previousElementSibling;
      }
      current = current.parentElement;
    }
    return null;
  };

  const clearAnchors = () => {
    document.querySelectorAll('[data-selection-anchor="true"]').forEach((node) => {
      node.removeAttribute("data-selection-anchor");
    });
  };

  const reportSelection = () => {
    clearAnchors();
    const selection = window.getSelection();
    const text = selection ? selection.toString().trim() : "";

    if (!selection || selection.rangeCount === 0 || !text) {
      window.webkit?.messageHandlers?.selection?.postMessage(JSON.stringify({ text: "" }));
      return;
    }

    const range = selection.getRangeAt(0);
    const first = mappedBlock(range.startContainer);
    const last = mappedBlock(range.endContainer) || first;
    if (!first) return;

    first.setAttribute("data-selection-anchor", "true");
    const startLine = Number.parseInt(first.dataset.sourceStart || "0", 10);
    const endLine = Number.parseInt(last.dataset.sourceEnd || first.dataset.sourceEnd || "0", 10);

    window.webkit?.messageHandlers?.selection?.postMessage(JSON.stringify({
      text: text.slice(0, 12000),
      startLine,
      endLine,
      heading: headingFor(first),
    }));
  };

  let timer = 0;
  document.addEventListener("selectionchange", () => {
    window.clearTimeout(timer);
    timer = window.setTimeout(reportSelection, 80);
  });

  const headings = Array.from(document.querySelectorAll(
    "h1[id], h2[id], h3[id], h4[id], h5[id], h6[id]",
  ));
  let activeHeadingId = null;
  let headingDelay = 0;
  let scrollFrame = 0;

  const reportActiveHeading = () => {
    scrollFrame = 0;
    const probe = Math.min(160, Math.max(64, window.innerHeight * 0.18));
    let active = headings.length ? headings[0] : null;
    const atDocumentEnd = (
      window.scrollY + window.innerHeight >= document.documentElement.scrollHeight - 2
    );

    if (atDocumentEnd && headings.length) {
      active = headings[headings.length - 1];
    } else {
      let low = 0;
      let high = headings.length - 1;
      while (low <= high) {
        const middle = Math.floor((low + high) / 2);
        if (headings[middle].getBoundingClientRect().top <= probe) {
          active = headings[middle];
          low = middle + 1;
        } else {
          high = middle - 1;
        }
      }
    }

    const id = active?.id || "";
    if (id === activeHeadingId) return;
    activeHeadingId = id;
    window.webkit?.messageHandlers?.outline?.postMessage(JSON.stringify({ id }));
  };

  const scheduleActiveHeading = () => {
    if (headingDelay || scrollFrame) return;
    headingDelay = window.setTimeout(() => {
      headingDelay = 0;
      scrollFrame = window.requestAnimationFrame(reportActiveHeading);
    }, 72);
  };

  window.addEventListener("scroll", scheduleActiveHeading, { passive: true });
  window.addEventListener("resize", scheduleActiveHeading, { passive: true });
  scrollFrame = window.requestAnimationFrame(reportActiveHeading);

  const prefersReducedMotion = window.matchMedia(
    "(prefers-reduced-motion: reduce)",
  ).matches;

  let smoothScrollFrame = 0;
  let smoothScrollStart = 0;
  let smoothScrollTarget = 0;
  let smoothScrollStartedAt = 0;

  const animateSmoothScroll = (now) => {
    const progress = Math.min(1, (now - smoothScrollStartedAt) / 150);
    const eased = 1 - Math.pow(1 - progress, 3);
    window.scrollTo(0, smoothScrollStart + (smoothScrollTarget - smoothScrollStart) * eased);
    if (progress < 1) {
      smoothScrollFrame = window.requestAnimationFrame(animateSmoothScroll);
    } else {
      smoothScrollFrame = 0;
    }
  };

  const queueSmoothScroll = (delta) => {
    const maximum = Math.max(0, document.documentElement.scrollHeight - window.innerHeight);
    smoothScrollStart = window.scrollY;
    if (!smoothScrollFrame) smoothScrollTarget = window.scrollY;
    smoothScrollTarget = Math.max(0, Math.min(maximum, smoothScrollTarget + delta * 1.65));
    smoothScrollStartedAt = window.performance.now();
    if (!smoothScrollFrame) {
      smoothScrollFrame = window.requestAnimationFrame(animateSmoothScroll);
    }
  };

  const zoomBounds = (percent) => Math.max(75, Math.min(200, Number(percent) || 100));

  const currentZoom = () => {
    const target = document.body || document.documentElement;
    const value = Number.parseFloat(
      window.getComputedStyle(target).getPropertyValue("--reader-zoom"),
    );
    return zoomBounds(Math.round((Number.isFinite(value) ? value : 1) * 100));
  };

  const setZoom = (percent, anchorY = null, anchorX = null) => {
    const bounded = zoomBounds(percent);
    const target = document.body || document.documentElement;
    const viewportAnchorY = anchorY != null && Number.isFinite(Number(anchorY))
      ? Math.max(0, Math.min(window.innerHeight, Number(anchorY)))
      : window.innerHeight / 2;
    const viewportAnchorX = anchorX != null && Number.isFinite(Number(anchorX))
      ? Math.max(0, Math.min(window.innerWidth, Number(anchorX)))
      : window.innerWidth / 2;
    const anchorNode = document.elementFromPoint(viewportAnchorX, viewportAnchorY);
    const anchorBlock = anchorNode?.closest?.("[data-source-start]") || anchorNode;
    const oldRect = anchorBlock?.getBoundingClientRect?.();
    const anchorRatio = oldRect && oldRect.height > 0
      ? Math.max(0, Math.min(1, (viewportAnchorY - oldRect.top) / oldRect.height))
      : null;
    const oldHeight = document.documentElement.scrollHeight;
    const oldAnchor = window.scrollY + viewportAnchorY;

    target.style.setProperty("--reader-zoom", String(bounded / 100));

    const newRect = anchorBlock?.getBoundingClientRect?.();
    if (anchorRatio != null && newRect && newRect.height > 0) {
      const newAnchorY = newRect.top + newRect.height * anchorRatio;
      window.scrollBy({ top: newAnchorY - viewportAnchorY, behavior: "auto" });
    } else {
      const newHeight = document.documentElement.scrollHeight;
      if (oldHeight > 0 && newHeight !== oldHeight) {
        window.scrollTo({
          top: (oldAnchor / oldHeight) * newHeight - viewportAnchorY,
          behavior: "auto",
        });
      }
    }
    scheduleActiveHeading();
    return bounded;
  };

  const setTheme = (tokens) => {
    if (!tokens || typeof tokens !== "object") return;
    const target = document.documentElement;
    Object.entries(tokens).forEach(([name, value]) => {
      if (/^--[a-z0-9-]+$/.test(name) && typeof value === "string") {
        target.style.setProperty(name, value);
      }
    });
  };

  const zoomImpulse = 5;
  const zoomWheelThreshold = 24;
  let zoomWheelDelta = 0;
  let zoomWheelDirection = 0;
  let pendingZoomDelta = 0;
  let zoomFrame = 0;
  let zoomAnchorY = null;
  let zoomAnchorX = null;

  const flushZoom = () => {
    zoomFrame = 0;
    const delta = pendingZoomDelta;
    pendingZoomDelta = 0;
    if (!delta) return;

    const current = currentZoom();
    const requested = zoomBounds(current + delta);
    if (requested !== current) {
      setZoom(requested, zoomAnchorY, zoomAnchorX);
      window.webkit?.messageHandlers?.zoom?.postMessage(JSON.stringify({
        percent: requested,
        anchorY: zoomAnchorY,
      }));
    }
  };

  const scheduleZoom = () => {
    if (!zoomFrame) zoomFrame = window.requestAnimationFrame(flushZoom);
  };

  window.addEventListener("wheel", (event) => {
    const unit = event.deltaMode === WheelEvent.DOM_DELTA_LINE
      ? 28
      : event.deltaMode === WheelEvent.DOM_DELTA_PAGE
        ? window.innerHeight
        : 1;
    const delta = event.deltaY * unit;
    if (!delta) return;

    if (!event.ctrlKey) {
      const discreteWheel = (
        event.deltaMode !== WheelEvent.DOM_DELTA_PIXEL || Math.abs(delta) >= 32
      );
      if (!prefersReducedMotion && discreteWheel) {
        event.preventDefault();
        queueSmoothScroll(delta);
      }
      return;
    }
    event.preventDefault();

    const direction = delta < 0 ? 1 : -1;
    if (direction !== zoomWheelDirection) {
      zoomWheelDelta = 0;
      pendingZoomDelta = 0;
      zoomWheelDirection = direction;
    }
    const discreteWheel = (
      event.deltaMode !== WheelEvent.DOM_DELTA_PIXEL || Math.abs(delta) >= 32
    );
    let impulses = 1;
    if (discreteWheel) {
      zoomWheelDelta = 0;
    } else {
      zoomWheelDelta += Math.abs(delta);
      impulses = Math.floor(zoomWheelDelta / zoomWheelThreshold);
      if (!impulses) return;
      zoomWheelDelta %= zoomWheelThreshold;
    }
    pendingZoomDelta = Math.max(
      -20,
      Math.min(20, pendingZoomDelta + direction * zoomImpulse * impulses),
    );
    zoomAnchorY = event.clientY;
    zoomAnchorX = event.clientX;
    scheduleZoom();
  }, { passive: false });

  const motionBehavior = prefersReducedMotion
    ? "auto"
    : "smooth";

  window.mdReader = {
    setZoom,
    setTheme,
    scrollToHeading(id) {
      const target = document.getElementById(id);
      if (target) target.scrollIntoView({ block: "start", behavior: motionBehavior });
    },
    scrollToSource(line) {
      const target = Array.from(document.querySelectorAll("[data-source-start]")).find((node) => {
        const start = Number.parseInt(node.dataset.sourceStart || "0", 10);
        const end = Number.parseInt(node.dataset.sourceEnd || "0", 10);
        return start <= line && line <= end;
      });
      if (target) target.scrollIntoView({ block: "center", behavior: motionBehavior });
    },
    clearSelection() {
      window.getSelection()?.removeAllRanges();
      clearAnchors();
      reportSelection();
    },
  };
})();
