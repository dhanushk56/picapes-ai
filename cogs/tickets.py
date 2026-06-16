"""
cogs/tickets.py
Full ticket system with category dropdown, per-type modals, claim, close with reason, transcript.
Per-category staff roles, type-specific overwrites, staff role check on all buttons.
All commands are grouped under /ticket <subcommand>.
Support timings embed shown on every new ticket — set via /ticket supporttimes.
AI auto-response enabled with 10 message cap per ticket and 70 tickets/day limit.
"""

import discord
from discord import app_commands
from discord.ext import commands, tasks
import asyncio
import io
import re
import aiohttp
from datetime import datetime, timezone, timedelta
from config import Config
from utils.data import load, save

TICKETS_FILE = "tickets.json"
AI_RESPONSE_LIMIT_PER_TICKET = 10 
AI_TICKETS_PER_DAY_LIMIT = 70

TICKET_CATEGORIES = {
    "bug":        {"label": "Bug Report",          "description": "Report a bug or issue"},
    "cape":       {"label": "Cape Submit",         "description": "Submit or manage a cape"},
    "general":    {"label": "General Support",     "description": "Get help with general issues"},
    "partnership":{"label": "Partnership Request",  "description": "Apply for a partnership"},
    "ign":        {"label": "Claimed IGN Recovery", "description": "Recover a claimed IGN"},
}

_dm_on_response: dict[int, bool] = {}

def _is_staff(interaction: discord.Interaction, channel_id: int = None) -> bool:
    if interaction.user.guild_permissions.manage_channels:
        return True
    settings     = load("guild_settings.json").get(str(interaction.guild.id), {})
    global_roles = settings.get("ticket_staff_roles", [])
    type_roles   = []
    if channel_id:
        td   = _ticket_data(interaction.guild.id)
        info = td.get("open", {}).get(str(channel_id), {})
        cat  = info.get("category", "general")
        type_roles = settings.get("ticket_type_roles", {}).get(cat, [])
    all_roles = list(set(global_roles + type_roles))
    return any(r.id in all_roles for r in interaction.user.roles)

def _parse_time(time_str: str) -> tuple[int, int] | None:
    time_str = time_str.strip()
    m = re.match(r'^(\d{1,2}):(\d{2})\s*(AM|PM)$', time_str, re.IGNORECASE)
    if m:
        h, mi, ampm = int(m.group(1)), int(m.group(2)), m.group(3).upper()
        if ampm == "PM" and h != 12: h += 12
        if ampm == "AM" and h == 12: h = 0
        if 0 <= h <= 23 and 0 <= mi <= 59: return h, mi
    m = re.match(r'^(\d{1,2}):(\d{2})$', time_str)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        if 0 <= h <= 23 and 0 <= mi <= 59: return h, mi
    return None

def _is_within_support_hours(start_str: str, end_str: str) -> bool:
    start = _parse_time(start_str)
    end   = _parse_time(end_str)
    if not start or not end: return False
    now         = datetime.now(timezone.utc)
    now_mins    = now.hour * 60 + now.minute
    start_mins  = start[0] * 60 + start[1]
    end_mins    = end[0] * 60 + end[1]
    if start_mins <= end_mins:
        return start_mins <= now_mins < end_mins
    else:
        return now_mins >= start_mins or now_mins < end_mins

def _get_ai_response_count(td, channel_id: int) -> int:
    return td.get("open", {}).get(str(channel_id), {}).get("ai_response_count", 0)

def _increment_ai_response(td, channel_id: int):
    info = td.get("open", {}).get(str(channel_id), {})
    if info: info["ai_response_count"] = info.get("ai_response_count", 0) + 1

def _get_tickets_created_today(td) -> int:
    today = datetime.now(timezone.utc).date()
    count = 0
    for info in td.get("open", {}).values():
        opened_at_str = info.get("opened_at", "")
        if opened_at_str:
            try:
                if datetime.fromisoformat(opened_at_str).date() == today: count += 1
            except Exception: pass
    return count

async def _generate_ai_response(message_content: str, ticket_category: str) -> str:
    account_id = getattr(Config, "CLOUDFLARE_ACCOUNT_ID", None)
    api_token  = getattr(Config, "CLOUDFLARE_API_TOKEN", None)
    
    if not account_id or not api_token or "YOUR_" in api_token:
        return None
    
    category_prompts = {
        "bug": "You are a helpful support agent for bug reports. Provide a concise, helpful response acknowledging the bug report and asking clarifying questions if needed.",
        "cape": "You are a helpful support agent for cape submissions. Provide a concise, helpful response acknowledging the cape submission and asking for any needed information.",
        "general": "You are a helpful support agent. Provide a concise, helpful response to the user's support question.",
        "partnership": "You are a helpful support agent for partnership inquiries. Provide a concise, helpful response acknowledging the partnership request and asking for any needed details.",
        "ign": "You are a helpful support agent for IGN recovery. Provide a concise, helpful response acknowledging the recovery request and asking for proof of ownership.",
    }
    
    system_prompt = category_prompts.get(ticket_category, category_prompts["general"])
    url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/@cf/ibm/granite-3.2-8b-instruct"
    
    try:
        headers = {"Authorization": f"Bearer {api_token}", "Content-Type": "application/json"}
        payload = {
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": message_content}
            ],
            "max_tokens": 300,
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json=payload, timeout=30) as resp:
                data = await resp.json()
                if resp.status == 200 and data.get("success"):
                    return data["result"]["response"].strip()
                else:
                    print(f"[Tickets] AI API error: {resp.status} - {data}")
    except Exception as e:
        print(f"[Tickets] AI response generation error: {e}")
    return None

def _ticket_data(guild_id):
    data = load(TICKETS_FILE)
    return data.get(str(guild_id), {"counter": 0, "open": {}})

def _save_ticket(guild_id, td):
    data = load(TICKETS_FILE)
    data[str(guild_id)] = td
    save(TICKETS_FILE, data)

def _real_open_count(guild, td) -> int:
    return sum(1 for ch_id in td.get("open", {}) if guild.get_channel(int(ch_id)))

async def _generate_transcript_url(channel):
    api_key = getattr(Config, "COOKIE_API_KEY", None)
    if not api_key or "YOUR_" in api_key: return None
    try:
        url     = f"https://api.cookie-api.com/api/transcript?channel_id={channel.id}"
        headers = {"Authorization": api_key, "Content-Type": "application/json"}
        payload = {"bot_token": Config.TOKEN}
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json=payload, timeout=30) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("success"): return data.get("url")
    except Exception as e: print(f"[Tickets] Transcript API error: {e}")
    return None

async def _generate_transcript_fallback(channel):
    lines = []
    async for msg in channel.history(limit=500, oldest_first=True):
        ts = msg.created_at.strftime("%Y-%m-%d %H:%M:%S")
        text = msg.content or ""
        for a in msg.attachments: text += f" [Attachment: {a.url}]"
        lines.append(f"[{ts}] {msg.author.display_name}: {text}")
    return discord.File(io.BytesIO("\n".join(lines).encode()), filename=f"transcript-{channel.name}.txt")

async def _do_close(guild, channel, closer, reason: str = None):
    td = _ticket_data(guild.id)
    info = td.get("open", {}).get(str(channel.id))
    if not info: return

    settings  = load("guild_settings.json").get(str(guild.id), {})
    log_ch_id = settings.get("ticket_log_channel")
    log_ch    = guild.get_channel(int(log_ch_id)) if log_ch_id else None

    opener = guild.get_member(info["opener"])
    cat    = TICKET_CATEGORIES.get(info.get("category", "general"), {}).get("label", "Unknown")
    number = info.get("number", "?")

    transcript_url  = await _generate_transcript_url(channel)
    transcript_file = None if transcript_url else await _generate_transcript_fallback(channel)

    if log_ch:
        e = discord.Embed(title=f"Ticket #{number:04d} Closed", color=Config.COLOR_ERR, timestamp=datetime.now(timezone.utc))
        e.add_field(name="Opener",    value=opener.mention if opener else str(info["opener"]), inline=True)
        e.add_field(name="Category",  value=cat,            inline=True)
        e.add_field(name="Closed by", value=closer.mention, inline=True)
        if reason: e.add_field(name="Reason", value=reason, inline=False)
        if transcript_url: e.add_field(name="Transcript", value=f"[View Transcript]({transcript_url})", inline=False)
        
        await log_ch.send(embed=e, file=transcript_file) if transcript_file else await log_ch.send(embed=e)

    td["open"].pop(str(channel.id), None)
    _save_ticket(guild.id, td)
    await asyncio.sleep(3)
    try: await channel.delete(reason=f"Ticket closed by {closer}")
    except Exception: pass

class CloseReasonModal(discord.ui.Modal, title="Close Ticket with Reason"):
    reason = discord.ui.TextInput(label="Reason for closing", style=discord.TextStyle.paragraph, required=True, max_length=500)
    def __init__(self, channel, closer):
        super().__init__()
        self.channel, self.closer = channel, closer
    async def on_submit(self, interaction):
        await interaction.response.send_message(f"Closing ticket: {self.reason.value}")
        await _do_close(interaction.guild, self.channel, self.closer, self.reason.value)

class CloseRequestOpenerView(discord.ui.View):
    def __init__(self, channel, opener_id, requester):
        super().__init__(timeout=300)
        self.channel, self.opener_id, self.requester = channel, opener_id, requester
    async def interaction_check(self, interaction):
        if interaction.user.id != self.opener_id:
            await interaction.response.send_message("Only the ticket opener can respond.", ephemeral=True)
            return False
        return True
    @discord.ui.button(label="Yes, close it", style=discord.ButtonStyle.danger)
    async def yes(self, interaction, button):
        await interaction.response.send_message("Closing ticket...")
        await _do_close(interaction.guild, self.channel, interaction.user)
    @discord.ui.button(label="No, keep it open", style=discord.ButtonStyle.secondary)
    async def no(self, interaction, button):
        await interaction.response.send_message("Ticket kept open.")
        self.stop()

class TicketControlView(discord.ui.View):
    def __init__(self, channel, opener_id, claimed_by=None):
        super().__init__(timeout=None)
        self.channel   = channel
        self.opener_id = opener_id
        
        for item in self.children:
            if getattr(item, "custom_id", None) == "ticket_claim_btn":
                if claimed_by:
                    item.label = "Unclaim"
                    item.style = discord.ButtonStyle.secondary
                else:
                    item.label = "Claim"
                    item.style = discord.ButtonStyle.primary

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        button_id = interaction.data.get("custom_id")
        if button_id == "ticket_dm_btn":
            if interaction.user.id != self.opener_id:
                await interaction.response.send_message("Only the ticket opener can toggle this.", ephemeral=True)
                return False
            return True
        if button_id in ["ticket_close_btn", "ticket_close_reason_btn", "ticket_claim_btn", "ticket_delete_btn"]:
            if not _is_staff(interaction, interaction.channel.id):
                await interaction.response.send_message("Only staff can use this button.", ephemeral=True)
                return False
            return True
        return True

    @discord.ui.button(label="Close", style=discord.ButtonStyle.danger, custom_id="ticket_close_btn")
    async def close_btn(self, interaction, button):
        await interaction.response.send_message("Closing ticket...")
        await _do_close(interaction.guild, interaction.channel, interaction.user)

    @discord.ui.button(label="Close with Reason", style=discord.ButtonStyle.danger, custom_id="ticket_close_reason_btn")
    async def close_reason_btn(self, interaction, button):
        await interaction.response.send_modal(CloseReasonModal(interaction.channel, interaction.user))

    @discord.ui.button(label="Claim", style=discord.ButtonStyle.primary, custom_id="ticket_claim_btn")
    async def claim_btn(self, interaction, button):
        td   = _ticket_data(interaction.guild.id)
        info = td.get("open", {}).get(str(interaction.channel.id), {})
        
        current_claim = info.get("claimed_by")
        
        if current_claim:
            if current_claim == interaction.user.id:
                info["claimed_by"] = None
                button.label = "Claim"
                button.style = discord.ButtonStyle.primary
                td["open"][str(interaction.channel.id)] = info
                _save_ticket(interaction.guild.id, td)
                await interaction.channel.edit(topic="Unclaimed Ticket")
                await interaction.response.edit_message(view=self)
                await interaction.followup.send(f"{interaction.user.mention} has unclaimed this ticket.")
            else:
                claimer = interaction.guild.get_member(current_claim)
                await interaction.response.send_message(f"Already claimed by {claimer.mention if claimer else 'someone'}.", ephemeral=True)
        else:
            info["claimed_by"] = interaction.user.id
            button.label = "Unclaim"
            button.style = discord.ButtonStyle.secondary
            td["open"][str(interaction.channel.id)] = info
            _save_ticket(interaction.guild.id, td)
            await interaction.channel.edit(topic=f"Claimed by {interaction.user.display_name}")
            await interaction.response.edit_message(view=self)
            await interaction.followup.send(f"{interaction.user.mention} has claimed this ticket.")

    @discord.ui.button(label="Delete", style=discord.ButtonStyle.secondary, custom_id="ticket_delete_btn")
    async def delete_btn(self, interaction, button):
        await interaction.response.send_message("Deleting channel...")
        td = _ticket_data(interaction.guild.id)
        td["open"].pop(str(interaction.channel.id), None)
        _save_ticket(interaction.guild.id, td)
        await asyncio.sleep(2)
        await interaction.channel.delete()

    @discord.ui.button(label="DM on response", style=discord.ButtonStyle.secondary, custom_id="ticket_dm_btn")
    async def dm_btn(self, interaction, button):
        current = _dm_on_response.get(interaction.user.id, False)
        _dm_on_response[interaction.user.id] = not current
        await interaction.response.send_message(f"DM on response {'enabled' if not current else 'disabled'}.", ephemeral=True)

async def _send_support_timing_embed(channel: discord.TextChannel):
    settings = load("guild_settings.json").get(str(channel.guild.id), {})
    start, end = settings.get("support_start"), settings.get("support_end")
    if not start or not end: return

    if _is_within_support_hours(start, end):
        e = discord.Embed(title="We are currently working and will get to you soon", description="Please refrain from pinging staff multiple times as this will not expedite the process", color=Config.COLOR_OK)
    else:
        e = discord.Embed(title="Sorry, we aren't working right now", description=(f"Staff support timings are from **{start}** to **{end}** (UTC). Please do not expect a response before or after these timings although you may receive one"), color=Config.COLOR_ERR)
    await channel.send(embed=e)

async def _send_ticket_embed(channel, opener, number, category_label, fields: dict):
    td = _ticket_data(channel.guild.id)
    e = discord.Embed(
        title=f"Ticket #{number:04d} — {category_label}",
        description=f"Welcome {opener.mention}! Staff will assist you shortly.\n**{_real_open_count(channel.guild, td)}** ticket(s) currently open.",
        color=Config.COLOR_INFO,
        timestamp=datetime.now(timezone.utc),
    )
    for name, value in fields.items(): e.add_field(name=name, value=value or "—", inline=False)

    control_view = TicketControlView(channel, opener.id, claimed_by=None)
    await channel.send(embed=e, view=control_view)

    settings = load("guild_settings.json").get(str(channel.guild.id), {})
    cat = td.get("open", {}).get(str(channel.id), {}).get("category", "general")
    ping_ids = settings.get("ticket_type_roles", {}).get(cat, []) or settings.get("ticket_staff_roles", [])
    valid_ping_ids = [rid for rid in ping_ids if channel.guild.get_role(int(rid))]
    if valid_ping_ids: await channel.send(f"{' '.join(f'<@&{rid}>' for rid in valid_ping_ids)} — New {category_label} ticket!")
    await _send_support_timing_embed(channel)

async def _create_ticket_channel(guild, author, category_key):
    td, settings = _ticket_data(guild.id), load("guild_settings.json").get(str(guild.id), {})

    for ch_id, info in td.get("open", {}).items():
        if info.get("opener") == author.id and guild.get_channel(int(ch_id)):
            return None, guild.get_channel(int(ch_id))

    if _get_tickets_created_today(td) >= AI_TICKETS_PER_DAY_LIMIT:
        return "DAILY_LIMIT", None

    cat_name = settings.get("ticket_category", Config.TICKET_CATEGORY_NAME)
    category = discord.utils.get(guild.categories, name=cat_name) or await guild.create_category(cat_name)

    td["counter"] = td.get("counter", 0) + 1
    number, slug = td["counter"], category_key[:6]

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(read_messages=False),
        author:             discord.PermissionOverwrite(read_messages=True, send_messages=True, attach_files=True),
        guild.me:           discord.PermissionOverwrite(read_messages=True, send_messages=True, manage_channels=True),
    }
    for r_list in [settings.get("ticket_staff_roles", []), settings.get("ticket_type_roles", {}).get(category_key, [])]:
        for role_id in r_list:
            if role := guild.get_role(int(role_id)): overwrites[role] = discord.PermissionOverwrite(read_messages=True, send_messages=True, manage_messages=True)

    channel = await category.create_text_channel(f"{slug}-{number:04d}", overwrites=overwrites)
    td.setdefault("open", {})[str(channel.id)] = {
        "opener":     author.id,
        "number":     number,
        "category":   category_key,
        "opened_at":  datetime.now(timezone.utc).isoformat(),
        "claimed_by": None,
        "ai_response_count": 0,
        "autoclose_at": None
    }
    _save_ticket(guild.id, td)
    return number, channel

class TicketCategorySelect(discord.ui.Select):
    def __init__(self):
        super().__init__(placeholder="Select a ticket category...", min_values=1, max_values=1, options=[discord.SelectOption(label=i["label"], value=k, description=i["description"]) for k, i in TICKET_CATEGORIES.items()], custom_id="ticket_category_select")
    async def callback(self, interaction):
        category_key, td = self.values[0], _ticket_data(interaction.guild.id)
        for ch_id, info in td.get("open", {}).items():
            if info.get("opener") == interaction.user.id and interaction.guild.get_channel(int(ch_id)):
                return await interaction.response.send_message(f"You already have an open ticket: <#{ch_id}>", ephemeral=True)
        if _get_tickets_created_today(td) >= AI_TICKETS_PER_DAY_LIMIT:
            return await interaction.response.send_message(f"Daily ticket limit reached ({AI_TICKETS_PER_DAY_LIMIT}). Please try again tomorrow.", ephemeral=True)
        await interaction.response.send_modal(CATEGORY_MODALS[category_key](interaction.guild, interaction.user, category_key))

class BugReportModal(discord.ui.Modal, title="Bug Report"):
    bug_description = discord.ui.TextInput(label="Describe the bug", style=discord.TextStyle.paragraph, placeholder="What went wrong?", required=True, max_length=500)
    reproduction = discord.ui.TextInput(label="How to reproduce", style=discord.TextStyle.paragraph, placeholder="Steps to reproduce the bug...", required=False, max_length=300)
    expected = discord.ui.TextInput(label="Expected behavior", style=discord.TextStyle.paragraph, placeholder="What should happen instead?", required=False, max_length=300)
    def __init__(self, guild, author, category_key):
        super().__init__()
        self.guild, self.author, self.category_key = guild, author, category_key
    async def on_submit(self, interaction):
        try:
            result = await _create_ticket_channel(self.guild, self.author, self.category_key)
            if result[0] == "DAILY_LIMIT": return await interaction.response.send_message("Daily ticket limit reached.", ephemeral=True)
            number, channel = result
            if number is None: return await interaction.response.send_message(f"You already have an open ticket: {channel.mention}", ephemeral=True)
            await interaction.response.send_message(f"Ticket created: {channel.mention}", ephemeral=True)
            await _send_ticket_embed(channel, self.author, number, "Bug Report", {"Bug Description": self.bug_description.value, "Reproduction": self.reproduction.value or "Not specified", "Expected Behavior": self.expected.value or "Not specified"})
        except Exception: await interaction.response.send_message("Error creating ticket.", ephemeral=True)

class CapeSubmitModal(discord.ui.Modal, title="Cape Submit"):
    cape_name = discord.ui.TextInput(label="Cape name", required=True, max_length=100)
    cape_link = discord.ui.TextInput(label="Cape image/design link", required=True, max_length=300)
    description = discord.ui.TextInput(label="Cape description", style=discord.TextStyle.paragraph, required=False, max_length=300)
    def __init__(self, guild, author, category_key):
        super().__init__()
        self.guild, self.author, self.category_key = guild, author, category_key
    async def on_submit(self, interaction):
        try:
            result = await _create_ticket_channel(self.guild, self.author, self.category_key)
            if result[0] == "DAILY_LIMIT": return await interaction.response.send_message("Daily ticket limit reached.", ephemeral=True)
            number, channel = result
            if number is None: return await interaction.response.send_message(f"You already have an open ticket: {channel.mention}", ephemeral=True)
            await interaction.response.send_message(f"Ticket created: {channel.mention}", ephemeral=True)
            await _send_ticket_embed(channel, self.author, number, "Cape Submit", {"Cape Name": self.cape_name.value, "Link": self.cape_link.value, "Description": self.description.value or "None provided"})
        except Exception: await interaction.response.send_message("Error creating ticket.", ephemeral=True)

class GeneralSupportModal(discord.ui.Modal, title="General Support"):
    issue = discord.ui.TextInput(label="What do you need help with?", style=discord.TextStyle.paragraph, placeholder="Describe your issue in detail...", required=True, max_length=500)
    tried = discord.ui.TextInput(label="What have you already tried?", style=discord.TextStyle.paragraph, placeholder="e.g. Checked the FAQ, restarted, etc.", required=False, max_length=300)
    def __init__(self, guild, author, category_key):
        super().__init__()
        self.guild, self.author, self.category_key = guild, author, category_key
    async def on_submit(self, interaction):
        try:
            result = await _create_ticket_channel(self.guild, self.author, self.category_key)
            if result[0] == "DAILY_LIMIT": return await interaction.response.send_message("Daily ticket limit reached.", ephemeral=True)
            number, channel = result
            if number is None: return await interaction.response.send_message(f"You already have an open ticket: {channel.mention}", ephemeral=True)
            await interaction.response.send_message(f"Ticket created: {channel.mention}", ephemeral=True)
            await _send_ticket_embed(channel, self.author, number, "General Support", {"Issue": self.issue.value, "Already Tried": self.tried.value or "Not specified"})
        except Exception: await interaction.response.send_message("Error creating ticket.", ephemeral=True)

class PartnershipModal(discord.ui.Modal, title="Partnership Request"):
    server_name  = discord.ui.TextInput(label="Your server name", required=True, max_length=100)
    invite       = discord.ui.TextInput(label="Invite link", required=True, max_length=200)
    member_count = discord.ui.TextInput(label="Member count", required=True, max_length=20)
    description  = discord.ui.TextInput(label="Brief description of your server", style=discord.TextStyle.paragraph, required=True, max_length=400)
    def __init__(self, guild, author, category_key):
        super().__init__()
        self.guild, self.author, self.category_key = guild, author, category_key
    async def on_submit(self, interaction):
        try:
            result = await _create_ticket_channel(self.guild, self.author, self.category_key)
            if result[0] == "DAILY_LIMIT": return await interaction.response.send_message("Daily ticket limit reached.", ephemeral=True)
            number, channel = result
            if number is None: return await interaction.response.send_message(f"You already have an open ticket: {channel.mention}", ephemeral=True)
            await interaction.response.send_message(f"Ticket created: {channel.mention}", ephemeral=True)
            await _send_ticket_embed(channel, self.author, number, "Partnership Request", {"Server": self.server_name.value, "Invite": self.invite.value, "Members": self.member_count.value, "Description": self.description.value})
        except Exception: await interaction.response.send_message("Error creating ticket.", ephemeral=True)

class IGNRecoveryModal(discord.ui.Modal, title="Claimed IGN Recovery"):
    ign_name = discord.ui.TextInput(label="Claimed IGN to recover", required=True, max_length=100)
    proof = discord.ui.TextInput(label="Proof of ownership", style=discord.TextStyle.paragraph, required=True, max_length=500)
    account_email = discord.ui.TextInput(label="Associated email", required=False, max_length=200)
    def __init__(self, guild, author, category_key):
        super().__init__()
        self.guild, self.author, self.category_key = guild, author, category_key
    async def on_submit(self, interaction):
        try:
            result = await _create_ticket_channel(self.guild, self.author, self.category_key)
            if result[0] == "DAILY_LIMIT": return await interaction.response.send_message("Daily ticket limit reached.", ephemeral=True)
            number, channel = result
            if number is None: return await interaction.response.send_message(f"You already have an open ticket: {channel.mention}", ephemeral=True)
            await interaction.response.send_message(f"Ticket created: {channel.mention}", ephemeral=True)
            await _send_ticket_embed(channel, self.author, number, "Claimed IGN Recovery", {"IGN": self.ign_name.value, "Proof": self.proof.value, "Email": self.account_email.value or "Not provided"})
        except Exception: await interaction.response.send_message("Error creating ticket.", ephemeral=True)

CATEGORY_MODALS = {
    "bug":        BugReportModal,
    "cape":       CapeSubmitModal,
    "general":    GeneralSupportModal,
    "partnership": PartnershipModal,
    "ign":        IGNRecoveryModal,
}

class TicketPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(TicketCategorySelect())

class Tickets(commands.Cog):
    """Advanced ticket system with AI auto-responses and category dropdown."""

    def __init__(self, bot):
        self.bot = bot
        bot.add_view(TicketPanelView())
        bot.add_view(TicketControlView(None, None))
        self.autoclose_loop.start()

    def cog_unload(self):
        self.autoclose_loop.cancel()

    @tasks.loop(minutes=5)
    async def autoclose_loop(self):
        now = datetime.now(timezone.utc)
        data = load(TICKETS_FILE)
        
        for guild_id_str, td in data.items():
            guild = self.bot.get_guild(int(guild_id_str))
            if not guild: continue
            
            for ch_id_str, info in list(td.get("open", {}).items()):
                autoclose_at_str = info.get("autoclose_at")
                if autoclose_at_str:
                    autoclose_at = datetime.fromisoformat(autoclose_at_str)
                    if now >= autoclose_at:
                        channel = guild.get_channel(int(ch_id_str))
                        if channel:
                            await _do_close(guild, channel, guild.me, "Autoclosed due to timeout.")

    @autoclose_loop.before_loop
    async def before_autoclose_loop(self):
        await self.bot.wait_until_ready()

    ticket = app_commands.Group(name="ticket", description="Ticket system commands")

    
    @ticket.command(name="adduser", description="Add a user to the current ticket.")
    @app_commands.describe(user="The user to add")
    async def ticket_adduser(self, interaction: discord.Interaction, user: discord.Member):
        if not _is_staff(interaction, interaction.channel.id):
            return await interaction.response.send_message("Only staff can use this command.", ephemeral=True)
        td = _ticket_data(interaction.guild.id)
        if str(interaction.channel.id) not in td.get("open", {}):
            return await interaction.response.send_message("This channel is not an active ticket.", ephemeral=True)
            
        await interaction.channel.set_permissions(user, read_messages=True, send_messages=True, attach_files=True)
        await interaction.response.send_message(f"✅ Added {user.mention} to the ticket.")

    @ticket.command(name="removeuser", description="Remove a user from the current ticket.")
    @app_commands.describe(user="The user to remove")
    async def ticket_removeuser(self, interaction: discord.Interaction, user: discord.Member):
        if not _is_staff(interaction, interaction.channel.id):
            return await interaction.response.send_message("Only staff can use this command.", ephemeral=True)
        td = _ticket_data(interaction.guild.id)
        if str(interaction.channel.id) not in td.get("open", {}):
            return await interaction.response.send_message("This channel is not an active ticket.", ephemeral=True)
            
        await interaction.channel.set_permissions(user, read_messages=False, send_messages=False)
        await interaction.response.send_message(f"✅ Removed {user.mention} from the ticket.")

    @ticket.command(name="close", description="Close the current ticket immediately.")
    @app_commands.describe(reason="Optional reason for closing")
    async def ticket_close(self, interaction: discord.Interaction, reason: str = None):
        if not _is_staff(interaction, interaction.channel.id):
            return await interaction.response.send_message("Only staff can use this command.", ephemeral=True)
        td = _ticket_data(interaction.guild.id)
        if str(interaction.channel.id) not in td.get("open", {}):
            return await interaction.response.send_message("This channel is not an active ticket.", ephemeral=True)
            
        await interaction.response.send_message("Closing ticket...")
        await _do_close(interaction.guild, interaction.channel, interaction.user, reason)

    @ticket.command(name="closerequest", description="Ask the ticket opener for permission to close the ticket.")
    async def ticket_closerequest(self, interaction: discord.Interaction):
        if not _is_staff(interaction, interaction.channel.id):
            return await interaction.response.send_message("Only staff can use this command.", ephemeral=True)
        td = _ticket_data(interaction.guild.id)
        info = td.get("open", {}).get(str(interaction.channel.id))
        if not info:
            return await interaction.response.send_message("This channel is not an active ticket.", ephemeral=True)
            
        opener_id = info["opener"]
        opener = interaction.guild.get_member(opener_id)
        
        view = CloseRequestOpenerView(interaction.channel, opener_id, interaction.user)
        await interaction.response.send_message(
            f"{opener.mention if opener else f'<@{opener_id}>'}, staff has requested to close this ticket. Do you agree to close?",
            view=view
        )

    @ticket.command(name="autoclose", description="Set a timer to automatically close this ticket.")
    @app_commands.describe(hours="Number of hours before the ticket auto-closes")
    async def ticket_autoclose(self, interaction: discord.Interaction, hours: float):
        if not _is_staff(interaction, interaction.channel.id):
            return await interaction.response.send_message("Only staff can use this command.", ephemeral=True)
        if hours <= 0:
            return await interaction.response.send_message("Time must be greater than 0.", ephemeral=True)
            
        td = _ticket_data(interaction.guild.id)
        info = td.get("open", {}).get(str(interaction.channel.id))
        if not info:
            return await interaction.response.send_message("This channel is not an active ticket.", ephemeral=True)
            
        close_time = datetime.now(timezone.utc) + timedelta(hours=hours)
        info["autoclose_at"] = close_time.isoformat()
        
        td["open"][str(interaction.channel.id)] = info
        _save_ticket(interaction.guild.id, td)
        
        await interaction.response.send_message(f"✅ Ticket is scheduled to automatically close in **{hours}** hours.")

    @ticket.command(name="open", description="Manually open a ticket for a specific user.")
    @app_commands.describe(user="The user to open a ticket for", category="Ticket category key (e.g., bug, general)")
    async def ticket_open(self, interaction: discord.Interaction, user: discord.Member, category: str):
        if not _is_staff(interaction, interaction.channel.id):
            return await interaction.response.send_message("Only staff can use this command.", ephemeral=True)
            
        if category not in TICKET_CATEGORIES:
            return await interaction.response.send_message(f"Invalid category. Choose from: {', '.join(TICKET_CATEGORIES.keys())}", ephemeral=True)
            
        result = await _create_ticket_channel(interaction.guild, user, category)
        if result[0] == "DAILY_LIMIT":
            return await interaction.response.send_message("Daily ticket limit reached.", ephemeral=True)
        number, channel = result
        if number is None:
            return await interaction.response.send_message(f"User already has an open ticket: {channel.mention}", ephemeral=True)
            
        await interaction.response.send_message(f"✅ Manually opened ticket {channel.mention} for {user.mention}.", ephemeral=True)
        await _send_ticket_embed(channel, user, number, TICKET_CATEGORIES[category]["label"], {"Manual Open": f"Ticket opened by {interaction.user.mention}."})


    @ticket.command(name="setcategory", description="Set the category name where tickets will be created.")
    @app_commands.default_permissions(administrator=True)
    async def ticket_setcategory(self, interaction: discord.Interaction, category_name: str):
        data = load("guild_settings.json")
        gd   = data.setdefault(str(interaction.guild.id), {})
        gd["ticket_category"] = category_name
        save("guild_settings.json", data)
        await interaction.response.send_message(f"Ticket category name set to **{category_name}**.", ephemeral=True)

    @ticket.command(name="setup", description="Send a ticket panel to a channel.")
    @app_commands.default_permissions(administrator=True)
    async def ticket_setup(self, interaction: discord.Interaction, channel: discord.TextChannel = None, title: str = "Help Desk"):
        ch = channel or interaction.channel
        e = discord.Embed(
            title=f"{title}",
            description="Select a category from the dropdown to open a ticket.",
            color=Config.COLOR_INFO,
        )
        if interaction.guild.icon: e.set_thumbnail(url=interaction.guild.icon.url)
        await ch.send(embed=e, view=TicketPanelView())
        await interaction.response.send_message(f"Ticket panel sent to {ch.mention}.", ephemeral=True)

    @ticket.command(name="supporttimes", description="Set the staff support hours shown on every new ticket.")
    @app_commands.default_permissions(manage_guild=True)
    async def ticket_supporttimes(self, interaction: discord.Interaction, start: str, end: str):
        if not _parse_time(start) or not _parse_time(end):
            return await interaction.response.send_message("Invalid time format. Use HH:MM or H:MM AM/PM.", ephemeral=True)
        data = load("guild_settings.json")
        data.setdefault(str(interaction.guild.id), {})["support_start"] = start
        data[str(interaction.guild.id)]["support_end"] = end
        save("guild_settings.json", data)
        await interaction.response.send_message(f"Support times set to **{start}** through **{end}** (UTC).", ephemeral=True)

async def setup(bot):
    await bot.add_cog(Tickets(bot))