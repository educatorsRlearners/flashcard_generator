// extension/content_extract.js
//
// Content-extraction step injected into the active tab (see #39). Turns the
// live, already-rendered DOM into {title, text} for the card-generation
// pipeline, using Mozilla's Readability.js (vendored at extension/lib/,
// see extension/lib/README.md for the pinned version/commit and license).
//
// Readability.js must be loaded into the page/execution context before this
// script runs (e.g. as a separate chrome.scripting.executeScript file, or a
// <script> tag), so the global `Readability` constructor is available.

/**
 * Extract {title, text, images} from the current document.
 *
 * Runs a bounded lazy-image reveal pass first (see revealLazyImages),
 * then tries Readability.js (on a clone of `document`, so DOM mutations
 * from parsing don't affect the live page). Falls back to a bare
 * document.title / document.body.innerText grab if Readability's parse()
 * returns null, throws, or is unavailable. Always resolves to a result
 * and never throws/rejects out to its caller.
 *
 * `images` holds candidate image URLs scoped to Readability's parsed
 * article content (`article.content`), in DOM order, deduplicated and
 * capped (see MAX_IMAGE_CANDIDATES below). The image-collecting step is
 * fail-soft: any failure degrades to an empty array. No marker-based or
 * usability filtering happens here — that stays server-side.
 *
 * Async because the pre-collection reveal pass waits briefly between
 * scroll steps so IntersectionObserver-driven lazy images can populate
 * their attributes before the post-scroll DOM is cloned.
 *
 * @returns {Promise<{title: string, text: string, images: string[]}>}
 */
const MAX_IMAGE_CANDIDATES = 25;

// Bounded lazy-reveal pass (#50): at most LAZY_SCROLL_MAX_STEPS viewport
// hops, LAZY_SCROLL_STEP_WAIT_MS per hop, so the total added wait is at
// most LAZY_SCROLL_MAX_STEPS * LAZY_SCROLL_STEP_WAIT_MS = 10 * 150ms =
// 1.5s (within the ~2s cap). Stops early at page bottom or when there is
// no scrollable overflow. Leaves nothing behind: no listeners, observers,
// timers (each step's setTimeout resolves before the next step), or DOM
// mutations — only transient window.scrollTo calls, restored afterwards.
const LAZY_SCROLL_MAX_STEPS = 10;
const LAZY_SCROLL_STEP_WAIT_MS = 150;

/**
 * Read one <img>'s candidate URL, mirroring the attribute order
 * images.py::_ImgTagCollector already checks server-side.
 *
 * @param {Element} img
 * @returns {string} raw attribute value (possibly relative) or "".
 */
function rawImageSource(img) {
  const pick = (name) => (img.getAttribute(name) || "").trim();
  return (
    pick("src") ||
    pick("data-src") ||
    pick("data-original") ||
    pick("data-lazy-src") ||
    firstSrcsetUrl(pick("srcset"))
  );
}

/**
 * First URL of a srcset attribute value ("url 2x, url2 1x" -> "url").
 *
 * @param {string} srcset
 * @returns {string}
 */
function firstSrcsetUrl(srcset) {
  if (!srcset) {
    return "";
  }
  const first = srcset.split(",", 1)[0].trim();
  return first.split(/\s+/, 1)[0].trim();
}

/**
 * Collect candidate image URLs from Readability's article HTML.
 *
 * Parses `articleHtml` into a detached container (never the live page),
 * resolves each <img> URL against `baseURI`, drops `data:` URIs,
 * deduplicates preserving DOM order, and caps the result. Never throws:
 * any failure yields [].
 *
 * @param {string} articleHtml Readability article.content HTML.
 * @param {string} baseURI base against which to resolve relative URLs.
 * @returns {string[]}
 */
function collectArticleImages(articleHtml, baseURI) {
  try {
    if (!articleHtml) {
      return [];
    }
    const container = document.createElement("div");
    container.innerHTML = articleHtml;
    const seen = new Set();
    const out = [];
    const imgs = container.querySelectorAll("img");
    for (const img of imgs) {
      let raw = "";
      try {
        raw = rawImageSource(img);
      } catch (err) {
        continue;
      }
      if (!raw || raw.toLowerCase().startsWith("data:")) {
        continue;
      }
      let absolute = "";
      try {
        absolute = new URL(raw, baseURI).href;
      } catch (err) {
        continue;
      }
      if (!absolute || absolute.toLowerCase().startsWith("data:")) {
        continue;
      }
      if (seen.has(absolute)) {
        continue;
      }
      seen.add(absolute);
      out.push(absolute);
      if (out.length >= MAX_IMAGE_CANDIDATES) {
        break;
      }
    }
    return out;
  } catch (err) {
    return [];
  }
}

/**
 * Bounded scroll-and-wait pass that gives IntersectionObserver-driven
 * lazy images a chance to populate `src`/`data-*`/`srcset` before
 * collection runs on the post-scroll DOM.
 *
 * - Scrolls at most LAZY_SCROLL_MAX_STEPS viewport hops, waiting
 *   LAZY_SCROLL_STEP_WAIT_MS after each hop; stops early at the page
 *   bottom or when there is no scrollable overflow.
 * - Restores the pre-pass scroll x/y via window.scrollTo before
 *   returning, on both success and failure paths.
 * - Fail-soft: any failure (detached/weird DOM, no scrolling API,
 *   scrollTo throwing) degrades to a no-op so the caller can run
 *   today's immediate-collection behavior. Never throws, never leaves
 *   listeners/observers/timers/DOM mutations behind.
 *
 * @returns {Promise<void>}
 */
async function revealLazyImages() {
  let origX = 0;
  let origY = 0;
  let haveOrig = false;
  let didScroll = false;
  try {
    if (typeof window === "undefined" || typeof document === "undefined") {
      return;
    }
    try {
      origX =
        typeof window.scrollX === "number"
          ? window.scrollX
          : typeof window.pageXOffset === "number"
            ? window.pageXOffset
            : 0;
      origY =
        typeof window.scrollY === "number"
          ? window.scrollY
          : typeof window.pageYOffset === "number"
            ? window.pageYOffset
            : 0;
    } catch (err) {
      origX = 0;
      origY = 0;
    }
    haveOrig = true;

    const docEl = document.documentElement || null;
    const body = (document.body || null);
    if (!docEl && !body) {
      return;
    }
    const viewport =
      (typeof window.innerHeight === "number" && window.innerHeight > 0
        ? window.innerHeight
        : 0) ||
      (docEl && docEl.clientHeight) ||
      0;
    if (!viewport) {
      return;
    }
    const scrollHeight = () => {
      try {
        return Math.max(
          docEl ? docEl.scrollHeight || 0 : 0,
          body ? body.scrollHeight || 0 : 0,
        );
      } catch (err) {
        return 0;
      }
    };
    if (scrollHeight() <= viewport + 1) {
      return; // No scrollable overflow: extract as before.
    }
    if (typeof window.scrollTo !== "function") {
      return;
    }
    const canWait =
      typeof setTimeout === "function" &&
      typeof Promise !== "undefined";
    const wait = (ms) =>
      canWait
        ? new Promise((resolve) => setTimeout(resolve, ms))
        : Promise.resolve();

    for (let i = 1; i <= LAZY_SCROLL_MAX_STEPS; i++) {
      let targetY = 0;
      try {
        targetY = Math.max(
          0,
          Math.min(i * viewport, scrollHeight() - viewport),
        );
        window.scrollTo(0, targetY);
        didScroll = true;
      } catch (err) {
        return; // scrollTo unavailable/broken: degrade to no-scroll.
      }
      try {
        await wait(LAZY_SCROLL_STEP_WAIT_MS);
      } catch (err) {
        return;
      }
      // Early stop once the bottom is reached (recompute: lazy content
      // may have grown the page mid-pass).
      try {
        const curY =
          typeof window.scrollY === "number"
            ? window.scrollY
            : typeof window.pageYOffset === "number"
              ? window.pageYOffset
              : targetY;
        if (curY + viewport >= scrollHeight() - 2) {
          break;
        }
      } catch (err) {
        break;
      }
    }
  } catch (err) {
    // Fail-soft: fall through to scroll restoration.
  } finally {
    if (haveOrig && didScroll && typeof window !== "undefined") {
      try {
        if (typeof window.scrollTo === "function") {
          window.scrollTo(origX, origY);
        }
      } catch (err) {
        // Restoration is best-effort; never throw.
      }
    }
  }
}
async function extractPageContent() {
  try {
    try {
      await revealLazyImages();
    } catch (err) {
      // Reveal is fail-soft: fall through to immediate collection.
    }
  } catch (err) {
    // Belt-and-braces: never let the reveal pass break extraction.
  }
  try {
    const clone = document.cloneNode(true);
    const article = new Readability(clone).parse();
    if (article) {
      return {
        title: article.title || document.title || "",
        text: article.textContent || "",
        images: collectArticleImages(article.content || "", document.baseURI),
      };
    }
  } catch (err) {
    // Fall through to the innerText fallback below.
  }

  return {
    title: document.title || "",
    text: (document.body && document.body.innerText) || "",
    images: [],
  };
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    extractPageContent,
    revealLazyImages,
    collectArticleImages,
    rawImageSource,
    firstSrcsetUrl,
    MAX_IMAGE_CANDIDATES,
    LAZY_SCROLL_MAX_STEPS,
    LAZY_SCROLL_STEP_WAIT_MS,
  };
}
