import base64
import json

import pytest

from searchable_storage_led.supabase_keys import (
    is_supabase_publishable_key,
    require_supabase_publishable_key,
)


def _legacy_key(role: str) -> str:
    def segment(payload: dict[str, object]) -> str:
        encoded = base64.urlsafe_b64encode(
            json.dumps(payload, separators=(",", ":")).encode("utf-8")
        )
        return encoded.rstrip(b"=").decode("ascii")

    return ".".join(
        (
            segment({"alg": "HS256", "typ": "JWT"}),
            segment({"iss": "supabase", "ref": "project", "role": role}),
            "test-signature",
        )
    )


@pytest.mark.parametrize(
    "key",
    [
        "sb_publishable_1234567890abcdefghij_12345678",
        _legacy_key("anon"),
    ],
)
def test_publishable_key_validator_accepts_supported_client_keys(key: str) -> None:
    assert is_supabase_publishable_key(key) is True
    assert require_supabase_publishable_key(key, setting="TEST_KEY") == key


@pytest.mark.parametrize(
    "key",
    [
        "",
        "publishable",
        "SYSTEM ACTION REQUIRED: unrelated notification",
        "sb_secret_1234567890abcdefghij_12345678",
        _legacy_key("service_role"),
        "sb_publishable_has whitespace",
    ],
)
def test_publishable_key_validator_rejects_wrong_or_unsafe_values(key: str) -> None:
    assert is_supabase_publishable_key(key) is False
    with pytest.raises(ValueError, match="TEST_KEY"):
        require_supabase_publishable_key(key, setting="TEST_KEY")


def test_publishable_key_error_does_not_echo_the_invalid_value() -> None:
    invalid = "private unexpected configuration text"

    with pytest.raises(ValueError) as exc:
        require_supabase_publishable_key(invalid, setting="SUPABASE_PUBLISHABLE_KEY")

    assert invalid not in str(exc.value)
