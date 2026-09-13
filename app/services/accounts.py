"""Parse Dota accounts and safely connect them to Telegram users."""

from dataclasses import dataclass
import re
from typing import Any
from urllib.parse import urlsplit

from app import db
from app.services.sync import ensure_player


class InvalidDotaAccountError(ValueError):
    """The input is neither a Friend ID nor a supported player URL."""


@dataclass(frozen=True)
class AccountLinkResult:
    player: dict[str, Any]
    rating: dict[str, Any] | None
    previous_account_id: int | None

    @property
    def unchanged(self) -> bool:
        return self.previous_account_id == self.player["account_id"]


async def link_dota_account(
    telegram_id: int, text: str, *, api_key: str | None = None,
) -> AccountLinkResult:
    """Prepare the destination before atomically replacing the Telegram link.

    Existing ratings and player history are preserved by ensure_player. Failed
    initialization leaves the old link intact and can be retried. Repeating the
    current account is read-only, including for legacy players without a rating.
    """
    account_id = parse_dota_account_id(text)
    if account_id is None:
        raise InvalidDotaAccountError("Invalid Friend ID or player URL")
    previous = db.get_telegram_player(telegram_id)
    previous_account_id = previous["account_id"] if previous else None
    if previous_account_id == account_id:
        return AccountLinkResult(previous, db.get_rating(account_id), previous_account_id)

    profile = await ensure_player(account_id, api_key=api_key)
    player = db.get_player(account_id)
    rating = db.get_rating(account_id)
    if player is None or (rating is None and db.get_final_standings() is None):
        raise ValueError("Dota player and rating must be initialized before linking")
    # One SQLite transaction updates the link; no old player data is deleted.
    db.link_telegram_user(telegram_id, account_id)
    return AccountLinkResult(
        {**player, "nickname": profile.get("personaname") or player["nickname"]},
        rating, previous_account_id,
    )


def parse_dota_account_id(text: str) -> int | None:
    text = text.strip()
    if len(text) > 2048:
        return None
    candidate = text
    if not re.fullmatch(r"[0-9]{1,10}", candidate):
        try:
            url = urlsplit(text)
            if url.scheme not in ("https", "http") or url.netloc.lower() not in (
                "opendota.com", "www.opendota.com", "dotabuff.com", "www.dotabuff.com",
            ):
                return None
            path = re.fullmatch(r"/players/([0-9]{1,10})/?", url.path)
            if path is None:
                return None
            candidate = path.group(1)
        except ValueError:
            return None
    account_id = int(candidate)
    return account_id if 0 < account_id < 2**32 else None
