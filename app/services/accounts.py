"""Parse a Friend ID or a public player URL without network requests."""

import re
from urllib.parse import urlsplit


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
