# Page-to-Anki Flashcard Generator — Project Spec (v2: Browser Extension)

> Supplements (not supersedes) the original URL-batch web-app spec. Core generation/review/feedback
> logic is unchanged — a browser extension (Chrome or Brave) plus a local backend
> was added as a second, parallel entry point alongside the original Django
> pasted-URL web UI (`submissions/views.py:home`, `submissions/forms.py:URLSubmissionForm`),
> which still accepts a pasted list of URLs unchanged.

## 1. Overview
A browser extension + local backend that generates Anki flashcards from the page
you're currently viewing, alongside the unchanged pasted-URL batch web UI.
Click the extension icon, then the popup's single button — with no
confirmation dialog in between (`extension/popup.js` has no `confirm()` call
anywhere, per #39) — and the current
tab's content is extracted, turned into flashcards (Q&A and/or cloze, with
images), and opened in a review tab where you accept or reject each card before
it's pushed to a single Anki deck via AnkiConnect. Rejected cards feed back into
future generations as few-shot examples.

## 2. What Changed from v1
| Area | v1 (Web App) | v2 (Extension) |
|---|---|---|
| Trigger | Django page, paste URL(s), batch input | Both coexist: unchanged Django pasted-URL batch UI (`submissions/views.py:home`) plus extension popup, single button, current tab only (`extension/popup.js`, `submissions/extension_api.py:submit`) |
| Batch input | Yes — list of URLs (`submissions/forms.py:URLSubmissionForm`, `submissions/views.py:home` create `Batch`/`SubmittedURL`/`BatchRequest`) | Both input modes coexist: pasted-URL batch input unchanged; the extension adds a single-page path alongside it, it does not replace it |
| Content extraction | Static fetch + trafilatura, Playwright headless-Chromium fallback, plus document/OCR paths (`submissions/extraction.py`) — still serves the pasted-URL web path, unchanged | Content script (`extension/content_extract.js` + `extension/lib/Readability.js`, #40) used only for extension submissions; the `submissions/extraction.py` static/trafilatura/Playwright stack is still used, unchanged, for the pasted-URL web path |
| Failed URL handling | Skip + log reason, continue batch | Unchanged for the pasted-URL web path; not applicable to the extension path (you're already on a loaded page) |
| Progress indicator | "Processing URL 3 of 8..." | Unchanged for the pasted-URL web path; not applicable to the extension single-page pipeline (which polls `GET /api/extension/submit/<id>/status/` instead, see `extension/popup.js:pollStatus`) |
| Review UI | Django page, grid/list, accept/reject | Extension path: backend returns a `review_url` that the popup opens as a full browser tab (`extension/popup.js`, `submissions/extension_api.py:submission_status`), same grid/list, accept/reject |
| Backend | Django web app (primary interface) | Django app gains a JSON API (`submissions/extension_api.py`) the extension talks to, alongside its existing role as the pasted-URL web UI (`submissions/views.py:home`) |

Everything else — card generation rules, dedup, feedback loop, tech stack for
Claude/image-gen/Anki/storage — carries over unchanged.

## 3. Core Workflow
1. User clicks the extension icon on the page they're viewing.
2. Popup shows a single button: **"Generate cards from this page."**
3. On click, a **content script extracts the current tab's DOM** — `{title, text, images}`
   (`extension/content_extract.js`, #40 plus #42): Readability article text plus
   candidate image URLs scoped to the parsed article content (capped at
   `MAX_IMAGE_CANDIDATES`, fail-soft to `[]`), sent alongside the title/text.
   (Note: this corrects the #45-drafted wording that said `{title, text}` only
   with image extraction deferred to #42 — #42 has since landed, so the
   content script already returns `images` and `submit()` in
   `submissions/extension_api.py` persists them as `extension_image_urls`.)
4. Extension sends the extracted content to the **local Django backend**
   (`localhost`).
5. Backend runs the generation pipeline:
   - Extracts key terms/concepts automatically (no manual selection).
   - Generates cards (see Card Generation below).
   - Runs **semantic dedup** against existing deck content.
6. Backend opens a **full browser tab** with a grid/list preview of all
   generated cards.
7. User reviews and clicks **Accept** or **Reject** on each card (thumbs
   up/down mandatory; rejection reason optional free text).
8. Accepted cards are pushed to a **single Anki deck** via AnkiConnect.
9. All feedback (accept/reject + optional reason) is stored and used as
   **few-shot examples** in future prompts to Claude.

## 4. Card Generation
*(Unchanged from v1)*
- **Content type**: Vocabulary-heavy (e.g., LLM terminology like "traces",
  "spans", "evals") — mostly term → definition, plus cloze deletions for terms
  used in context.
- **Note type**: Auto-detected per content (Basic Q&A vs. Cloze).
- **Volume**: Content-driven — no fixed card count per page.
- **Definitions**: Sourced from page context **and** Claude's general
  knowledge.
- **Scope of extraction**: Full extraction of all notable terms/concepts —
  cleanup happens via reject or later deck edits.

## 5. Visuals
*(Unchanged from v1)*
- **Source priority**: Pull existing images/diagrams from the page via the merged
  pipeline in `submissions/images.py:_merged_candidates` (#42, landed):
  extension-submitted candidate URLs (content script, live DOM — the only
  candidates on authenticated / JS-rendered pages) first, followed by
  server-side re-fetch candidates (`submissions/images.py` re-fetches the
  submitted URL's HTML reusing the Playwright/trafilatura extraction fetch
  stack and collects `<img>` URLs), both filtered by the same
  `_filter_absolute_candidates` exclusions, deduplicated and capped. (Note:
  this corrects the #45-drafted wording that described content-script images
  as deferred to #42 — #42 has landed, so both candidate sources are live
  for the extension path alike; the pasted-URL path uses the server-refetch
  list only.)
- **Fallback**: Locally generated via Draw Things (`submissions/images.py:DrawThingsClient`)
  when no usable page image exists; otherwise no image (never an error state).
- **Image generation**: Local Stable Diffusion on Apple Silicon (M3), via MPS
  backend or a tool like Draw Things. No API cost.
- **Placement**: Depends on card type (e.g., cloze upfront, Q&A after-answer —
  exact rule to be refined during build).

## 6. Deduplication
*(Unchanged from v1)*
- Semantic/embedding-based similarity matching against existing deck content,
  applied before cards reach the review tab.

## 7. Review & Feedback Loop
*(Unchanged from v1; the extension path additionally serves the review UI as a full tab opened from the `review_url` the backend returns — see §2)*
- **Review UI**: Full browser tab opened by the backend, grid/list of
  generated cards with inline Accept/Reject buttons.
- **Feedback captured**: Thumbs up/down (mandatory) + optional free-text
  reason.
- **Storage**: SQLite — cards, source metadata, feedback history.
- **Learning mechanism**: Rejected/approved cards (and reasons) injected into
  the Claude prompt as few-shot examples in future runs.

## 8. Anki Integration
*(Unchanged from v1)*
- **Sync method**: AnkiConnect.
- **Deck structure**: Single deck (MVP simplicity).
- **Tagging**: Source URL, date added, topic.

## 9. Tech Stack
- **Extension**: Chrome/Brave extension (Manifest V3) — popup UI + content
  script. Loaded unpacked for personal use; no store publishing required.
- **Backend**: Python + Django, still the pasted-URL web UI
  (`submissions/views.py:home`) and now also the extension's backend via a JSON
  API (`submissions/extension_api.py`: `POST /api/extension/submit/` +
  `GET /api/extension/submit/<id>/status/`, bearer-token auth) at `localhost`.
  Bootstrap: the native messaging host (`native_host/host.py`, #37) that Chrome
  launches via `chrome.runtime.connectNative` to auto-start the backend
  (`uv run python manage.py dev` if not already up) and hand the extension its
  auth token (`token` + `base_url` reply) before any HTTP call happens
  (see `extension/popup.js:connectNativeHost`).
- **Content extraction**: Two coexisting routes — `submissions/extraction.py`
  (static fetch + trafilatura, Playwright headless-Chromium fallback, plus
  document/OCR paths) still serves the pasted-URL web UI, and the content
  script (`extension/content_extract.js`, `extension/lib/Readability.js`,
  DOM read of the active tab, #40) is the extension's separate route —
  added alongside, not a replacement for Playwright.
- **LLM (text/extraction/definitions)**: Claude API, interchangeable via
  config (OpenAI-compatible format) for provider portability.
- **Image generation**: Local Stable Diffusion on M3 Mac — fixed for MVP.
- **Database**: SQLite — cards, metadata, feedback/history.
- **Anki sync**: AnkiConnect (called from the backend; could also be called
  directly from the extension via `localhost:8765`, but routing through the
  backend keeps dedup/storage/sync in one place).

## 10. Explicitly Out of Scope (for MVP)
- Per-page or per-topic deck creation (single deck only for now).
- Manual highlight/selection of page content (extraction is fully automatic).
- Fixed card-count targets per page.
- Rule-based/keyword-blocklist feedback filtering (using few-shot examples
  instead).
- Config-driven image-gen provider swapping (fixed to local SD for MVP).
- Knowledge-gauging / spaced-repetition-aware extraction.
- Browser Web Store publishing (unpacked/personal use only).

## 11. Open Questions for Build Phase
- Exact rule for image placement per card type (upfront vs. after-answer). [Still open — placement itself is implemented per note type as `Card.image_placement` (see `submissions/images.py` module docstring), but the per-type rule may still be refined.]
- Specific embedding model/approach for semantic dedup. [Still open.]
- ~~Manifest V3 permissions needed for the content script (host permissions,
  `activeTab`, etc.) and how the popup communicates with the background
  script / backend.~~ **Answered by #37/#39/#40:** permissions are
  `nativeMessaging`, `activeTab`, `scripting`, `tabs` plus host permission
  `http://127.0.0.1:8000/*` (`extension/manifest.json`); there is no
  background script — the popup calls `chrome.runtime.connectNative` (native
  host bootstrap), `chrome.scripting.executeScript` (Readability + content
  script injection), `fetch` (extension JSON API), and `chrome.tabs.create`
  (review tab) directly (`extension/popup.js`, `native_host/host.py`,
  `submissions/extension_api.py`).
- ~~Whether AnkiConnect is called from the backend only, or also directly from
  the extension for any use case.~~ **Answered:** backend only — the extension
  never calls AnkiConnect; accepted cards are pushed server-side
  (`uv run python manage.py push_to_anki` via AnkiConnect; see §9 Tech Stack).
- Local Stable Diffusion setup details (which tool/model, resolution/quality
  tradeoffs for speed on M3). [Still open — current answer is the Draw Things
  local HTTP API client (`submissions/images.py:DrawThingsClient`), but
  model/resolution tradeoffs are untuned.]

## 12. Suggested Next Step
With both entry points (pasted-URL web UI and extension) implemented and stable, run a formal
code-quality/efficiency review (redundancy, dead code, over-engineering) on
the backend logic — Claude prompt/generation code, dedup, SQLite
schema, and AnkiConnect integration.