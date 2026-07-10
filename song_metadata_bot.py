"""
EchoAtlas — Telegram Music Metadata Bot
Sources:
- MusicBrainz: song search
- Wikipedia: metadata
- Genius: song context / About text + official lyrics page link
"""

import os
import re
import logging
from typing import Dict, List, Optional

import requests
from bs4 import BeautifulSoup

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("echoatlas")

MUSICBRAINZ_API = "https://musicbrainz.org/ws/2"
WIKIPEDIA_API = "https://en.wikipedia.org/w/api.php"
GENIUS_API = "https://api.genius.com"

MUSICBRAINZ_USER_AGENT = "EchoAtlasBot/3.0 (Telegram music metadata bot)"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GENIUS_ACCESS_TOKEN = os.getenv("GENIUS_ACCESS_TOKEN")


class SongMetadataFetcher:
    @staticmethod
    def search_songs(query: str) -> List[Dict]:
        """Search MusicBrainz with a precise search and broad fallback."""
        headers = {"User-Agent": MUSICBRAINZ_USER_AGENT}

        try:
            precise_response = requests.get(
                f"{MUSICBRAINZ_API}/recording/",
                headers=headers,
                params={
                    "query": f'recording:"{query}"',
                    "fmt": "json",
                    "limit": 15,
                },
                timeout=15,
            )
            precise_response.raise_for_status()
            recordings = precise_response.json().get("recordings", [])

            if len(recordings) < 2:
                broad_response = requests.get(
                    f"{MUSICBRAINZ_API}/recording/",
                    headers=headers,
                    params={
                        "query": query,
                        "fmt": "json",
                        "limit": 15,
                    },
                    timeout=15,
                )
                broad_response.raise_for_status()
                recordings = broad_response.json().get("recordings", [])

            results = []
            seen = set()

            for recording in recordings:
                credits = recording.get("artist-credit", [])
                artists = [
                    credit.get("name", "").strip()
                    for credit in credits
                    if credit.get("name")
                ]

                if not artists:
                    continue

                title = recording.get("title", "").strip()
                if not title:
                    continue

                unique_key = f"{title.lower()}::{','.join(artists).lower()}"

                if unique_key in seen:
                    continue

                seen.add(unique_key)

                results.append({
                    "id": recording.get("id"),
                    "title": title,
                    "artist": artists[0],
                    "featured_artists": artists[1:],
                    "score": int(recording.get("score", 0)),
                })

            results.sort(key=lambda song: song["score"], reverse=True)
            return results[:10]

        except Exception as error:
            logger.exception("MusicBrainz search failed: %s", error)
            return []

    @staticmethod
    def get_wikipedia_metadata(title: str, artist: str) -> Optional[Dict]:
        try:
            search = requests.get(
                WIKIPEDIA_API,
                params={
                    "action": "query",
                    "list": "search",
                    "srsearch": f'"{title}" {artist} song',
                    "format": "json",
                    "srlimit": 3,
                },
                timeout=15,
            )
            search.raise_for_status()

            pages = search.json().get("query", {}).get("search", [])

            for page in pages:
                page_title = page["title"]

                parsed = requests.get(
                    WIKIPEDIA_API,
                    params={
                        "action": "parse",
                        "page": page_title,
                        "prop": "text",
                        "format": "json",
                    },
                    timeout=15,
                )
                parsed.raise_for_status()

                html = parsed.json().get("parse", {}).get("text", {}).get("*")
                if not html:
                    continue

                soup = BeautifulSoup(html, "html.parser")
                infobox = soup.find("table", class_="infobox")

                if not infobox:
                    continue

                metadata = {
                    "artist": None,
                    "album": None,
                    "year": None,
                    "genre": None,
                    "url": f"https://en.wikipedia.org/wiki/{page_title.replace(' ', '_')}",
                }

                for row in infobox.find_all("tr"):
                    label = row.find("th")
                    value = row.find("td")

                    if not label or not value:
                        continue

                    key = label.get_text(" ", strip=True).lower()
                    text = value.get_text(" ", strip=True)

                    if "artist" in key and not metadata["artist"]:
                        metadata["artist"] = text

                    elif "album" in key and not metadata["album"]:
                        metadata["album"] = text.split("(")[0].strip()

                    elif "released" in key and not metadata["year"]:
                        match = re.search(r"\b(?:19|20)\d{2}\b", text)
                        if match:
                            metadata["year"] = match.group(0)

                    elif "genre" in key and not metadata["genre"]:
                        metadata["genre"] = text

                if any([
                    metadata["artist"],
                    metadata["album"],
                    metadata["year"],
                    metadata["genre"],
                ]):
                    return metadata

        except Exception as error:
            logger.warning("Wikipedia lookup failed: %s", error)

        return None

    @staticmethod
    def clean_about(text: str) -> Optional[str]:
        """Remove common Genius interface / contributor junk."""
        if not text:
            return None

        text = re.sub(
            r"^\s*(Song Bio|About|[0-9,]+\s+Contributors?|"
            r"[0-9,]+\s+Translations?|Lyrics)\s*",
            "",
            text,
            flags=re.IGNORECASE,
        )

        text = re.sub(
            r"\s*(Read More|Expand|Share|Ask us a question|Add a comment).*$",
            "",
            text,
            flags=re.IGNORECASE | re.DOTALL,
        )

        text = re.sub(r"\s+", " ", text).strip()

        if len(text) < 35:
            return None

        if len(text) > 1200:
            text = text[:1200].rsplit(" ", 1)[0] + "…"

        return text

    @staticmethod
    def get_genius_data(title: str, artist: str) -> Optional[Dict]:
        """Gets official Genius song URL and clean song description."""
        if not GENIUS_ACCESS_TOKEN:
            logger.warning("GENIUS_ACCESS_TOKEN is missing.")
            return None

        headers = {"Authorization": f"Bearer {GENIUS_ACCESS_TOKEN}"}

        try:
            search = requests.get(
                f"{GENIUS_API}/search",
                headers=headers,
                params={"q": f"{title} {artist}"},
                timeout=15,
            )
            search.raise_for_status()

            hits = search.json().get("response", {}).get("hits", [])
            if not hits:
                return None

            selected = hits[0]["result"]

            for hit in hits[:5]:
                candidate = hit["result"]
                candidate_artist = candidate.get(
                    "primary_artist", {}
                ).get("name", "").lower()

                if artist.lower() in candidate_artist:
                    selected = candidate
                    break

            song_id = selected["id"]

            details = requests.get(
                f"{GENIUS_API}/songs/{song_id}",
                headers=headers,
                params={"text_format": "plain"},
                timeout=15,
            )
            details.raise_for_status()

            song = details.json().get("response", {}).get("song", {})
            description = (song.get("description") or {}).get("plain", "")

            return {
                "url": song.get("url") or selected.get("url"),
                "album": (song.get("album") or {}).get("name"),
                "description": SongMetadataFetcher.clean_about(description),
            }

        except Exception as error:
            logger.warning("Genius lookup failed: %s", error)
            return None

    @staticmethod
    def get_metadata(song: Dict) -> Dict:
        metadata = {
            "title": song["title"],
            "artist": song["artist"],
            "featured_artists": song.get("featured_artists", []),
            "album": None,
            "year": None,
            "genre": None,
            "about": None,
            "genius_url": None,
            "wikipedia_url": None,
        }

        wiki = SongMetadataFetcher.get_wikipedia_metadata(
            metadata["title"],
            metadata["artist"],
        )

        if wiki:
            metadata["album"] = wiki.get("album")
            metadata["year"] = wiki.get("year")
            metadata["genre"] = wiki.get("genre")
            metadata["wikipedia_url"] = wiki.get("url")

        genius = SongMetadataFetcher.get_genius_data(
            metadata["title"],
            metadata["artist"],
        )

        if genius:
            metadata["genius_url"] = genius.get("url")
            metadata["about"] = genius.get("description")

            if not metadata["album"] and genius.get("album"):
                metadata["album"] = genius["album"]

        return metadata


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎧 *Welcome to EchoAtlas*\n\n"
        "Search any song to explore its artist, album, release year, genre, "
        "and music context.\n\n"
        "Type a track name or use:\n"
        "`House Tour - Sabrina Carpenter`",
        parse_mode="Markdown",
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📚 *How EchoAtlas works*\n\n"
        "• Send a song name\n"
        "• Choose the correct result\n"
        "• Explore clean metadata and song context\n"
        "• Open the official Genius lyrics page\n\n"
        "*Sources:* MusicBrainz, Wikipedia, Genius",
        parse_mode="Markdown",
    )


async def handle_song_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = (update.message.text or "").strip()

    if len(query) < 2:
        await update.message.reply_text("Enter a song name to search.")
        return

    loading = await update.message.reply_text("🔎 Searching the music archive…")
    results = SongMetadataFetcher.search_songs(query)

    if not results:
        await loading.edit_text(
            "❌ No matches found.\n\n"
            "Try a simpler search, for example:\n"
            "`House Tour`",
            parse_mode="Markdown",
        )
        return

    context.user_data["search_results"] = results

    keyboard = []

    for index, song in enumerate(results):
        artist_text = song["artist"]

        if song["featured_artists"]:
            artist_text += f" ft. {', '.join(song['featured_artists'])}"

        keyboard.append([
            InlineKeyboardButton(
                f"🎵 {song['title']} — {artist_text}",
                callback_data=f"song:{index}",
            )
        ])

    await loading.edit_text(
        "🎼 *Choose the correct track*",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def handle_song_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not query.data.startswith("song:"):
        return

    try:
        index = int(query.data.split(":")[1])
        song = context.user_data["search_results"][index]
    except (ValueError, KeyError, IndexError):
        await query.edit_message_text(
            "This search has expired. Send the song name again."
        )
        return

    await query.edit_message_text("⏳ Building your music brief…")

    metadata = SongMetadataFetcher.get_metadata(song)

    artist_line = metadata["artist"]

    if metadata["featured_artists"]:
        artist_line += f" ft. {', '.join(metadata['featured_artists'])}"

    message = (
        "🎵 *EchoAtlas Music Brief*\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"📌 *Title:* {metadata['title']}\n"
        f"🎤 *Artist:* {artist_line}\n"
    )

    if metadata["album"]:
        message += f"💿 *Album:* {metadata['album']}\n"

    if metadata["year"]:
        message += f"📅 *Released:* {metadata['year']}\n"

    if metadata["genre"]:
        message += f"🎶 *Genre:* {metadata['genre']}\n"

    if metadata["about"]:
        message += (
            "\n📖 *Song Context*\n"
            f"_{metadata['about']}_\n"
        )

    buttons = []

    if metadata["genius_url"]:
        buttons.append([
            InlineKeyboardButton(
                "📝 Open Lyrics on Genius",
                url=metadata["genius_url"],
            )
        ])

    if metadata["wikipedia_url"]:
        buttons.append([
            InlineKeyboardButton(
                "📚 Wikipedia",
                url=metadata["wikipedia_url"],
            )
        ])

    await query.edit_message_text(
        message,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons) if buttons else None,
        disable_web_page_preview=True,
    )


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
