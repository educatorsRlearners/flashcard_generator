"""Per-card images (issue #12).

Every generated :class:`~submissions.models.Card` gets **at most one**
image:

1. a *usable* image pulled from the card's source page, if one exists;
2. otherwise a locally generated image from **Draw Things**;
3. otherwise no image at all - a card is never left in an error state
   because image work failed.

Placement in the #9 review grid follows the note type and is expressed by
:attr:`Card.image_placement` (cloze -> question side, Basic -> answer
side); this module only stores ``image`` / ``image_source`` on the card.

Everything that touches the network goes through a small, replaceable
seam so the test suite runs offline with no Draw Things:

* :func:`_fetch_page_html` - re-fetch the source page HTML (reuses the
  #3 / #17 extraction fetch stack);
* :func:`_fetch_image` - download one candidate image;
* :class:`DrawThingsClient` - the only thing that talks to Draw Things.
"""

from __future__ import annotations

import base64
import io
import logging
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import httpx
from django.conf import settings
from django.core.files.base import ContentFile
from PIL import Image, UnidentifiedImageError

from submissions import extraction
from submissions.models import Card

logger = logging.getLogger(__name__)

# --- "Usable image" rules (documented, applied consistently) -----------
#
# An image taken from the source page must clear *all* of these. The
# thresholds are deliberately conservative: the goal is to exclude
# tracking pixels, spacer GIFs, sprite sheets, icons, logos and
# data-URI favicons, not to rank images by relevance (out of scope).

#: Minimum rendered width / height in pixels. Anything smaller is an
#: icon, bullet, spacer or tracking pixel, not content.
MIN_IMAGE_WIDTH = 200
MIN_IMAGE_HEIGHT = 200
#: Minimum encoded size in bytes. 1x1 tracking pixels and spacer GIFs are
#: well under 1 KB even before the dimension check.
MIN_IMAGE_BYTES = 1024
#: Upper bound on a downloaded image (guards memory / disk).
MAX_IMAGE_BYTES = 8 * 1024 * 1024
#: Never consider more than this many candidate images from one page.
MAX_IMAGE_CANDIDATES = 25
#: Bounded per-image HTTP timeout (seconds). Image work for one card must
#: not block the rest of the batch beyond this.
IMAGE_FETCH_TIMEOUT = 10.0
#: Bounded timeout (seconds) for a single Draw Things generation call.
DRAW_THINGS_TIMEOUT = 45.0

#: Substrings in an image URL / path that mark it as non-content chrome
#: (icons, logos, sprites, spacers, tracking, ads). Matched case-insensitively.
EXCLUDE_URL_MARKERS = (
    "favicon",
    "sprite",
    "spacer",
    "blank.gif",
    "pixel",
    "1x1",
    "transparent",
    "tracking",
    "track.",
    "beacon",
    "analytics",
    "doubleclick",
    "/ads/",
    "/ad-",
    "logo",
    "icon",
    "avatar",
    "badge",
    "button",
    "emoji",
)
#: Raster content types we accept. SVG is excluded on purpose (usually an
#: icon / logo and has no intrinsic pixel size).
ALLOWED_IMAGE_CONTENT_TYPES = {
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/gif",
}
_RASTER_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp", ".gif")
_PIL_FORMAT_TO_EXT = {
    "JPEG": ".jpg",
    "PNG": ".png",
    "WEBP": ".webp",
    "GIF": ".gif",
}


@dataclass
class FetchedImage:
    """A downloaded candidate image."""

    content: bytes
    content_type: str = ""


@dataclass
class ImageOutcome:
    """Result of choosing an image for one card."""

    data: bytes | None
    source: str  # a ``Card.ImageSource`` value

    @property
    def has_image(self) -> bool:
        return self.data is not None


# --- Candidate discovery ----------------------------------------------


class _ImgTagCollector(HTMLParser):
    """Collect ``<img>`` (and lazy-load) source URLs from page HTML."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.sources: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag not in ("img", "source"):
            return
        d = {k.lower(): (v or "") for k, v in attrs}
        raw = (
            d.get("src")
            or d.get("data-src")
            or d.get("data-original")
            or d.get("data-lazy-src")
            or _first_srcset_url(d.get("srcset", ""))
        )
        if not raw:
            return
        # Drop obvious non-content by declared dimensions.
        if _too_small_dim(d.get("width")) or _too_small_dim(d.get("height")):
            return
        self.sources.append(raw.strip())


def _first_srcset_url(srcset: str) -> str:
    if not srcset:
        return ""
    first = srcset.split(",", 1)[0].strip()
    return first.split(" ", 1)[0].strip()


def _too_small_dim(value: str | None) -> bool:
    if not value:
        return False
    try:
        return int(str(value).strip().rstrip("px") or "0") < min(
            MIN_IMAGE_WIDTH, MIN_IMAGE_HEIGHT
        )
    except ValueError:
        return False


def _looks_excluded(url: str) -> bool:
    low = url.lower()
    return any(marker in low for marker in EXCLUDE_URL_MARKERS)


def _filter_absolute_candidates(urls: list[str]) -> list[str]:
    """Apply the cheap exclusion rules to already-absolute candidate URLs.

    Shared by :func:`image_candidates` (server-refetched HTML) and
    :func:`attach_images` (extension-submitted URLs from issue #42), so
    both sources are filtered by one consistent set of rules: only
    ``http(s)`` URLs survive, chrome-marker URLs (favicons, logos, ads,
    ...) and non-raster extensions are dropped. De-duplicated, DOM order
    preserved, uncapped - callers apply :data:`MAX_IMAGE_CANDIDATES`.
    Never raises on weird input.
    """
    seen: set[str] = set()
    out: list[str] = []
    for url in urls or []:
        try:
            if not isinstance(url, str) or not url:
                continue
            absolute = url.strip()
            if not absolute.lower().startswith(("http://", "https://")):
                continue
            if _looks_excluded(absolute):
                continue
            path = urlsplit(absolute).path.lower()
            if "." in path.rsplit("/", 1)[-1] and not path.endswith(
                _RASTER_SUFFIXES
            ):
                continue
            if absolute in seen:
                continue
            seen.add(absolute)
            out.append(absolute)
        except Exception:  # noqa: BLE001 - one bad URL never fails the batch
            logger.debug("image: skipping bad candidate URL %r", url)
            continue
    return out


def image_candidates(html: str | None, base_url: str) -> list[str]:
    """Ordered, de-duplicated list of absolute candidate image URLs from
    *html*, cheapest exclusions applied (data URIs, chrome markers,
    non-raster extensions). Capped at :data:`MAX_IMAGE_CANDIDATES`.
    """
    if not html:
        return []
    collector = _ImgTagCollector()
    try:
        collector.feed(html)
    except Exception:  # noqa: BLE001 - never fail on malformed markup
        logger.debug("image: HTML parse of %s failed", base_url, exc_info=True)

    absolute_urls: list[str] = []
    for raw in collector.sources:
        if not raw or raw.startswith("data:"):
            continue
        absolute_urls.append(urljoin(base_url, raw))
    return _filter_absolute_candidates(absolute_urls)[:MAX_IMAGE_CANDIDATES]


# --- Relevance ranking (issue #48) --------------------------------------


class _RankingContextCollector(HTMLParser):
    """Collect hero meta URLs + per-image alt / declared size from HTML.

    Pure offline parse: records ``og:image`` / ``twitter:image`` contents
    in document order and, for each ``<img>`` with a resolvable ``src``,
    its ``alt`` text and declared ``width x height`` area. Never raises
    out to callers (malformed markup is skipped).
    """

    #: Meta names/properties treated as the hero-image signal.
    HERO_META_KEYS = frozenset(
        {"og:image", "twitter:image", "twitter:image:src"}
    )

    def __init__(self, base_url: str = "") -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url or ""
        self.hero_raw: list[str] = []
        # absolute URL -> {"alt": str, "area": int}; first occurrence wins.
        self.infos: dict[str, dict] = {}

    def handle_starttag(self, tag, attrs):
        try:
            if tag == "meta":
                d = {k.lower(): (v or "") for k, v in attrs}
                key = (d.get("property") or d.get("name") or "").strip().lower()
                if key in self.HERO_META_KEYS and d.get("content", "").strip():
                    self.hero_raw.append(d["content"].strip())
                return
            if tag != "img":
                return
            d = {k.lower(): (v or "") for k, v in attrs}
            raw = (
                d.get("src")
                or d.get("data-src")
                or d.get("data-original")
                or d.get("data-lazy-src")
                or _first_srcset_url(d.get("srcset", ""))
            )
            if not raw or raw.strip().startswith("data:"):
                return
            try:
                absolute = urljoin(self.base_url, raw.strip())
            except Exception:  # noqa: BLE001 - one bad URL never fails parse
                return
            if absolute in self.infos:
                return
            self.infos[absolute] = {
                "alt": d.get("alt", ""),
                "area": _declared_area(d.get("width"), d.get("height")),
            }
        except Exception:  # noqa: BLE001 - ranking parse never raises
            logger.debug("image: ranking parse skipped a tag", exc_info=True)


def _declared_area(width: str | None, height: str | None) -> int:
    """Declared ``width x height`` area, or 0 when missing/unparseable.

    Never excludes: unparseable or missing dimensions simply score 0.
    """
    try:
        w = int(str(width or "").strip().rstrip("px") or "0")
        h = int(str(height or "").strip().rstrip("px") or "0")
    except (ValueError, TypeError):
        return 0
    if w <= 0 or h <= 0:
        return 0
    return w * h


def _term_tokens(source_term: str | None, front: str | None) -> list[str]:
    """Lowercase alphanumeric tokens (len >= 3) from term/front text."""
    text = f"{source_term or ''} {front or ''}".lower()
    return [t for t in re.findall(r"[a-z0-9]+", text) if len(t) >= 3]


def rank_image_candidates(
    candidates,
    *,
    html: str | None = None,
    base_url: str = "",
    source_term: str = "",
    front: str = "",
) -> list[str]:
    """Reorder *candidates* by relevance, best first (issue #48).

    Pure and deterministic: no network, no DB, no settings reads. Ties
    keep input (DOM/merge) order via a stable sort.

    Signal priority (fixed): hero ``og:image`` / ``twitter:image`` >
    term-match (card token in image ``alt`` or URL slug) >
    larger declared ``width x height`` area (missing = 0) > DOM order.

    Reorders only - never admits or rejects: every valid string in
    *candidates* appears exactly once in the output. Fail-soft: any
    problem degrades to the input order; ``None`` / malformed entries
    never raise (non-string / empty entries are dropped).
    """
    try:
        if not candidates:
            return []
        # Preserve input order; drop non-string / empty entries.
        ordered: list[str] = []
        for url in candidates:
            try:
                if isinstance(url, str) and url.strip():
                    ordered.append(url)
            except Exception:  # noqa: BLE001 - one bad entry never fails rank
                continue
        if len(ordered) <= 1:
            return ordered

        hero_urls: set[str] = set()
        infos: dict[str, dict] = {}
        try:
            collector = _RankingContextCollector(base_url or "")
            if html:
                collector.feed(html)
            for raw in collector.hero_raw:
                try:
                    hero_urls.add(urljoin(base_url or "", raw))
                except Exception:  # noqa: BLE001 - skip one bad hero URL
                    continue
            infos = collector.infos
        except Exception:  # noqa: BLE001 - parse failure -> no hero/size info
            logger.debug("image: ranking HTML parse failed", exc_info=True)
            hero_urls = set()
            infos = {}

        try:
            tokens = _term_tokens(source_term, front)
        except Exception:  # noqa: BLE001 - bad card text -> no term signal
            tokens = []

        def _term_match(url: str) -> bool:
            if not tokens:
                return False
            try:
                alt = str((infos.get(url) or {}).get("alt") or "").lower()
                slug = urlsplit(url).path.lower()
            except Exception:  # noqa: BLE001 - malformed URL -> no match
                return False
            for tok in tokens:
                if tok in alt or tok in slug:
                    return True
            return False

        def _area(url: str) -> int:
            try:
                return int((infos.get(url) or {}).get("area") or 0)
            except Exception:  # noqa: BLE001 - never raise on bad info
                return 0

        scored = []
        for index, url in enumerate(ordered):
            try:
                is_hero = url in hero_urls
            except Exception:  # noqa: BLE001
                is_hero = False
            try:
                match = _term_match(url)
            except Exception:  # noqa: BLE001
                match = False
            scored.append((not is_hero, not match, -_area(url), index, url))
        scored.sort(key=lambda row: (row[0], row[1], row[2], row[3]))
        return [row[4] for row in scored]
    except Exception:  # noqa: BLE001 - ranking never raises; degrade to input
        logger.debug("image: ranking failed; keeping DOM order", exc_info=True)
        try:
            return [u for u in candidates if isinstance(u, str) and u]
        except Exception:  # noqa: BLE001
            return []


# --- Usable-image check ---------------------------------------------


def is_usable_image(data: bytes, content_type: str = "") -> bool:
    """True when *data* is a raster image that satisfies every rule in
    this module's "usable image" section.
    """
    if not data or not (MIN_IMAGE_BYTES <= len(data) <= MAX_IMAGE_BYTES):
        return False
    if content_type:
        ct = content_type.split(";", 1)[0].strip().lower()
        if ct and ct not in ALLOWED_IMAGE_CONTENT_TYPES:
            return False
    try:
        with Image.open(io.BytesIO(data)) as img:
            width, height = img.size
            fmt = img.format or ""
    except (UnidentifiedImageError, OSError, ValueError):
        return False
    if fmt.upper() not in _PIL_FORMAT_TO_EXT:
        return False
    return width >= MIN_IMAGE_WIDTH and height >= MIN_IMAGE_HEIGHT


def _extension_for(data: bytes) -> str:
    try:
        with Image.open(io.BytesIO(data)) as img:
            return _PIL_FORMAT_TO_EXT.get((img.format or "").upper(), ".img")
    except (UnidentifiedImageError, OSError, ValueError):
        return ".img"


# --- Network seams (replaced wholesale in tests) ----------------------


def _fetch_page_html(url: str) -> str | None:
    """Re-fetch *url* and return its HTML, or ``None``.

    Reuses the #3 / #17 static fetch (politeness, size cap, retries live
    in :mod:`submissions.extraction`). A document (PDF / docx) or any
    fetch error yields ``None`` - the card just falls back to Draw Things.
    """
    try:
        fetched = extraction.fetch_static(url)
    except Exception as exc:  # noqa: BLE001 - fetch failure must never raise here
        logger.info("image: could not fetch source page %s: %s", url, exc)
        return None
    return fetched if isinstance(fetched, str) else None


def _fetch_image(url: str) -> FetchedImage:
    """Download one image. Raises on any HTTP / network / size problem;
    the caller treats a raise as "try the next candidate".
    """
    with httpx.Client(
        timeout=IMAGE_FETCH_TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": extraction._USER_AGENT},
    ) as client:
        resp = client.get(url)
    resp.raise_for_status()
    content = resp.content
    if len(content) > MAX_IMAGE_BYTES:
        raise ValueError(f"image exceeds {MAX_IMAGE_BYTES} bytes")
    content_type = resp.headers.get("content-type", "")
    return FetchedImage(content=content, content_type=content_type)


# --- Draw Things client ---------------------------------------------


class DrawThingsClient:
    """Minimal client for a local Draw Things HTTP API.

    Only :meth:`generate` is public. It **never raises**: a disabled
    flag, an unreachable server, a non-2xx response, a malformed body or
    an empty result all return ``None`` (with the reason logged) so batch
    generation is never aborted by image work.
    """

    #: Automatic1111-compatible text-to-image endpoint Draw Things serves.
    ENDPOINT = "/sdapi/v1/txt2img"

    def __init__(
        self,
        base_url: str | None = None,
        *,
        enabled: bool | None = None,
        timeout: float = DRAW_THINGS_TIMEOUT,
    ) -> None:
        self.base_url = (base_url or settings.DRAW_THINGS_URL).rstrip("/")
        self.enabled = (
            settings.DRAW_THINGS_ENABLED if enabled is None else enabled
        )
        self.timeout = timeout

    def generate(self, prompt: str) -> bytes | None:
        """Return image bytes for *prompt*, or ``None`` if generation was
        skipped / failed / empty (reason logged).
        """
        if not self.enabled:
            logger.info("Draw Things disabled; card gets no fallback image")
            return None
        payload = {
            "prompt": prompt,
            "negative_prompt": "text, watermark, signature, blurry",
            "steps": 20,
            "width": 512,
            "height": 512,
        }
        try:
            data = _draw_things_post(
                f"{self.base_url}{self.ENDPOINT}", payload, self.timeout
            )
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "Draw Things returned HTTP %s; card gets no image",
                exc.response.status_code,
            )
            return None
        except (httpx.HTTPError, OSError) as exc:
            logger.warning(
                "Draw Things unreachable (%s); card gets no image", exc
            )
            return None
        except ValueError as exc:
            logger.warning("Draw Things sent a non-JSON body (%s)", exc)
            return None

        images = (data or {}).get("images") or []
        if not images:
            logger.warning("Draw Things returned an empty result; no image")
            return None
        try:
            raw = base64.b64decode(images[0], validate=False)
        except (ValueError, TypeError) as exc:
            logger.warning("Draw Things sent an undecodable image (%s)", exc)
            return None
        if not raw:
            logger.warning("Draw Things returned an empty image; no image")
            return None
        return raw


def _draw_things_post(url: str, payload: dict, timeout: float) -> dict:
    """POST *payload* to *url* and return the parsed JSON body.

    Isolated seam: tests replace this to simulate an unreachable server,
    an error response, or an empty / malformed body - no wire traffic.
    """
    with httpx.Client(timeout=timeout) as client:
        resp = client.post(url, json=payload)
    resp.raise_for_status()
    return resp.json()


def _draw_things_prompt(card: Card) -> str:
    """Build a short text-to-image prompt from a card's own text."""
    topic = ""
    if isinstance(card.tags, dict):
        topic = str(card.tags.get("topic") or "").strip()
    subject = card.source_term or card.front
    bits = [subject]
    if topic and topic.lower() not in subject.lower():
        bits.append(topic)
    return (
        "A clear, simple educational illustration of "
        + ", ".join(b for b in bits if b)
        + ". Clean background, no text."
    )


# --- Orchestration -------------------------------------------------


def choose_card_image(
    card: Card,
    candidate_urls: list[str],
    draw_things: DrawThingsClient,
) -> ImageOutcome:
    """Walk the fallback chain for one card and return an
    :class:`ImageOutcome`:

    source candidate 1 -> ... -> source candidate N -> Draw Things -> none

    A candidate that fails to fetch, or fetches but is not a usable
    image, is skipped and the next candidate is tried. Draw Things is
    only asked once, after every source candidate is exhausted.
    """
    for url in candidate_urls:
        try:
            fetched = _fetch_image(url)
        except Exception as exc:  # noqa: BLE001 - any failure -> next candidate
            logger.info("image: candidate %s failed to fetch (%s)", url, exc)
            continue
        if is_usable_image(fetched.content, fetched.content_type):
            return ImageOutcome(fetched.content, Card.ImageSource.SOURCE_PAGE)
        logger.info("image: candidate %s is not a usable image", url)

    generated = draw_things.generate(_draw_things_prompt(card))
    if generated:
        return ImageOutcome(generated, Card.ImageSource.DRAW_THINGS)
    return ImageOutcome(None, Card.ImageSource.NONE)


def _store_card_image(card: Card, outcome: ImageOutcome) -> None:
    """Persist (or clear) one card's image. Enforces zero-or-one: the
    ``ImageField`` holds a single file and this is the only writer.
    """
    if not outcome.has_image:
        if card.image or card.image_source != Card.ImageSource.NONE:
            card.image.delete(save=False)
            card.image_source = Card.ImageSource.NONE
            card.save(update_fields=["image", "image_source"])
        return
    filename = f"card_{card.pk or 'new'}{_extension_for(outcome.data)}"
    card.image.save(filename, ContentFile(outcome.data), save=False)
    card.image_source = outcome.source
    card.save(update_fields=["image", "image_source"])


def attach_images(
    submitted_url,
    cards: list[Card],
    *,
    draw_things: DrawThingsClient | None = None,
) -> None:
    """Attach at most one image to each card in *cards*.

    Best-effort and self-contained: any failure for one card is logged
    and skipped; the source page is fetched once and its candidate list
    shared across every card from that URL.
    """
    cards = [c for c in cards if c is not None]
    if not cards:
        return
    client = draw_things or DrawThingsClient()
    candidates = _merged_candidates(submitted_url)
    try:
        page_html: str | None = _fetch_page_html(submitted_url.url)
    except Exception:  # noqa: BLE001 - ranking context is best-effort only
        logger.debug("image: ranking HTML refetch failed", exc_info=True)
        page_html = None
    try:
        page_base = submitted_url.url
    except Exception:  # noqa: BLE001
        page_base = ""
    for card in cards:
        try:
            try:
                ordered = rank_image_candidates(
                    candidates,
                    html=page_html,
                    base_url=page_base or "",
                    source_term=getattr(card, "source_term", "") or "",
                    front=getattr(card, "front", "") or "",
                )
            except Exception:  # noqa: BLE001 - ranking degrades to DOM order
                logger.debug("image: ranking failed; keeping DOM order", exc_info=True)
                ordered = candidates
            outcome = choose_card_image(card, ordered, client)
            _store_card_image(card, outcome)
        except Exception:  # noqa: BLE001 - one card's image never breaks the rest
            logger.exception(
                "image: unexpected failure attaching image to card %s", card.pk
            )


def _merged_candidates(submitted_url) -> list[str]:
    """Merge extension-submitted and server-refetched candidates (issue #42).

    Extension-submitted URLs (collected by the content script from the
    live DOM - the only candidates on authenticated / JS-rendered pages)
    come first, in the order received, followed by the existing
    server-refetch-derived candidates; deduplicated across both lists and
    capped at :data:`MAX_IMAGE_CANDIDATES` total. Both sources pass
    through the same :func:`_filter_absolute_candidates` exclusions.
    Fail-soft: any problem yields just the server list (or ``[]``) -
    never a raise.
    """
    try:
        extension_raw = getattr(submitted_url, "extension_image_urls", None) or []
    except Exception:  # noqa: BLE001 - unreadable field degrades to no extension list
        logger.debug("image: could not read extension_image_urls", exc_info=True)
        extension_raw = []
    if not isinstance(extension_raw, list):
        extension_raw = []
    extension_candidates = _filter_absolute_candidates(extension_raw)
    try:
        html = _fetch_page_html(submitted_url.url)
    except Exception:  # noqa: BLE001 - fetch failure degrades to extension-only list
        logger.info(
            "image: could not fetch source page %s", submitted_url.url, exc_info=True
        )
        html = None
    server_candidates = image_candidates(html, submitted_url.url)
    merged: list[str] = []
    seen: set[str] = set()
    for url in extension_candidates + server_candidates:
        if url in seen:
            continue
        seen.add(url)
        merged.append(url)
        if len(merged) >= MAX_IMAGE_CANDIDATES:
            break
    return merged
