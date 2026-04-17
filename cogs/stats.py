import discord
from discord.ext import commands, tasks
from discord import app_commands
import json
import os
from datetime import datetime


class StatsGraph(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.stats_file = 'member_history.json'
        self.update_stats.start()

    def cog_unload(self):
        self.update_stats.cancel()

    @tasks.loop(seconds=5)
    async def update_stats(self):
        data = self.load_data()
        for guild in self.bot.guilds:
            gid = str(guild.id)
            if gid not in data:
                data[gid] = []
            now = datetime.now().strftime("%H:%M:%S")
            if not data[gid] or data[gid][-1]['count'] != guild.member_count:
                data[gid].append({"date": now, "count": guild.member_count})
                data[gid] = data[gid][-20:]
        self.save_data(data)

    def load_data(self):
        if os.path.exists(self.stats_file):
            with open(self.stats_file, 'r') as f:
                return json.load(f)
        return {}

    def save_data(self, data):
        with open(self.stats_file, 'w') as f:
            json.dump(data, f)

    @app_commands.command(name="chart", description="График изменения участников сервера")
    async def send_chart(self, interaction: discord.Interaction):
        await interaction.response.defer()
        try:
            import matplotlib.pyplot as plt
        except ImportError:
            await interaction.followup.send("Установи matplotlib: `pip install matplotlib`")
            return

        data = self.load_data()
        gid = str(interaction.guild.id)

        if gid not in data or len(data[gid]) < 1:
            await interaction.followup.send("Недостаточно данных.")
            return

        dates = [e['date'] for e in data[gid]]
        counts = [e['count'] for e in data[gid]]

        plt.style.use('dark_background')
        plt.figure(figsize=(10, 5))
        plt.plot(dates, counts, color='#2ecc71', marker='o', linewidth=2, label='Members')
        plt.fill_between(dates, counts, color='#2ecc71', alpha=0.1)
        plt.xticks(rotation=45)
        plt.grid(axis='y', linestyle='--', alpha=0.2)
        plt.tight_layout()

        path = f'chart_{interaction.guild.id}.png'
        plt.savefig(path)
        plt.close()

        await interaction.followup.send(file=discord.File(path))
        os.remove(path)


async def setup(bot):
    await bot.add_cog(StatsGraph(bot))
