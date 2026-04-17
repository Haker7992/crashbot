import discord
from discord.ext import commands
from discord import app_commands


class Help(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="help", description="Список всех команд бота")
    async def help_command(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="ArchAngels Bot — Команды",
            color=discord.Color.red()
        )
        embed.add_field(name="Краш", value="`/deleteall` - Запустить краш\n`/stop` - Остановить краш\n`/cleanup` - Очистить сервер", inline=False)
        embed.add_field(name="Управление", value="`/rename` - Переименовать все каналы\n`/nsfw_all` - Включить NSFW\n`/unnsfw_all` - Выключить NSFW\n`/invs_delete` - Удалить приглашения\n`/nicks_all` - Сменить ники всем", inline=False)
        embed.add_field(name="Утилиты", value="`/webhooks` - Список вебхуков\n`/ip` - Инфо об IP\n`/alltags` - Анализ тегов\n`/chart` - График участников", inline=False)
        await interaction.response.send_message(embed=embed)


async def setup(bot):
    await bot.add_cog(Help(bot))
