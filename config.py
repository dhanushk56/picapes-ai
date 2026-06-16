"""
config.py — Bot configuration.
"""

import os


class Config:
    TOKEN: str = ""

    CLOUDFLARE_ACCOUNT_ID: str = ""
    CLOUDFLARE_API_TOKEN: str = ""

    COOKIE_API_KEY: str = "https://api.cookie-api.com/api/transcript?channel_id={channel.id}"

    GUILD_ID: int = int(os.getenv("GUILD_ID", "0") or 0)

    TICKET_CATEGORY_NAME: str = "Tickets"

    COLOR_INFO: int = 0x5865F2   # Blurple
    COLOR_OK:   int = 0x57F287   # Green
    COLOR_ERR:  int = 0xED4245   # Red
    COLOR_MOD:  int = 0xFEE75C   # Yellow

    # Dashboard OAuth2 — https://discord.com/developers/applications
    DISCORD_CLIENT_ID: str = os.getenv("DISCORD_CLIENT_ID", "")
    DISCORD_CLIENT_SECRET: str = os.getenv("DISCORD_CLIENT_SECRET", "")
    DISCORD_REDIRECT_URI: str = os.getenv("DISCORD_REDIRECT_URI", "")