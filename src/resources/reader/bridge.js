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

  let activeHeadingId = null;
  let scrollFrame = 0;

  const reportActiveHeading = () => {
    scrollFrame = 0;
    const headings = document.querySelectorAll(
      "h1[id], h2[id], h3[id], h4[id], h5[id], h6[id]",
    );
    const probe = Math.min(160, Math.max(64, window.innerHeight * 0.18));
    let active = headings.length ? headings[0] : null;
    const atDocumentEnd = (
      window.scrollY + window.innerHeight >= document.documentElement.scrollHeight - 2
    );

    if (atDocumentEnd && headings.length) {
      active = headings[headings.length - 1];
    } else {
      for (const heading of headings) {
        if (heading.getBoundingClientRect().top > probe) break;
        active = heading;
      }
    }

    const id = active?.id || "";
    if (id === activeHeadingId) return;
    activeHeadingId = id;
    window.webkit?.messageHandlers?.outline?.postMessage(JSON.stringify({ id }));
  };

  const scheduleActiveHeading = () => {
    if (scrollFrame) return;
    scrollFrame = window.requestAnimationFrame(reportActiveHeading);
  };

  window.addEventListener("scroll", scheduleActiveHeading, { passive: true });
  window.addEventListener("resize", scheduleActiveHeading, { passive: true });
  scheduleActiveHeading();

  window.mdReader = {
    setZoom(percent) {
      const bounded = Math.max(75, Math.min(200, Number(percent) || 100));
      document.documentElement.style.setProperty("--reader-zoom", String(bounded / 100));
    },
    scrollToHeading(id) {
      const target = document.getElementById(id);
      if (target) target.scrollIntoView({ block: "start", behavior: "smooth" });
    },
    scrollToSource(line) {
      const target = Array.from(document.querySelectorAll("[data-source-start]")).find((node) => {
        const start = Number.parseInt(node.dataset.sourceStart || "0", 10);
        const end = Number.parseInt(node.dataset.sourceEnd || "0", 10);
        return start <= line && line <= end;
      });
      if (target) target.scrollIntoView({ block: "center", behavior: "smooth" });
    },
    clearSelection() {
      window.getSelection()?.removeAllRanges();
      clearAnchors();
      reportSelection();
    },
  };
})();
