import discord
from discord.ext import commands
from discord import app_commands
from collections import Counter
import os


class AllTag(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="alltags", description="Анализ тегов участников сервера")
    async def check_tags(self, interaction: discord.Interaction):
        await interaction.response.defer()
        guild = interaction.guild
        tag_counter = Counter()
        tag_members = {}
        total_members = 0
        members_with_tags = 0

        safe_guild_name = "".join(c for c in guild.name if c.isalnum() or c in (' ', '-', '_')).strip() or str(guild.id)
        server_folder = os.path.join('tags', safe_guild_name)
        os.makedirs(server_folder, exist_ok=True)

        for member in guild.members:
            total_members += 1
            primary_guild = getattr(member, 'primary_guild', None)
            if primary_guild and primary_guild.tag:
                tag_name = primary_guild.tag
                tag_counter[tag_name] += 1
                members_with_tags += 1
                if tag_name not in tag_members:
                    tag_members[tag_name] = []
                tag_members[tag_name].append(str(member.id))

        sorted_tags = tag_counter.most_common()
        result = [f"Всего участников: {total_members}", f"Участников с тегами: {members_with_tags}", ""]
        for tag, count in sorted_tags:
            result.append(f"Тег {tag} - {count}")

        tags_file = os.path.join(server_folder, 'tags.txt')
        with open(tags_file, 'w', encoding='utf-8') as f:
            f.write('\n'.join(result))

        for tag, member_ids in tag_members.items():
            try:
                safe_tag = "".join(c for c in tag if c.isalnum() or c in (' ', '-', '_')).strip() or tag.encode('utf-8').hex()
                with open(os.path.join(server_folder, f"{safe_tag}tagsmembers.txt"), 'w', encoding='utf-8') as f:
                    f.write(f"Тег: {tag}\nКоличество: {len(member_ids)}\n\nID:\n" + '\n'.join(member_ids))
            except Exception as e:
                print(f"Ошибка файла {tag}: {e}")

        if sorted_tags:
            output = '\n'.join(result[:23])
            await interaction.followup.send(f"```\n{output}\n```", file=discord.File(tags_file))
        else:
            await interaction.followup.send(f"Теги не найдены. Всего участников: {total_members}")


async def setup(bot):
    await bot.add_cog(AllTag(bot))
