"""
cogs/ai.py
Slash group: /ai
Auto-respond AI channel — assign a channel and the bot replies to every message there.
Memory system — store server context (staff names, rules, etc.) the AI draws on.
Export/import memory via JSON file.
Per-user rate limiting — global default + per-user overrides.
"""

import discord
from discord.ext import commands
from discord import app_commands
import aiohttp
import json
import io
import time
import traceback
from config import Config
from utils.data import load, save

AI_FILE    = "ai.json"
CF_MODEL   = "@cf/meta/llama-4-scout-17b-16e-instruct"

DEFAULT_RATE_MESSAGES = 10
DEFAULT_RATE_WINDOW   = 60


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

def _build_system_prompt(memory: dict) -> str:
    lines = [
        "You are a helpful assistant for this Discord server.",
        "Keep responses concise and under 1800 characters.",
        "Respond in plain conversational text only — no markdown bold, italics, or headers.",
    ]
    if memory:
        lines.append("\nServer context:")
        for key, value in memory.items():
            lines.append(f"- {key}: {value}")
    return "\n".join(lines)


class AI(commands.Cog):
    """🤖 Auto-respond AI channel with memory and rate limiting."""

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
        """Return (max_messages, window_seconds) for this user.
        Per-user override takes priority over global; falls back to defaults."""
        user_override = d.get("user_limits", {}).get(str(user_id))
        if user_override:
            return user_override["messages"], user_override["window"]
        
        global_limit = d.get("rate_limit")
        if global_limit:
            return global_limit["messages"], global_limit["window"]
            
        return DEFAULT_RATE_MESSAGES, DEFAULT_RATE_WINDOW

    def _check_rate_limit(self, guild_id: int, user_id: int, max_msgs: int, window: int) -> tuple[bool, float]:
        """Check if the user is within their rate limit.
        Returns (allowed, seconds_until_reset).
        Prunes old timestamps and records the new one if allowed."""
        now       = time.monotonic()
        tracker   = self._rate_tracker.setdefault(guild_id, {})
        stamps    = tracker.setdefault(user_id, [])

        tracker[user_id] = [t for t in stamps if now - t < window]
        stamps = tracker[user_id]

        if len(stamps) >= max_msgs:
            reset_in = window - (now - stamps[0])
            return False, max(0.0, reset_in)

        stamps.append(now)
        return True, 0.0

    @slash.command(name="setchannel", description="Set the channel where the AI auto-responds to every message.")
    @app_commands.describe(channel="Channel to enable AI auto-responses in")
    @app_commands.default_permissions(administrator=True)
    async def setchannel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        d = _ai_data(interaction.guild.id)
        d["channel_id"] = channel.id
        _save_ai_data(interaction.guild.id, d)
        
        await interaction.response.send_message(
            f"✅ AI will now auto-respond to every message in {channel.mention}.", ephemeral=True
        )


    @slash.command(name="setcategory", description="Set a category where the AI auto-responds in every channel.")
    @app_commands.describe(category="Category to enable AI auto-responses in")
    @app_commands.default_permissions(administrator=True)
    async def setcategory(self, interaction: discord.Interaction, category: discord.CategoryChannel):
        d = _ai_data(interaction.guild.id)
        d["category_id"] = category.id
        _save_ai_data(interaction.guild.id, d)
        
        await interaction.response.send_message(
            f"AI will now auto-respond in every text channel under **{category.name}**.",
            ephemeral=True,
        )

    @slash.command(name="removechannel", description="Remove the AI auto-respond channel.")
    @app_commands.default_permissions(administrator=True)
    async def removechannel(self, interaction: discord.Interaction):
        d = _ai_data(interaction.guild.id)
        
        if "channel_id" not in d:
            return await interaction.response.send_message("❌ No AI channel is currently set.", ephemeral=True)
            
        d.pop("channel_id")
        _save_ai_data(interaction.guild.id, d)
        
        await interaction.response.send_message("✅ AI auto-respond channel removed.", ephemeral=True)


    @slash.command(name="removecategory", description="Remove the AI auto-respond category.")
    @app_commands.default_permissions(administrator=True)
    async def removecategory(self, interaction: discord.Interaction):
        d = _ai_data(interaction.guild.id)
        
        if "category_id" not in d:
            return await interaction.response.send_message("No AI category is currently set.", ephemeral=True)
            
        d.pop("category_id")
        _save_ai_data(interaction.guild.id, d)
        
        await interaction.response.send_message("AI auto-respond category removed.", ephemeral=True)

    @slash.command(name="setlimit", description="Set the global per-user rate limit for the AI (applies to everyone).")
    @app_commands.describe(
        messages="Max number of messages allowed per user in the time window",
        window="Time window in seconds (e.g. 60 = 1 minute)",
    )
    @app_commands.default_permissions(administrator=True)
    async def setlimit(self, interaction: discord.Interaction, messages: int, window: int):
        if messages < 1 or window < 1:
            return await interaction.response.send_message(
                "❌ Both values must be 1 or greater.", ephemeral=True
            )
            
        d = _ai_data(interaction.guild.id)
        d["rate_limit"] = {"messages": messages, "window": window}
        _save_ai_data(interaction.guild.id, d)
        
        await interaction.response.send_message(
            f"✅ Global rate limit set: **{messages}** message(s) per **{window}s** per user.",
            ephemeral=True,
        )


    @slash.command(name="setuserlimit", description="Set a rate limit for a specific user, overriding the global limit.")
    @app_commands.describe(
        user="The user to apply the custom limit to",
        messages="Max number of messages allowed in the time window (0 = remove override)",
        window="Time window in seconds",
    )
    @app_commands.default_permissions(administrator=True)
    async def setuserlimit(self, interaction: discord.Interaction, user: discord.Member, messages: int, window: int):
        d = _ai_data(interaction.guild.id)
        user_limits = d.setdefault("user_limits", {})

        if messages == 0:
            user_limits.pop(str(user.id), None)
            d["user_limits"] = user_limits
            _save_ai_data(interaction.guild.id, d)
            return await interaction.response.send_message(
                f"✅ Removed custom rate limit for {user.mention} — they will now use the global limit.",
                ephemeral=True,
            )

        if messages < 1 or window < 1:
            return await interaction.response.send_message(
                "❌ Both values must be 1 or greater (or set messages to 0 to remove the override).",
                ephemeral=True,
            )

        user_limits[str(user.id)] = {"messages": messages, "window": window}
        d["user_limits"] = user_limits
        _save_ai_data(interaction.guild.id, d)
        
        await interaction.response.send_message(
            f"✅ Custom rate limit for {user.mention}: **{messages}** message(s) per **{window}s**.",
            ephemeral=True,
        )


    @slash.command(name="limits", description="Show current AI rate limits and model info.")
    @app_commands.default_permissions(manage_channels=True)
    async def limits(self, interaction: discord.Interaction):
        d = _ai_data(interaction.guild.id)

  
        gl = d.get("rate_limit")
        if gl:
            global_str = f"{gl['messages']} message(s) per {gl['window']}s"
        else:
            global_str = f"{DEFAULT_RATE_MESSAGES} message(s) per {DEFAULT_RATE_WINDOW}s (default)"

        ch_id = d.get("channel_id")
        category_id = d.get("category_id")
        ch_str = f"<#{ch_id}>" if ch_id else "Not set"
        category = interaction.guild.get_channel(int(category_id)) if category_id else None
        category_str = category.name if category else ("Not set" if not category_id else f"Unknown category `{category_id}`")

        user_limits = d.get("user_limits", {})
        if user_limits:
            override_lines = []
            for uid, lim in user_limits.items():
                member = interaction.guild.get_member(int(uid))
                name   = member.mention if member else f"<@{uid}>"
                override_lines.append(f"• {name} — {lim['messages']} msg / {lim['window']}s")
            overrides_str = "\n".join(override_lines)
        else:
            overrides_str = "None"

        lines = [
            f"**Model:** `{CF_MODEL}`",
            f"**AI Channel:** {ch_str}",
            f"**AI Category:** {category_str}",
            f"**Global rate limit:** {global_str}",
            f"**Per-user overrides:**\n{overrides_str}",
        ]
        
        await interaction.response.send_message("\n".join(lines), ephemeral=True)


    @slash.command(name="memoryadd", description="Add or update a memory entry the AI will know about.")
    @app_commands.describe(
        key="Category or label, e.g. staff_names, server_rules, server_name",
        value="The information to store",
    )
    @app_commands.default_permissions(administrator=True)
    async def memoryadd(self, interaction: discord.Interaction, key: str, value: str):
        d = _ai_data(interaction.guild.id)
        d.setdefault("memory", {})[key] = value
        _save_ai_data(interaction.guild.id, d)
        
        await interaction.response.send_message(f"✅ Memory entry `{key}` saved.", ephemeral=True)


    @slash.command(name="memoryremove", description="Remove a memory entry.")
    @app_commands.describe(key="The key to remove")
    @app_commands.default_permissions(administrator=True)
    async def memoryremove(self, interaction: discord.Interaction, key: str):
        d = _ai_data(interaction.guild.id)
        memory = d.get("memory", {})
        
        if key not in memory:
            return await interaction.response.send_message(f"❌ No memory entry found for `{key}`.", ephemeral=True)
            
        del memory[key]
        d["memory"] = memory
        _save_ai_data(interaction.guild.id, d)
        
        await interaction.response.send_message(f"✅ Removed memory entry `{key}`.", ephemeral=True)


    @slash.command(name="memorylist", description="List all saved memory entries.")
    @app_commands.default_permissions(manage_channels=True)
    async def memorylist(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        d = _ai_data(interaction.guild.id)
        memory = d.get("memory", {})
        
        if not memory:
            return await interaction.followup.send(
                "No memory entries saved yet. Use `/ai memoryadd` to add some.", ephemeral=True
            )
            
        lines = [f"**{k}:** {v}" for k, v in memory.items()]
        
        pages = []
        current, current_len = [], 0
        
        for line in lines:
            if current_len + len(line) + 1 > 1900 and current:
                pages.append("\n".join(current))
                current, current_len = [], 0
            current.append(line)
            current_len += len(line) + 1
            
        if current:
            pages.append("\n".join(current))
            
        total = len(memory)
        for i, page in enumerate(pages):
            header = f"**Memory entries ({total} total){f' — page {i+1}/{len(pages)}' if len(pages) > 1 else ''}:**\n"
            await interaction.followup.send(header + page, ephemeral=True)


    @slash.command(name="memoryexport", description="Export AI memory as a JSON file you can re-import later.")
    @app_commands.default_permissions(administrator=True)
    async def memoryexport(self, interaction: discord.Interaction):
        d = _ai_data(interaction.guild.id)
        memory = d.get("memory", {})
        
        content = json.dumps(memory, indent=2).encode()
        file = discord.File(io.BytesIO(content), filename="ai_memory.json")
        
        await interaction.response.send_message(
            "📤 Here is your AI memory export. Upload this file to `/ai memoryimport` to restore it.",
            file=file,
            ephemeral=True,
        )


    @slash.command(name="memoryimport", description="Import AI memory from a JSON file.")
    @app_commands.describe(file="Upload a .json file exported from /ai memoryexport")
    @app_commands.default_permissions(administrator=True)
    async def memoryimport(self, interaction: discord.Interaction, file: discord.Attachment):
        if not file.filename.endswith(".json"):
            return await interaction.response.send_message("❌ Please upload a `.json` file.", ephemeral=True)
            
        await interaction.response.defer(ephemeral=True)
        
        try:
            raw      = await file.read()
            imported = json.loads(raw)
            
            if not isinstance(imported, dict):
                return await interaction.followup.send(
                    "❌ Invalid format — the file should contain a JSON object (key-value pairs).", ephemeral=True
                )
                
            d = _ai_data(interaction.guild.id)
            d["memory"] = imported
            _save_ai_data(interaction.guild.id, d)
            
            await interaction.followup.send(f"✅ Imported **{len(imported)}** memory entries.", ephemeral=True)
            
        except json.JSONDecodeError:
            await interaction.followup.send("❌ Could not parse the file — make sure it's valid JSON.", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Import failed: {e}", ephemeral=True)


    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild or not message.content:
            return

        if self.bot.user not in message.mentions:
            return

        d = _ai_data(message.guild.id)
        channel_id = d.get("channel_id")
        category_id = d.get("category_id")
        
        in_ai_channel = channel_id == message.channel.id
        in_ai_category = (
            category_id is not None
            and getattr(message.channel, "category_id", None) == category_id
        )
        
        if not in_ai_channel and not in_ai_category:
            return
            
        if not self._check_key():
            return

        max_msgs, window = self._get_limit(d, message.author.id)
        allowed, reset_in = self._check_rate_limit(
            message.guild.id, message.author.id, max_msgs, window
        )
        
        if not allowed:
            secs = int(reset_in)
            mins, s = divmod(secs, 60)
            time_str = f"{mins}m {s}s" if mins else f"{s}s"
            await message.reply(
                f"You're sending messages too fast. Please wait **{time_str}** before trying again.",
                mention_author=False,
            )
            return

        memory        = d.get("memory", {})
        system_prompt = _build_system_prompt(memory)

        guild_history = self._history.setdefault(message.guild.id, {})
        user_history  = guild_history.setdefault(message.author.id, [])

        clean_user_message = message.clean_content.replace(f"@{message.guild.me.display_name}", "").strip()

        messages = [{"role": "system", "content": system_prompt}]
        messages.extend(user_history[-20:])
        messages.append({"role": "user", "content": clean_user_message})

        async with message.channel.typing():
            try:
                reply = await _cf_request(
                    Config.CLOUDFLARE_ACCOUNT_ID,
                    Config.CLOUDFLARE_API_TOKEN,
                    messages
                )

                user_history.append({"role": "user",      "content": clean_user_message})
                user_history.append({"role": "assistant", "content": reply})
                
                if len(user_history) > 40:
                    guild_history[message.author.id] = user_history[-40:]

                await message.reply(reply[:2000], mention_author=False)
                
            except Exception:
                print(f"[AI] on_message error: {traceback.format_exc()}")


async def setup(bot):
    await bot.add_cog(AI(bot))