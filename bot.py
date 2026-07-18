"""
HMT+ Award Bot
--------------
Discord bot that:
  1. Takes a Roblox username (via /awardhmtexpansion or ?awardhmtexpansion)
  2. Finds the matching Discord member in the server (trimming clan tags
     like "[OF-1] " from their display name)
  3. Looks up the Roblox user ID for that username via Roblox's public API
  4. Writes HMT+ = true for that user ID to a Roblox DataStore via
     Roblox Open Cloud
  5. DMs the Discord member congratulating them

If no Discord member match is found, the bot DMs the configured
"resolver" (you) and asks you to specify who it is, then waits for
your reply before continuing.

Config comes from environment variables — see .env.example.
"""

import os
import re
import asyncio
import logging
from typing import Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("hmt-bot")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]

ROBLOX_API_KEY = os.environ["ROBLOX_API_KEY"]
ROBLOX_UNIVERSE_ID = os.environ["ROBLOX_UNIVERSE_ID"]
ROBLOX_DATASTORE_NAME = os.environ.get("ROBLOX_DATASTORE_NAME", "HMT+")

# Comma-separated Discord user IDs allowed to run the command.
AUTHORIZED_USER_IDS = {
    int(uid.strip())
    for uid in os.environ.get("AUTHORIZED_USER_IDS", "").split(",")
    if uid.strip()
}

# Who gets DM'd to manually resolve an ambiguous/missing match.
RESOLVER_USER_ID = int(os.environ["RESOLVER_USER_ID"])

# How long to wait for the resolver's reply before giving up.
RESOLVER_TIMEOUT_SECONDS = int(os.environ.get("RESOLVER_TIMEOUT_SECONDS", "300"))

ROBLOX_USERS_API = "https://users.roblox.com/v1/usernames/users"
ROBLOX_OPEN_CLOUD_DATASTORE_API = (
    "https://apis.roblox.com/datastores/v1/universes/{universe_id}/standard-datastores"
    "/datastore/entries/entry"
)

# ---------------------------------------------------------------------------
# Discord setup
# ---------------------------------------------------------------------------

intents = discord.Intents.default()
intents.members = True  # required to search server members
intents.message_content = True  # required for the "?" prefix command

bot = commands.Bot(command_prefix="?", intents=intents)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def normalize(name: str) -> str:
    """Lowercase and strip anything that isn't a letter/digit, for fuzzy
    comparison between a Roblox username and a Discord display name."""
    return re.sub(r"[^a-z0-9]", "", name.lower())


def strip_clan_tag(display_name: str) -> str:
    """
    Strip a leading clan/rank tag like "[OF-1] " or "[NCO]" from a Discord
    display name, leaving (hopefully) just the Roblox username part.

    Examples:
        "[OF-1] Player1"  -> "Player1"
        "[NCO] John_Smith" -> "John_Smith"
        "Player1"          -> "Player1"   (no tag, unchanged)
    """
    # Strip one or more leading bracketed tags, with optional whitespace
    # between them, e.g. "[OF-1][HQ] Player1" -> "Player1"
    stripped = re.sub(r"^(\s*\[[^\]]*\]\s*)+", "", display_name)
    return stripped.strip()


def find_matching_member(
    guild: discord.Guild, roblox_username: str
) -> list[discord.Member]:
    """
    Search guild members for anyone whose (clan-tag-stripped) display name
    or raw username matches the given Roblox username.

    Returns a list because more than one member could plausibly match —
    the caller decides how to handle 0, 1, or many results.
    """
    target = normalize(roblox_username)
    matches = []

    for member in guild.members:
        candidates = {
            member.name,                       # Discord username
            member.display_name,                # nickname/display name (may include tag)
            strip_clan_tag(member.display_name),  # display name, tag stripped
        }
        if member.global_name:
            candidates.add(member.global_name)

        if any(normalize(c) == target for c in candidates if c):
            matches.append(member)

    return matches


async def get_roblox_user_id(session: aiohttp.ClientSession, username: str) -> Optional[int]:
    """Resolve a Roblox username to a numeric user ID."""
    payload = {"usernames": [username], "excludeBannedUsers": False}
    async with session.post(ROBLOX_USERS_API, json=payload) as resp:
        if resp.status != 200:
            log.error("Roblox username lookup failed (%s): %s", resp.status, await resp.text())
            return None
        data = await resp.json()
        results = data.get("data", [])
        if not results:
            return None
        return results[0]["id"]


async def set_hmt_plus_datastore(
    session: aiohttp.ClientSession, roblox_user_id: int
) -> tuple[bool, str]:
    """
    Write HMT+ = true for the given Roblox user ID via Roblox Open Cloud
    Standard DataStore API.

    Returns (success, message).
    """
    url = ROBLOX_OPEN_CLOUD_DATASTORE_API.format(universe_id=ROBLOX_UNIVERSE_ID)
    params = {
        "datastoreName": ROBLOX_DATASTORE_NAME,
        "entryKey": str(roblox_user_id),
    }
    headers = {
        "x-api-key": ROBLOX_API_KEY,
        "Content-Type": "application/json",
    }

    async with session.post(url, params=params, headers=headers, json=True) as resp:
        text = await resp.text()
        if resp.status in (200, 201):
            return True, "OK"
        log.error("Roblox DataStore write failed (%s): %s", resp.status, text)
        return False, f"HTTP {resp.status}: {text}"


async def dm_congrats(member: discord.Member) -> tuple[bool, str]:
    """DM the member congratulating them on HMT+."""
    embed = discord.Embed(
        title="🎖️ Thank you for purchasing HM-T Expansion Pack!",
        description=(
            "You have bought **His Majesty's Treasurer Expansion Pack)** status.\n\n"
            "Thank you for your support and contribution - it's genuinely appreciated."
        ),
        color=discord.Color.gold(),
    )
    try:
        await member.send(embed=embed)
        return True, "OK"
    except discord.Forbidden:
        return False, "DMs are closed for this user."
    except discord.HTTPException as e:
        return False, f"Discord error: {e}"


def is_authorized():
    async def predicate(ctx_or_interaction) -> bool:
        if isinstance(ctx_or_interaction, discord.Interaction):
            user_id = ctx_or_interaction.user.id
        else:
            user_id = ctx_or_interaction.author.id
        return user_id in AUTHORIZED_USER_IDS

    return predicate


# ---------------------------------------------------------------------------
# Core award logic (shared by slash command and ? prefix command)
# ---------------------------------------------------------------------------

async def run_award_flow(
    *,
    guild: discord.Guild,
    roblox_username: str,
    reply: "callable",  # async fn(str) -> sends a status message to the invoker
):
    """
    reply() is called at each step so both the slash command and the
    prefix command can surface progress the same way.
    """
    async with aiohttp.ClientSession() as session:
        # 1. Find the Discord member.
        matches = find_matching_member(guild, roblox_username)
        member: Optional[discord.Member] = None

        if len(matches) == 1:
            member = matches[0]
        elif len(matches) > 1:
            names = ", ".join(f"{m.display_name} ({m.id})" for m in matches)
            await reply(
                f"Found multiple possible matches for `{roblox_username}`: {names}\n"
                f"Asking <@{RESOLVER_USER_ID}> to confirm which one."
            )
            member = await ask_resolver_to_pick(guild, roblox_username, matches)
        else:
            await reply(
                f"Couldn't find a Discord member matching `{roblox_username}`. "
                f"Asking <@{RESOLVER_USER_ID}> to specify manually."
            )
            member = await ask_resolver_for_member(guild, roblox_username)

        if member is None:
            await reply(
                f"No Discord member was resolved for `{roblox_username}`. "
                f"Skipping DM, but I'll still try to update the DataStore "
                f"if I can resolve the Roblox user ID."
            )

        # 2. Resolve Roblox user ID.
        roblox_user_id = await get_roblox_user_id(session, roblox_username)
        if roblox_user_id is None:
            await reply(
                f"Couldn't find a Roblox user with username `{roblox_username}`. "
                f"Aborting — nothing was written to the DataStore."
            )
            return

        # 3. Write to DataStore.
        ok, msg = await set_hmt_plus_datastore(session, roblox_user_id)
        if ok:
            await reply(
                f"✅ DataStore `{ROBLOX_DATASTORE_NAME}` updated: "
                f"user `{roblox_user_id}` ({roblox_username}) → `HMT+ = true`."
            )
        else:
            await reply(f"❌ Failed to update DataStore: {msg}")
            return  # don't DM if the actual award failed to save

        # 4. DM the member, if we have one.
        if member is not None:
            dm_ok, dm_msg = await dm_congrats(member)
            if dm_ok:
                await reply(f"✅ DM sent to {member.mention}.")
            else:
                await reply(f"⚠️ DataStore updated, but DM failed: {dm_msg}")


async def ask_resolver_for_member(
    guild: discord.Guild, roblox_username: str
) -> Optional[discord.Member]:
    """DM the resolver asking them to reply with a user ID or mention."""
    resolver = guild.get_member(RESOLVER_USER_ID) or await bot.fetch_user(RESOLVER_USER_ID)
    try:
        await resolver.send(
            f"I couldn't match Roblox username `{roblox_username}` to anyone in "
            f"**{guild.name}**. Reply here with their Discord user ID or an @mention "
            f"of them (you have {RESOLVER_TIMEOUT_SECONDS // 60} min)."
        )
    except discord.Forbidden:
        log.error("Cannot DM resolver %s — DMs closed.", RESOLVER_USER_ID)
        return None

    def check(msg: discord.Message) -> bool:
        return msg.author.id == RESOLVER_USER_ID and isinstance(msg.channel, discord.DMChannel)

    try:
        reply_msg = await bot.wait_for(
            "message", check=check, timeout=RESOLVER_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        await resolver.send("Timed out waiting for a reply — award was not completed for Discord DM purposes.")
        return None

    return await parse_member_reference(guild, reply_msg.content)


async def ask_resolver_to_pick(
    guild: discord.Guild, roblox_username: str, matches: list[discord.Member]
) -> Optional[discord.Member]:
    resolver = guild.get_member(RESOLVER_USER_ID) or await bot.fetch_user(RESOLVER_USER_ID)
    options = "\n".join(f"{i+1}. {m.display_name} — {m.mention} (`{m.id}`)" for i, m in enumerate(matches))
    try:
        await resolver.send(
            f"Multiple possible Discord matches for Roblox username `{roblox_username}`:\n"
            f"{options}\n\nReply with the number, a user ID, or an @mention."
        )
    except discord.Forbidden:
        log.error("Cannot DM resolver %s — DMs closed.", RESOLVER_USER_ID)
        return None

    def check(msg: discord.Message) -> bool:
        return msg.author.id == RESOLVER_USER_ID and isinstance(msg.channel, discord.DMChannel)

    try:
        reply_msg = await bot.wait_for(
            "message", check=check, timeout=RESOLVER_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        await resolver.send("Timed out — award was not completed for Discord DM purposes.")
        return None

    content = reply_msg.content.strip()
    if content.isdigit() and 1 <= int(content) <= len(matches):
        return matches[int(content) - 1]

    return await parse_member_reference(guild, content)


async def parse_member_reference(
    guild: discord.Guild, text: str
) -> Optional[discord.Member]:
    """Parse a user ID or @mention out of the resolver's reply."""
    text = text.strip()
    id_match = re.search(r"\d{15,20}", text)
    if not id_match:
        return None
    user_id = int(id_match.group())
    member = guild.get_member(user_id)
    if member is None:
        try:
            member = await guild.fetch_member(user_id)
        except discord.NotFound:
            return None
    return member


# ---------------------------------------------------------------------------
# Slash command: /awardhmtexpansion
# ---------------------------------------------------------------------------

@bot.tree.command(
    name="awardhmtexpansion",
    description="Award HMT+ to a player by Roblox username.",
)
@app_commands.describe(roblox_username="The player's Roblox username")
async def awardhmtexpansion_slash(
    interaction: discord.Interaction, roblox_username: str
):
    if interaction.user.id not in AUTHORIZED_USER_IDS:
        await interaction.response.send_message(
            "You're not authorized to use this command.", ephemeral=True
        )
        return

    if interaction.guild is None:
        await interaction.response.send_message(
            "This command must be used in a server.", ephemeral=True
        )
        return

    await interaction.response.defer(thinking=True, ephemeral=True)

    async def reply(text: str):
        await interaction.followup.send(text, ephemeral=True)

    await run_award_flow(
        guild=interaction.guild, roblox_username=roblox_username, reply=reply
    )


# ---------------------------------------------------------------------------
# Prefix command: ?awardhmtexpansion <username>
# ---------------------------------------------------------------------------

@bot.command(name="awardhmtexpansion")
async def awardhmtexpansion_prefix(ctx: commands.Context, roblox_username: str = None):
    if ctx.author.id not in AUTHORIZED_USER_IDS:
        await ctx.reply("You're not authorized to use this command.")
        return

    if roblox_username is None:
        await ctx.reply("Usage: `?awardhmtexpansion <roblox_username>`")
        return

    if ctx.guild is None:
        await ctx.reply("This command must be used in a server.")
        return

    async def reply(text: str):
        await ctx.reply(text)

    await run_award_flow(guild=ctx.guild, roblox_username=roblox_username, reply=reply)

class Health(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
    def log_message(self, *args):
        pass  # silence request logging

def run_health_server():
    port = int(os.environ.get("PORT", 8000))
    HTTPServer(("0.0.0.0", port), Health).serve_forever()

# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

@bot.event
async def on_ready():
    log.info("Logged in as %s (%s)", bot.user, bot.user.id)

    await bot.change_presence(
        activity=discord.CustomActivity(name="Controlling the army!"),
        status=discord.Status.online,
    )

    try:
        synced = await bot.tree.sync()
        log.info("Synced %d slash command(s).", len(synced))
    except Exception:
        log.exception("Failed to sync slash commands.")


if __name__ == "__main__":
    threading.Thread(target=run_health_server, daemon=True).start()
    bot.run(DISCORD_TOKEN)
