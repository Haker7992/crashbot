import discord
from discord.ext import commands
from discord import app_commands
import asyncio
import aiohttp
import config


class Utils(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="rename", description="Переименовать все каналы")
    @app_commands.checks.has_permissions(administrator=True)
    async def rename(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        tasks = [channel.edit(name=config.GUILD_NAME) for channel in interaction.guild.channels]
        await asyncio.gather(*tasks, return_exceptions=True)
        await interaction.followup.send("Готово.", ephemeral=True)

    @app_commands.command(name="nsfw_all", description="Включить NSFW во всех каналах")
    @app_commands.checks.has_permissions(administrator=True)
    async def nsfw_all(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        tasks = [channel.edit(nsfw=True) for channel in interaction.guild.text_channels]
        await asyncio.gather(*tasks, return_exceptions=True)
        await interaction.followup.send("Готово.", ephemeral=True)

    @app_commands.command(name="unnsfw_all", description="Выключить NSFW во всех каналах")
    @app_commands.checks.has_permissions(administrator=True)
    async def unnsfw_all(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        tasks = [channel.edit(nsfw=False) for channel in interaction.guild.text_channels]
        await asyncio.gather(*tasks, return_exceptions=True)
        await interaction.followup.send("Готово.", ephemeral=True)

    @app_commands.command(name="invs_delete", description="Удалить все приглашения")
    @app_commands.checks.has_permissions(administrator=True)
    async def invs_delete(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        invites = await interaction.guild.invites()
        await asyncio.gather(*[inv.delete() for inv in invites], return_exceptions=True)
        await interaction.followup.send("Готово.", ephemeral=True)

    @app_commands.command(name="webhooks", description="Список вебхуков сервера")
    @app_commands.checks.has_permissions(administrator=True)
    async def webhooks(self, interaction: discord.Interaction):
        whs = await interaction.guild.webhooks()
        if not whs:
            await interaction.response.send_message("Вебхуков нет.", ephemeral=True)
            return
        msg = "\n".join(f"{wh.name}: {wh.url}" for wh in whs)
        await interaction.response.send_message(f"```{msg[:1900]}```", ephemeral=True)

    @app_commands.command(name="ip", description="Информация об IP адресе")
    @app_commands.describe(address="IP адрес")
    async def ip_info(self, interaction: discord.Interaction, address: str):
        await interaction.response.defer()
        async with aiohttp.ClientSession() as session:
            async with session.get(f"http://ip-api.com/json/{address}") as resp:
                data = await resp.json()
        if data['status'] == 'fail':
            await interaction.followup.send(f"Ошибка: {data['message']}")
            return
        embed = discord.Embed(title=f"IP: {address}", color=discord.Color.blue())
        embed.add_field(name="Страна", value=data.get('country', 'N/A'))
        embed.add_field(name="Город", value=data.get('city', 'N/A'))
        embed.add_field(name="ISP", value=data.get('isp', 'N/A'))
        embed.add_field(name="Координаты", value=f"{data.get('lat')}, {data.get('lon')}")
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="nicks_all", description="Сменить ники всем участникам")
    @app_commands.checks.has_permissions(administrator=True)
    async def nicks_all(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        targets = [m for m in guild.members if m.id not in (interaction.user.id, self.bot.user.id, guild.owner_id)]
        async def change_nick(member):
            try:
                await member.edit(nick=config.NICK)
            except Exception:
                pass
        await asyncio.gather(*[change_nick(m) for m in targets])
        await interaction.followup.send("Готово.", ephemeral=True)


async def setup(bot):
    await bot.add_cog(Utils(bot))
