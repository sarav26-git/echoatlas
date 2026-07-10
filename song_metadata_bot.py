"""
EchoAtlas — Telegram song metadata bot
Sources:
- MusicBrainz: search + fallback metadata
- Wikipedia: metadata
- Genius: song description + source link
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
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ── Config ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

MUSICBRAINZ_API = "https://musicbrainz.org/ws/2"
MUSICBRAINZ_USER_AGENT = "EchoAtlasBot/2.1 (Telegram Bot)"

WIKIPEDIA_API = "https://en.wikipedia.org/w/api.php"
GENIUS_API = "https://api.genius.com"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GENIUS_ACCESS_TOKEN = os.getenv("GENIUS_ACCESS_TOKEN")

COMPILATION_RE = re.compile(
    r"\b(hits|best of|greatest|collection|playlist|vol\.|volume|"
    r"compilation|anthology|essentials|top\s*\d)\b",
    re.IGNORECASE,
)


# ── Data Fetcher ─────────────────────────────────────────────────────────────

class SongMetadataFetcher:

    @staticmethod
    def search_songs(song_name: str) -> List[Dict]:
        try:
            headers = {"User-Agent": MUSICBRAINZ_USER_AGENT}
            params = {
                "query": f'recording:"{song_name}"',
                "fmt": "json",
                "limit": 10,
            }

            response = requests.get(
                f"{MUSICBRAINZ_API}/recording/",
                params=params,
                headers=headers,
                timeout=12,
            )
            response.raise_for_status()

            recordings = response.json().get("recordings", [])
            results = []
            seen = set()

            for recording in recordings:
                credits = recording.get("artist-credit", [])
                if not credits:
                    continue

                artists = [item.get("name", "") for item in credits if item.get("name")]
                if not artists:
                    continue

                title = recording.get("title", "")
                key = f"{title.lower()}-{','.join(artists).lower()}"

                if key in seen:
                    continue
                seen.add(key)

                results.append({
                    "id": recording.get("id"),
                    "title": title,
                    "artist": artists[0],
                    "featured_artists": artists[1:],
                    "score": recording.get("score", 0),
                })

            return sorted(results, key=lambda item: item["score"], reverse=True)[:10]

        except Exception as error:
            logger.error("MusicBrainz search error: %s", error)
            return []

    @staticmethod
    def get_wikipedia_metadata(track: str, artist: str) -> Optional[Dict]:
        try:
            search_response = requests.get(
                WIKIPEDIA_API,
                params={
                    "action": "query",
                    "list": "search",
                    "srsearch": f'"{track}" {artist} song',
                    "format": "json",
                    "srlimit": 3,
                },
                timeout=12,
            )
            search_response.raise_for_status()

            results = search_response.json().get("query", {}).get("search", [])

            for result in results:
                page_title = result["title"]

                page_response = requests.get(
                    WIKIPEDIA_API,
                    params={
                        "action": "parse",
                        "page": page_title,
                        "prop": "text",
                        "format": "json",
                    },
                    timeout=12,
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
                        parts = re.split(r"featuring|feat\.|ft\.|,|&", text, flags=re.I)
                        parts = [part.strip() for part in parts if part.strip()]
                        if parts:
                            metadata["artist"] = parts[0]
                            metadata["featured_artists"] = parts[1:]

                    elif "album" in key:
                        metadata["album"] = text.split("(")[0].strip()

                    elif "released" in key:
                        year = re.search(r"\b(19|20)\d{2}\b", text)
                        if year:
                            metadata["year"] = year.group(0)

                    elif "genre" in key:
                        metadata["genre"] = text

                metadata["url"] = (
                    "https://en.wikipedia.org/wiki/" + page_title.replace(" ", "_")
                )

                if metadata.get("artist") or metadata.get("album"):
                    return metadata

            return None

        except Exception as error:
            logger.error("Wikipedia error: %s", error)
            return None

    @staticmethod
    def clean_about(text: str) -> str:
        text = re.sub(
            r"^\s*(Song Bio|About|[0-9]+\s+Contributors?)\s*",
            "",
            text,
            flags=re.I,
        )
        text = re.sub(r"\s*Read More.*$", "", text, flags=re.I | re.S)
        text = re.sub(r"\s*Expand.*$", "", text, flags=re.I | re.S)
        text = re.sub(r"\s+", " ", text).strip()
        return text

    @staticmethod
    def scrape_genius_page(song_url: str) -> Dict:
        """
        Best-effort fetch. It may work locally but can be blocked on Vercel.
        The bot still provides the Genius link even when this fails.
        """
        output = {}

        try:
            response = requests.get(
                song_url,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 Chrome/120 Safari/537.36"
                    )
                },
                timeout=15,
            )

            if response.status_code != 200:
                return output

            soup = BeautifulSoup(response.text, "html.parser")

            # About / description
            about_element = soup.find(
                "div",
                class_=re.compile(r"SongDescription|AboutSong", re.I),
            )

            if about_element:
                about = SongMetadataFetcher.clean_about(
                    about_element.get_text(" ", strip=True)
                )
                if len(about) > 40:
                    output["description"] = about

            # Lyrics
            lyric_containers = soup.find_all(
                "div",
                attrs={"data-lyrics-container": "true"},
            )

            if lyric_containers:
                lyric_parts = []

                for container in lyric_containers:
                    for br in container.find_all("br"):
                        br.replace_with("\n")

                    text = container.get_text("", strip=False)
                    text = re.sub(r"\n{3,}", "\n\n", text).strip()

                    if text:
                        lyric_parts.append(text)

                lyrics = "\n\n".join(lyric_parts).strip()

                if len(lyrics) > 30:
                    output["lyrics"] = lyrics

        except Exception as error:
            logger.warning("Genius scrape failed: %s", error)

        return output

    @staticmethod
    def get_genius_data(track: str, artist: str) -> Optional[Dict]:
        if not GENIUS_ACCESS_TOKEN:
            logger.warning("GENIUS_ACCESS_TOKEN is missing.")
            return None

        try:
            headers = {"Authorization": f"Bearer {GENIUS_ACCESS_TOKEN}"}

            search_response = requests.get(
                f"{GENIUS_API}/search",
                headers=headers,
                params={"q": f"{track} {artist}"},
                timeout=12,
            )
            search_response.raise_for_status()

            hits = search_response.json().get("response", {}).get("hits", [])
            if not hits:
                return None

            selected = None

            for hit in hits[:5]:
                result = hit["result"]
                genius_artist = result.get("primary_artist", {}).get("name", "").lower()

                if artist.lower() in genius_artist or genius_artist in artist.lower():
                    selected = result
                    break

            if not selected:
                selected = hits[0]["result"]

            song_id = selected["id"]
            song_url = selected.get("url")

            detail_response = requests.get(
                f"{GENIUS_API}/songs/{song_id}",
                headers=headers,
                params={"text_format": "plain"},
                timeout=12,
            )
            detail_response.raise_for_status()

            song = detail_response.json().get("response", {}).get("song", {})

            output = {
                "url": song_url,
                "album": (song.get("album") or {}).get("name"),
            }

            description = (song.get("description") or {}).get("plain", "").strip()

            if description and description != "?":
                cleaned = SongMetadataFetcher.clean_about(description)
                if len(cleaned) > 40:
                    output["description"] = cleaned

            # Optional scrape for lyrics. Failure does NOT break metadata.
            if song_url:
                scraped = SongMetadataFetcher.scrape_genius_page(song_url)

                if scraped.get("description"):
                    output["description"] = scraped["description"]

                if scraped.get("lyrics"):
                    output["lyrics"] = scraped["lyrics"]

            return output

        except Exception as error:
            logger.error("Genius API error: %s", error)
            return None

    @staticmethod
    def get_detailed_metadata(recording_id: str, title: str, artist: str) -> Dict:
        metadata = {
            "title": title,
            "artist": artist,
            "featured_artists": [],
            "album": "Unknown",
            "year": "Unknown",
            "genre": "Unknown",
            "description": None,
            "lyrics": None,
            "genius_url": None,
            "wikipedia_url": None,
        }

        try:
            wiki = SongMetadataFetcher.get_wikipedia_metadata(title, artist)

            if wiki:
                for key in ["artist", "featured_artists", "album", "year", "genre"]:
                    if wiki.get(key):
                        metadata[key] = wiki[key]

                metadata["wikipedia_url"] = wiki.get("url")

            genius = SongMetadataFetcher.get_genius_data(title, artist)

            if genius:
                if genius.get("description"):
                    metadata["description"] = genius["description"]

                if genius.get("lyrics"):
                    metadata["lyrics"] = genius["lyrics"]

                if genius.get("url"):
                    metadata["genius_url"] = genius["url"]

                genius_album = genius.get("album")

                if genius_album and (
                    metadata["album"] == "Unknown"
                    or COMPILATION_RE.search(metadata["album"])
                ):
                    metadata["album"] = genius_album

        except Exception as error:
            logger.error("Metadata error: %s", error)

        return metadata


# ── Telegram Handlers ─────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎵 *Welcome to EchoAtlas!*\n\n"
        "Search any song and get its artist, album, year, genre, music context, "
        "and a direct Genius lyrics link.\n\n"
        "*Example:* `House Tour - Sabrina Carpenter`",
        parse_mode="Markdown",
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *How to use EchoAtlas*\n\n"
        "1. Send a song name\n"
        "2. Choose the correct result\n"
        "3. Open metadata, Genius, or lyrics\n\n"
        "*Sources:* MusicBrainz, Wikipedia, and Genius.",
        parse_mode="Markdown",
    )


async def handle_song_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    song_name = update.message.text.strip()

    if not song_name:
        return

    loading_message = await update.message.reply_text("🔎 Searching...")

    results = SongMetadataFetcher.search_songs(song_name)

    if not results:
        await loading_message.edit_text("❌ No songs found. Try another search.")
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
                callback_data=f"select_{index}",
            )
        ])

    await loading_message.edit_text(
        "📋 *Select the correct song:*",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def handle_song_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    callback_data = query.data

    # ── Show lyrics inside Telegram ───────────────────────────────────────────
    if callback_data == "show_lyrics":
        metadata = context.user_data.get("current_metadata")

        if not metadata or not metadata.get("lyrics"):
            await query.answer(
                "Full lyrics could not be loaded. Use the Genius button.",
                show_alert=True,
            )
            return

        title = metadata.get("title", "Lyrics")
        artist = metadata.get("artist", "")
        lyrics = metadata["lyrics"]

        header = f"📝 *{title}*"
        if artist:
            header += f" — {artist}"
        header += "\n\n"

        max_length = 3900
        body = lyrics

        if len(header) + len(body) > max_length:
            body = body[: max_length - len(header)]
            last_newline = body.rfind("\n")

            if last_newline > 0:
                body = body[:last_newline]

            body += "\n\n_(Lyrics shortened. Open Genius for the full version.)_"

        await query.message.reply_text(
            header + body,
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )
        return

    # ── Song selected ─────────────────────────────────────────────────────────
    if not callback_data.startswith("select_"):
        return

    try:
        index = int(callback_data.split("_")[1])
        results = context.user_data.get("search_results", [])
        song = results[index]
    except (ValueError, IndexError):
        await query.edit_message_text("❌ Search session expired. Search again.")
        return

    await query.edit_message_text("⏳ Fetching song details...")

    metadata = SongMetadataFetcher.get_detailed_metadata(
        song["id"],
        song["title"],
        song["artist"],
    )

    context.user_data["current_metadata"] = metadata

    artist_line = metadata["artist"]

    if metadata["featured_artists"]:
        artist_line += f" ft. {', '.join(metadata['featured_artists'])}"

    message = "🎵 *Here's the Metadata...*\n\n"
    message += f"📌 *Title:* {metadata['title']}\n"
    message += f"🎤 *Artist:* {artist_line}\n"

    if metadata["album"] != "Unknown":
        message += f"💿 *Album:* {metadata['album']}\n"

    if metadata["year"] != "Unknown":
        message += f"📅 *Year:* {metadata['year']}\n"

    if metadata["genre"] != "Unknown":
        message += f"🎶 *Genre:* {metadata['genre']}\n"

    if metadata.get("description"):
        description = metadata["description"]

        if len(description) > 1300:
            description = description[:1300].rsplit(" ", 1)[0] + "…"

        message += f"\n📖 *About:*\n_{description}_\n"

    buttons = []

    # This ALWAYS appears when Genius found the song.
    if metadata.get("genius_url"):
        buttons.append([
            InlineKeyboardButton(
                "🎤 Open Lyrics on Genius",
                url=metadata["genius_url"],
            )
        ])

    # This appears only if lyrics were successfully fetched.
    if metadata.get("lyrics"):
        buttons.append([
            InlineKeyboardButton(
                "📝 Read Lyrics Here",
                callback_data="show_lyrics",
            )
        ])

    if metadata.get("wikipedia_url"):
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
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_song_search)
    )
    application.add_handler(CallbackQueryHandler(handle_song_selection))

    return application


def main():
    if not TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is missing.")

    application = build_application()

    logger.info("EchoAtlas started in polling mode.")
    application.run_polling()


if __name__ == "__main__":
    main()
