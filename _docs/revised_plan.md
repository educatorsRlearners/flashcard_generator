# Page-to-Anki Flashcard Generator — Project Spec (v2: Browser Extension)

> Supersedes the original URL-batch web-app spec. Core generation/review/feedback
> logic is unchanged — the trigger and content-extraction layers have been
> redesigned around a browser extension (Chrome or Brave) plus a local backend.

## 1. Overview
A browser extension + local backend that generates Anki flashcards from the page
you're currently viewing. Click the extension icon and the current
tab's content is extracted, turned into flashcards (Q&A and/or cloze, with
images), and opened in a review tab where you accept or reject each card before
it's pushed to a single Anki deck via AnkiConnect. Rejected cards feed back into
future generations as few-shot examples.

## 2. What Changed from v1
| Area | v1 (Web App) | v2 (Extension) |
|---|---|---|
| Trigger | Django page, paste URL(s), batch input | Extension popup, single button, current tab only |
| Batch input | Yes — list of URLs | **Removed** — one page at a time |
| Content extraction | Playwright (headless browser, handles JS-heavy pages) | Content script reads the current tab's already-rendered DOM directly |
| Failed URL handling | Skip + log reason, continue batch | **Removed** — not applicable, you're already on a loaded page |
| Progress indicator | "Processing URL 3 of 8..." | **Removed** — not applicable, single-page pipeline |
| Review UI | Django page, grid/list, accept/reject | Backend opens a full browser tab, same grid/list, accept/reject |
| Backend | Django web app (primary interface) | Django app becomes a local service the extension talks to |

Everything else — card generation rules, dedup, feedback loop, tech stack for
Claude/image-gen/Anki/storage — carries over unchanged.

## 3. Core Workflow
1. User clicks the extension icon on the page they're viewing.
2. Popup shows a single button: **"Generate cards from this page."**
3. On click, a **content script extracts the current tab's DOM** — text content
   and any images/diagrams on the page.
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
- **Source priority**: Pull existing images/diagrams from the page (now
  identified by the content script, not a Playwright fetch).
- **Fallback**: Locally generated if no usable image exists.
- **Image generation**: Local Stable Diffusion on Apple Silicon (M3), via MPS
  backend or a tool like Draw Things. No API cost.
- **Placement**: Depends on card type (e.g., cloze upfront, Q&A after-answer —
  exact rule to be refined during build).

## 6. Deduplication
*(Unchanged from v1)*
- Semantic/embedding-based similarity matching against existing deck content,
  applied before cards reach the review tab.

## 7. Review & Feedback Loop
*(Unchanged from v1, UI now served as a full tab from the backend)*
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
- **Backend**: Python + Django, now running as a **local service** the
  extension talks to via `localhost`, rather than the primary UI.
- **Content extraction**: Content script (DOM read of the active tab) —
  **replaces Playwright**; no headless browser dependency.
- **LLM (text/extraction/definitions)**: Claude API, interchangeable via
  config (OpenAI-compatible format) for provider portability.
- **Image generation**: Local Stable Diffusion on M3 Mac — fixed for MVP.
- **Database**: SQLite — cards, metadata, feedback/history.
- **Anki sync**: AnkiConnect (called from the backend; could also be called
  directly from the extension via `localhost:8765`, but routing through the
  backend keeps dedup/storage/sync in one place).

## 10. Explicitly Out of Scope (for MVP)
- Batch URL input (removed entirely — single active tab only).
- Headless browser / JS-rendering fetch of arbitrary URLs (removed —
  content script reads the tab you're already on).
- Per-page or per-topic deck creation (single deck only for now).
- Manual highlight/selection of page content (extraction is fully automatic).
- Fixed card-count targets per page.
- Rule-based/keyword-blocklist feedback filtering (using few-shot examples
  instead).
- Config-driven image-gen provider swapping (fixed to local SD for MVP).
- Knowledge-gauging / spaced-repetition-aware extraction.
- Browser Web Store publishing (unpacked/personal use only).

## 11. Open Questions for Build Phase
- Exact rule for image placement per card type (upfront vs. after-answer).
- Specific embedding model/approach for semantic dedup.
- Manifest V3 permissions needed for the content script (host permissions,
  `activeTab`, etc.) and how the popup communicates with the background
  script / backend.
- Whether AnkiConnect is called from the backend only, or also directly from
  the extension for any use case.
- Local Stable Diffusion setup details (which tool/model, resolution/quality
  tradeoffs for speed on M3).

## 12. Suggested Next Step
Once the extension architecture is implemented and stable, run a formal
code-quality/efficiency review (redundancy, dead code, over-engineering) on
the surviving backend logic — Claude prompt/generation code, dedup, SQLite
schema, and AnkiConnect integration — rather than auditing code that was
about to be restructured.