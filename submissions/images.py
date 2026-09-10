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

    seen: set[str] = set()
    out: list[str] = []
    for raw in collector.sources:
        if not raw or raw.startswith("data:"):
            continue
        absolute = urljoin(base_url, raw)
        if not absolute.lower().startswith(("http://", "https://")):
            continue
        if _looks_excluded(absolute):
            continue
        path = urlsplit(absolute).path.lower()
        if "." in path.rsplit("/", 1)[-1] and not path.endswith(_RASTER_SUFFIXES):
            continue
        if absolute in seen:
            continue
        seen.add(absolute)
        out.append(absolute)
        if len(out) >= MAX_IMAGE_CANDIDATES:
            break
    return out


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
    html = _fetch_page_html(submitted_url.url)
    candidates = image_candidates(html, submitted_url.url)
    for card in cards:
        try:
            outcome = choose_card_image(card, candidates, client)
            _store_card_image(card, outcome)
        except Exception:  # noqa: BLE001 - one card's image never breaks the rest
            logger.exception(
                "image: unexpected failure attaching image to card %s", card.pk
            )
