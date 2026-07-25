from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from functools import wraps
from threading import RLock
from typing import Any

from supabase import Client, create_client


def first_row(data: Any) -> dict[str, Any] | None:
    if isinstance(data, list):
        return data[0] if data and isinstance(data[0], dict) else None
    return data if isinstance(data, dict) else None


def serialized_store_call(method: Any) -> Any:
    @wraps(method)
    def wrapped(self: "SupabaseStore", *args: Any, **kwargs: Any) -> Any:
        # supabase-py usa um cliente HTTP sincrono compartilhado. As tarefas
        # do Discord rodam em threads, portanto cada requisicao precisa usar a
        # conexao por vez para evitar corrupcao de streams HTTP/2.
        with self._request_lock:
            return method(self, *args, **kwargs)

    return wrapped


@dataclass(frozen=True)
class InvoiceInput:
    invoice_id: str
    customer_email: str
    plan: str
    amount: float
    due_date: str
    status: str


class SupabaseStore:
    def __init__(self, url: str, service_key: str) -> None:
        self.client: Client = create_client(url, service_key)
        self._request_lock = RLock()

    @serialized_store_call
    def list_invoices(self, customer_email: str) -> list[dict[str, Any]]:
        response = (
            self.client.table("faturas")
            .select("id,cliente,plano,valor,status,vencimento,ciclo,created_at,contato_whatsapp")
            .eq("cliente", customer_email)
            .order("created_at", desc=True)
            .execute()
        )
        return response.data or []

    @serialized_store_call
    def latest_invoice(self) -> dict[str, Any] | None:
        invoices = self.list_recent_invoices(limit=1)
        return invoices[0] if invoices else None

    @serialized_store_call
    def list_recent_invoices(self, limit: int = 10, created_after: str | None = None) -> list[dict[str, Any]]:
        query = (
            self.client.table("faturas")
            .select("id,cliente,plano,valor,status,vencimento,ciclo,created_at")
            .order("created_at", desc=created_after is None)
            .limit(min(max(limit, 1), 50))
        )
        if created_after:
            query = query.gt("created_at", created_after)
        response = query.execute()
        return response.data or []

    @serialized_store_call
    def create_invoice(self, invoice: InvoiceInput) -> None:
        payload = {
            "id": invoice.invoice_id,
            "cliente": invoice.customer_email,
            "plano": invoice.plan,
            "valor": invoice.amount,
            "vencimento": invoice.due_date,
            "status": invoice.status,
            "ciclo": "Personalizado",
        }
        self.client.table("faturas").insert(payload).execute()

    @serialized_store_call
    def list_catalog_plans(self) -> list[dict[str, Any]]:
        response = (
            self.client.table("catalogo_planos")
            .select(
                "sku,maquina_id,maquina_nome,ram_gb,cpu_percent,storage_gb,primeiro_mes,"
                "valor_renovacao,ativo,promocao_rotulo,atualizado_em"
            )
            .order("maquina_id")
            .order("ram_gb")
            .execute()
        )
        return response.data or []

    @serialized_store_call
    def update_catalog_plan(
        self,
        sku: str,
        first_month: float,
        renewal: float,
        promotion_label: str | None,
    ) -> dict[str, Any] | None:
        payload: dict[str, Any] = {
            "primeiro_mes": first_month,
            "valor_renovacao": renewal,
            "atualizado_em": datetime.now(timezone.utc).isoformat(),
        }
        if promotion_label is not None:
            payload["promocao_rotulo"] = promotion_label
        response = (
            self.client.table("catalogo_planos")
            .update(payload)
            .eq("sku", sku)
            .select(
                "sku,maquina_nome,ram_gb,cpu_percent,storage_gb,primeiro_mes,"
                "valor_renovacao,ativo,promocao_rotulo,atualizado_em"
            )
            .execute()
        )
        return first_row(response.data)

    @serialized_store_call
    def set_catalog_plan_active(self, sku: str, active: bool) -> dict[str, Any] | None:
        response = (
            self.client.table("catalogo_planos")
            .update({"ativo": active, "atualizado_em": datetime.now(timezone.utc).isoformat()})
            .eq("sku", sku)
            .select("sku,maquina_nome,ram_gb,ativo")
            .execute()
        )
        return first_row(response.data)

    @serialized_store_call
    def list_coupons(self, limit: int = 20) -> list[dict[str, Any]]:
        response = (
            self.client.table("cupons")
            .select("codigo,descricao,tipo,valor,limite_usos,usos,inicia_em,expira_em,planos_sku,ativo")
            .order("criado_em", desc=True)
            .limit(min(max(limit, 1), 50))
            .execute()
        )
        return response.data or []

    @serialized_store_call
    def create_coupon(
        self,
        code: str,
        description: str,
        coupon_type: str,
        value: float,
        usage_limit: int | None,
        expires_at: str | None,
        plan_skus: list[str] | None = None,
    ) -> dict[str, Any]:
        response = (
            self.client.table("cupons")
            .insert(
                {
                    "codigo": code,
                    "descricao": description,
                    "tipo": coupon_type,
                    "valor": value,
                    "limite_usos": usage_limit,
                    "expira_em": expires_at,
                    "planos_sku": plan_skus or [],
                    "ativo": True,
                }
            )
            .select("codigo,descricao,tipo,valor,limite_usos,usos,expira_em,planos_sku,ativo")
            .execute()
        )
        coupon = first_row(response.data)
        if not coupon:
            raise RuntimeError("O Supabase nao retornou o cupom criado.")
        return coupon

    @serialized_store_call
    def get_coupon(self, code: str) -> dict[str, Any] | None:
        response = (
            self.client.table("cupons")
            .select("codigo,descricao,tipo,valor,limite_usos,usos,inicia_em,expira_em,planos_sku,ativo")
            .eq("codigo", code)
            .execute()
        )
        return first_row(response.data)

    @serialized_store_call
    def update_coupon(self, code: str, changes: dict[str, Any]) -> dict[str, Any] | None:
        payload = {**changes, "atualizado_em": datetime.now(timezone.utc).isoformat()}
        response = (
            self.client.table("cupons")
            .update(payload)
            .eq("codigo", code)
            .select("codigo,descricao,tipo,valor,limite_usos,usos,inicia_em,expira_em,planos_sku,ativo")
            .execute()
        )
        return first_row(response.data)

    @serialized_store_call
    def set_coupon_active(self, code: str, active: bool) -> dict[str, Any] | None:
        response = (
            self.client.table("cupons")
            .update({"ativo": active, "atualizado_em": datetime.now(timezone.utc).isoformat()})
            .eq("codigo", code)
            .select("codigo,ativo,usos,limite_usos")
            .execute()
        )
        return first_row(response.data)

    @serialized_store_call
    def latest_customer_support_message(self) -> dict[str, Any] | None:
        response = (
            self.client.table("mensagens_suporte")
            .select("id,email_cliente,mensagem,is_user,created_at")
            .eq("is_user", True)
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        data = response.data or []
        return data[0] if data else None

    @serialized_store_call
    def customer_support_messages_after(self, created_at: str | None) -> list[dict[str, Any]]:
        query = (
            self.client.table("mensagens_suporte")
            .select("id,email_cliente,mensagem,is_user,created_at")
            .eq("is_user", True)
            .order("created_at", desc=False)
            .limit(50)
        )

        if created_at:
            query = query.gt("created_at", created_at)

        response = query.execute()
        return response.data or []

    @serialized_store_call
    def create_support_reply(self, customer_email: str, message: str) -> None:
        self.client.table("mensagens_suporte").insert(
            {
                "email_cliente": customer_email,
                "mensagem": message,
                "is_user": False,
            }
        ).execute()
