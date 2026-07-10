"""
Project Name: EchoAtlas
Project Type: Telegram Bot
Integrations for References: MusicBrainz, Wikipedia, Genius

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
WIKIPEDIA_API = "https://en.wikipedia.org/w/api.php"
GENIUS_API = "https://api.genius.com"

USER_AGENT = "EchoAtlasBot/3.0 (Telegram music metadata bot)"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
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
            params = {
                "query": f'recording:"{song_name}"',
                "fmt": "json",
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

            # Broader search if exact-title search is weak
            if len(recordings) < 2:
                response = requests.get(
                    f"{MUSICBRAINZ_API}/recording/",
                    params={
                        "query": song_name,
                        "fmt": "json",
                        "limit": 15,
                    },
                    headers=headers,
                    timeout=15,
                )
                response.raise_for_status()
                recordings = response.json().get("recordings", [])

            results = []
            seen = set()

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

                title = recording.get("title", "").strip()
                artist = artists[0].strip()
                key = f"{title.lower()}::{artist.lower()}"

                if not title or key in seen:
                    continue

                seen.add(key)

                results.append({
                    "id": recording.get("id"),
                    "title": title,
                    "artist": artist,
                    "featured_artists": artists[1:],
                    "score": int(recording.get("score", 0)),
                })

            results.sort(key=lambda item: item["score"], reverse=True)
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
                    "action": "query",
                    "list": "search",
                    "srsearch": f'"{track}" {artist} song',
                    "format": "json",
                    "srlimit": 5,
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
                        "page": page_title,
                        "prop": "text",
                        "format": "json",
                    },
                    timeout=15,
                )
                page_response.raise_for_status()

                parsed = page_response.json().get("parse")
                if not parsed:
                    continue

                soup = BeautifulSoup(parsed["text"]["*"], "html.parser")
                infobox = soup.find("table", class_="infobox")

                if not infobox:
                    continue

                metadata = {}

                for row in infobox.find_all("tr"):
                    heading = row.find("th")
                    value = row.find("td")

                    if not heading or not value:
                        continue

                    key = heading.get_text(" ", strip=True).lower()
                    text = value.get_text(" ", strip=True)

                    if "artist" in key:
                        artist_parts = re.split(
                            r"\bfeaturing\b|\bfeat\.?\b|\bft\.?\b|,|&",
                            text,
                            flags=re.IGNORECASE,
                        )
                        artist_parts = [
                            part.strip()
                            for part in artist_parts
                            if part.strip()
                        ]

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
                            genre.strip()
                            for genre in re.split(r",|;|\n", text)
                            if genre.strip()
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
        """
        Keeps only actual lyrics.
        Removes Genius junk, contributor labels, descriptions and Read More.
        """
        if not lyrics:
            return ""

        lyrics = BeautifulSoup(lyrics, "html.parser").get_text("\n")
        lyrics = lyrics.replace("&amp;", "&").replace("&apos;", "'")
        lyrics = lyrics.replace("&#x27;", "'")

        lyrics = re.sub(
            r"(?is)^\s*(?:"
            r"\d+\s+(?:contributors?|translations?|comments?)\s*|"
            r"translations?\s*|"
            r"lyrics\s*|"
            r"read more\s*"
            r")+",
            "",
            lyrics,
        )

        # If Genius description leaked before actual lyric sections,
        # retain from [Intro], [Verse], [Chorus], etc.
        section_match = re.search(
            r"(?m)^\[(?:"
            r"intro|verse(?:\s+\d+)?|chorus|pre-chorus|post-chorus|"
            r"bridge|outro|hook|refrain|interlude|breakdown"
            r").*?\]\s*$",
            lyrics,
            flags=re.IGNORECASE,
        )

        if section_match:
            lyrics = lyrics[section_match.start():]

        # Remove repeated Genius UI junk lines
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

        clean_lines = []

        for line in lyrics.splitlines():
            line = line.strip()

            if not line:
                if clean_lines and clean_lines[-1] != "":
                    clean_lines.append("")
                continue

            if junk_lines.match(line):
                continue

            clean_lines.append(line)

        lyrics = "\n".join(clean_lines)
        lyrics = re.sub(r"\n{3,}", "\n\n", lyrics).strip()

        return lyrics

    @staticmethod
    def _extract_lyrics_from_page(song_url: str) -> Optional[str]:
        """
        Scrapes Genius lyrics containers.
        Returns cleaned lyrics only.
        """
        if not song_url:
            return None

        try:
            response = requests.get(
                song_url,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 Chrome/120 Safari/537.36"
                    )
                },
                timeout=20,
            )

            if response.status_code != 200:
                return None

            soup = BeautifulSoup(response.text, "html.parser")

            containers = soup.find_all(
                "div",
                attrs={"data-lyrics-container": "true"},
            )

            if not containers:
                return None

            lyrics_parts = []

            for container in containers:
                for br in container.find_all("br"):
                    br.replace_with("\n")

                part = container.get_text("\n", strip=True)

                if part:
                    lyrics_parts.append(part)

            raw_lyrics = "\n\n".join(lyrics_parts)
            cleaned = SongMetadataFetcher._clean_lyrics(raw_lyrics)

            return cleaned if len(cleaned) > 20 else None

        except Exception as error:
            logger.warning("Genius lyrics scrape failed: %s", error)
            return None

    @staticmethod
    def get_genius_data(track: str, artist: str) -> Optional[Dict]:
        """
        Gets Genius song URL, album, context and lyrics.
        Lyrics are returned for Telegram in-app display.
        """
        if not GENIUS_ACCESS_TOKEN:
            logger.warning("GENIUS_ACCESS_TOKEN is missing.")
            return None

        headers = {
            "Authorization": f"Bearer {GENIUS_ACCESS_TOKEN}",
        }

        try:
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
                result = hit.get("result", {})
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

            song_id = selected.get("id")
            song_url = selected.get("url", "")

            if not song_id:
                return None

            detail_response = requests.get(
                f"{GENIUS_API}/songs/{song_id}",
                headers=headers,
                params={"text_format": "plain"},
                timeout=15,
            )
            detail_response.raise_for_status()

            song = (
                detail_response.json()
                .get("response", {})
                .get("song", {})
            )

            result = {
                "url": song_url,
                "album": (song.get("album") or {}).get("name", ""),
                "description": "",
                "lyrics": None,
            }

            description = (
                song.get("description") or {}
            ).get("plain", "")

            if description:
                result["description"] = SongMetadataFetcher._clean_about(
                    description
                )

            # Actual lyrics for in-app Telegram output
            if song_url:
                result["lyrics"] = SongMetadataFetcher._extract_lyrics_from_page(
                    song_url
                )

            return result

        except Exception as error:
            logger.warning("Genius API failed: %s", error)
            return None

    @staticmethod
    def get_detailed_metadata(
        recording_id: str,
        song_title: str,
        artist: str,
    ) -> Dict:
        metadata = {
            "title": song_title,
            "artist": artist,
            "featured_artists": [],
            "album": "Unknown",
            "year": "Unknown",
            "genre": "Unknown",
            "description": "",
            "lyrics": None,
            "genius_url": None,
            "wikipedia_url": None,
        }

        # Wikipedia
        wikipedia = SongMetadataFetcher.get_wikipedia_metadata(
            song_title,
            artist,
        )

        if wikipedia:
            for key in [
                "artist",
                "featured_artists",
                "album",
                "year",
                "genre",
            ]:
                if wikipedia.get(key):
                    metadata[key] = wikipedia[key]

            metadata["wikipedia_url"] = wikipedia.get("url")

        # MusicBrainz fallback
        try:
            response = requests.get(
                f"{MUSICBRAINZ_API}/recording/{recording_id}",
                params={
                    "inc": "releases+artist-credits+genres+tags",
                    "fmt": "json",
                },
                headers={"User-Agent": USER_AGENT},
                timeout=15,
            )
            response.raise_for_status()
            musicbrainz = response.json()

            if not metadata["featured_artists"]:
                credits = musicbrainz.get("artist-credit", [])
                if len(credits) > 1:
                    metadata["featured_artists"] = [
                        credit.get("name")
                        for credit in credits[1:]
                        if credit.get("name")
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
                    key=lambda release: (
                        bool(COMPILATION_RE.search(release.get("title", ""))),
                        release.get("date", "9999"),
                    )
                )

                best_release = releases[0]

                if metadata["album"] == "Unknown":
                    metadata["album"] = best_release.get(
                        "title",
                        "Unknown",
                    )

                if metadata["year"] == "Unknown":
                    date = best_release.get("date", "")
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
        "🎵 <b>Welcome to EchoAtlas</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Search any song and get its artist, album, year, genre, "
        "music context and clean in-app lyrics.\n\n"
        "<b>Example:</b> <code>House Tour - Sabrina Carpenter</code>",
        parse_mode="HTML",
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎵 <b>EchoAtlas Help</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "1. Send a song title with artist\n"
        "2. Select your choice\n"
        "3. Tap <b>📝 Wanna Sing Along?</b> for lyrics inside Telegram\n\n"
        "As EchoAtlas currently supports only Popular* Songs, some Regionals may have been missing from our Service.\nStay with us for Further Upgradation!"
        "<b>Commands</b>\n"
        "/start — Start EchoAtlas\n"
        "/help — Show this guide",
        parse_mode="HTML",
    )


async def handle_song_search(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    song_name = (update.message.text or "").strip()

    if not song_name:
        await update.message.reply_text("Send a song name first.")
        return

    loading_message = await update.message.reply_text(
        f"🔎 Searching for <b>{song_name}</b>...",
        parse_mode="HTML",
    )

    results = SongMetadataFetcher.search_songs(song_name)

    if not results:
        await loading_message.edit_text(
            "❌ No songs found. Try adding the artist name.\n\n"
            "<i>Example: House Tour - Sabrina Carpenter</i>",
            parse_mode="HTML",
        )
        return

    context.user_data["search_results"] = results

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
        "🎵 <b>Select the correct song</b>",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML",
    )


async def send_in_app_lyrics(
    query,
    metadata: Dict,
):
    """Send lyrics inside Telegram, split safely into messages."""

    lyrics = SongMetadataFetcher._clean_lyrics(
        metadata.get("lyrics", "")
    )

    if not lyrics:
        await query.answer(
            "Lyrics are not available for this song.",
            show_alert=True,
        )
        return

    title = metadata.get("title", "Lyrics")
    artist = metadata.get("artist", "")

    header = f"📝 <b>{title}</b>"
    if artist:
        header += f" — {artist}"
    header += "\n━━━━━━━━━━━━━━━━━━━━\n\n"

    max_length = 3800
    chunks = []

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

    # ── IN-APP LYRICS BUTTON ────────────────────────────────────────────────
    if callback_data == "show_lyrics":
        metadata = context.user_data.get("current_metadata", {})
        await send_in_app_lyrics(query, metadata)
        return

    # ── SONG SELECTION ──────────────────────────────────────────────────────
    if not callback_data.startswith("select_"):
        return

    try:
        index = int(callback_data.split("_")[1])
    except (IndexError, ValueError):
        await query.edit_message_text("❌ Invalid selection.")
        return

    results = context.user_data.get("search_results", [])

    if index >= len(results):
        await query.edit_message_text(
            "❌ This search expired. Search for the song again."
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
        artist_line += " ft. " + ", ".join(
            metadata["featured_artists"]
        )

    message = (
        "<b>EchoAtlas Music Brief</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
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

        # Telegram message safety
        if len(description) > 1600:
            description = description[:1600].rsplit(" ", 1)[0] + "…"

        message += (
            "\n📖 <b>Song Context</b>\n"
            f"<i>{description}</i>\n"
        )

    buttons = []

    # THIS BUTTON SHOWS LYRICS INSIDE TELEGRAM
    if metadata.get("lyrics"):
        buttons.append([
            InlineKeyboardButton(
                "📝 Wanna Sing Along?",
                callback_data="show_lyrics",
            )
        ])

    # Separate optional external source button
    if metadata.get("genius_url"):
        buttons.append([
            InlineKeyboardButton(
                "🔗 Open on Genius",
                url=metadata["genius_url"],
            )
        ])

    await query.edit_message_text(
        message,
        parse_mode="HTML",
        reply_markup=(
            InlineKeyboardMarkup(buttons)
            if buttons
            else None
        ),
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
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_song_search,
        )
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
        asyncio.set_event_loop_policy(
            asyncio.WindowsSelectorEventLoopPolicy()
        )

    app = build_application()

    logger.info("EchoAtlas started.")
    print("🎵 EchoAtlas is running...")

    app.run_polling(
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
