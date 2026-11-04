import asyncio
import os
import random
import re
import time
import unicodedata
from typing import Optional

import discord
from discord import app_commands


TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = 1559455132821753856

if not TOKEN:
    raise RuntimeError("Falta la variable DISCORD_TOKEN en Railway.")

GUILD = discord.Object(id=GUILD_ID)

# Message Content se necesita para comandos ?... y respuestas numéricas.
# Members se necesita para autorole y eventos de entrada.
intents = discord.Intents.default()
intents.guild_messages = True
intents.message_content = True
intents.members = True

client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# Estos datos viven en memoria y se reinician al reiniciar el proceso.
afk_users = {}
autoroles = {}
invite_cache = {}
invite_counts = {}
active_number_games = {}
number_game_lock = asyncio.Lock()


def formato_duracion(segundos: float) -> str:
    segundos = max(0, int(segundos))
    unidades = [("día", 86400), ("hora", 3600), ("minuto", 60), ("segundo", 1)]
    resultado = []
    for nombre, cantidad in unidades:
        valor, segundos = divmod(segundos, cantidad)
        if valor:
            resultado.append(f"{valor} {nombre if valor == 1 else nombre + 's'}")
    return ", ".join(resultado) if resultado else "0 segundos"


def analizar_duracion(texto: str) -> Optional[int]:
    texto = unicodedata.normalize("NFD", texto.strip().lower())
    texto = "".join(c for c in texto if unicodedata.category(c) != "Mn")
    match = re.fullmatch(
        r"(\d+)\s*(meses?|minutos?|mins?|m|horas?|hrs?|h|dias?|d|segundos?|segs?|s)",
        texto,
    )
    if not match:
        return None

    cantidad = int(match.group(1))
    unidad = match.group(2)
    if cantidad < 1:
        return None
    if unidad.startswith("mes"):
        return cantidad * 30 * 24 * 60 * 60
    if unidad.startswith("d"):
        return cantidad * 24 * 60 * 60
    if unidad.startswith("h"):
        return cantidad * 60 * 60
    if unidad.startswith("m"):
        return cantidad * 60
    return cantidad


async def activar_afk(guild_id: int, user: discord.abc.User, motivo: str) -> None:
    afk_users[(guild_id, user.id)] = {"inicio": time.time(), "motivo": motivo or "Sin motivo"}


async def moderar_miembro(
    guild: discord.Guild,
    actor: discord.Member,
    miembro: discord.Member,
    accion: str,
) -> str:
    permiso = "ban_members" if accion == "ban" else "kick_members"
    if not getattr(actor.guild_permissions, permiso, False):
        return "No tienes permiso para hacer eso."

    bot_member = guild.me
    if bot_member is None or not getattr(bot_member.guild_permissions, permiso, False):
        return "Al bot le falta el permiso necesario."
    if miembro.id == guild.owner_id:
        return "No se puede moderar al propietario del servidor."
    if miembro.id == actor.id:
        return "No puedes usar este comando contigo mismo."
    if actor.id != guild.owner_id and miembro.top_role >= actor.top_role:
        return "No puedes moderar a alguien con un rol igual o superior al tuyo."
    if miembro.top_role >= bot_member.top_role:
        return "El rol del miembro está por encima o al mismo nivel que el rol del bot."

    try:
        if accion == "ban":
            await guild.ban(miembro, reason=f"Acción solicitada por {actor}")
            return f"🔨 {miembro.mention} fue baneado."
        await guild.kick(miembro, reason=f"Acción solicitada por {actor}")
        return f"👢 {miembro.mention} fue expulsado."
    except discord.Forbidden:
        return "Discord rechazó la acción. Revisa los permisos y la posición de los roles."
    except discord.HTTPException:
        return "No pude completar la acción por un error de Discord."


@tree.command(name="ping", description="Comprueba si el bot responde", guild=GUILD)
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("¡Pong! ✅")


@tree.command(name="hola", description="Saluda al usuario", guild=GUILD)
async def hola(interaction: discord.Interaction):
    await interaction.response.send_message(
        f"¡Hola, {interaction.user.mention}! 👋",
        allowed_mentions=discord.AllowedMentions.none(),
    )


@tree.command(name="ayuda", description="Muestra los comandos disponibles", guild=GUILD)
async def ayuda(interaction: discord.Interaction):
    await interaction.response.send_message(
        "**Comandos:** `/ping`, `/hola`, `/afk`, `/avatar`, `/sorteo`, "
        "`/cookie game`, `/número`, `/autorole`, `/ban`, `/kick`, `/invite` y `/anuncio`.\n"
        "También: `?afk motivo`, `?avatar` y responde a un mensaje con `?ban` o `?kick`.",
        ephemeral=True,
    )


@tree.command(name="afk", description="Activa tu estado AFK", guild=GUILD)
@app_commands.describe(motivo="Motivo opcional, por ejemplo: durmiendo")
async def afk_slash(interaction: discord.Interaction, motivo: Optional[str] = None):
    if interaction.guild_id is None:
        await interaction.response.send_message("Úsalo dentro de un servidor.", ephemeral=True)
        return
    motivo_final = motivo.strip() if motivo and motivo.strip() else "Sin motivo"
    await activar_afk(interaction.guild_id, interaction.user, motivo_final)
    await interaction.response.send_message(
        f"{interaction.user.mention} está en modo AFK. Motivo: {motivo_final}",
        allowed_mentions=discord.AllowedMentions.none(),
    )


async def crear_embed_avatar(usuario: discord.abc.User) -> discord.Embed:
    try:
        usuario_completo = await client.fetch_user(usuario.id)
    except discord.HTTPException:
        usuario_completo = usuario

    embed = discord.Embed(title=f"Avatar y banner de {usuario}", color=discord.Color.blurple())
    embed.set_thumbnail(url=usuario.display_avatar.url)
    banner = getattr(usuario_completo, "banner", None)
    if banner:
        embed.set_image(url=banner.url)
    else:
        embed.description = "Este usuario no tiene un banner configurado."
    return embed


@tree.command(name="avatar", description="Muestra el avatar y banner de un miembro", guild=GUILD)
@app_commands.describe(usuario="Miembro cuyo avatar y banner quieres ver")
async def avatar_slash(interaction: discord.Interaction, usuario: discord.Member):
    await interaction.response.send_message(embed=await crear_embed_avatar(usuario))


class SorteoView(discord.ui.View):
    def __init__(self, premio: str, cantidad_ganadores: int, duracion: int):
        super().__init__(timeout=None)
        self.premio = premio
        self.cantidad_ganadores = cantidad_ganadores
        self.duracion = duracion
        self.participantes = set()
        self.mensaje = None
        self.canal = None
        self.finalizado = False
        self.lock = asyncio.Lock()

    def crear_embed(self, finalizado: bool = False, ganadores=None) -> discord.Embed:
        embed = discord.Embed(
            title="🎉 Sorteo",
            description=f"**Premio:** {self.premio}",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Ganadores", value=str(self.cantidad_ganadores), inline=True)
        embed.add_field(name="Participantes", value=str(len(self.participantes)), inline=True)
        if finalizado:
            resultado = " ".join(f"<@{user_id}>" for user_id in ganadores) if ganadores else "No hubo participantes."
            embed.add_field(name="Resultado", value=resultado, inline=False)
        else:
            embed.add_field(name="Cómo participar", value="Pulsa **🎁 Participar**; vuelve a pulsar para salir.", inline=False)
        return embed

    @discord.ui.button(label="🎁 Participar", style=discord.ButtonStyle.primary, custom_id="sorteo:participar")
    async def participar(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with self.lock:
            if self.finalizado:
                await interaction.response.send_message("Este sorteo ya terminó.", ephemeral=True)
                return
            if interaction.user.id in self.participantes:
                self.participantes.remove(interaction.user.id)
                respuesta = "Saliste del sorteo."
            else:
                self.participantes.add(interaction.user.id)
                respuesta = "¡Ya estás participando!"
            await interaction.response.send_message(respuesta, ephemeral=True)
            if self.mensaje:
                await self.mensaje.edit(embed=self.crear_embed(), view=self)

    async def terminar(self):
        await asyncio.sleep(self.duracion)
        async with self.lock:
            self.finalizado = True
            ganadores = random.sample(
                list(self.participantes),
                k=min(self.cantidad_ganadores, len(self.participantes)),
            ) if self.participantes else []
            for item in self.children:
                item.disabled = True
            if self.mensaje:
                await self.mensaje.edit(embed=self.crear_embed(True, ganadores), view=self)
            if ganadores and self.canal:
                menciones = " ".join(f"<@{user_id}>" for user_id in ganadores)
                await self.canal.send(
                    f"🎉 Sorteo terminado. Ganador(es) de **{self.premio}**: {menciones}",
                    allowed_mentions=discord.AllowedMentions(users=True),
                )


@tree.command(name="sorteo", description="Crea un sorteo en este canal", guild=GUILD)
@app_commands.describe(
    ganadores="Cantidad de ganadores (1 a 10)",
    duracion="Ejemplo: 10 minutos, 2 horas, 3 días o 1 mes",
    premio="Premio opcional",
)
async def sorteo(
    interaction: discord.Interaction,
    ganadores: app_commands.Range[int, 1, 10],
    duracion: str,
    premio: Optional[str] = None,
):
    segundos = analizar_duracion(duracion)
    if segundos is None:
        await interaction.response.send_message(
            "Duración no válida. Usa, por ejemplo, `10 minutos`, `2 horas`, `3 días` o `1 mes`.",
            ephemeral=True,
        )
        return
    if interaction.channel is None:
        await interaction.response.send_message("No encontré este canal.", ephemeral=True)
        return
    premio_final = premio.strip() if premio and premio.strip() else "Premio sorpresa"
    vista = SorteoView(premio_final, ganadores, segundos)
    vista.canal = interaction.channel
    await interaction.response.send_message(embed=vista.crear_embed(), view=vista)
    vista.mensaje = await interaction.original_response()
    asyncio.create_task(vista.terminar())


class CookieGameView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)
        self.finished = False
        self.lock = asyncio.Lock()
        self.message = None

    @discord.ui.button(label="🍪", style=discord.ButtonStyle.success, custom_id="cookie_game:click")
    async def click_cookie(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with self.lock:
            if self.finished:
                await interaction.response.send_message("Alguien ya tocó la galleta y ganó.", ephemeral=True)
                return
            self.finished = True
            button.disabled = True
            await interaction.response.edit_message(
                content=f"🍪 {interaction.user.mention} tocó la galleta primero y ganó.",
                view=self,
                allowed_mentions=discord.AllowedMentions.none(),
            )

    async def on_timeout(self):
        if self.finished:
            return
        self.finished = True
        for item in self.children:
            item.disabled = True
        if self.message:
            await self.message.edit(content="⏰ Se acabó el tiempo: nadie tocó la galleta.", view=self)


cookie_group = app_commands.Group(name="cookie", description="Juegos de galleta")


@cookie_group.command(name="game", description="Pulsa la galleta cuando aparezca")
async def cookie_game(interaction: discord.Interaction):
    await interaction.response.send_message("La galleta aparece en **5**…")
    mensaje = await interaction.original_response()
    for cuenta in range(4, 0, -1):
        await asyncio.sleep(1)
        await mensaje.edit(content=f"La galleta aparece en **{cuenta}**…")
    await asyncio.sleep(1)
    vista = CookieGameView()
    vista.message = mensaje
    await mensaje.edit(content="🍪 ¡Apareció la galleta! ¡Tócala primero!", view=vista)


tree.add_command(cookie_group, guild=GUILD)


@tree.command(name="número", description="Elige un número secreto del 1 al 200", guild=GUILD)
@app_commands.describe(numero="Número secreto, entre 1 y 200")
async def numero_game(interaction: discord.Interaction, numero: app_commands.Range[int, 1, 200]):
    if interaction.guild_id is None or interaction.channel is None:
        await interaction.response.send_message("Úsalo dentro de un servidor.", ephemeral=True)
        return
    async with number_game_lock:
        if interaction.guild_id in active_number_games:
            await interaction.response.send_message(
                "Ya hay una partida activa en este servidor.", ephemeral=True
            )
            return
        active_number_games[interaction.guild_id] = {
            "numero": numero,
            "autor_id": interaction.user.id,
        }
    await interaction.response.send_message("Partida iniciada. El número no se mostrará aquí.", ephemeral=True)
    await interaction.channel.send(
        f"🎲 {interaction.user.mention} está pensando en un número del 1 al 200. ¡Adivínenlo antes que los demás!",
        allowed_mentions=discord.AllowedMentions.none(),
    )


@tree.command(name="autorole", description="Configura el rol de los nuevos miembros", guild=GUILD)
@app_commands.default_permissions(manage_roles=True)
@app_commands.describe(rol="Rol que recibirán los nuevos miembros")
async def autorole_command(interaction: discord.Interaction, rol: discord.Role):
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("Úsalo dentro de un servidor.", ephemeral=True)
        return
    actor = interaction.user
    bot_member = interaction.guild.me
    if not actor.guild_permissions.manage_roles:
        await interaction.response.send_message("Necesitas el permiso Administrar roles.", ephemeral=True)
        return
    if bot_member is None or not bot_member.guild_permissions.manage_roles:
        await interaction.response.send_message("Al bot le falta Administrar roles.", ephemeral=True)
        return
    if rol.is_default() or rol.managed:
        await interaction.response.send_message("Ese rol no se puede asignar automáticamente.", ephemeral=True)
        return
    if rol >= bot_member.top_role:
        await interaction.response.send_message("El rol del bot debe estar por encima del rol elegido.", ephemeral=True)
        return
    if actor.id != interaction.guild.owner_id and rol >= actor.top_role:
        await interaction.response.send_message("Solo puedes elegir roles por debajo de tu rol más alto.", ephemeral=True)
        return
    autoroles[interaction.guild_id] = rol.id
    await interaction.response.send_message(
        f"✅ Los nuevos miembros recibirán el rol {rol.mention}.",
        allowed_mentions=discord.AllowedMentions.none(),
    )


@tree.command(name="ban", description="Banea a un miembro", guild=GUILD)
@app_commands.default_permissions(ban_members=True)
@app_commands.describe(usuario="Miembro que quieres banear")
async def ban_command(interaction: discord.Interaction, usuario: discord.Member):
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("Úsalo dentro de un servidor.", ephemeral=True)
        return
    resultado = await moderar_miembro(interaction.guild, interaction.user, usuario, "ban")
    await interaction.response.send_message(resultado, allowed_mentions=discord.AllowedMentions.none())


@tree.command(name="kick", description="Expulsa a un miembro", guild=GUILD)
@app_commands.default_permissions(kick_members=True)
@app_commands.describe(usuario="Miembro que quieres expulsar")
async def kick_command(interaction: discord.Interaction, usuario: discord.Member):
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("Úsalo dentro de un servidor.", ephemeral=True)
        return
    resultado = await moderar_miembro(interaction.guild, interaction.user, usuario, "kick")
    await interaction.response.send_message(resultado, allowed_mentions=discord.AllowedMentions.none())


@tree.command(name="invite", description="Muestra invitaciones atribuidas a un miembro", guild=GUILD)
@app_commands.describe(usuario="Usuario opcional; vacío muestra tus invitaciones")
async def invite_command(interaction: discord.Interaction, usuario: Optional[discord.Member] = None):
    if interaction.guild_id is None:
        await interaction.response.send_message("Úsalo dentro de un servidor.", ephemeral=True)
        return
    objetivo = usuario or interaction.user
    total = invite_counts.get((interaction.guild_id, objetivo.id), 0)
    await interaction.response.send_message(
        f"📨 {objetivo.mention} ha invitado a **{total}** persona(s), contadas mientras el bot está activo.",
        allowed_mentions=discord.AllowedMentions.none(),
    )


@tree.command(name="anuncio", description="Envía un anuncio importante", guild=GUILD)
@app_commands.default_permissions(manage_messages=True)
@app_commands.describe(mensaje="Texto obligatorio del anuncio")
async def anuncio(interaction: discord.Interaction, mensaje: str):
    mensaje = mensaje.strip()
    if not mensaje:
        await interaction.response.send_message("Debes escribir el mensaje del anuncio.", ephemeral=True)
        return
    texto = f"📢 **ANUNCIO IMPORTANTE**\n{mensaje}"
    if len(texto) > 2000:
        await interaction.response.send_message("El anuncio supera el límite de 2000 caracteres.", ephemeral=True)
        return
    await interaction.response.send_message(texto, allowed_mentions=discord.AllowedMentions.none())


@client.event
async def on_member_join(member: discord.Member):
    guild_obj = member.guild
    role_id = autoroles.get(guild_obj.id)
    if role_id:
        role = guild_obj.get_role(role_id)
        bot_member = guild_obj.me
        if role and bot_member and bot_member.guild_permissions.manage_roles and role < bot_member.top_role:
            try:
                await member.add_roles(role, reason="Rol automático para nuevo miembro")
            except (discord.Forbidden, discord.HTTPException) as error:
                print(f"No se pudo asignar el autorole: {error}")

    try:
        current_invites = await guild_obj.invites()
    except (discord.Forbidden, discord.HTTPException) as error:
        print(f"No pude consultar invitaciones en {guild_obj.id}: {error}")
        return

    previous = invite_cache.get(guild_obj.id, {})
    used = next(
        (invite for invite in current_invites if (invite.uses or 0) > previous.get(invite.code, 0)),
        None,
    )
    invite_cache[guild_obj.id] = {invite.code: invite.uses or 0 for invite in current_invites}
    if used and used.inviter:
        key = (guild_obj.id, used.inviter.id)
        invite_counts[key] = invite_counts.get(key, 0) + 1


@client.event
async def on_invite_create(invite: discord.Invite):
    if invite.guild:
        invite_cache.setdefault(invite.guild.id, {})[invite.code] = invite.uses or 0


@client.event
async def on_invite_delete(invite: discord.Invite):
    if invite.guild:
        invite_cache.setdefault(invite.guild.id, {}).pop(invite.code, None)


@client.event
async def on_message(message: discord.Message):
    if message.author.bot or message.guild is None:
        return

    content = message.content.strip()
    lower_content = content.lower()
    parts = content.split(maxsplit=1)
    command = parts[0].lower() if parts else ""

    # ?ban y ?kick requieren responder al mensaje del miembro objetivo.
    if lower_content in ("?ban", "?kick"):
        if not isinstance(message.author, discord.Member):
            return
        reference = message.reference
        if not reference or not reference.message_id:
            await message.channel.send("Responde al mensaje del miembro con `?ban` o `?kick`.")
            return
        try:
            target_message = reference.resolved
            if target_message is None:
                channel = message.guild.get_channel(reference.channel_id)
                if channel is None:
                    channel = await client.fetch_channel(reference.channel_id)
                target_message = await channel.fetch_message(reference.message_id)
            target = message.guild.get_member(target_message.author.id)
            if target is None:
                target = await message.guild.fetch_member(target_message.author.id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            await message.channel.send("No pude encontrar al miembro del mensaje respondido.")
            return
        action = "ban" if lower_content == "?ban" else "kick"
        result = await moderar_miembro(message.guild, message.author, target, action)
        await message.channel.send(result, allowed_mentions=discord.AllowedMentions.none())
        return

    # ?afk acepta un motivo opcional, por ejemplo: ?afk durmiendo
    if command == "?afk":
        reason = parts[1].strip() if len(parts) > 1 else "Sin motivo"
        await activar_afk(message.guild.id, message.author, reason)
        await message.channel.send(
            f"{message.author.mention} está en modo AFK. Motivo: {reason}",
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return

    # Cualquier mensaje normal elimina el AFK del autor.
    own_key = (message.guild.id, message.author.id)
    afk_state = afk_users.pop(own_key, None)
    if afk_state:
        elapsed = formato_duracion(time.time() - afk_state["inicio"])
        await message.channel.send(
            f"{message.author.mention} salió de su estado AFK y llevaba {elapsed}.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    # ?avatar muestra al autor del mensaje respondido, o al autor del comando.
    if command == "?avatar":
        target_user = message.author
        reference = message.reference
        if reference and reference.message_id:
            try:
                referenced = reference.resolved
                if referenced is None:
                    referenced = await message.channel.fetch_message(reference.message_id)
                target_user = referenced.author
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass
        await message.channel.send(embed=await crear_embed_avatar(target_user))
        return

    # La partida puede adivinarse en cualquier canal del servidor.
    if content.isdecimal():
        async with number_game_lock:
            game = active_number_games.get(message.guild.id)
            if (
                game
                and message.author.id != game["autor_id"]
                and 1 <= int(content) <= 200
                and int(content) == game["numero"]
            ):
                active_number_games.pop(message.guild.id, None)
            else:
                game = None
        if game:
            author = message.guild.get_member(game["autor_id"])
            author_text = author.mention if author else "El usuario"
            await message.channel.send(
                f"🎉 ¡Felicidades {message.author.mention}! {author_text} estaba pensando el número **{game['numero']}** y lo adivinaste.",
                allowed_mentions=discord.AllowedMentions.users(),
            )
            return

    # Avisar si mencionan a alguien AFK.
    seen = set()
    for user in message.mentions:
        key = (message.guild.id, user.id)
        state = afk_users.get(key)
        if state and user.id not in seen:
            seen.add(user.id)
            await message.channel.send(
                f"{user.mention} está en modo AFK. Motivo: {state['motivo']}",
                allowed_mentions=discord.AllowedMentions.none(),
            )


@client.event
async def on_ready():
    print(f"Conectado como {client.user}")
    for server in client.guilds:
        try:
            invites = await server.invites()
            invite_cache[server.id] = {invite.code: invite.uses or 0 for invite in invites}
        except (discord.Forbidden, discord.HTTPException) as error:
            print(f"No pude cargar invitaciones de {server.id}: {error}")
    try:
        commands = await tree.sync(guild=GUILD)
        print(f"Comandos sincronizados: {len(commands)}")
    except Exception as error:
        print(f"Error al sincronizar comandos: {error}")


client.run(TOKEN)
