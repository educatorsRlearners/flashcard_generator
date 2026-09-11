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
 * Extract {title, text} from the current document.
 *
 * Tries Readability.js first (on a clone of `document`, so DOM mutations
 * from parsing don't affect the live page). Falls back to a bare
 * document.title / document.body.innerText grab if Readability's parse()
 * returns null, throws, or is unavailable. Always returns a result and
 * never throws out to its caller.
 *
 * @returns {{title: string, text: string}}
 */
function extractPageContent() {
  try {
    const clone = document.cloneNode(true);
    const article = new Readability(clone).parse();
    if (article) {
      return {
        title: article.title || document.title || "",
        text: article.textContent || "",
      };
    }
  } catch (err) {
    // Fall through to the innerText fallback below.
  }

  return {
    title: document.title || "",
    text: (document.body && document.body.innerText) || "",
  };
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = { extractPageContent };
}
