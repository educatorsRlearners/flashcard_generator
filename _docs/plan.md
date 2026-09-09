# URL-to-Anki Flashcard Generator — Project Spec (MVP)

## 1. Overview
A local web app that takes one or more URLs, extracts key vocabulary/concepts from the page content, generates Anki flashcards (Q&A and/or cloze, with images), lets you preview and approve/reject them, and pushes approved cards into a single Anki deck via AnkiConnect. Rejected cards feed back into future generations as few-shot examples so output quality improves over time.

## 2. Core Workflow
1. User submits a **batch of URLs** in the web app.
2. App shows a **live progress indicator** ("Processing URL 3 of 8... 12 cards generated so far").
3. For each URL:
   - Fetch page content (headless browser, handles JS-heavy pages).
   - Extract key terms/concepts automatically (no manual selection).
   - If a URL fails to load (blocked, 404, paywall, timeout) → **skip it, log/display the reason**, continue with the rest.
4. Generate flashcards from extracted content (see Card Generation below).
5. Run **semantic dedup** against existing deck content — skip near-duplicate terms/cards.
6. Show all generated cards from the batch in a **grid/list preview view**.
7. User reviews and clicks **Accept** or **Reject** on each card (thumbs up/down is mandatory; a rejection reason is optional free text).
8. Accepted cards are pushed to a **single Anki deck** via AnkiConnect.
9. All feedback (accept/reject + optional reason) is stored and used as **few-shot examples** in future prompts to Claude, to steer generation quality.

## 3. Card Generation
- **Content type**: Vocabulary-heavy (e.g., LLM terminology like "traces", "spans", "evals") — mostly term → definition, plus cloze deletions for terms used in context.
- **Note type**: Auto-detected per content (Basic Q&A vs. Cloze) — no fixed rule.
- **Volume**: Content-driven — no fixed card count per URL; density of the page determines how many cards are generated.
- **Definitions**: Sourced from page context **and** Claude's general knowledge (to fill gaps / write cleaner definitions).
- **Scope of extraction**: Full extraction of all notable terms/concepts — no attempt to gauge what the user already knows. Cleanup happens manually via reject or later deck edits.

## 4. Visuals
- **Source priority**: Pull existing images/diagrams from the source page first.
- **Fallback**: If no usable image exists, generate one locally.
- **Image generation**: Local Stable Diffusion on Apple Silicon (M3), via MPS backend or a tool like Draw Things. No API cost, unlimited generations; ~10–30s per image.
- **Placement**: Depends on card type — e.g., cloze cards may show the image upfront, Q&A cards show it after the answer (exact rule to be refined during build).

## 5. Deduplication
- **Method**: Semantic/embedding-based similarity matching against existing deck content (not just exact string match) — catches variants like "trace" vs. "traces".
- **Scope**: Applied at generation time, before cards reach the preview/approval stage.

## 6. Review & Feedback Loop
- **Review UI**: Grid/list of all generated cards from a batch, each with inline **Accept** / **Reject** buttons (no modal, no one-at-a-time flow).
- **Feedback captured**: Thumbs up/down (mandatory) + optional free-text reason (e.g., "too trivial," "wrong definition," "bad image").
- **Storage**: SQLite (local, simple, inspectable) — stores card content, source metadata, and feedback history.
- **Learning mechanism**: Rejected/approved cards (and reasons, when given) are injected into the Claude prompt as **few-shot examples** in future generation runs, so the model learns to avoid patterns you've rejected.

## 7. Anki Integration
- **Sync method**: AnkiConnect (local add-on, HTTP API to the running Anki app) — no manual file export/import.
- **Deck structure**: Single deck for all cards (MVP simplicity; per-topic/per-URL decks are a future enhancement).
- **Tagging**: Each card is tagged with metadata — source URL, date added, topic — for filtering/searching later in Anki.

## 8. Tech Stack
- **Backend/Web app**: Python + Django (local web app, not a browser extension for MVP).
- **Content extraction**: Headless browser (e.g., Playwright) to handle JS-heavy/dynamically rendered pages.
- **LLM (text/extraction/definitions)**: Claude API — user already has an API key.
- **LLM provider portability**: Interchangeable via config (OpenAI-compatible API format), so swapping providers/models later is a config change, not a code change.
- **Image generation**: Local Stable Diffusion on M3 Mac (MPS backend or Draw Things) — fixed for MVP, not config-driven (unlike text LLM).
- **Database**: SQLite — stores cards, metadata, and feedback/history for the few-shot learning loop.
- **Anki sync**: AnkiConnect.

## 9. Explicitly Out of Scope (for MVP)
- Browser extension interface (considered, deferred — local web app is faster to build and the extension would still need a local backend for LLM/image-gen/AnkiConnect calls anyway).
- Per-URL or per-topic deck creation (single deck only for now).
- Manual highlight/selection of page content (extraction is fully automatic).
- Fixed card-count targets per URL.
- Rule-based/keyword-blocklist feedback filtering (using few-shot examples instead).
- Config-driven image-gen provider swapping (fixed to local SD for MVP).
- Knowledge-gauging / spaced-repetition-aware extraction (skip terms user "already knows").

## 10. Open Questions for Build Phase
- Exact rule for image placement per card type (upfront vs. after-answer).
- Specific embedding model/approach for semantic dedup.
- Django project structure and whether background/async processing (e.g., Celery) is needed for batch URL processing and progress updates.
- Local Stable Diffusion setup details (which tool/model, resolution/quality tradeoffs for speed on M3).