from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


DEFAULT_SUPPORT_CATEGORY_ID = 1482820409077403791
DEFAULT_HONEYPOT_CHANNEL_ID = 1524624437809381386


def _get_required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Variavel de ambiente obrigatoria ausente: {name}")
    return value


def _get_int(name: str, default: int | None = None) -> int:
    value = os.getenv(name, "").strip()
    if not value:
        if default is None:
            raise RuntimeError(f"Variavel de ambiente obrigatoria ausente: {name}")
        return default

    try:
        return int(value)
    except ValueError as exc:
        raise RuntimeError(f"Variavel de ambiente invalida para inteiro: {name}") from exc


def _get_optional_int(name: str) -> int | None:
    value = os.getenv(name, "").strip()
    return _get_int(name) if value else None


def _get_int_list(name: str) -> tuple[int, ...]:
    value = os.getenv(name, "").strip()
    if not value:
        return ()

    try:
        return tuple(int(item.strip()) for item in value.split(",") if item.strip())
    except ValueError as exc:
        raise RuntimeError(f"Variavel de ambiente invalida para lista de IDs: {name}") from exc


def _get_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name, "").strip().lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "sim", "on"}


@dataclass(frozen=True)
class Settings:
    discord_token: str
    guild_id: int
    support_category_id: int
    supabase_url: str
    supabase_service_key: str
    bot_name: str = "FriendHost Bot"
    support_poll_seconds: int = 4
    support_sync_on_start: bool = False
    staff_role_ids: tuple[int, ...] = ()
    orders_channel_id: int | None = None
    orders_poll_seconds: int = 20
    orders_sync_on_start: bool = False
    honeypot_channel_id: int | None = DEFAULT_HONEYPOT_CHANNEL_ID
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    auto_update_enabled: bool = True
    auto_update_remote: str = "origin"
    auto_update_branch: str = ""

    @classmethod
    def load(cls) -> "Settings":
        load_dotenv()

        return cls(
            discord_token=_get_required("DISCORD_TOKEN"),
            guild_id=_get_int("GUILD_ID"),
            support_category_id=_get_int("SUPPORT_CATEGORY_ID", DEFAULT_SUPPORT_CATEGORY_ID),
            supabase_url=_get_required("SUPABASE_URL").rstrip("/"),
            supabase_service_key=_get_required("SUPABASE_SERVICE_KEY"),
            bot_name=os.getenv("BOT_NAME", "FriendHost Bot").strip() or "FriendHost Bot",
            support_poll_seconds=max(5, _get_int("SUPPORT_POLL_SECONDS", 8)),
            support_sync_on_start=_get_bool("SUPPORT_SYNC_ON_START", False),
            staff_role_ids=_get_int_list("STAFF_ROLE_IDS"),
            orders_channel_id=_get_optional_int("ORDERS_CHANNEL_ID"),
            orders_poll_seconds=max(10, _get_int("ORDERS_POLL_SECONDS", 20)),
            orders_sync_on_start=_get_bool("ORDERS_SYNC_ON_START", False),
            honeypot_channel_id=_get_optional_int("HONEYPOT_CHANNEL_ID")
            if os.getenv("HONEYPOT_CHANNEL_ID", "").strip()
            else DEFAULT_HONEYPOT_CHANNEL_ID,
            gemini_api_key=os.getenv("GEMINI_API_KEY", "").strip(),
            gemini_model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip() or "gemini-2.5-flash",
            auto_update_enabled=_get_bool("AUTO_UPDATE_ENABLED", True),
            auto_update_remote=os.getenv("AUTO_UPDATE_REMOTE", "origin").strip() or "origin",
            auto_update_branch=os.getenv("AUTO_UPDATE_BRANCH", "").strip(),
        )
