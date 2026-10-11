from __future__ import annotations

import re


class MachineAuthError(ValueError):
    pass


def cloudflare_access_headers(
    client_id: str = "",
    client_secret: str = "",
) -> dict[str, str]:
    clean_id = client_id.strip()
    clean_secret = client_secret.strip()
    if bool(clean_id) != bool(clean_secret):
        raise MachineAuthError(
            "both MAGPIE_CF_ACCESS_CLIENT_ID and "
            "MAGPIE_CF_ACCESS_CLIENT_SECRET are required"
        )
    if not clean_id:
        return {}
    for value in (clean_id, clean_secret):
        if not re.fullmatch(r"[A-Za-z0-9._~+/=-]{8,512}", value):
            raise MachineAuthError("Cloudflare Access credentials contain invalid characters")
    return {
        "CF-Access-Client-Id": clean_id,
        "CF-Access-Client-Secret": clean_secret,
    }
