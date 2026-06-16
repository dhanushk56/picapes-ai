"""
cogs/ai.py
Slash group: /ai
Auto-respond AI channel — assign a channel and the bot replies to every message there.
Memory system — store server context (staff names, rules, etc.) the AI draws on.
Export/import memory via JSON file.
Per-user rate limiting — global default + per-user overrides.
Character limit — set max response length, enforced via truncation.
Intelligent truncation exemption for copy‑paste content (steps, code, applications).
Request character limit — prevents token consumption when user message is too long.
Image support — downloads Discord images, compresses them, converts to base64 for Cloudflare API.
"""

import discord
from discord.ext import commands
from discord import app_commands
import aiohttp
import json
import io
import time
import re
import base64
import traceback
from PIL import Image
from config import Config
from utils.data import load, save

AI_FILE    = "ai.json"
CF_MODEL   = "@cf/moonshotai/kimi-k2.6"

DEFAULT_RATE_MESSAGES = 10
DEFAULT_RATE_WINDOW   = 60

# Max image size in bytes before compression (Cloudflare limit is ~1MB after base64)
MAX_IMAGE_BYTES = 750_000  # ~750KB raw → ~1MB base64
MAX_IMAGE_DIMENSIONS = (1024, 1024)  # Resize large images to this max

# Patterns that indicate a response should NOT be truncated
DEFAULT_EXEMPT_PATTERNS = [
    r"```",
    r"^\d+\.\s+",
    r"^\-\s+",
    r"^\*\s+",
    r"steps?:",
    r"how to:",
    r"application questions?:",
    r"copy this:",
    r"paste this:",
    r"template:",
]


def _ai_data(guild_id: int) -> dict:
    return load(AI_FILE).get(str(guild_id), {})

def _save_ai_data(guild_id: int, d: dict):
    data = load(AI_FILE)
    data[str(guild_id)] = d
    save(AI_FILE, data)


async def _cf_request(account_id: str, api_token: str, messages: list, max_tokens: int = 800) -> str:
    url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1/chat/completions"
    
    headers = {
        "Authorization": f"Bearer {api_token.strip()}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": CF_MODEL,
        "messages": messages,
        "max_tokens": max_tokens,
    }
    
    async with aiohttp.ClientSession() as session:
        async with session.post(
            url, headers=headers, json=payload,
            timeout=aiohttp.ClientTimeout(total=20)
        ) as resp:
            data = await resp.json()
            
            if resp.status != 200:
                print(f"[DEBUG] CF Response Data: {data}")
                raise Exception(f"Cloudflare error {resp.status}: {data.get('errors', str(data))}")
            
            return data["choices"][0]["message"]["content"].strip()


def _compress_image(raw_bytes: bytes, mime_type: str) -> tuple[bytes, str]:
    """
    Compress/resize an image to fit within Cloudflare's limits.
    Returns (compressed_bytes, output_mime_type).
    """
    try:
        img = Image.open(io.BytesIO(raw_bytes))
        
        # Convert RGBA to RGB if needed (JPEG doesn't support alpha)
        if img.mode in ("RGBA", "P"):
            rgb_img = Image.new("RGB", img.size, (255, 255, 255))
            if img.mode == "P":
                img = img.convert("RGBA")
            rgb_img.paste(img, mask=img.split()[-1] if img.mode == "RGBA" else None)
            img = rgb_img
        
        # Resize if too large
        if img.width > MAX_IMAGE_DIMENSIONS[0] or img.height > MAX_IMAGE_DIMENSIONS[1]:
            img.thumbnail(MAX_IMAGE_DIMENSIONS, Image.Resampling.LANCZOS)
        
        # Save as JPEG with quality reduction until it fits
        output = io.BytesIO()
        quality = 85
        img.save(output, format="JPEG", quality=quality, optimize=True)
        
        while output.tell() > MAX_IMAGE_BYTES and quality > 20:
            output = io.BytesIO()
            quality -= 10
            img.save(output, format="JPEG", quality=quality, optimize=True)
        
        return output.getvalue(), "image/jpeg"
    
    except Exception as e:
        print(f"[AI] Image compression failed: {e}")
        # Return original if compression fails
        return raw_bytes, mime_type


def _build_system_prompt(memory: dict, char_limit: int = None) -> str:
    lines = [
        "You are a helpful assistant for this Discord server.",
        "Keep responses concise and in plain conversational text — no markdown bold, italics, or headers.",
    ]
    if char_limit:
        lines.append(f"Keep your entire response under {char_limit} characters.")
    else:
        lines.append("Keep responses concise and under 1800 characters.")

    if memory:
        lines.append("\nServer context:")
        for key, value in memory.items():
            lines.append(f"- {key}: {value}")
    return "\n".join(lines)


def is_copy_paste_response(text: str, custom_patterns: list = None) -> bool:
    patterns = DEFAULT_EXEMPT_PATTERNS.copy()
    if custom_patterns:
        patterns.extend(custom_patterns)
    for pat in patterns:
        try:
            if re.search(pat, text, re.IGNORECASE | re.MULTILINE):
                return True
        except re.error:
            continue
    return False


class AI(commands.Cog):
    """🤖 Auto-respond AI channel with memory, rate limiting, and image analysis."""

    slash = app_commands.Group(name="ai", description="AI auto-respond channel and memory management")

    def __init__(self, bot):
        self.bot = bot
        self._history: dict[int, dict[int, list]] = {}
        self._rate_tracker: dict[int, dict[int, list[float]]] = {}

    def _check_key(self):
        account = getattr(Config, "CLOUDFLARE_ACCOUNT_ID", "")
        token = getattr(Config, "CLOUDFLARE_API_TOKEN", "")
        return bool(account and token and "YOUR_" not in token)

    def _get_limit(self, d: dict, user_id: int) -> tuple[int, int]:
        user_override = d.get("user_limits", {}).get(str(user_id))
        if user_override:
            return user_override["messages"], user_override["window"]
        global_limit = d.get("rate_limit")
        if global_limit:
            return global_limit["messages"], global_limit["window"]
        return DEFAULT_RATE_MESSAGES, DEFAULT_RATE_WINDOW

    def _check_rate_limit(self, guild_id: int, user_id: int, max_msgs: int, window: int) -> tuple[bool, float]:
        now = time.monotonic()
        tracker = self._rate_tracker.setdefault(guild_id, {})
        stamps = tracker.setdefault(user_id, [])
        tracker[user_id] = [t for t in stamps if now - t < window]
        stamps = tracker[user_id]
        if len(stamps) >= max_msgs:
            reset_in = window - (now - stamps[0])
            return False, max(0.0, reset_in)
        stamps.append(now)
        return True, 0.0

    # ── Channel / Category ────────────────────────────

    @slash.command(name="setchannel", description="Set the channel where the AI auto-responds to every message.")
    @app_commands.describe(channel="Channel to enable AI auto-responses in")
    @app_commands.default_permissions(administrator=True)
    async def setchannel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        d = _ai_data(interaction.guild.id)
        d["channel_id"] = channel.id
        _save_ai_data(interaction.guild.id, d)
        await interaction.response.send_message(
            f"✅ AI will now auto-respond in {channel.mention}.", ephemeral=True
        )

    @slash.command(name="setcategory", description="Set a category where the AI auto-responds in every channel.")
    @app_commands.describe(category="Category to enable AI auto-responses in")
    @app_commands.default_permissions(administrator=True)
    async def setcategory(self, interaction: discord.Interaction, category: discord.CategoryChannel):
        d = _ai_data(interaction.guild.id)
        d["category_id"] = category.id
        _save_ai_data(interaction.guild.id, d)
        await interaction.response.send_message(
            f"AI will now auto-respond under **{category.name}**.", ephemeral=True,
        )

    @slash.command(name="removechannel", description="Remove the AI auto-respond channel.")
    @app_commands.default_permissions(administrator=True)
    async def removechannel(self, interaction: discord.Interaction):
        d = _ai_data(interaction.guild.id)
        if "channel_id" not in d:
            return await interaction.response.send_message("❌ No AI channel set.", ephemeral=True)
        d.pop("channel_id")
        _save_ai_data(interaction.guild.id, d)
        await interaction.response.send_message("✅ AI channel removed.", ephemeral=True)

    @slash.command(name="removecategory", description="Remove the AI auto-respond category.")
    @app_commands.default_permissions(administrator=True)
    async def removecategory(self, interaction: discord.Interaction):
        d = _ai_data(interaction.guild.id)
        if "category_id" not in d:
            return await interaction.response.send_message("No AI category set.", ephemeral=True)
        d.pop("category_id")
        _save_ai_data(interaction.guild.id, d)
        await interaction.response.send_message("AI category removed.", ephemeral=True)

    # ── Rate / Response Limits ─────────────────────────

    @slash.command(name="setlimit", description="Set the global per-user rate limit for the AI.")
    @app_commands.describe(messages="Max messages per window", window="Window in seconds")
    @app_commands.default_permissions(administrator=True)
    async def setlimit(self, interaction: discord.Interaction, messages: int, window: int):
        if messages < 1 or window < 1:
            return await interaction.response.send_message("❌ Both values must be 1+.", ephemeral=True)
        d = _ai_data(interaction.guild.id)
        d["rate_limit"] = {"messages": messages, "window": window}
        _save_ai_data(interaction.guild.id, d)
        await interaction.response.send_message(f"✅ Rate limit: **{messages}** msg per **{window}s**.", ephemeral=True)

    @slash.command(name="setuserlimit", description="Set a rate limit for a specific user.")
    @app_commands.describe(user="User", messages="Max messages (0 = remove)", window="Window in seconds")
    @app_commands.default_permissions(administrator=True)
    async def setuserlimit(self, interaction: discord.Interaction, user: discord.Member, messages: int, window: int):
        d = _ai_data(interaction.guild.id)
        user_limits = d.setdefault("user_limits", {})
        if messages == 0:
            user_limits.pop(str(user.id), None)
            _save_ai_data(interaction.guild.id, d)
            return await interaction.response.send_message(f"✅ Removed override for {user.mention}.", ephemeral=True)
        if messages < 1 or window < 1:
            return await interaction.response.send_message("❌ Both values must be 1+.", ephemeral=True)
        user_limits[str(user.id)] = {"messages": messages, "window": window}
        _save_ai_data(interaction.guild.id, d)
        await interaction.response.send_message(f"✅ {user.mention}: **{messages}** msg per **{window}s**.", ephemeral=True)

    @slash.command(name="charlimit", description="Set the max AI response length (0 = no limit).")
    @app_commands.describe(limit="Max characters (1–2000) or 0")
    @app_commands.default_permissions(administrator=True)
    async def charlimit(self, interaction: discord.Interaction, limit: int):
        if limit < 0 or limit > 2000:
            return await interaction.response.send_message("❌ 1–2000 or 0.", ephemeral=True)
        d = _ai_data(interaction.guild.id)
        if limit == 0:
            d.pop("char_limit", None)
            await interaction.response.send_message("✅ Response limit removed.", ephemeral=True)
        else:
            d["char_limit"] = limit
            await interaction.response.send_message(f"✅ Responses capped at **{limit}** chars.", ephemeral=True)
        _save_ai_data(interaction.guild.id, d)

    @slash.command(name="requestcharlimit", description="Set max user request length (0 = unlimited).")
    @app_commands.describe(limit="Max characters (0 = no limit)")
    @app_commands.default_permissions(administrator=True)
    async def requestcharlimit(self, interaction: discord.Interaction, limit: int):
        if limit < 0:
            return await interaction.response.send_message("❌ 0 or positive.", ephemeral=True)
        d = _ai_data(interaction.guild.id)
        if limit == 0:
            d.pop("request_char_limit", None)
            await interaction.response.send_message("✅ Request limit removed.", ephemeral=True)
        else:
            d["request_char_limit"] = limit
            await interaction.response.send_message(f"✅ Requests limited to **{limit}** chars.", ephemeral=True)
        _save_ai_data(interaction.guild.id, d)

    @slash.command(name="exemptpattern", description="Add/remove a truncation exemption pattern.")
    @app_commands.describe(pattern="Regex pattern", remove="True to remove")
    @app_commands.default_permissions(administrator=True)
    async def exemptpattern(self, interaction: discord.Interaction, pattern: str, remove: bool = False):
        d = _ai_data(interaction.guild.id)
        patterns = d.setdefault("exempt_patterns", [])
        if remove:
            if pattern in patterns:
                patterns.remove(pattern)
                await interaction.response.send_message(f"✅ Removed `{pattern}`.", ephemeral=True)
            else:
                await interaction.response.send_message(f"❌ `{pattern}` not found.", ephemeral=True)
        else:
            if pattern not in patterns:
                patterns.append(pattern)
                await interaction.response.send_message(f"✅ Added `{pattern}`.", ephemeral=True)
            else:
                await interaction.response.send_message(f"⚠️ Already exists.", ephemeral=True)
        _save_ai_data(interaction.guild.id, d)

    @slash.command(name="limits", description="Show current AI limits and model info.")
    @app_commands.default_permissions(manage_channels=True)
    async def limits(self, interaction: discord.Interaction):
        d = _ai_data(interaction.guild.id)
        gl = d.get("rate_limit")
        gs = f"{gl['messages']}/{gl['window']}s" if gl else f"{DEFAULT_RATE_MESSAGES}/{DEFAULT_RATE_WINDOW}s (default)"
        ch_id = d.get("channel_id")
        ch_str = f"<#{ch_id}>" if ch_id else "Not set"
        cat_id = d.get("category_id")
        cat_str = interaction.guild.get_channel(int(cat_id)).name if cat_id and interaction.guild.get_channel(int(cat_id)) else ("Not set" if not cat_id else "Unknown")
        lines = [
            f"**Model:** `{CF_MODEL}`",
            f"**Channel:** {ch_str}",
            f"**Category:** {cat_str}",
            f"**Response limit:** {d.get('char_limit', 'None')}",
            f"**Request limit:** {d.get('request_char_limit', 'None')}",
            f"**Rate limit:** {gs}",
            f"**Image max:** {MAX_IMAGE_BYTES//1024}KB (auto-compressed)",
        ]
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    # ── Memory ────────────────────────────────────────

    @slash.command(name="memoryadd", description="Add/update a memory entry.")
    @app_commands.describe(key="Label", value="Information")
    @app_commands.default_permissions(administrator=True)
    async def memoryadd(self, interaction: discord.Interaction, key: str, value: str):
        d = _ai_data(interaction.guild.id)
        d.setdefault("memory", {})[key] = value
        _save_ai_data(interaction.guild.id, d)
        await interaction.response.send_message(f"✅ `{key}` saved.", ephemeral=True)

    @slash.command(name="memoryremove", description="Remove a memory entry.")
    @app_commands.describe(key="Key to remove")
    @app_commands.default_permissions(administrator=True)
    async def memoryremove(self, interaction: discord.Interaction, key: str):
        d = _ai_data(interaction.guild.id)
        if key not in d.get("memory", {}):
            return await interaction.response.send_message(f"❌ `{key}` not found.", ephemeral=True)
        del d["memory"][key]
        _save_ai_data(interaction.guild.id, d)
        await interaction.response.send_message(f"✅ `{key}` removed.", ephemeral=True)

    @slash.command(name="memorylist", description="List all memory entries.")
    @app_commands.default_permissions(manage_channels=True)
    async def memorylist(self, interaction: discord.Interaction):
        d = _ai_data(interaction.guild.id)
        memory = d.get("memory", {})
        if not memory:
            return await interaction.response.send_message("No entries yet.", ephemeral=True)
        lines = [f"**{k}:** {v}" for k, v in memory.items()]
        await interaction.response.send_message("\n".join(lines)[:2000], ephemeral=True)

    @slash.command(name="memoryexport", description="Export memory as JSON.")
    @app_commands.default_permissions(administrator=True)
    async def memoryexport(self, interaction: discord.Interaction):
        d = _ai_data(interaction.guild.id)
        memory = d.get("memory", {})
        file = discord.File(io.BytesIO(json.dumps(memory, indent=2).encode()), filename="ai_memory.json")
        await interaction.response.send_message("📤 Export:", file=file, ephemeral=True)

    @slash.command(name="memoryimport", description="Import memory from JSON.")
    @app_commands.describe(file="Upload .json file")
    @app_commands.default_permissions(administrator=True)
    async def memoryimport(self, interaction: discord.Interaction, file: discord.Attachment):
        if not file.filename.endswith(".json"):
            return await interaction.response.send_message("❌ .json only.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        try:
            imported = json.loads(await file.read())
            if not isinstance(imported, dict):
                return await interaction.followup.send("❌ Must be a JSON object.", ephemeral=True)
            d = _ai_data(interaction.guild.id)
            d["memory"] = imported
            _save_ai_data(interaction.guild.id, d)
            await interaction.followup.send(f"✅ Imported **{len(imported)}** entries.", ephemeral=True)
        except json.JSONDecodeError:
            await interaction.followup.send("❌ Invalid JSON.", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ {e}", ephemeral=True)

    # ── on_message ────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        if self.bot.user not in message.mentions:
            return

        d = _ai_data(message.guild.id)
        ch_id = d.get("channel_id")
        cat_id = d.get("category_id")
        if not (ch_id == message.channel.id or (cat_id and getattr(message.channel, "category_id", None) == cat_id)):
            return
        if not self._check_key():
            return

        clean = message.clean_content.replace(f"@{message.guild.me.display_name}", "").strip()

        req_limit = d.get("request_char_limit")
        if req_limit and len(clean) > req_limit:
            await message.reply("⚠️ Too long. Please shorten your request.", mention_author=False, delete_after=10)
            return

        max_msgs, window = self._get_limit(d, message.author.id)
        ok, wait = self._check_rate_limit(message.guild.id, message.author.id, max_msgs, window)
        if not ok:
            m, s = divmod(int(wait), 60)
            t = f"{m}m {s}s" if m else f"{s}s"
            await message.reply(f"⏳ Slow down! Wait **{t}**.", mention_author=False)
            return

        memory = d.get("memory", {})
        char_limit = d.get("char_limit")
        patterns = d.get("exempt_patterns", [])
        sys_prompt = _build_system_prompt(memory, char_limit)

        hist = self._history.setdefault(message.guild.id, {})
        user_hist = hist.setdefault(message.author.id, [])

        # ── Image handling with compression ────────────
        imgs = [a for a in message.attachments if a.content_type and a.content_type.startswith("image/")]

        if imgs:
            parts = []
            if clean:
                parts.append({"type": "text", "text": clean})
            for img in imgs[:4]:
                try:
                    async with aiohttp.ClientSession() as s:
                        async with s.get(img.url, timeout=aiohttp.ClientTimeout(total=15)) as r:
                            if r.status != 200:
                                parts.append({"type": "text", "text": f"[Download failed: {img.filename}]"})
                                continue
                            raw = await r.read()
                            mime = r.content_type or "image/png"
                            
                            # Compress if needed
                            if len(raw) > MAX_IMAGE_BYTES:
                                raw, mime = _compress_image(raw, mime)
                            
                            b64 = base64.b64encode(raw).decode()
                            parts.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
                except Exception as e:
                    parts.append({"type": "text", "text": f"[Image error: {e}]"})
            user_content = parts
        else:
            user_content = clean

        msgs = [{"role": "system", "content": sys_prompt}]
        msgs.extend(user_hist[-20:])
        msgs.append({"role": "user", "content": user_content})

        async with message.channel.typing():
            try:
                reply = await _cf_request(Config.CLOUDFLARE_ACCOUNT_ID, Config.CLOUDFLARE_API_TOKEN, msgs)
                final = reply[:2000] if (not char_limit or is_copy_paste_response(reply, patterns)) else reply[:char_limit]
                user_hist.append({"role": "user", "content": clean})
                user_hist.append({"role": "assistant", "content": reply})
                if len(user_hist) > 40:
                    hist[message.author.id] = user_hist[-40:]
                await message.reply(final, mention_author=False)
            except Exception:
                print(f"[AI] on_message error: {traceback.format_exc()}")


async def setup(bot):
    await bot.add_cog(AI(bot))
