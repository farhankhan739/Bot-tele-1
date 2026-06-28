#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
bot.py

A Telegram deep-link delivery bot built with python-telegram-bot v22+.

Flow (NO membership gate in this version):
    1. User opens a deep link, e.g. https://t.me/YourBot?start=jjk_ep1
    2. Bot immediately copies the episode/file from a PRIVATE storage
       channel straight to the user (the user never sees or joins the
       storage channel itself).
    3. Bot sends a follow-up notice: "This message will be deleted in
       10 minutes."
    4. After 10 minutes, the bot automatically deletes BOTH the episode
       message and the notice message from the user's chat.

Run locally or on Railway with:
    python bot.py

IMPORTANT - install the job-queue extra (needed for scheduled deletion):
    pip install "python-telegram-bot[job-queue]"

Environment variables required:
    BOT_TOKEN          - your bot token from @BotFather
    STORAGE_CHANNEL_ID - numeric chat_id of the PRIVATE storage channel
                         (e.g. -1001234567890). The bot must be an admin
                         there. This is shared across all campaigns.

Configuration (config.json, same directory as this file):
    Each key is a deep-link parameter. Each value needs:
        - message_id : the message_id inside STORAGE_CHANNEL_ID that
                        contains the actual episode/file

    Example:
    {
      "jjk_ep1": { "message_id": 101 },
      "jjk_ep2": { "message_id": 102 }
    }
"""

import json
import logging
import os
import sys
from typing import Any, Dict

from dotenv import load_dotenv
from telegram import Update
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# How long (in seconds) the episode + notice should stay before being
# auto-deleted. 10 minutes = 600 seconds.
AUTO_DELETE_SECONDS = 10 * 60

# ---------------------------------------------------------------------------
# Load environment variables (.env locally; Railway injects real env vars
# directly, so load_dotenv() is a harmless no-op there if no .env exists)
# ---------------------------------------------------------------------------
load_dotenv()

# ---------------------------------------------------------------------------
# Read and validate required environment variables
# ---------------------------------------------------------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    logger.critical(
        "BOT_TOKEN environment variable is not set. "
        "Set it locally in a .env file or in Railway's Variables tab."
    )
    sys.exit(1)

STORAGE_CHANNEL_ID_RAW = os.getenv("STORAGE_CHANNEL_ID")
if not STORAGE_CHANNEL_ID_RAW:
    logger.critical(
        "STORAGE_CHANNEL_ID environment variable is not set. "
        "It must be the numeric chat_id of your private storage channel "
        "(e.g. -1001234567890)."
    )
    sys.exit(1)

try:
    # Telegram numeric chat IDs for channels/supergroups are negative
    # integers (e.g. -1001234567890), so STORAGE_CHANNEL_ID must be int,
    # not a string, when passed to the API.
    STORAGE_CHANNEL_ID = int(STORAGE_CHANNEL_ID_RAW)
except ValueError:
    logger.critical(
        "STORAGE_CHANNEL_ID must be a numeric chat_id (e.g. -1001234567890). "
        "Got: %r",
        STORAGE_CHANNEL_ID_RAW,
    )
    sys.exit(1)

# ---------------------------------------------------------------------------
# Path to the campaign configuration file (same directory as this script)
# ---------------------------------------------------------------------------
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")


def load_config(path: str) -> Dict[str, Any]:
    """
    Loads the campaign configuration from config.json exactly once at
    startup. Returns an empty dict (with a warning logged) if the file
    is missing or malformed, so the bot can still start and simply
    report "Invalid or expired link." for every deep link rather than
    crashing.
    """
    if not os.path.exists(path):
        logger.warning(
            "config.json not found at '%s'. The bot will start, but every "
            "deep link will be treated as invalid until the file is added.",
            path,
        )
        return {}

    try:
        with open(path, "r", encoding="utf-8") as config_file:
            data = json.load(config_file)
    except json.JSONDecodeError as exc:
        logger.error("config.json contains invalid JSON: %s", exc)
        return {}
    except OSError as exc:
        logger.error("Failed to read config.json: %s", exc)
        return {}

    if not isinstance(data, dict):
        logger.error("config.json must contain a top-level JSON object. Ignoring file.")
        return {}

    # Validate each campaign entry; skip (and warn about) malformed ones
    # instead of letting one bad entry break the whole config.
    validated: Dict[str, Any] = {}
    for key, value in data.items():
        has_message_id = isinstance(value, dict) and isinstance(value.get("message_id"), int)

        if has_message_id:
            validated[key] = value
        else:
            logger.warning(
                "Skipping campaign '%s': requires 'message_id' (integer).",
                key,
            )

    logger.info("Loaded %d campaign(s) from config.json.", len(validated))
    return validated


# Config is loaded once at startup and kept in memory for the lifetime
# of the process. Restart the bot to pick up changes to config.json.
CAMPAIGNS: Dict[str, Any] = load_config(CONFIG_PATH)


async def deliver_episode(
    context: ContextTypes.DEFAULT_TYPE, user_id: int, message_id: int
):
    """
    Copies the episode/file message from the private STORAGE_CHANNEL_ID
    straight to the user via copy_message. The user never sees or joins
    the storage channel - the bot acts as the middleman.

    Returns the resulting Message object on success, or None if delivery
    failed (e.g. the source message was deleted, or the bot lost admin
    rights in the storage channel).
    """
    try:
        copied_message = await context.bot.copy_message(
            chat_id=user_id,
            from_chat_id=STORAGE_CHANNEL_ID,
            message_id=message_id,
        )
        return copied_message
    except (BadRequest, Forbidden) as exc:
        logger.error(
            "Failed to copy message_id=%s from storage channel to user %s: %s",
            message_id,
            user_id,
            exc,
        )
        return None
    except TelegramError as exc:
        logger.error("Unexpected Telegram error during copy_message: %s", exc)
        return None


async def delete_messages_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Scheduled job callback (run once, AUTO_DELETE_SECONDS after delivery).
    Deletes both the episode message and the notice message from the
    user's chat.

    job.data carries the info we need: chat_id and the list of
    message_ids to delete (set when the job was scheduled).
    """
    job = context.job
    chat_id = job.data["chat_id"]
    message_ids = job.data["message_ids"]

    for message_id in message_ids:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
        except (BadRequest, Forbidden) as exc:
            # Common if the user already deleted the message themselves,
            # or blocked the bot in the meantime - safe to ignore.
            logger.warning(
                "Could not delete message_id=%s in chat %s: %s",
                message_id,
                chat_id,
                exc,
            )
        except TelegramError as exc:
            logger.error("Unexpected error deleting message_id=%s: %s", message_id, exc)


async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handles the /start command, including deep-link parameters such as
    /start jjk_ep1 (sent by Telegram when a user opens
    https://t.me/YourBot?start=jjk_ep1).

    No membership gate in this version - the episode is delivered
    immediately, followed by a "will be deleted in 10 minutes" notice.
    Both messages are auto-deleted after AUTO_DELETE_SECONDS.
    """
    message = update.message
    user = update.effective_user
    if message is None or user is None:
        return

    # context.args contains whatever follows "/start ", split by spaces.
    # e.g. "/start jjk_ep1" -> context.args == ["jjk_ep1"]
    args = context.args
    param = args[0].strip().lower() if args else None

    # No parameter, or parameter not found in our loaded config.
    if not param or param not in CAMPAIGNS:
        logger.info("Invalid or missing deep-link parameter: %r", param)
        await message.reply_text("Invalid or expired link.")
        return

    campaign = CAMPAIGNS[param]
    message_id = campaign["message_id"]

    # Step 1: deliver the episode immediately.
    episode_message = await deliver_episode(context, user.id, message_id)
    if episode_message is None:
        await message.reply_text(
            "⚠️ We couldn't deliver this content right now. "
            "Please try again later or contact support."
        )
        return

    # Step 2: send the "will be deleted" notice right after the episode.
    notice_message = await context.bot.send_message(
        chat_id=user.id,
        text="⏳ This message will be deleted in 10 minutes. Please save it if needed.",
    )

    # Step 3: schedule deletion of BOTH messages after AUTO_DELETE_SECONDS.
    # context.job_queue.run_once schedules a one-off job; job.data carries
    # everything delete_messages_job needs when it eventually fires.
    context.job_queue.run_once(
        callback=delete_messages_job,
        when=AUTO_DELETE_SECONDS,
        data={
            "chat_id": user.id,
            "message_ids": [episode_message.message_id, notice_message.message_id],
        },
        name=f"auto_delete_{user.id}_{episode_message.message_id}",
    )


async def unknown_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Catches any message that isn't recognized as a known command
    (registered as a fallback MessageHandler with filters.COMMAND).
    """
    message = update.message
    if message is None:
        return
    await message.reply_text("Unknown command. Please use a valid deep link to get started.")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Global error handler. Logs unhandled exceptions with traceback so
    they're visible in Railway's log viewer, without crashing the bot.
    """
    logger.error("Unhandled exception while processing update %s", update, exc_info=context.error)


def main() -> None:
    """
    Builds the Application, registers all handlers, and starts polling.
    """
    logger.info("Starting bot...")

    application = ApplicationBuilder().token(BOT_TOKEN).build()

    # /start (with or without a deep-link parameter)
    application.add_handler(CommandHandler("start", start_handler))

    # Fallback for any other command the bot doesn't explicitly handle.
    # filters.COMMAND matches any message starting with "/".
    application.add_handler(MessageHandler(filters.COMMAND, unknown_command_handler))

    # Global error handler for unhandled exceptions in any handler above.
    application.add_error_handler(error_handler)

    logger.info("Bot is running. Press Ctrl+C to stop.")

    # Long polling - no webhook server, no open port required. This is
    # exactly what's needed for a Railway "Worker" style deployment.
    application.run_polling(allowed_updates=["message"])


if __name__ == "__main__":
    main()
