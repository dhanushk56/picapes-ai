"""
main.py — Discord bot entry point with embedded dashboard.
Run with: python main.py
"""

import discord
from discord.ext import commands
import asyncio
import os
import traceback

from config import Config

COGS = [
    "cogs.ai",
    "cogs.tickets",
    "cogs.moderation",
]


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

        self.dashboard = None

    async def setup_hook(self):
        for cog in COGS:
            try:
                await self.load_extension(cog)
                print(f"[+] Loaded {cog}")
            except Exception as e:
                print(f"[!] Failed to load {cog}: {e}")
                traceback.print_exc()

        try:
            if Config.GUILD_ID:
                guild = discord.Object(id=Config.GUILD_ID)
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
                print(f"[+] Synced {len(synced)} guild slash command(s) to {Config.GUILD_ID}")
            else:
                synced = await self.tree.sync()
                print(f"[+] Synced {len(synced)} global slash command(s)")
        except Exception as e:
            print(f"[!] Failed to sync slash commands: {e}")

        # Start embedded dashboard
        if getattr(Config, "DISCORD_CLIENT_ID", None):
            from dashboard import Dashboard
            self.dashboard = Dashboard(self)
            await self.dashboard.start()
        else:
            print("[Dashboard] Skipped — set DISCORD_CLIENT_ID in config to enable")

    async def on_ready(self):
        print(f"[+] Logged in as {self.user} (ID: {self.user.id})")
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="over the server",
            )
        )
        # Push initial state to dashboard
        if self.dashboard:
            self.dashboard.update_guilds_cache()
            await self.dashboard.emit_status()
            await self.dashboard.emit_guilds()

    async def on_command_error(self, ctx, error):
        if isinstance(error, commands.CommandNotFound):
            return
        if isinstance(error, commands.MissingRequiredArgument):
            return await ctx.reply(f"❌ Missing argument: `{error.param.name}`")
        if isinstance(error, commands.MemberNotFound):
            return await ctx.reply("❌ Member not found.")
        if isinstance(error, commands.BadArgument):
            return await ctx.reply(f"❌ Bad argument: {error}")
        print(f"[!] Command error in {ctx.command}: {traceback.format_exc()}")

    # --- Dashboard event forwarding ---

    async def on_member_join(self, member):
        if self.dashboard:
            await self.dashboard.on_member_join(member)

    async def on_member_remove(self, member):
        if self.dashboard:
            await self.dashboard.on_member_remove(member)

    async def on_command(self, ctx):
        if self.dashboard:
            await self.dashboard.on_command_used(ctx)
        await super().on_command(ctx)

    async def on_app_command_completion(self, interaction, command):
        if self.dashboard and interaction.guild:
            await self.dashboard.on_slash_command(interaction, command)

    async def on_guild_join(self, guild):
        if self.dashboard:
            self.dashboard.update_guilds_cache()
            await self.dashboard.emit_guilds()

    async def on_guild_remove(self, guild):
        if self.dashboard:
            self.dashboard.update_guilds_cache()
            await self.dashboard.emit_guilds()

    async def close(self):
        if self.dashboard:
            await self.dashboard.stop()
        await super().close()


async def main():
    bot = Bot()
    async with bot:
        await bot.start(Config.TOKEN)


if __name__ == "__main__":
    asyncio.run(main())