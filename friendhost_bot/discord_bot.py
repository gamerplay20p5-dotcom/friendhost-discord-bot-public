from __future__ import annotations

import asyncio
import re
import secrets
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any

import discord
from discord import app_commands

from .ai_assistant import GeminiAssistant
from .config import Settings
from .supabase_store import InvoiceInput, SupabaseStore
from .updater import UpdateError, restart_process, update_from_git


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def clean_email(value: str) -> str:
    email = value.strip().lower()
    if not EMAIL_RE.match(email):
        raise ValueError("E-mail invalido.")
    return email


def clean_text(value: str, max_length: int = 180) -> str:
    return re.sub(r"\s+", " ", value.strip())[:max_length]


def clean_sku(value: str) -> str:
    sku = clean_text(value, 32).lower()
    if not re.fullmatch(r"[a-z0-9-]{3,32}", sku):
        raise ValueError("SKU invalido. Exemplo: ddr4-8.")
    return sku


def clean_coupon_code(value: str) -> str:
    code = clean_text(value, 32).upper()
    if not re.fullmatch(r"[A-Z0-9_-]{3,32}", code):
        raise ValueError("Codigo invalido. Use 3 a 32 letras, numeros, _ ou -.")
    return code


def money(value: Any) -> str:
    try:
        amount = float(value)
    except (TypeError, ValueError):
        amount = 0
    return f"R$ {amount:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def make_invoice_id() -> str:
    return f"FH-MN-{secrets.randbelow(100000):05d}"


def support_channel_name(customer_email: str) -> str:
    local_part = customer_email.split("@", 1)[0].lower()
    safe = re.sub(r"[^a-z0-9-]", "", local_part.replace(".", "-").replace("_", "-"))
    return f"atendimento-{safe[:70] or 'cliente'}"


def format_datetime(value: Any) -> str:
    if not value:
        return "Nao informado"
    return str(value).replace("T", " ").replace("+00:00", " UTC")[:19]


class FriendHostBot(discord.Client):
    def __init__(self, settings: Settings, store: SupabaseStore) -> None:
        intents = discord.Intents.default()
        intents.guilds = True
        intents.messages = True
        intents.message_content = True

        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none())
        self.settings = settings
        self.store = store
        self.tree = app_commands.CommandTree(self)
        self.guild_object = discord.Object(id=settings.guild_id)
        self.last_support_created_at: str | None = None
        self.processed_support_ids: set[str] = set()
        self.support_task: asyncio.Task[None] | None = None
        self.last_invoice_created_at: str | None = None
        self.processed_invoice_ids: set[str] = set()
        self.orders_task: asyncio.Task[None] | None = None
        self.ai = GeminiAssistant(settings.gemini_api_key, settings.gemini_model) if settings.gemini_api_key else None
        self.ai_semaphore = asyncio.Semaphore(1)
        self.ai_last_request: dict[int, float] = {}
        self.honeypot_kicks = 0

    async def setup_hook(self) -> None:
        self._register_commands()
        self.tree.copy_global_to(guild=self.guild_object)
        await self.tree.sync(guild=self.guild_object)

    async def on_ready(self) -> None:
        if self.last_support_created_at is None and not self.settings.support_sync_on_start:
            try:
                latest = await asyncio.to_thread(self.store.latest_customer_support_message)
            except Exception as exc:
                print(f"Erro ao iniciar sincronizacao do suporte: {exc}")
                latest = None
            if latest:
                self.last_support_created_at = latest.get("created_at")
                if latest.get("id"):
                    self.processed_support_ids.add(str(latest["id"]))

        if self.support_task is None or self.support_task.done():
            self.support_task = asyncio.create_task(self._support_loop())

        if self.settings.orders_channel_id and (self.orders_task is None or self.orders_task.done()):
            if self.last_invoice_created_at is None and not self.settings.orders_sync_on_start:
                try:
                    latest_invoice = await asyncio.to_thread(self.store.latest_invoice)
                except Exception as exc:
                    print(f"Erro ao iniciar sincronizacao de pedidos: {exc}")
                    latest_invoice = None
                if latest_invoice:
                    self.last_invoice_created_at = latest_invoice.get("created_at")
                    if latest_invoice.get("id"):
                        self.processed_invoice_ids.add(str(latest_invoice["id"]))
            self.orders_task = asyncio.create_task(self._orders_loop())

        if self.settings.honeypot_channel_id:
            try:
                await self._ensure_honeypot_notice()
            except Exception as exc:
                print(f"Erro ao preparar honeypot: {exc}")

        user = self.user.name if self.user else self.settings.bot_name
        ai_status = "ativa" if self.ai else "desativada"
        print(f"{user} online. Comandos sincronizados, suporte ativo e IA {ai_status}.")

    def _is_staff(self, user: discord.abc.User) -> bool:
        if not isinstance(user, discord.Member):
            return False
        if user.guild_permissions.administrator:
            return True
        allowed_roles = set(self.settings.staff_role_ids)
        return bool(allowed_roles and any(role.id in allowed_roles for role in user.roles))

    async def _require_staff(self, interaction: discord.Interaction) -> bool:
        if self._is_staff(interaction.user):
            return True
        await interaction.response.send_message(
            "Este comando e restrito a equipe FriendHost.",
            ephemeral=True,
        )
        return False

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or not isinstance(message.channel, discord.TextChannel):
            return

        if message.channel.id == self.settings.honeypot_channel_id:
            await self._handle_honeypot_message(message)
            return

        if not message.channel.name.startswith("atendimento-"):
            return

        if not self._is_staff(message.author):
            return

        customer_email = (message.channel.topic or "").strip().lower()
        if not customer_email:
            await message.reply("Nao encontrei o e-mail do cliente no topico deste canal.")
            return

        if not message.content.strip():
            await message.reply("Mensagem vazia. Envie um texto para responder ao cliente.")
            return

        try:
            await asyncio.to_thread(
                self.store.create_support_reply,
                customer_email,
                message.content.strip()[:2000],
            )
        except Exception as exc:
            print(f"Erro ao enviar resposta para o Supabase: {exc}")
            await message.add_reaction("❌")
            return

        await message.add_reaction("✅")

    def _honeypot_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="NAO ENVIE MENSAGENS AQUI.",
            description=(
                "Este canal e usado para identificar contas automatizadas de spam. "
                "Qualquer mensagem enviada aqui sera removida e a conta sera expulsa automaticamente."
            ),
            color=0xD97706,
        )
        embed.add_field(
            name="Protecao anti-spam ativa",
            value="Nao envie mensagens, links ou arquivos neste canal.",
            inline=False,
        )
        embed.set_footer(text="FriendHost // Honeypot ativo")
        return embed

    async def _ensure_honeypot_notice(self) -> None:
        channel_id = self.settings.honeypot_channel_id
        if not channel_id:
            return

        channel = self.get_channel(channel_id)
        if channel is None:
            channel = await self.fetch_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            raise RuntimeError("HONEYPOT_CHANNEL_ID nao aponta para um canal de texto.")

        async for message in channel.history(limit=25):
            if self.user is None or message.author.id != self.user.id or not message.embeds:
                continue
            if message.embeds[0].footer.text == "FriendHost // Honeypot ativo":
                return

        await channel.send(embed=self._honeypot_embed(), allowed_mentions=discord.AllowedMentions.none())

    async def _handle_honeypot_message(self, message: discord.Message) -> None:
        member = message.author
        if not isinstance(member, discord.Member):
            return
        if member.guild_permissions.administrator or member.id == message.guild.owner_id:
            return

        try:
            await message.delete()
        except discord.Forbidden:
            print("Sem permissao para remover mensagem do honeypot.")
        except discord.HTTPException as exc:
            print(f"Nao foi possivel remover mensagem do honeypot: {exc}")

        try:
            await member.kick(reason="FriendHost honeypot: mensagem enviada em canal anti-spam")
        except discord.Forbidden:
            print(f"Sem permissao para expulsar {member} pelo honeypot. Verifique a hierarquia de cargos.")
            return
        except discord.HTTPException as exc:
            print(f"Falha ao expulsar {member} pelo honeypot: {exc}")
            return

        self.honeypot_kicks += 1
        embed = discord.Embed(
            title="Honeypot acionado",
            description="Uma conta que enviou mensagem neste canal foi expulsa automaticamente.",
            color=0xEF4444,
        )
        embed.add_field(name="Remocoes nesta sessao", value=str(self.honeypot_kicks), inline=True)
        embed.set_footer(text="FriendHost // Protecao anti-spam")
        await message.channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    def _register_commands(self) -> None:
        @self.tree.command(name="central", description="Abre a central administrativa da FriendHost.")
        async def dashboard(interaction: discord.Interaction) -> None:
            if not await self._require_staff(interaction):
                return

            embed = discord.Embed(
                title="Central FriendHost",
                description="Atalhos administrativos. As respostas desta central so aparecem para voce.",
                color=0x6366F1,
            )
            embed.add_field(name="Financeiro", value="`/pedidos_recentes`\n`/faturas email:`\n`/gerar_fatura`", inline=True)
            embed.add_field(
                name="Catalogo e promocoes",
                value="`/catalogo`\n`/alterar_plano`\n`/cupons`\n`/criar_cupom`",
                inline=True,
            )
            embed.add_field(name="Operacao", value="`/assistente pergunta:`\n`/atualizar_bot`", inline=True)
            embed.add_field(
                name="Privacidade",
                value="Nunca envie tokens, senhas, dados de cartao ou informacoes pessoais para a IA.",
                inline=False,
            )
            embed.set_footer(text=f"{self.settings.bot_name} // Central administrativa")
            await interaction.response.send_message(embed=embed, ephemeral=True)

        @self.tree.command(name="catalogo", description="Lista os planos ativos e os valores atuais.")
        async def catalog(interaction: discord.Interaction) -> None:
            if not await self._require_staff(interaction):
                return
            await interaction.response.defer(thinking=True, ephemeral=True)
            try:
                plans = await asyncio.to_thread(self.store.list_catalog_plans)
            except Exception as exc:
                print(f"Erro ao buscar catalogo: {exc}")
                await interaction.followup.send("Catalogo indisponivel. Confirme a migracao do Supabase.", ephemeral=True)
                return

            if not plans:
                await interaction.followup.send("Nenhum plano encontrado. Execute `supabase/catalogo.sql`.", ephemeral=True)
                return

            lines = []
            for plan in plans:
                state = "ativo" if plan.get("ativo") else "pausado"
                label = f" | {plan['promocao_rotulo']}" if plan.get("promocao_rotulo") else ""
                lines.append(
                    f"`{plan['sku']}` {plan['ram_gb']}GB / {plan['cpu_percent']}% / {plan['storage_gb']}GB\n"
                    f"{money(plan['primeiro_mes'])} primeiro mes | {money(plan['valor_renovacao'])} renovacao | {state}{label}"
                )

            embed = discord.Embed(title="Catalogo FriendHost", color=0x6366F1)
            description = "\n\n".join(lines)
            embed.description = description[:4000]
            if len(description) > 4000:
                embed.set_footer(text="Resultado resumido. Use os SKUs acima em /alterar_plano.")
            else:
                embed.set_footer(text="Use /alterar_plano ou /plano_status para administrar.")
            await interaction.followup.send(embed=embed, ephemeral=True)

        @self.tree.command(name="alterar_plano", description="Altera os valores de venda de um SKU do catalogo.")
        @app_commands.describe(
            sku="Exemplo: ddr4-8 ou 5900xt-16",
            primeiro_mes="Valor cobrado no primeiro mes",
            renovacao="Valor mensal a partir da renovacao",
            rotulo_promocao="Opcional. Use - para remover o rotulo atual",
        )
        async def update_plan(
            interaction: discord.Interaction,
            sku: str,
            primeiro_mes: float,
            renovacao: float,
            rotulo_promocao: str | None = None,
        ) -> None:
            if not await self._require_staff(interaction):
                return
            await interaction.response.defer(thinking=True, ephemeral=True)
            try:
                normalized_sku = clean_sku(sku)
                if not 0 < primeiro_mes <= 5000 or not 0 < renovacao <= 5000:
                    raise ValueError("Os valores devem ficar entre R$ 0,01 e R$ 5.000,00.")
                label = None
                if rotulo_promocao is not None:
                    label = "" if rotulo_promocao.strip() == "-" else clean_text(rotulo_promocao, 80)
                updated = await asyncio.to_thread(
                    self.store.update_catalog_plan,
                    normalized_sku,
                    round(primeiro_mes, 2),
                    round(renovacao, 2),
                    label,
                )
            except ValueError as exc:
                await interaction.followup.send(str(exc), ephemeral=True)
                return
            except Exception as exc:
                print(f"Erro ao alterar plano: {exc}")
                await interaction.followup.send("Nao foi possivel alterar o plano no Supabase.", ephemeral=True)
                return

            if not updated:
                await interaction.followup.send("SKU nao encontrado.", ephemeral=True)
                return

            embed = discord.Embed(title="Plano atualizado", color=0x10B981)
            embed.add_field(name="SKU", value=f"`{updated['sku']}`", inline=True)
            embed.add_field(name="Primeiro mes", value=money(updated["primeiro_mes"]), inline=True)
            embed.add_field(name="Renovacao", value=money(updated["valor_renovacao"]), inline=True)
            if updated.get("promocao_rotulo"):
                embed.add_field(name="Rotulo", value=updated["promocao_rotulo"], inline=False)
            embed.set_footer(text="O site atualiza o catalogo automaticamente.")
            await interaction.followup.send(embed=embed, ephemeral=True)

        @self.tree.command(name="plano_status", description="Ativa ou pausa um SKU no catalogo publico.")
        @app_commands.describe(sku="SKU do plano", ativo="true mostra o plano; false o remove do catalogo")
        async def plan_status(interaction: discord.Interaction, sku: str, ativo: bool) -> None:
            if not await self._require_staff(interaction):
                return
            await interaction.response.defer(thinking=True, ephemeral=True)
            try:
                plan = await asyncio.to_thread(self.store.set_catalog_plan_active, clean_sku(sku), ativo)
            except ValueError as exc:
                await interaction.followup.send(str(exc), ephemeral=True)
                return
            except Exception as exc:
                print(f"Erro ao alterar status do plano: {exc}")
                await interaction.followup.send("Nao foi possivel alterar o status do plano.", ephemeral=True)
                return
            if not plan:
                await interaction.followup.send("SKU nao encontrado.", ephemeral=True)
                return
            await interaction.followup.send(
                f"`{plan['sku']}` esta agora **{'ativo' if plan['ativo'] else 'pausado'}**.",
                ephemeral=True,
            )

        @self.tree.command(name="cupons", description="Lista os cupons recentes e seu uso.")
        async def coupons(interaction: discord.Interaction) -> None:
            if not await self._require_staff(interaction):
                return
            await interaction.response.defer(thinking=True, ephemeral=True)
            try:
                items = await asyncio.to_thread(self.store.list_coupons)
            except Exception as exc:
                print(f"Erro ao buscar cupons: {exc}")
                await interaction.followup.send("Nao foi possivel consultar os cupons.", ephemeral=True)
                return
            if not items:
                await interaction.followup.send("Nenhum cupom cadastrado.", ephemeral=True)
                return

            lines = []
            for item in items:
                value = f"{item['valor']}%" if item["tipo"] == "percentual" else money(item["valor"])
                limit = item["limite_usos"] if item["limite_usos"] is not None else "sem limite"
                state = "ativo" if item["ativo"] else "pausado"
                scope = ", ".join(item.get("planos_sku") or []) or "todos os planos"
                lines.append(f"`{item['codigo']}` {value} | {item['usos']}/{limit} | {state} | {scope}")
            embed = discord.Embed(title="Cupons FriendHost", description="\n".join(lines)[:4000], color=0x8B5CF6)
            embed.set_footer(text="Use /criar_cupom ou /cupom_status para administrar.")
            await interaction.followup.send(embed=embed, ephemeral=True)

        @self.tree.command(name="criar_cupom", description="Cria um cupom valido para o checkout.")
        @app_commands.describe(
            codigo="Exemplo: MAQUINAS",
            tipo="Percentual ou desconto fixo em reais",
            valor="Percentual ou valor em reais",
            descricao="Motivo da promocao",
            limite_usos="Opcional. Deixe vazio para ilimitado",
            validade_horas="Opcional. Deixe vazio para nao expirar",
            plano_sku="Opcional. Restringe o cupom a um SKU, ex: ddr4-8",
        )
        @app_commands.choices(
            tipo=[
                app_commands.Choice(name="Percentual", value="percentual"),
                app_commands.Choice(name="Valor fixo", value="fixo"),
            ]
        )
        async def create_coupon(
            interaction: discord.Interaction,
            codigo: str,
            tipo: app_commands.Choice[str],
            valor: float,
            descricao: str,
            limite_usos: int | None = None,
            validade_horas: int | None = None,
            plano_sku: str | None = None,
        ) -> None:
            if not await self._require_staff(interaction):
                return
            await interaction.response.defer(thinking=True, ephemeral=True)
            try:
                code = clean_coupon_code(codigo)
                description = clean_text(descricao, 240)
                if not description:
                    raise ValueError("Informe uma descricao curta para o cupom.")
                if not 0 < valor <= (100 if tipo.value == "percentual" else 5000):
                    raise ValueError("Valor de desconto invalido.")
                if limite_usos is not None and not 0 < limite_usos <= 100_000:
                    raise ValueError("Limite de usos invalido.")
                if validade_horas is not None and not 0 < validade_horas <= 8_760:
                    raise ValueError("Validade deve ficar entre 1 hora e 365 dias.")
                expires_at = (
                    (datetime.now(timezone.utc) + timedelta(hours=validade_horas)).isoformat()
                    if validade_horas is not None
                    else None
                )
                plan_skus = [clean_sku(plano_sku)] if plano_sku else []
                coupon = await asyncio.to_thread(
                    self.store.create_coupon,
                    code,
                    description,
                    tipo.value,
                    round(valor, 2),
                    limite_usos,
                    expires_at,
                    plan_skus,
                )
            except ValueError as exc:
                await interaction.followup.send(str(exc), ephemeral=True)
                return
            except Exception as exc:
                print(f"Erro ao criar cupom: {exc}")
                await interaction.followup.send("Nao foi possivel criar o cupom. Verifique se o codigo ja existe.", ephemeral=True)
                return

            value = f"{coupon['valor']}%" if coupon["tipo"] == "percentual" else money(coupon["valor"])
            embed = discord.Embed(title="Cupom criado", color=0x10B981)
            embed.add_field(name="Codigo", value=f"`{coupon['codigo']}`", inline=True)
            embed.add_field(name="Desconto", value=value, inline=True)
            embed.add_field(name="Limite", value=str(coupon["limite_usos"] or "sem limite"), inline=True)
            scope = ", ".join(coupon.get("planos_sku") or []) or "Todos os planos"
            embed.add_field(name="Aplicacao", value=scope, inline=False)
            embed.add_field(name="Descricao", value=coupon["descricao"], inline=False)
            embed.set_footer(text="O cupom passa a ser validado no checkout do site.")
            await interaction.followup.send(embed=embed, ephemeral=True)

        @self.tree.command(name="cupom_status", description="Ativa ou pausa um cupom.")
        @app_commands.describe(codigo="Codigo do cupom", ativo="true ativa; false pausa")
        async def coupon_status(interaction: discord.Interaction, codigo: str, ativo: bool) -> None:
            if not await self._require_staff(interaction):
                return
            await interaction.response.defer(thinking=True, ephemeral=True)
            try:
                coupon = await asyncio.to_thread(self.store.set_coupon_active, clean_coupon_code(codigo), ativo)
            except ValueError as exc:
                await interaction.followup.send(str(exc), ephemeral=True)
                return
            except Exception as exc:
                print(f"Erro ao alterar status do cupom: {exc}")
                await interaction.followup.send("Nao foi possivel alterar o cupom.", ephemeral=True)
                return
            if not coupon:
                await interaction.followup.send("Cupom nao encontrado.", ephemeral=True)
                return
            await interaction.followup.send(
                f"Cupom `{coupon['codigo']}` esta agora **{'ativo' if coupon['ativo'] else 'pausado'}**.",
                ephemeral=True,
            )

        @self.tree.command(name="pedidos_recentes", description="Lista os pedidos mais recentes do site.")
        @app_commands.describe(quantidade="Numero de pedidos, de 1 a 20")
        async def recent_orders(interaction: discord.Interaction, quantidade: app_commands.Range[int, 1, 20] = 10) -> None:
            if not await self._require_staff(interaction):
                return
            await interaction.response.defer(thinking=True, ephemeral=True)

            try:
                invoices_data = await asyncio.to_thread(self.store.list_recent_invoices, quantidade)
            except Exception as exc:
                print(f"Erro ao buscar pedidos recentes: {exc}")
                await interaction.followup.send("Erro ao buscar pedidos no Supabase.", ephemeral=True)
                return

            if not invoices_data:
                await interaction.followup.send("Nenhum pedido encontrado.", ephemeral=True)
                return

            embed = discord.Embed(title="Pedidos recentes", color=0x6366F1)
            for invoice in invoices_data:
                embed.add_field(
                    name=f"{invoice.get('status') or 'Sem status'} | {invoice.get('id') or 'sem-id'}",
                    value=(
                        f"Cliente: `{invoice.get('cliente') or 'Nao informado'}`\n"
                        f"Plano: {invoice.get('plano') or 'Nao informado'}\n"
                        f"Valor: {money(invoice.get('valor'))} | Pedido: {format_datetime(invoice.get('created_at'))}"
                    ),
                    inline=False,
                )
            embed.set_footer(text=f"{self.settings.bot_name} // Financeiro FriendHost")
            await interaction.followup.send(embed=embed, ephemeral=True)

        @self.tree.command(name="faturas", description="Busca o historico de faturas de um cliente.")
        @app_commands.describe(email="E-mail do cliente cadastrado no site")
        async def invoices(interaction: discord.Interaction, email: str) -> None:
            if not await self._require_staff(interaction):
                return
            await interaction.response.defer(thinking=True, ephemeral=True)

            try:
                customer_email = clean_email(email)
                invoices_data = await asyncio.to_thread(self.store.list_invoices, customer_email)
            except ValueError as exc:
                await interaction.followup.send(str(exc), ephemeral=True)
                return
            except Exception as exc:
                print(f"Erro ao buscar faturas: {exc}")
                await interaction.followup.send("Erro ao buscar faturas no Supabase.", ephemeral=True)
                return

            if not invoices_data:
                await interaction.followup.send(f"Nenhuma fatura encontrada para {customer_email}.", ephemeral=True)
                return

            embed = discord.Embed(
                title=f"Faturas de {customer_email}",
                color=0x6366F1,
            )
            embed.set_footer(text=f"{self.settings.bot_name} // Financeiro FriendHost")

            for invoice in invoices_data[:20]:
                status = invoice.get("status") or "Sem status"
                invoice_id = invoice.get("id") or "sem-id"
                plan = invoice.get("plano") or "Plano nao informado"
                due_date = invoice.get("vencimento") or "Sem vencimento"
                embed.add_field(
                    name=f"{status} | {invoice_id} | {money(invoice.get('valor'))}",
                    value=f"Plano: {plan}\nVencimento: {due_date}",
                    inline=False,
                )

            if len(invoices_data) > 20:
                embed.add_field(
                    name="Resultado limitado",
                    value=f"Mostrando 20 de {len(invoices_data)} faturas.",
                    inline=False,
                )

            await interaction.followup.send(embed=embed, ephemeral=True)

        @self.tree.command(name="gerar_fatura", description="[ADMIN] Cria uma fatura manual no Supabase.")
        @app_commands.describe(
            email="E-mail do cliente",
            plano="Nome do plano",
            valor="Valor cobrado",
            status="Status inicial da fatura",
        )
        @app_commands.choices(
            status=[
                app_commands.Choice(name="Pendente", value="Pendente"),
                app_commands.Choice(name="Pago", value="Pago"),
            ]
        )
        async def create_invoice(
            interaction: discord.Interaction,
            email: str,
            plano: str,
            valor: float,
            status: app_commands.Choice[str],
        ) -> None:
            if not await self._require_staff(interaction):
                return
            await interaction.response.defer(thinking=True, ephemeral=True)

            try:
                customer_email = clean_email(email)
                plan_name = clean_text(plano, 120)
                if not plan_name:
                    raise ValueError("Informe o nome do plano.")
                if valor <= 0 or valor > 50000:
                    raise ValueError("Valor invalido.")

                invoice = InvoiceInput(
                    invoice_id=make_invoice_id(),
                    customer_email=customer_email,
                    plan=plan_name,
                    amount=round(float(valor), 2),
                    due_date=(date.today() + timedelta(days=30)).strftime("%d/%m/%Y"),
                    status=status.value,
                )
                await asyncio.to_thread(self.store.create_invoice, invoice)
            except ValueError as exc:
                await interaction.followup.send(str(exc), ephemeral=True)
                return
            except Exception as exc:
                print(f"Erro ao gerar fatura: {exc}")
                await interaction.followup.send("Erro ao salvar a fatura no Supabase.", ephemeral=True)
                return

            embed = discord.Embed(
                title="Fatura manual criada",
                color=0x10B981 if invoice.status == "Pago" else 0xF59E0B,
            )
            embed.add_field(name="ID", value=f"`{invoice.invoice_id}`", inline=True)
            embed.add_field(name="Cliente", value=f"`{invoice.customer_email}`", inline=True)
            embed.add_field(name="Plano", value=invoice.plan, inline=False)
            embed.add_field(name="Valor", value=money(invoice.amount), inline=True)
            embed.add_field(name="Status", value=invoice.status, inline=True)
            embed.add_field(name="Vencimento", value=invoice.due_date, inline=True)
            embed.set_footer(text=f"{self.settings.bot_name} // Financeiro FriendHost")

            await interaction.followup.send(embed=embed, ephemeral=True)

        @self.tree.command(name="assistente", description="[EQUIPE] Consulta a IA interna da FriendHost.")
        @app_commands.describe(pergunta="Pergunta operacional, sem dados pessoais ou financeiros")
        async def assistant(interaction: discord.Interaction, pergunta: str) -> None:
            if not await self._require_staff(interaction):
                return
            if self.ai is None:
                await interaction.response.send_message(
                    "A IA esta desativada. Configure `GEMINI_API_KEY` no servidor do bot.",
                    ephemeral=True,
                )
                return

            question = clean_text(pergunta, 1500)
            if not question:
                await interaction.response.send_message("Escreva uma pergunta para a IA.", ephemeral=True)
                return

            now = time.monotonic()
            last_request = self.ai_last_request.get(interaction.user.id, 0)
            if now - last_request < 8:
                await interaction.response.send_message("Aguarde alguns segundos antes de consultar a IA novamente.", ephemeral=True)
                return
            self.ai_last_request[interaction.user.id] = now
            await interaction.response.defer(thinking=True, ephemeral=True)

            try:
                async with self.ai_semaphore:
                    answer = await asyncio.to_thread(self.ai.answer, question)
            except Exception as exc:
                print(f"Erro na IA: {exc}")
                await interaction.followup.send("A IA nao respondeu agora. Tente novamente em alguns instantes.", ephemeral=True)
                return

            embed = discord.Embed(title="Assistente FriendHost", description=answer, color=0x8B5CF6)
            embed.set_footer(text="IA sem acesso ao Supabase, Discord, pagamentos ou dados de clientes")
            await interaction.followup.send(embed=embed, ephemeral=True)

        @self.tree.command(name="atualizar_bot", description="[ADMIN] Atualiza o bot pelo GitHub e reinicia.")
        async def update_bot(interaction: discord.Interaction) -> None:
            if not await self._require_staff(interaction):
                return
            await interaction.response.defer(thinking=True, ephemeral=True)

            if not self.settings.auto_update_enabled:
                await interaction.followup.send("Auto-update esta desativado no `.env`.", ephemeral=True)
                return

            try:
                result = await asyncio.to_thread(
                    update_from_git,
                    self.settings.auto_update_remote,
                    self.settings.auto_update_branch,
                )
            except UpdateError as exc:
                await interaction.followup.send(f"Atualizacao nao aplicada:\n```text\n{str(exc)[:1800]}\n```", ephemeral=True)
                return
            except Exception as exc:
                print(f"Erro inesperado ao atualizar o bot: {exc}")
                await interaction.followup.send("Erro inesperado ao atualizar o bot. Veja o console do servidor.", ephemeral=True)
                return

            embed = discord.Embed(
                title="Atualizacao do bot",
                description=result.summary,
                color=0x10B981 if result.changed else 0x6366F1,
            )
            embed.add_field(name="Branch", value=result.branch, inline=True)
            embed.add_field(name="Antes", value=f"`{result.before}`", inline=True)
            embed.add_field(name="Depois", value=f"`{result.after}`", inline=True)
            embed.set_footer(text=f"{self.settings.bot_name} // Auto-update")

            await interaction.followup.send(embed=embed, ephemeral=True)

            if result.changed:
                asyncio.create_task(self._restart_after_response())

    async def _support_loop(self) -> None:
        await self.wait_until_ready()

        while not self.is_closed():
            try:
                await self._sync_support_messages()
            except Exception as exc:
                print(f"Erro na sincronizacao do suporte: {exc}")

            await asyncio.sleep(self.settings.support_poll_seconds)

    async def _orders_loop(self) -> None:
        await self.wait_until_ready()

        while not self.is_closed():
            try:
                await self._sync_order_notifications()
            except Exception as exc:
                print(f"Erro na sincronizacao de pedidos: {exc}")

            await asyncio.sleep(self.settings.orders_poll_seconds)

    async def _sync_order_notifications(self) -> None:
        if not self.settings.orders_channel_id:
            return

        invoices = await asyncio.to_thread(
            self.store.list_recent_invoices,
            50,
            self.last_invoice_created_at,
        )
        for invoice in sorted(invoices, key=lambda item: str(item.get("created_at") or "")):
            invoice_id = str(invoice.get("id") or "")
            if invoice_id and invoice_id in self.processed_invoice_ids:
                continue

            await self._send_invoice_notification(invoice)
            if invoice_id:
                self.processed_invoice_ids.add(invoice_id)
            if invoice.get("created_at"):
                self.last_invoice_created_at = invoice["created_at"]

        if len(self.processed_invoice_ids) > 500:
            self.processed_invoice_ids = set(list(self.processed_invoice_ids)[-250:])

    async def _send_invoice_notification(self, invoice: dict[str, Any]) -> None:
        channel = self.get_channel(self.settings.orders_channel_id or 0)
        if channel is None:
            channel = await self.fetch_channel(self.settings.orders_channel_id or 0)
        if not isinstance(channel, discord.TextChannel):
            raise RuntimeError("ORDERS_CHANNEL_ID nao aponta para um canal de texto.")

        embed = discord.Embed(
            title="Novo pedido recebido",
            color=0x6366F1,
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Pedido", value=f"`{invoice.get('id') or 'sem-id'}`", inline=True)
        embed.add_field(name="Status", value=str(invoice.get("status") or "Pendente"), inline=True)
        embed.add_field(name="Valor", value=money(invoice.get("valor")), inline=True)
        embed.add_field(name="Cliente", value=f"`{invoice.get('cliente') or 'Nao informado'}`", inline=False)
        embed.add_field(name="Plano", value=str(invoice.get("plano") or "Nao informado"), inline=True)
        embed.add_field(name="Criado em", value=format_datetime(invoice.get("created_at")), inline=True)
        embed.set_footer(text=f"{self.settings.bot_name} // Canal restrito a equipe")
        await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    async def _sync_support_messages(self) -> None:
        messages = await asyncio.to_thread(
            self.store.customer_support_messages_after,
            self.last_support_created_at,
        )

        for msg in messages:
            msg_id = str(msg.get("id") or "")
            if msg_id and msg_id in self.processed_support_ids:
                continue

            try:
                await self._send_customer_message_to_discord(msg)
            except ValueError as exc:
                print(f"Mensagem de suporte ignorada por dados invalidos: {exc}")
            else:
                if msg_id:
                    self.processed_support_ids.add(msg_id)

            if msg.get("created_at"):
                self.last_support_created_at = msg["created_at"]

        if len(self.processed_support_ids) > 500:
            self.processed_support_ids = set(list(self.processed_support_ids)[-250:])

    async def _send_customer_message_to_discord(self, msg: dict[str, Any]) -> None:
        customer_email = clean_email(str(msg.get("email_cliente") or ""))
        content = str(msg.get("mensagem") or "").strip()
        if not content:
            return

        guild = self.get_guild(self.settings.guild_id)
        if guild is None:
            guild = await self.fetch_guild(self.settings.guild_id)

        channel_name = support_channel_name(customer_email)
        channel = discord.utils.get(guild.text_channels, name=channel_name)

        if channel is None:
            category = self.get_channel(self.settings.support_category_id)
            if category is None:
                category = await guild.fetch_channel(self.settings.support_category_id)
            if not isinstance(category, discord.CategoryChannel):
                raise RuntimeError("SUPPORT_CATEGORY_ID nao aponta para uma categoria do Discord.")

            overwrites: dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
            }
            if guild.me:
                overwrites[guild.me] = discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True,
                )
            for role_id in self.settings.staff_role_ids:
                role = guild.get_role(role_id)
                if role:
                    overwrites[role] = discord.PermissionOverwrite(
                        view_channel=True,
                        send_messages=True,
                        read_message_history=True,
                    )

            channel = await guild.create_text_channel(
                name=channel_name,
                category=category,
                topic=customer_email,
                overwrites=overwrites,
                reason="Novo atendimento FriendHost",
            )
            await channel.send(f"Novo ticket aberto. Cliente: `{customer_email}`")

        await channel.send(f"**Cliente:** {discord.utils.escape_mentions(content[:1900])}")

    async def _restart_after_response(self) -> None:
        await asyncio.sleep(3)
        restart_process()


def run() -> None:
    settings = Settings.load()
    store = SupabaseStore(settings.supabase_url, settings.supabase_service_key)
    bot = FriendHostBot(settings, store)
    bot.run(settings.discord_token)
