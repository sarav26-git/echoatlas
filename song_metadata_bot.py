"""
EchoAtlas - Telegram Bot for Song Metadata

Sources:
- Wikipedia: song metadata
- Genius: song description and lyrics
- MusicBrainz: search + metadata fallback
"""

import os
import re
import logging
import asyncio
from typing import List, Dict, Optional

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

# ── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── API Configuration ────────────────────────────────────────────────────────

MUSICBRAINZ_API = "https://musicbrainz.org/ws/2"
MUSICBRAINZ_USER_AGENT = "EchoAtlasBot/2.0 (echoatlasbot@telegram.com)"

GENIUS_API = "https://api.genius.com"
GENIUS_ACCESS_TOKEN = os.getenv("GENIUS_ACCESS_TOKEN")

WIKIPEDIA_API = "https://en.wikipedia.org/w/api.php"
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

_COMPILATION_RE = re.compile(
    r"\b(hits|best of|greatest|collection|playlist|vol\.|volume|"
    r"compilation|anthology|essentials|now that'?s|top\s*\d|"
    r"\d+\s*%|nrj|universal music)\b",
    re.IGNORECASE,
)


class SongMetadataFetcher:
    """Fetch song search results, metadata, descriptions, and lyrics."""

    @staticmethod
    def search_songs(song_name: str) -> List[Dict]:
        try:
            headers = {"User-Agent": MUSICBRAINZ_USER_AGENT}
            params = {
                "query": f'recording:"{song_name}"',
                "fmt": "json",
                "limit": 15,
            }

            response = requests.get(
                f"{MUSICBRAINZ_API}/recording/",
                params=params,
                headers=headers,
                timeout=10,
            )
            response.raise_for_status()
            data = response.json()

            if len(data.get("recordings", [])) < 3:
                params["query"] = song_name
                response = requests.get(
                    f"{MUSICBRAINZ_API}/recording/",
                    params=params,
                    headers=headers,
                    timeout=10,
                )
                response.raise_for_status()
                data = response.json()

            results = []
            seen = set()

            for recording in data.get("recordings", []):
                credits = recording.get("artist-credit", [])

                if not credits:
                    continue

                artists = [credit["name"] for credit in credits if "name" in credit]
                if not artists:
                    continue

                title = recording.get("title", "")
                artist = artists[0]
                key = f"{title.lower()}_{'&'.join(sorted(artists)).lower()}"

                if key in seen:
                    continue

                seen.add(key)

                results.append(
                    {
                        "id": recording.get("id"),
                        "title": title,
                        "artist": artist,
                        "featured_artists": artists[1:],
                        "score": recording.get("score", 0),
                    }
                )

            results.sort(key=lambda item: item.get("score", 0), reverse=True)
            return results[:10]

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
                timeout=10,
            )
            search_response.raise_for_status()

            results = search_response.json().get("query", {}).get("search", [])

            for result in results[:2]:
                page_title = result["title"]

                page_response = requests.get(
                    WIKIPEDIA_API,
                    params={
                        "action": "parse",
                        "page": page_title,
                        "prop": "text",
                        "format": "json",
                    },
                    timeout=10,
                )
                page_response.raise_for_status()

                page_data = page_response.json()

                if "parse" not in page_data:
                    continue

                soup = BeautifulSoup(
                    page_data["parse"]["text"]["*"],
                    "html.parser",
                )

                infobox = soup.find("table", class_="infobox")

                if not infobox:
                    continue

                metadata = {}

                for row in infobox.find_all("tr"):
                    heading = row.find("th")
                    value = row.find("td")

                    if not heading or not value:
                        continue

                    key = heading.get_text(strip=True).lower()
                    text = value.get_text(separator=" ", strip=True)

                    if "artist" in key:
                        artists = [
                            item.strip()
                            for item in re.split(r"featuring|feat\.|ft\.|,|&", text)
                            if item.strip()
                        ]

                        if artists:
                            metadata["artist"] = artists[0]
                            metadata["featured_artists"] = artists[1:]

                    elif "album" in key:
                        metadata["album"] = text.split("(")[0].strip()

                    elif "released" in key or "published" in key:
                        year_match = re.search(r"\b(19|20)\d{2}\b", text)
                        if year_match:
                            metadata["year"] = year_match.group(0)

                    elif "genre" in key:
                        genres = [
                            item.strip()
                            for item in re.split(r",|;|\n", text)
                            if len(item.strip()) > 2
                        ]

                        if genres:
                            metadata["genre"] = ", ".join(genres[:4])

                metadata["url"] = (
                    "https://en.wikipedia.org/wiki/"
                    + page_title.replace(" ", "_")
                )

                if metadata.get("artist") or metadata.get("album"):
                    return metadata

            return None

        except Exception as error:
            logger.error("Wikipedia metadata error: %s", error)
            return None

    @staticmethod
    def _clean_lyrics(text: str) -> str:
        """Clean lyrics extracted from Genius HTML."""

        text = text.replace("&amp;", "&")
        text = text.replace("&apos;", "'")
        text = text.replace("&#x27;", "'")

        text = re.sub(r"[ \t]{2,}", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = re.sub(r"(?<!\w)\d{4,6}(?!\w)", "", text)

        lines = [line.rstrip() for line in text.splitlines()]
        return "\n".join(lines).strip()

    @staticmethod
    def _html_to_plain(html: str) -> str:
        """Convert Genius lyric HTML to clean Telegram-ready text."""

        soup = BeautifulSoup(html, "html.parser")

        def walk(node) -> str:
            parts = []

            for child in node.children:
                if isinstance(child, NavigableString):
                    parts.append(str(child))

                elif isinstance(child, Tag):
                    if child.name == "br":
                        parts.append("\n")

                    elif child.name in (
                        "a",
                        "span",
                        "b",
                        "i",
                        "em",
                        "strong",
                    ):
                        parts.append(walk(child))

                    elif child.name == "div":
                        inner = walk(child).strip()
                        if inner:
                            parts.append(f"\n{inner}\n")

            return "".join(parts)

        containers = soup.find_all(
            "div",
            {"data-lyrics-container": "true"},
        )

        if containers:
            lyrics = "\n\n".join(
                walk(container).strip()
                for container in containers
                if walk(container).strip()
            )
        else:
            lyrics = walk(soup).strip()

        return SongMetadataFetcher._clean_lyrics(lyrics)

    @staticmethod
    def _clean_about(text: str, title: str = "", artist: str = "") -> str:
        """
        Removes Genius UI junk such as:
        - Song title Lyrics
        - Read More
        - Contributors
        - Translations
        - Share
        - Braces and copied UI text
        """

        if not text:
            return ""

        text = re.sub(r"\s+", " ", text).strip()

        text = re.sub(
            r"^(Song Bio|About|Lyrics)\s*",
            "",
            text,
            flags=re.IGNORECASE,
        )

        text = re.sub(
            r"^[A-Za-z0-9 .,&'’\-]+ Lyrics\s*",
            "",
            text,
            flags=re.IGNORECASE,
        )

        if title:
            escaped_title = re.escape(title)

            if artist:
                escaped_artist = re.escape(artist)
                text = re.sub(
                    rf"^{escaped_title}\s*(?:by|—|-)?\s*{escaped_artist}\s*",
                    "",
                    text,
                    flags=re.IGNORECASE,
                )

            text = re.sub(
                rf"^{escaped_title}\s*",
                "",
                text,
                flags=re.IGNORECASE,
            )

        text = re.sub(
            r"\b(Read More|Expand|Share|Translations?|Contributors?|Comments?)\b.*$",
            "",
            text,
            flags=re.IGNORECASE,
        )

        text = re.sub(r"[\{\}\[\]]+", "", text)
        text = re.sub(r"\s{2,}", " ", text)

        return text.strip()

    @staticmethod
    def _scrape_page(song_url: str, title: str, artist: str) -> Dict:
        """
        Fetch Genius page once and extract:
        - Clean About description
        - Clean Lyrics
        """

        output = {}

        try:
            response = requests.get(
                song_url,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 "
                        "(KHTML, like Gecko) "
                        "Chrome/120.0 Safari/537.36"
                    )
                },
                timeout=15,
            )

            if response.status_code != 200:
                return output

            soup = BeautifulSoup(response.text, "html.parser")

            # ── About ──────────────────────────────────────────────────────

            for tag in soup.find_all(["section", "div"]):
                classes = " ".join(tag.get("class", []))

                if re.search(r"About\w*Content|SongDescription", classes):
                    description = SongMetadataFetcher._clean_about(
                        tag.get_text(separator=" ", strip=True),
                        title,
                        artist,
                    )

                    if len(description) > 40:
                        output["description"] = description
                        break

            # ── Lyrics ─────────────────────────────────────────────────────

            lyric_divs = soup.find_all(
                "div",
                {"data-lyrics-container": "true"},
            )

            if lyric_divs:
                lyrics_html = "".join(str(div) for div in lyric_divs)
                lyrics = SongMetadataFetcher._html_to_plain(lyrics_html)

                if lyrics:
                    output["lyrics"] = lyrics

        except Exception as error:
            logger.warning("Genius scrape error: %s", error)

        return output

    @staticmethod
    def get_genius_full_data(track: str, artist: str) -> Optional[Dict]:
        """Get Genius description, lyrics, album, and source URL."""

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
                timeout=10,
            )
            search_response.raise_for_status()

            hits = search_response.json().get("response", {}).get("hits", [])

            if not hits:
                return None

            song_info = None

            for hit in hits[:5]:
                result = hit["result"]
                genius_artist = result.get(
                    "primary_artist",
                    {},
                ).get("name", "").lower()

                if artist.lower() in genius_artist or genius_artist in artist.lower():
                    song_info = result
                    break

            if not song_info:
                song_info = hits[0]["result"]

            song_id = song_info["id"]
            song_url = song_info.get("url", "")

            detail_response = requests.get(
                f"{GENIUS_API}/songs/{song_id}",
                headers=headers,
                params={"text_format": "plain"},
                timeout=10,
            )
            detail_response.raise_for_status()

            song_details = detail_response.json().get(
                "response",
                {},
            ).get("song", {})

            result = {
                "url": song_url,
            }

            album = (song_details.get("album") or {}).get("name")
            if album:
                result["genius_album"] = album

            # First: page scrape for clean About + lyrics
            if song_url:
                scraped = SongMetadataFetcher._scrape_page(
                    song_url,
                    track,
                    artist,
                )

                if scraped.get("description"):
                    result["description"] = scraped["description"]

                if scraped.get("lyrics"):
                    result["lyrics"] = scraped["lyrics"]

            # Fallback: Genius API description
            if not result.get("description"):
                plain_description = (
                    song_details.get("description") or {}
                ).get("plain", "").strip()

                cleaned_description = SongMetadataFetcher._clean_about(
                    plain_description,
                    track,
                    artist,
                )

                if len(cleaned_description) > 40:
                    result["description"] = cleaned_description

            # Fallback: Genius API HTML lyrics
            if not result.get("lyrics"):
                html_response = requests.get(
                    f"{GENIUS_API}/songs/{song_id}",
                    headers=headers,
                    params={"text_format": "html"},
                    timeout=10,
                )

                if html_response.status_code == 200:
                    html_song = html_response.json().get(
                        "response",
                        {},
                    ).get("song", {})

                    lyrics_html = (
                        html_song.get("lyrics") or {}
                    ).get("html", "")

                    if lyrics_html:
                        lyrics = SongMetadataFetcher._html_to_plain(
                            lyrics_html
                        )

                        if len(lyrics) > 20:
                            result["lyrics"] = lyrics

            return result if result.get("description") or result.get("lyrics") else None

        except Exception as error:
            logger.error("Genius API error: %s", error)
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
            "description": "No description available",
            "lyrics": None,
            "genius_url": None,
            "wikipedia_url": None,
        }

        try:
            wiki = SongMetadataFetcher.get_wikipedia_metadata(
                song_title,
                artist,
            )

            if wiki:
                for key in (
                    "artist",
                    "featured_artists",
                    "album",
                    "year",
                    "genre",
                ):
                    if wiki.get(key):
                        metadata[key] = wiki[key]

                if wiki.get("url"):
                    metadata["wikipedia_url"] = wiki["url"]

            if any(
                metadata[key] == "Unknown"
                for key in ("album", "year", "genre")
            ):
                response = requests.get(
                    f"{MUSICBRAINZ_API}/recording/{recording_id}",
                    params={
                        "inc": "releases+artist-credits+genres+tags",
                        "fmt": "json",
                    },
                    headers={"User-Agent": MUSICBRAINZ_USER_AGENT},
                    timeout=10,
                )
                response.raise_for_status()

                musicbrainz_data = response.json()

                if not metadata["featured_artists"]:
                    credits = musicbrainz_data.get("artist-credit", [])

                    if len(credits) > 1:
                        metadata["featured_artists"] = [
                            credit["name"]
                            for credit in credits[1:]
                            if "name" in credit
                        ]

                if metadata["genre"] == "Unknown":
                    genres = [
                        genre["name"]
                        for genre in musicbrainz_data.get("genres", [])[:3]
                    ]

                    if not genres:
                        genres = [
                            tag["name"]
                            for tag in musicbrainz_data.get("tags", [])[:3]
                        ]

                    if genres:
                        metadata["genre"] = ", ".join(genres)

                releases = musicbrainz_data.get("releases", [])

                if releases and (
                    metadata["album"] == "Unknown"
                    or metadata["year"] == "Unknown"
                ):
                    releases.sort(
                        key=lambda release: (
                            int(
                                bool(
                                    _COMPILATION_RE.search(
                                        release.get("title", "")
                                    )
                                )
                            ),
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
                            metadata["year"] = date.split("-")[0]

            genius = SongMetadataFetcher.get_genius_full_data(
                song_title,
                artist,
            )

            if genius:
                if genius.get("description"):
                    metadata["description"] = genius["description"]

                if genius.get("lyrics"):
                    metadata["lyrics"] = genius["lyrics"]

                if genius.get("url"):
                    metadata["genius_url"] = genius["url"]

                genius_album = genius.get("genius_album", "")

                if genius_album and (
                    metadata["album"] == "Unknown"
                    or _COMPILATION_RE.search(metadata["album"])
                ):
                    metadata["album"] = genius_album

        except Exception as error:
            logger.error("Detailed metadata error: %s", error)

        return metadata


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎵 *Welcome to EchoAtlas!*\n\n"
        "Your music metadata companion.\n\n"
        "📌 Song details, album, year and genre\n"
        "📖 Song descriptions from Genius\n"
        "📝 Lyrics with one tap\n"
        "🔗 Direct source links\n\n"
        "✨ *Send any song name with artist*\n\n"
        '_Example: "Wildflower - Billie Eilish"_',
        parse_mode="Markdown",
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *How to use EchoAtlas*\n\n"
        "1️⃣ Send a song name\n"
        "2️⃣ Select the correct result\n"
        "3️⃣ Get metadata and lyrics\n\n"
        "*Commands*\n"
        "/start — Start the bot\n"
        "/help — Show this guide",
        parse_mode="Markdown",
    )


async def handle_song_search(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    song_name = update.message.text.strip()

    if not song_name:
        await update.message.reply_text("Please enter a song name.")
        return

    searching_message = await update.message.reply_text(
        f"🔎 Searching for '{song_name}'..."
    )

    results = SongMetadataFetcher.search_songs(song_name)

    if not results:
        await searching_message.edit_text(
            "❌ No results found. Try another song or artist."
        )
        return

    keyboard = []

    for index, song in enumerate(results):
        artist_text = song["artist"]

        if song["featured_artists"]:
            artist_text += f" ft. {', '.join(song['featured_artists'])}"

        keyboard.append(
            [
                InlineKeyboardButton(
                    f"🎵 {song['title']} — {artist_text}",
                    callback_data=f"select_{index}",
                )
            ]
        )

    context.user_data["search_results"] = results

    await searching_message.edit_text(
        "📋 *Select the correct song:*",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )


async def handle_song_selection(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query
    await query.answer()

    callback_data = query.data

    # ── Lyrics button ─────────────────────────────────────────────────────────

    if callback_data == "show_lyrics":
        metadata = context.user_data.get("current_metadata")

        if not metadata or not metadata.get("lyrics"):
            await query.answer("Lyrics are not available.", show_alert=True)
            return

        lyrics = metadata["lyrics"]
        title = metadata.get("title", "Lyrics")
        artist = metadata.get("artist", "")

        header = f"📝 *{title}*"
        if artist:
            header += f" — {artist}"
        header += "\n\n"

        maximum_length = 4096 - len(header) - 100
        lyrics_body = lyrics
        suffix = ""

        if len(lyrics_body) > maximum_length:
            lyrics_body = lyrics_body[:maximum_length]

            if "\n" in lyrics_body:
                lyrics_body = lyrics_body[:lyrics_body.rfind("\n")]

            suffix = "\n\n_(Lyrics shortened. Use the Genius link for the full page.)_"

        safe_lyrics = (
            lyrics_body.replace("\\", "\\\\")
            .replace("_", "\\_")
            .replace("*", "\\*")
            .replace("[", "\\[")
            .replace("`", "\\`")
        )

        await query.message.reply_text(
            f"{header}{safe_lyrics}{suffix}",
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )
        return

    # ── Song result selection ─────────────────────────────────────────────────

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
            "❌ Search expired. Please search again."
        )
        return

    await query.edit_message_text("⏳ Fetching metadata...")

    song = results[index]

    metadata = SongMetadataFetcher.get_detailed_metadata(
        song["id"],
        song["title"],
        song["artist"],
    )

    context.user_data["current_metadata"] = metadata

    artist_line = metadata["artist"]

    if metadata.get("featured_artists"):
        artist_line += f" ft. {', '.join(metadata['featured_artists'])}"

    message = "🎵 *Here's the Metadata...*\n\n"
    message += f"📌 *Title:* {metadata['title']}\n"
    message += f"🎤 *Artist:* {artist_line}\n"

    if metadata["album"] != "Unknown":
        message += f"💿 *Album:* {metadata['album']}\n"

    if metadata["year"] != "Unknown":
        message += f"📅 *Year:* {metadata['year']}\n"

    if metadata["genre"] != "Unknown":
        message += f"🎶 *Genre:* {metadata['genre'].title()}\n"

    if metadata["description"] != "No description available":
        description = metadata["description"]

        if len(description) > 900:
            description = description[:900]
            last_period = description.rfind(". ")

            if last_period > 400:
                description = description[:last_period + 1]

            description += "…"

        safe_description = (
            description.replace("_", "\\_")
            .replace("*", "\\*")
            .replace("[", "\\[")
            .replace("`", "\\`")
        )

        message += f"\n📖 *About:*\n_{safe_description}_\n"

    links = []

    if metadata.get("genius_url"):
        links.append(f"[Genius]({metadata['genius_url']})")

    if metadata.get("wikipedia_url"):
        links.append(f"[Wikipedia]({metadata['wikipedia_url']})")

    if links:
        message += f"\n🔗 More: {' • '.join(links)}"

    buttons = []

    if metadata.get("lyrics"):
        buttons.append(
            [
                InlineKeyboardButton(
                    "📝 View Lyrics",
                    callback_data="show_lyrics",
                )
            ]
        )

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
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_song_search,
        )
    )
    application.add_handler(
        CallbackQueryHandler(handle_song_selection)
    )

    return application


def main():
    if not TELEGRAM_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN is missing.")
        return

    if not GENIUS_ACCESS_TOKEN:
        logger.warning(
            "GENIUS_ACCESS_TOKEN is missing. "
            "Lyrics and Genius descriptions will not work."
        )

    if os.name == "nt":
        asyncio.set_event_loop_policy(
            asyncio.WindowsSelectorEventLoopPolicy()
        )

    application = build_application()

    logger.info("EchoAtlas polling bot started.")
    application.run_polling()


if __name__ == "__main__":
    main()
