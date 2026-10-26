"use strict";
(() => {
  const MISSAV_HOSTS = new Set(["missav.ws", "missav.live", "missav.ai"]);

  function codeNumber(value) {
    return String(value || "").match(/\d{6,8}/)?.[0] || "";
  }

  function directMissavURL(value, code = "") {
    try {
      const url = new URL(value, location.href);
      if (!MISSAV_HOSTS.has(url.hostname.toLowerCase())) return "";
      const match = url.pathname.match(/(?:^|\/)(?:ja\/)?fc2-ppv-(\d{6,8})(?:\/|$)/i);
      if (!match) return "";
      const expected = codeNumber(code);
      if (expected && match[1] !== expected) return "";
      return url.href;
    } catch {
      return "";
    }
  }

  function itemCode(root) {
    return (
      root?.dataset?.code ||
      root?.querySelector?.(".card-code")?.textContent ||
      root?.querySelector?.(".menu-title span")?.textContent ||
      ""
    );
  }

  function confirmedMissav(root, code) {
    if (!root?.querySelectorAll) return "";
    for (const anchor of root.querySelectorAll("a[href]")) {
      const url = directMissavURL(anchor.getAttribute("href"), code);
      if (url) return url;
    }
    return "";
  }

  function unwrapTitle(anchor) {
    if (!(anchor instanceof HTMLAnchorElement)) return;
    const replacement = document.createElement("div");
    replacement.className = anchor.className;
    replacement.textContent = anchor.textContent;
    replacement.setAttribute("aria-label", anchor.textContent || "作品タイトル");
    anchor.replaceWith(replacement);
  }

  function setAttributeIfChanged(element, name, value) {
    if (element.getAttribute(name) !== value) element.setAttribute(name, value);
  }

  function secureAnchor(anchor, url) {
    setAttributeIfChanged(anchor, "href", url);
    setAttributeIfChanged(anchor, "target", "_blank");
    setAttributeIfChanged(anchor, "rel", "noopener noreferrer");
  }

  function sanitizeCard(card) {
    const code = itemCode(card);
    const missav = confirmedMissav(card, code);
    const title = card.querySelector(".card-title");

    if (title instanceof HTMLAnchorElement) {
      if (missav) {
        secureAnchor(title, missav);
      } else {
        unwrapTitle(title);
      }
    }

    const sourceRow = card.querySelector(".source-row");
    if (sourceRow) {
      const direct = [...sourceRow.querySelectorAll("a[href]")].find((anchor) =>
        directMissavURL(anchor.getAttribute("href"), code),
      );
      // Keep the existing node (and its listeners). Replacing/cloning it on
      // every pass creates a childList mutation even when nothing changed.
      if (direct) {
        if (sourceRow.childNodes.length !== 1 || sourceRow.firstChild !== direct) {
          sourceRow.replaceChildren(direct);
        }
      } else if (sourceRow.hasChildNodes()) {
        sourceRow.replaceChildren();
      }
    }
  }

  function sanitizeMenu(dialog) {
    if (!dialog?.querySelectorAll) return;
    const code = itemCode(dialog);
    for (const anchor of [...dialog.querySelectorAll("a[href]")]) {
      const missav = directMissavURL(anchor.getAttribute("href"), code);
      if (!missav) {
        anchor.remove();
        continue;
      }
      secureAnchor(anchor, missav);
    }

    for (const heading of [...dialog.querySelectorAll(".menu-subheading")]) {
      let node = heading.nextElementSibling;
      let hasLink = false;
      while (node && !node.classList.contains("menu-subheading")) {
        if (node.matches?.("a[href]")) {
          hasLink = true;
          break;
        }
        node = node.nextElementSibling;
      }
      if (!hasLink) heading.remove();
    }
  }

  function sanitize(root = document) {
    if (!root?.querySelectorAll) return;
    root.querySelectorAll(".video-card, .ranking-card").forEach(sanitizeCard);
    const dialog = document.getElementById("menuDialog");
    if (dialog) sanitizeMenu(dialog);
  }

  let queued = false;
  const observer = new MutationObserver(() => {
    if (queued) return;
    queued = true;
    queueMicrotask(() => {
      queued = false;
      sanitizeWithoutObserving();
    });
  });

  function sanitizeWithoutObserving() {
    // Ignore our own writes. Otherwise the observer queues another microtask
    // before the browser can paint or handle a tap, starving the event loop.
    observer.disconnect();
    try {
      sanitize();
    } finally {
      observer.observe(document.documentElement, { childList: true, subtree: true });
    }
  }

  document.addEventListener(
    "click",
    (event) => {
      const anchor = event.target.closest?.("a[href]");
      if (!anchor) return;
      const card = anchor.closest(".video-card, .ranking-card, #menuDialog");
      if (!card) return;
      if (!directMissavURL(anchor.getAttribute("href"), itemCode(card))) {
        event.preventDefault();
        event.stopImmediatePropagation();
      }
    },
    true,
  );

  sanitizeWithoutObserving();
})();
