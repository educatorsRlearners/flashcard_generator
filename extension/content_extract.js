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
 * Tries Readability.js first (on a clone of `document`, so DOM mutations
 * from parsing don't affect the live page). Falls back to a bare
 * document.title / document.body.innerText grab if Readability's parse()
 * returns null, throws, or is unavailable. Always returns a result and
 * never throws out to its caller.
 *
 * `images` holds candidate image URLs scoped to Readability's parsed
 * article content (`article.content`), in DOM order, deduplicated and
 * capped (see MAX_IMAGE_CANDIDATES below). The image-collecting step is
 * fail-soft: any failure degrades to an empty array. No marker-based or
 * usability filtering happens here — that stays server-side.
 *
 * @returns {{title: string, text: string, images: string[]}}
 */
const MAX_IMAGE_CANDIDATES = 25;

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

function extractPageContent() {
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
    collectArticleImages,
    rawImageSource,
    firstSrcsetUrl,
    MAX_IMAGE_CANDIDATES,
  };
}
