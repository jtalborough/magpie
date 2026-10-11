from __future__ import annotations


VOICE_SCOPE_MESSAGE = (
    "Voice is for finding and putting away items. "
    "Use the Storage screen to create or manage places, rules, and other changes."
)


def interaction_contract() -> dict[str, object]:
    """Describe which product surface owns each class of storage interaction."""
    return {
        "schema": "magpie.interaction_contract.v1",
        "voice": {
            "primary_actions": ["find", "put_away"],
            "put_away_destination_policy": "existing_places_only",
            "follow_up_actions": [
                "confirm",
                "cancel",
                "correct_pending",
                "undo",
                "clear_lights",
            ],
            "place_management": False,
            "place_configuration_handoff": "/storage?view=places",
            "out_of_scope_message": VOICE_SCOPE_MESSAGE,
        },
        "visual": {
            "place_management": True,
            "place_actions": [
                "create",
                "edit",
                "move",
                "archive",
                "restore",
                "label",
                "bind_hardware",
            ],
            "places_path": "/storage?view=places",
        },
    }
