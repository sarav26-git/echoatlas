"""
Project Name: EchoAtlas
Project Type: Telegram Bot
Integrations for References: MusicBrainz, Wikipedia, Genius, Lyrics.ovh

By @sarav26-git
"""

import os
import re
import logging
import asyncio
from typing import Dict, List, Optional

import requests
from bs4 import BeautifulSoup, NavigableString, Tag

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("EchoAtlas")

MUSICBRAINZ_API = "https://musicbrainz.org/ws/2"
WIKIPEDIA_API   = "https://en.wikipedia.org/w/api.php"
GENIUS_API      = "https://api.genius.com"

USER_AGENT = "EchoAtlasBot/3.0 (Telegram music metadata bot)"

TELEGRAM_TOKEN      = os.getenv("TELEGRAM_BOT_TOKEN", "")
GENIUS_ACCESS_TOKEN = os.getenv("GENIUS_ACCESS_TOKEN", "")

COMPILATION_RE = re.compile(
    r"\b(hits|best of|greatest|collection|playlist|vol\.|volume|"
    r"compilation|anthology|essentials|now that'?s|top\s*\d|"
    r"\d+\s*%|nrj|universal music)\b",
    re.IGNORECASE,
)


# ─────────────────────────────────────────────────────────────────────────────
# FETCHER
# ─────────────────────────────────────────────────────────────────────────────

class SongMetadataFetcher:

    @staticmethod
    def search_songs(song_name: str) -> List[Dict]:
        """Search MusicBrainz and return selectable song results."""
        try:
            headers = {"User-Agent": USER_AGENT}
            params  = {
                "query": f'recording:"{song_name}"',
                "fmt":   "json",
                "limit": 15,
            }

            response = requests.get(
                f"{MUSICBRAINZ_API}/recording/",
                params=params,
                headers=headers,
                timeout=15,
            )
            response.raise_for_status()
            recordings = response.json().get("recordings", [])

            if len(recordings) < 2:
                response = requests.get(
                    f"{MUSICBRAINZ_API}/recording/",
                    params={"query": song_name, "fmt": "json", "limit": 15},
                    headers=headers,
                    timeout=15,
                )
                response.raise_for_status()
                recordings = response.json().get("recordings", [])

            VARIANT_RE = re.compile(
                r"(acoustic|live|demo|instrumental|remix|edit|cover|"
                r"version|remaster|remastered|reprise|karaoke|radio|"
                r"extended|mix|flip|rework|stripped|session|unplugged)",
                re.IGNORECASE,
            )

            results = []
            seen_base = set()  # deduplicate by base title+artist (no variants)
            seen_full = set()  # deduplicate exact duplicates

            for recording in recordings:
                credits = recording.get("artist-credit", [])
                if not credits:
                    continue

                artists = [
                    credit.get("name", "")
                    for credit in credits
                    if credit.get("name")
                ]

                if not artists:
                    continue

                title  = recording.get("title", "").strip()
                artist = artists[0].strip()

                if not title:
                    continue

                # Skip variant versions (acoustic, live, remix, etc.)
                if VARIANT_RE.search(title):
                    continue

                full_key = f"{title.lower()}::{artist.lower()}"
                if full_key in seen_full:
                    continue
                seen_full.add(full_key)

                results.append({
                    "id":               recording.get("id"),
                    "title":            title,
                    "artist":           artist,
                    "featured_artists": artists[1:],
                    "score":            int(recording.get("score", 0)),
                })

            results.sort(key=lambda item: item["score"], reverse=True)

            # Filter: at least one word from the search input must appear
            # in the song title — prevents returning songs where the artist
            # name matches but the title is completely unrelated
            input_words = set(
                w.lower() for w in re.split(r"[\s\-]+", song_name)
                if len(w) > 2
            )

            if input_words:
                filtered = [
                    r for r in results
                    if any(
                        w in r["title"].lower()
                        for w in input_words
                    )
                ]
                # Only apply filter if it leaves at least one result
                if filtered:
                    results = filtered

            return results[:10]

        except Exception as error:
            logger.exception("MusicBrainz search failed: %s", error)
            return []

    @staticmethod
    def get_wikipedia_metadata(track: str, artist: str) -> Optional[Dict]:
        """Get album/year/genre from Wikipedia infobox."""
        try:
            search_response = requests.get(
                WIKIPEDIA_API,
                params={
                    "action":   "query",
                    "list":     "search",
                    "srsearch": f'"{track}" {artist} song',
                    "format":   "json",
                    "srlimit":  5,
                },
                timeout=15,
            )
            search_response.raise_for_status()

            search_results = (
                search_response.json()
                .get("query", {})
                .get("search", [])
            )

            for result in search_results:
                page_title = result.get("title", "")

                page_response = requests.get(
                    WIKIPEDIA_API,
                    params={
                        "action": "parse",
                        "page":   page_title,
                        "prop":   "text",
                        "format": "json",
                    },
                    timeout=15,
                )
                page_response.raise_for_status()

                parsed = page_response.json().get("parse")
                if not parsed:
                    continue

                soup    = BeautifulSoup(parsed["text"]["*"], "html.parser")
                infobox = soup.find("table", class_="infobox")

                if not infobox:
                    continue

                metadata = {}

                for row in infobox.find_all("tr"):
                    heading = row.find("th")
                    value   = row.find("td")

                    if not heading or not value:
                        continue

                    key  = heading.get_text(" ", strip=True).lower()
                    text = value.get_text(" ", strip=True)

                    if "artist" in key:
                        artist_parts = re.split(
                            r"\bfeaturing\b|\bfeat\.?\b|\bft\.?\b|,|&",
                            text,
                            flags=re.IGNORECASE,
                        )
                        artist_parts = [p.strip() for p in artist_parts if p.strip()]

                        if artist_parts:
                            metadata["artist"] = artist_parts[0]
                            if len(artist_parts) > 1:
                                metadata["featured_artists"] = artist_parts[1:]

                    elif "album" in key:
                        metadata["album"] = text.split("(")[0].strip()

                    elif "released" in key or "published" in key:
                        year_match = re.search(r"\b(19|20)\d{2}\b", text)
                        if year_match:
                            metadata["year"] = year_match.group(0)

                    elif "genre" in key:
                        genres = [
                            g.strip()
                            for g in re.split(r",|;|\n", text)
                            if g.strip()
                        ]
                        if genres:
                            metadata["genre"] = ", ".join(genres[:3])

                metadata["url"] = (
                    "https://en.wikipedia.org/wiki/"
                    + page_title.replace(" ", "_")
                )

                if metadata:
                    return metadata

            return None

        except Exception as error:
            logger.warning("Wikipedia lookup failed: %s", error)
            return None

    @staticmethod
    def _clean_about(text: str) -> str:
        """Clean Genius description junk."""
        if not text:
            return ""

        text = re.sub(
            r"(?is)^\s*(?:song bio|about|"
            r"\d+\s+(?:contributors?|translations?|comments?)|"
            r"translations?|lyrics)\s*",
            "",
            text,
        )

        text = re.sub(
            r"(?is)\s*(?:read more|expand|share|"
            r"add a comment|ask us anything).*?$",
            "",
            text,
        )

        text = re.sub(r"\s+", " ", text).strip()
        return text

    @staticmethod
    def _clean_lyrics(lyrics: str) -> str:
        """Strip all Genius chrome, keep only actual lyrics text."""
        if not lyrics:
            return ""

        # Remove Genius UI junk lines
        junk_lines = re.compile(
            r"^(?:"
            r"\d+\s+(?:contributors?|translations?|comments?)|"
            r"translations?|"
            r"read more|"
            r"embed|"
            r"share|"
            r"ask us anything|"
            r"you might also like"
            r")$",
            re.IGNORECASE,
        )

        clean = []
        for line in lyrics.splitlines():
            line = line.strip()
            if not line:
                if clean and clean[-1] != "":
                    clean.append("")
                continue
            if junk_lines.match(line):
                continue
            clean.append(line)

        lyrics = "\n".join(clean)
        lyrics = re.sub(r"\n{3,}", "\n\n", lyrics).strip()
        return lyrics

    @staticmethod
    def _html_lyrics_to_plain(html: str) -> str:
        """
        Convert Genius lyrics HTML (from text_format=html API response)
        to clean plain text, preserving section headers and line breaks.
        """
        soup = BeautifulSoup(html, "html.parser")

        def walk(node) -> str:
            buf = []
            for child in node.children:
                if isinstance(child, NavigableString):
                    buf.append(str(child))
                elif isinstance(child, Tag):
                    if child.name == "br":
                        buf.append("\n")
                    elif child.name in ("a", "span", "b", "i", "em", "strong"):
                        buf.append(walk(child))
                    elif child.name in ("p", "div"):
                        inner = walk(child).strip()
                        if inner:
                            buf.append("\n" + inner + "\n")
            return "".join(buf)

        # Try lyrics containers first (page HTML format)
        containers = soup.find_all("div", attrs={"data-lyrics-container": "true"})
        if containers:
            parts = [walk(c).strip() for c in containers]
        else:
            # API html format — the whole response IS the lyrics HTML
            parts = [walk(soup).strip()]

        raw = "\n\n".join(p for p in parts if p)
        raw = raw.replace("&amp;", "&").replace("&apos;", "'").replace("&#x27;", "'")
        raw = re.sub(r"[ \t]{2,}", " ", raw)
        raw = re.sub(r"\n{3,}", "\n\n", raw)
        raw = "\n".join(line.rstrip() for line in raw.splitlines())
        # Remove stray Genius annotation numbers
        raw = re.sub(r"(?<!\w)\d{4,6}(?!\w)", "", raw)
        return re.sub(r"\n{3,}", "\n\n", raw).strip()

    @staticmethod
    def _fetch_lyrics_ovh(artist: str, title: str) -> Optional[str]:
        """
        Fetch lyrics from lyrics.ovh — free, no API key, works on Vercel.
        https://api.lyrics.ovh/v1/{artist}/{title}
        """
        try:
            url = f"https://api.lyrics.ovh/v1/{requests.utils.quote(artist)}/{requests.utils.quote(title)}"
            response = requests.get(url, timeout=10)
            if response.status_code == 200:
                data   = response.json()
                lyrics = data.get("lyrics", "").strip()
                if lyrics and len(lyrics) > 20:
                    return SongMetadataFetcher._clean_lyrics(lyrics)
            logger.warning("lyrics.ovh: status %s for %s - %s", response.status_code, artist, title)
            return None
        except Exception as e:
            logger.warning("lyrics.ovh fetch failed: %s", e)
            return None

    @staticmethod
    def get_genius_data(track: str, artist: str) -> Optional[Dict]:
        """
        Gets Genius song URL, album, context and lyrics.

        Lyrics strategy:
          1. API text_format=html  — uses Bearer token, bypasses Cloudflare.
             Works reliably on Vercel / any serverless host.
          2. Page scrape fallback  — only attempted when running locally
             and the API html field is empty.
        """
        if not GENIUS_ACCESS_TOKEN:
            logger.warning("GENIUS_ACCESS_TOKEN is missing.")
            return None

        headers = {"Authorization": f"Bearer {GENIUS_ACCESS_TOKEN}"}

        try:
            # ── Search ────────────────────────────────────────────────────
            search_response = requests.get(
                f"{GENIUS_API}/search",
                headers=headers,
                params={"q": f"{track} {artist}"},
                timeout=15,
            )
            search_response.raise_for_status()

            hits = (
                search_response.json()
                .get("response", {})
                .get("hits", [])
            )

            if not hits:
                return None

            selected = None
            for hit in hits[:10]:
                result       = hit.get("result", {})
                genius_artist = (
                    result.get("primary_artist", {})
                    .get("name", "")
                    .lower()
                )
                if artist.lower() in genius_artist or genius_artist in artist.lower():
                    selected = result
                    break

            if not selected:
                selected = hits[0].get("result", {})

            song_id  = selected.get("id")
            song_url = selected.get("url", "")

            if not song_id:
                return None

            # ── Song detail — request BOTH plain and html formats ─────────
            detail_response = requests.get(
                f"{GENIUS_API}/songs/{song_id}",
                headers=headers,
                params={"text_format": "plain,html"},
                timeout=15,
            )
            detail_response.raise_for_status()

            song = (
                detail_response.json()
                .get("response", {})
                .get("song", {})
            )

            result: Dict = {
                "url":         song_url,
                "album":       (song.get("album") or {}).get("name", ""),
                "description": "",
                "lyrics":      None,
            }

            # ── About / description ───────────────────────────────────────
            description = (song.get("description") or {}).get("plain", "")
            if description:
                result["description"] = SongMetadataFetcher._clean_about(description)

            # ── Lyrics: lyrics.ovh ──────────
            lyrics_fetched = SongMetadataFetcher._fetch_lyrics_ovh(
                artist, track
            )
            if lyrics_fetched:
                result["lyrics"] = lyrics_fetched
                logger.info("Lyrics: fetched via lyrics.ovh for %s - %s", artist, track)
            else:
                logger.warning("Lyrics: lyrics.ovh returned nothing for %s - %s", artist, track)

            return result

        except Exception as error:
            logger.warning("Genius API failed: %s", error)
            return None

    @staticmethod
    def get_detailed_metadata(
        recording_id: str,
        song_title:   str,
        artist:       str,
    ) -> Dict:
        metadata = {
            "title":            song_title,
            "artist":           artist,
            "featured_artists": [],
            "album":            "Unknown",
            "year":             "Unknown",
            "genre":            "Unknown",
            "description":      "",
            "lyrics":           None,
            "genius_url":       None,
            "wikipedia_url":    None,
        }

        # Wikipedia
        wikipedia = SongMetadataFetcher.get_wikipedia_metadata(song_title, artist)
        if wikipedia:
            for key in ["artist", "featured_artists", "album", "year", "genre"]:
                if wikipedia.get(key):
                    metadata[key] = wikipedia[key]
            metadata["wikipedia_url"] = wikipedia.get("url")

        # MusicBrainz fallback
        try:
            response = requests.get(
                f"{MUSICBRAINZ_API}/recording/{recording_id}",
                params={"inc": "releases+artist-credits+genres+tags", "fmt": "json"},
                headers={"User-Agent": USER_AGENT},
                timeout=15,
            )
            response.raise_for_status()
            musicbrainz = response.json()

            if not metadata["featured_artists"]:
                credits = musicbrainz.get("artist-credit", [])
                if len(credits) > 1:
                    metadata["featured_artists"] = [
                        c.get("name") for c in credits[1:] if c.get("name")
                    ]

            if metadata["genre"] == "Unknown":
                genres = [
                    item.get("name")
                    for item in musicbrainz.get("genres", [])[:3]
                    if item.get("name")
                ]
                if not genres:
                    genres = [
                        item.get("name")
                        for item in musicbrainz.get("tags", [])[:3]
                        if item.get("name")
                    ]
                if genres:
                    metadata["genre"] = ", ".join(genres)

            releases = musicbrainz.get("releases", [])
            if releases:
                releases.sort(
                    key=lambda r: (
                        bool(COMPILATION_RE.search(r.get("title", ""))),
                        r.get("date", "9999"),
                    )
                )
                best = releases[0]
                if metadata["album"] == "Unknown":
                    metadata["album"] = best.get("title", "Unknown")
                if metadata["year"] == "Unknown":
                    date = best.get("date", "")
                    if date:
                        metadata["year"] = date[:4]

        except Exception as error:
            logger.warning("MusicBrainz metadata fallback failed: %s", error)

        # Genius
        genius = SongMetadataFetcher.get_genius_data(song_title, artist)
        if genius:
            if genius.get("description"):
                metadata["description"] = genius["description"]
            if genius.get("lyrics"):
                metadata["lyrics"] = genius["lyrics"]
            if genius.get("url"):
                metadata["genius_url"] = genius["url"]
            genius_album = genius.get("album", "")
            if genius_album and (
                metadata["album"] == "Unknown"
                or COMPILATION_RE.search(metadata["album"])
            ):
                metadata["album"] = genius_album

        return metadata


# ─────────────────────────────────────────────────────────────────────────────
# TELEGRAM HANDLERS
# ─────────────────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 <b>Hey this is EchoAtlas</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Just enter the Song name with Artist and get it's metadata :)\n\n"
        "<i>Keep this Format: Song - Artist</i>",
        parse_mode="HTML",
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎵 <b>EchoAtlas Help</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "1. Send a song title with artist\n"
        "2. Select your query\n"
        "3. Tap <b>📝 Wanna Sing Along?</b> for lyrics inside Telegram\n\n"
        "As EchoAtlas currently supports only Popular* Songs, some Regionals may have been missing. We'll look forward to expand our Dataset.\n\nStay with us for Further Upgradation!\n\n"
        "<b>Commands:</b>\n"
        "/start — Start EchoAtlas\n"
        "/help — This Guide",
        parse_mode="HTML",
    )


async def handle_song_search(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    song_name = (update.message.text or "").strip()

    if not song_name:
        await update.message.reply_text("Send a song name first...")
        return

    loading_message = await update.message.reply_text(
        f"🔎 Searching for <b>{song_name}</b>...",
        parse_mode="HTML",
    )

    results = SongMetadataFetcher.search_songs(song_name)

    if not results:
        await loading_message.edit_text(
            "❌ Couldn't find <b>{}</b>.\n\n"
            "Make sure the song title is correct:\n"
            "<i>BLUE - Billie Eilish</i>\n"
            "<i>Espresso - Sabrina Carpenter</i>".format(song_name),
            parse_mode="HTML",
        )
        return

    context.user_data["search_results"] = results

    # Auto-select only when score is 100 AND title+artist match the input
    top = results[0]
    input_clean = song_name.lower().strip()
    top_title   = top["title"].lower().strip()
    top_artist  = top["artist"].lower().strip()
    is_exact = (
        top.get("score", 0) == 100
        and (
            top_title in input_clean
            or input_clean in top_title
            or top_artist in input_clean
        )
    )
    if is_exact:
        await loading_message.edit_text("⏳ Building your music brief...")
        song = results[0]
        metadata = SongMetadataFetcher.get_detailed_metadata(
            song["id"], song["title"], song["artist"],
        )
        context.user_data["current_metadata"] = metadata

        artist_line = metadata["artist"]
        if metadata.get("featured_artists"):
            artist_line += " ft. " + ", ".join(metadata["featured_artists"])

        message = (
            "<b>EchoAtlas Music Brief</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"📌 <b>Title:</b> {metadata['title']}\n"
            f"🎤 <b>Artist:</b> {artist_line}\n"
        )
        if metadata["album"] != "Unknown":
            message += f"💿 <b>Album:</b> {metadata['album']}\n"
        if metadata["year"] != "Unknown":
            message += f"📅 <b>Year:</b> {metadata['year']}\n"
        if metadata["genre"] != "Unknown":
            message += f"🎼 <b>Genre:</b> {metadata['genre'].title()}\n"
        if metadata.get("description"):
            description = metadata["description"]
            if len(description) > 1600:
                description = description[:1600].rsplit(". ", 1)[0] + "."
            message += f"\n📖 <b>Song Context</b>\n<i>{description}</i>\n"

        buttons = []
        if metadata.get("lyrics"):
            buttons.append([InlineKeyboardButton("📝 Wanna Sing Along?", callback_data="show_lyrics")])

        await loading_message.edit_text(
            message,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(buttons) if buttons else None,
            disable_web_page_preview=True,
        )
        return

    keyboard = []
    for index, song in enumerate(results):
        artist_text = song["artist"]
        if song["featured_artists"]:
            artist_text += " ft. " + ", ".join(song["featured_artists"])
        keyboard.append([
            InlineKeyboardButton(
                f"🎵 {song['title']} — {artist_text}",
                callback_data=f"select_{index}",
            )
        ])

    await loading_message.edit_text(
        "🎵 <b>Select your Query</b>",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML",
    )


async def send_in_app_lyrics(query, metadata: Dict):
    """Send lyrics inside Telegram, split safely into chunks."""
    lyrics = SongMetadataFetcher._clean_lyrics(metadata.get("lyrics", ""))

    if not lyrics:
        await query.answer(
            "Lyrics are not available for this song.",
            show_alert=True,
        )
        return

    title  = metadata.get("title", "Lyrics")
    artist = metadata.get("artist", "")

    header = f"📝 <b>{title}</b>"
    if artist:
        header += f" — {artist}"
    header += "\n━━━━━━━━━━━━━━━━━━━━━━━━\n\n"

    max_length = 3800
    chunks     = []

    while lyrics:
        if len(lyrics) <= max_length:
            chunks.append(lyrics)
            break
        split_at = lyrics.rfind("\n", 0, max_length)
        if split_at < max_length // 2:
            split_at = lyrics.rfind(" ", 0, max_length)
        if split_at < 1:
            split_at = max_length
        chunks.append(lyrics[:split_at].strip())
        lyrics = lyrics[split_at:].strip()

    await query.answer()

    await query.message.reply_text(
        header + chunks[0],
        parse_mode="HTML",
        disable_web_page_preview=True,
    )

    for chunk in chunks[1:]:
        await query.message.reply_text(
            chunk,
            disable_web_page_preview=True,
        )


async def handle_song_selection(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query
    await query.answer()

    callback_data = query.data

    # ── IN-APP LYRICS ────────────────────────────────────────────────────────
    if callback_data == "show_lyrics":
        metadata = context.user_data.get("current_metadata", {})
        await send_in_app_lyrics(query, metadata)
        return

    # ── SONG SELECTION ───────────────────────────────────────────────────────
    if not callback_data.startswith("select_"):
        return

    try:
        index = int(callback_data.split("_")[1])
    except (IndexError, ValueError):
        await query.edit_message_text("Invalid selection!")
        return

    results = context.user_data.get("search_results", [])

    if index >= len(results):
        await query.edit_message_text(
            "Search session expired! Search again."
        )
        return

    await query.edit_message_text("⏳ Building your music brief...")

    song = results[index]

    metadata = SongMetadataFetcher.get_detailed_metadata(
        song["id"],
        song["title"],
        song["artist"],
    )

    context.user_data["current_metadata"] = metadata

    artist_line = metadata["artist"]
    if metadata.get("featured_artists"):
        artist_line += " ft. " + ", ".join(metadata["featured_artists"])

    message = (
        "<b>EchoAtlas Music Brief</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📌 <b>Title:</b> {metadata['title']}\n"
        f"🎤 <b>Artist:</b> {artist_line}\n"
    )

    if metadata["album"] != "Unknown":
        message += f"💿 <b>Album:</b> {metadata['album']}\n"

    if metadata["year"] != "Unknown":
        message += f"📅 <b>Year:</b> {metadata['year']}\n"

    if metadata["genre"] != "Unknown":
        message += f"🎼 <b>Genre:</b> {metadata['genre'].title()}\n"

    if metadata.get("description"):
        description = metadata["description"]
        # Trim at last complete sentence if description is very long
        if len(description) > 1600:
            description = description[:1600].rsplit(". ", 1)[0] + "."
        message += (
            "\n📖 <b>Song Context</b>\n"
            f"<i>{description}</i>\n"
        )

    buttons = []

    if metadata.get("lyrics"):
        buttons.append([
            InlineKeyboardButton(
                "📝 Wanna Sing Along?",
                callback_data="show_lyrics",
            )
        ])

    await query.edit_message_text(
        message,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons) if buttons else None,
        disable_web_page_preview=True,
    )


# ─────────────────────────────────────────────────────────────────────────────
# APP
# ─────────────────────────────────────────────────────────────────────────────

def build_application() -> Application:
    if not TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is missing.")

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_song_search)
    )
    app.add_handler(CallbackQueryHandler(handle_song_selection))

    return app


def main():
    if not TELEGRAM_TOKEN:
        print("❌ TELEGRAM_BOT_TOKEN is missing.")
        return

    if not GENIUS_ACCESS_TOKEN:
        print("⚠️ GENIUS_ACCESS_TOKEN is missing. Lyrics may not work.")

    if os.name == "nt":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    app = build_application()

    logger.info("EchoAtlas started.")
    print("🎵 EchoAtlas is running...")

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
