"""
main.py — Discord bot entry point.
Run with: python main.py
"""

import discord
from discord.ext import commands
import asyncio
import os
import traceback
import sys

from config import Config

# Your user ID for error DMs
OWNER_ID = 1077905352244338688

COGS = [
    "cogs.ai",
    "cogs.tickets",
    "cogs.moderation",
]

async def send_dm_to_owner(bot, content: str):
    """Send a DM to the bot owner."""
    try:
        owner = await bot.fetch_user(OWNER_ID)
        await owner.send(content)
    except Exception as e:
        print(f"Failed to DM owner: {e}")

class Bot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        intents.guilds = True
        intents.voice_states = True

        super().__init__(
            command_prefix="~",
            intents=intents,
            help_command=None,
        )

    async def setup_hook(self):
        loaded_cogs = []
        failed_cogs = []

        for cog in COGS:
            try:
                await self.load_extension(cog)
                print(f"[+] Loaded {cog}")
                loaded_cogs.append(cog)
            except Exception as e:
                error_msg = f"[!] Failed to load {cog}: {e}\n{traceback.format_exc()}"
                print(error_msg)
                failed_cogs.append((cog, error_msg))
                await send_dm_to_owner(self, f"❌ Cog load failed: {cog}\n```py\n{error_msg[:1900]}\n```")

        if loaded_cogs:
            await send_dm_to_owner(self, f"✅ Loaded cogs: {', '.join(loaded_cogs)}")

        # Sync slash commands
        try:
            if Config.GUILD_ID:
                guild = discord.Object(id=Config.GUILD_ID)
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
                print(f"[+] Synced {len(synced)} guild slash command(s) to {Config.GUILD_ID}")
                await send_dm_to_owner(self, f"📡 Synced commands to guild `{Config.GUILD_ID}`")
            else:
                synced = await self.tree.sync()
                print(f"[+] Synced {len(synced)} global slash command(s)")
                await send_dm_to_owner(self, f"📡 Synced {len(synced)} global commands (may take up to 1h to appear)")
        except Exception as e:
            error_msg = f"Failed to sync slash commands: {e}\n{traceback.format_exc()}"
            print(error_msg)
            await send_dm_to_owner(self, f"❌ Command sync failed:\n```py\n{error_msg[:1900]}\n```")

    async def on_ready(self):
        print(f"[+] Logged in as {self.user} (ID: {self.user.id})")
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="over the server",
            )
        )
        await send_dm_to_owner(self, f"✅ Bot is online as `{self.user}`")

    async def on_command_error(self, ctx, error):
        if isinstance(error, commands.CommandNotFound):
            return
        if isinstance(error, commands.MissingRequiredArgument):
            return await ctx.reply(f"❌ Missing argument: `{error.param.name}`")
        if isinstance(error, commands.MemberNotFound):
            return await ctx.reply("❌ Member not found.")
        if isinstance(error, commands.BadArgument):
            return await ctx.reply(f"❌ Bad argument: {error}")
        error_msg = f"Command error in {ctx.command}:\n{traceback.format_exc()}"
        print(error_msg)
        await send_dm_to_owner(self, f"⚠️ {error_msg[:1900]}")

    # ---- Owner-only utility commands ----
    async def setup_owner_commands(self):
        @self.command(name="reload")
        @commands.is_owner()
        async def reload_cmd(ctx, cog_name: str = None):
            """Reload a cog (or all if none specified)."""
            if cog_name is None:
                for cog in COGS:
                    try:
                        await self.reload_extension(cog)
                        await ctx.send(f"✅ Reloaded `{cog}`")
                    except Exception as e:
                        await ctx.send(f"❌ Failed reloading `{cog}`: {e}")
                await self.tree.sync(guild=discord.Object(id=Config.GUILD_ID)) if Config.GUILD_ID else await self.tree.sync()
                await ctx.send("🔄 Slash commands re-synced.")
            else:
                cog_path = f"cogs.{cog_name}" if not cog_name.startswith("cogs.") else cog_name
                try:
                    await self.reload_extension(cog_path)
                    await ctx.send(f"✅ Reloaded `{cog_path}`")
                    await self.tree.sync(guild=discord.Object(id=Config.GUILD_ID)) if Config.GUILD_ID else await self.tree.sync()
                    await ctx.send("🔄 Slash commands re-synced.")
                except Exception as e:
                    await ctx.send(f"❌ Error: {e}")

        @self.command(name="sync")
        @commands.is_owner()
        async def sync_cmd(ctx):
            """Manually sync slash commands."""
            try:
                if Config.GUILD_ID:
                    guild = discord.Object(id=Config.GUILD_ID)
                    synced = await self.tree.sync(guild=guild)
                    await ctx.send(f"✅ Synced {len(synced)} guild commands.")
                else:
                    synced = await self.tree.sync()
                    await ctx.send(f"✅ Synced {len(synced)} global commands.")
            except Exception as e:
                await ctx.send(f"❌ Sync failed: {e}")

    async def on_connect(self):
        await self.setup_owner_commands()

async def main():
    bot = Bot()
    async with bot:
        await bot.start(Config.TOKEN)

if __name__ == "__main__":
    asyncio.run(main())
