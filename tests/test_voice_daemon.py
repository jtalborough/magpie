import array
import io
import json
import sys
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest

import searchable_storage_led.voice_daemon as voice_daemon
from searchable_storage_led.interaction_contract import interaction_contract
from searchable_storage_led.voice_daemon import (
    DEFAULT_AUDIO_THRESHOLD,
    DEFAULT_VAD_FRAME_THRESHOLD,
    DEFAULT_STT_FALLBACK_MODEL,
    DEFAULT_STT_MODEL,
    ask_magpie,
    attempt_from_diagnosis,
    build_parser,
    clip_activity_rms,
    clip_rms,
    command_needs_continuation,
    diagnose_voice_command,
    handle_clip,
    is_prompt_echo,
    is_plausible_voice_follow_up,
    is_voice_revision,
    merge_voice_continuation,
    normalize_spoken_hardware_terms,
    response_to_speech,
    retry_transcript_for_wake_word,
    route_voice_command,
    strip_wake_word,
    transcribe_with_fallback,
    transcript_command,
    VoiceConfig,
    VoiceLoopState,
)


def test_strip_wake_word_returns_command() -> None:
    assert strip_wake_word("Magpie, find solder wick") == "find solder wick"


def test_voice_daemon_uses_the_discoverable_interaction_contract() -> None:
    contract = interaction_contract()

    assert contract["voice"]["primary_actions"] == ["find", "put_away"]
    assert contract["voice"]["put_away_destination_policy"] == "existing_places_only"
    assert contract["voice"]["place_management"] is False
    assert contract["voice"]["place_configuration_handoff"] == "/storage?view=places"
    assert contract["visual"]["place_management"] is True
    assert (
        voice_daemon.VOICE_SCOPE_MESSAGE
        == contract["voice"]["out_of_scope_message"]
    )


def test_voice_runtime_stats_names_assistant_requests_without_breaking_compatibility() -> None:
    stats = voice_daemon.VoiceRuntimeStats(agent_requests=3).snapshot()

    assert stats["assistant_requests"] == 3
    assert stats["agent_requests"] == 3


def test_strip_wake_word_keeps_command_after_last_wake_word() -> None:
    assert (
        strip_wake_word("M3 screws. Magpie, show me M3 screws")
        == "show me M3 screws"
    )


def test_strip_wake_word_ignores_other_speech() -> None:
    assert strip_wake_word("find solder wick") is None


def test_wake_only_transcript_arms_next_clip() -> None:
    parsed = transcript_command("Magpie.")

    assert parsed.command is None
    assert parsed.wake_heard is True
    assert parsed.awaiting_command is True


def test_wake_alias_handles_bad_magpie_transcripts() -> None:
    parsed = transcript_command("Nangpai, fine stuff.")

    assert parsed.command == "fine stuff"
    assert parsed.wake_heard is True
    assert parsed.awaiting_command is False

    parsed = transcript_command("MagPi, find M4 T-nuts.")

    assert parsed.command == "find M4 T-nuts"
    assert parsed.wake_heard is True
    assert parsed.awaiting_command is False

def test_prompt_echo_is_not_treated_as_wake_command() -> None:
    transcript = (
        "Storage cabinet voice commands. Wake word: Magpie. "
        "Common phrases: Magpie find M4 T-nuts, find M3 screws. "
        "Hardware terms: M3, M4, T-nuts."
    )

    parsed = transcript_command(transcript)

    assert is_prompt_echo(transcript) is True
    assert parsed.command is None
    assert parsed.wake_heard is False
    assert parsed.awaiting_command is False


def test_default_stt_prompt_does_not_seed_the_wake_word() -> None:
    assert "magpie" not in voice_daemon.DEFAULT_STT_PROMPT.lower()
    assert "do not add" in voice_daemon.DEFAULT_STT_PROMPT.lower()


def test_wake_aliases_exclude_common_or_unrelated_words() -> None:
    assert voice_daemon.transcript_command("Meg, put away screws").wake_heard is False
    assert voice_daemon.transcript_command("Bag by the cabinet").wake_heard is False


def test_explicit_storage_command_recovers_dropped_wake_word() -> None:
    parsed = voice_daemon.transcript_command("Put away M4 button head screw.")

    assert parsed.command == "Put away M4 button head screw"
    assert parsed.wake_heard is True


@pytest.mark.parametrize(
    "transcript, expected",
    [
        ("Clear lights.", "Clear lights"),
        ("Turn all lights off.", "Turn all lights off"),
        ("Light B47.", "Light B47"),
    ],
)
def test_explicit_storage_controls_recover_dropped_wake_word(
    transcript: str,
    expected: str,
) -> None:
    parsed = voice_daemon.transcript_command(transcript)

    assert parsed.command == expected
    assert parsed.wake_heard is True
    assert parsed.awaiting_command is False


def test_place_management_question_does_not_trigger_without_wake_word() -> None:
    parsed = voice_daemon.transcript_command("What storage spaces do I have?")

    assert parsed.command is None
    assert parsed.wake_heard is False
    assert parsed.awaiting_command is False


def test_explicit_storage_command_recovers_mangled_wake_prefix() -> None:
    parsed = voice_daemon.transcript_command(
        "bag five. Put away M4 button head screw."
    )

    assert parsed.command == "Put away M4 button head screw"
    assert parsed.wake_heard is True


def test_recovery_does_not_route_general_conversation() -> None:
    parsed = voice_daemon.transcript_command(
        "I was talking about where the button head screws should go."
    )

    assert parsed.command is None
    assert parsed.wake_heard is False


@pytest.mark.parametrize(
    "transcript",
    [
        "Organize the electrical cabinet.",
        "Sort the fasteners.",
        "Consolidate the USB cables.",
        "Move all Allen keys and wrenches to a big bin.",
        "Relocate the printer cabinet to the office.",
    ],
)
def test_place_management_does_not_recover_a_dropped_wake_word(
    transcript: str,
) -> None:
    parsed = voice_daemon.transcript_command(transcript)

    assert parsed.command is None
    assert parsed.wake_heard is False
    assert parsed.awaiting_command is False


def test_demonstrative_putaway_still_recovers_a_dropped_wake_word() -> None:
    parsed = voice_daemon.transcript_command(
        "Move these M3 screws into their own small bin."
    )

    assert parsed.command == "Move these M3 screws into their own small bin"
    assert parsed.wake_heard is True


def test_voice_follow_up_requires_storage_intent() -> None:
    assert is_plausible_voice_follow_up("No, use a bigger bin") is True
    assert is_plausible_voice_follow_up("Put away USB dongles") is True
    assert is_plausible_voice_follow_up("the larger one") is True
    assert is_plausible_voice_follow_up("yes") is True
    assert is_plausible_voice_follow_up("no") is True
    assert is_plausible_voice_follow_up("clear the lights") is True
    assert is_plausible_voice_follow_up("Yeah I'm here") is False
    assert is_plausible_voice_follow_up("Didn't you borrow that box?") is False
    assert is_plausible_voice_follow_up("Organize the electrical cabinet") is False
    assert is_plausible_voice_follow_up("Sort the fasteners") is False


@pytest.mark.parametrize(
    "transcript",
    [
        "go ahead",
        "that's right",
        "okay",
        "not that bin",
        "pick another drawer",
        "that bin is full",
    ],
)
def test_voice_follow_up_accepts_natural_pending_plan_responses(
    transcript: str,
) -> None:
    assert is_plausible_voice_follow_up(transcript) is True


def test_inventory_vocabulary_prompt_echo_is_not_routed() -> None:
    transcript = "Magpie, put these away: " + ", ".join(
        f"inventory alias {index}" for index in range(30)
    )

    parsed = transcript_command(transcript)

    assert is_prompt_echo(transcript) is True
    assert parsed.command is None
    assert parsed.wake_heard is False


def test_awaiting_command_treats_next_transcript_as_command() -> None:
    parsed = transcript_command("find m four tea nuts", awaiting_command=True)

    assert parsed.command == "find m four tea nuts"
    assert parsed.awaiting_command is False


def test_punctuation_only_transcript_is_not_a_command() -> None:
    parsed = transcript_command(".", awaiting_command=True)

    assert parsed.command is None
    assert parsed.awaiting_command is False


def test_route_voice_find_strips_intent() -> None:
    assert route_voice_command("find solder wick") == ("/assistant/chat", "find solder wick")


def test_route_voice_find_accepts_stt_variants() -> None:
    assert route_voice_command("finds screws") == ("/assistant/chat", "find screws")
    assert route_voice_command("fine stuff") == ("/assistant/chat", "find stuff")
    assert route_voice_command(
        "grab the M3 screws, M4 T-nuts, T-slot nuts, solder wick, Allen keys, "
        "lock washers, and heat shrink"
    ) == (
        "/assistant/chat",
        "find the M3 screws, M4 T-nuts, T-slots nuts, solder wick, Allen keys, "
        "lock washers, and heat shrink",
    )


def test_route_voice_demonstrative_move_is_treated_as_putaway() -> None:
    assert route_voice_command("move these M3 screws into their own small bin") == (
        "/assistant/chat",
        "put away M3 screws into their own small bin",
    )


@pytest.mark.parametrize(
    "command",
    [
        "create a place called attic",
        "rename the printer cabinet",
        "archive garage overflow",
        "learn jewelry always goes in the main cabinet",
        "what rules do you know about jewelry",
        "I threw away the dead batteries",
        "move all Allen keys and wrenches to a big bin",
        "organize the electrical cabinet",
    ],
)
def test_route_voice_non_core_commands_stay_local(command: str) -> None:
    assert route_voice_command(command) == (voice_daemon.VOICE_SCOPE_ROUTE, command)


def test_route_voice_confirm_and_cancel_pending_plans() -> None:
    assert route_voice_command("confirm") == ("/assistant/voice/confirm", "confirm")
    assert route_voice_command("yes") == ("/assistant/voice/confirm", "confirm")
    assert route_voice_command("do it") == ("/assistant/voice/confirm", "confirm")
    assert route_voice_command("go ahead") == ("/assistant/voice/confirm", "confirm")
    assert route_voice_command("that's right") == ("/assistant/voice/confirm", "confirm")
    assert route_voice_command("okay") == ("/assistant/voice/confirm", "confirm")
    assert route_voice_command("cancel") == ("/assistant/voice/cancel", "cancel")
    assert route_voice_command("cancel that") == ("/assistant/voice/cancel", "cancel")
    assert route_voice_command("no") == ("/assistant/voice/cancel", "cancel")
    assert route_voice_command("never mind") == ("/assistant/voice/cancel", "cancel")
    assert route_voice_command("don't do it") == ("/assistant/voice/cancel", "cancel")
    assert route_voice_command("undo that") == ("/assistant/undo", "undo")
    assert route_voice_command("go back") == ("/assistant/undo", "undo")


def test_voice_revision_accepts_only_pending_plan_corrections() -> None:
    assert is_voice_revision("use the larger bin") is True
    assert is_voice_revision("no, put them in B002") is True
    assert is_voice_revision("actually use a medium drawer") is True
    assert is_voice_revision("not that bin") is True
    assert is_voice_revision("pick another drawer") is True
    assert is_voice_revision("that bin is occupied") is True
    assert is_voice_revision("wrong item") is True
    assert is_voice_revision("yes") is False
    assert is_voice_revision("no") is False
    assert is_voice_revision("we should buy a larger shelf") is False


def test_route_voice_putaway_strips_intent() -> None:
    assert route_voice_command("put away M4 T-nuts") == ("/assistant/chat", "put away M4 T-nuts")


@pytest.mark.parametrize(
    "command",
    [
        "create a place called attic",
        "move all Allen keys and wrenches to a big bin",
    ],
)
def test_out_of_scope_voice_command_records_visual_handoff_without_model_routing(
    monkeypatch, command: str
) -> None:
    config = SimpleNamespace(
        api_url="http://127.0.0.1:8765",
        edge_token="",
        edge_device_id="",
        stt_model=DEFAULT_STT_MODEL,
        runtime_stats=voice_daemon.VoiceRuntimeStats(),
    )
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return b'{"ok":true,"action":"voice_scope_ui_required","interaction_id":42}'

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse()

    monkeypatch.setattr(voice_daemon.urllib.request, "urlopen", fake_urlopen)

    response = ask_magpie(command, config, client_request_id="voice-scope-test")

    assert response == {
        "ok": True,
        "action": "voice_scope_ui_required",
        "interaction_id": 42,
    }
    assert config.runtime_stats.agent_requests == 1
    assert captured["url"] == "http://127.0.0.1:8765/assistant/chat"
    assert captured["body"] == {
        "text": command,
        "source": "voice",
        "voice_transcript": "",
        "voice_command": command,
        "voice_query": command,
        "stt_model": DEFAULT_STT_MODEL,
        "client_request_id": "voice-scope-test",
    }


def test_out_of_scope_voice_command_keeps_local_handoff_when_backend_is_offline(
    monkeypatch,
) -> None:
    config = SimpleNamespace(
        api_url="http://127.0.0.1:8765",
        edge_token="",
        edge_device_id="",
        stt_model=DEFAULT_STT_MODEL,
        runtime_stats=voice_daemon.VoiceRuntimeStats(),
    )
    monkeypatch.setattr(
        voice_daemon.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            voice_daemon.urllib.error.URLError("offline")
        ),
    )

    response = ask_magpie("create a place called attic", config)

    assert response["action"] == "voice_scope_ui_required"
    assert response["message"] == voice_daemon.VOICE_SCOPE_MESSAGE
    assert response["storage_url"] == "/storage?view=places"
    assert response["handoff_recorded"] is False


def test_route_voice_repairs_mount_putaway_transcription() -> None:
    assert route_voice_command("Mount M3 screws into their own small bin") == (
        "/assistant/chat",
        "put away M3 screws into their own small bin",
    )


def test_short_putaway_fragment_waits_for_continuation() -> None:
    assert command_needs_continuation("Put away res") is True
    assert command_needs_continuation("put away this") is True
    assert command_needs_continuation("put away M4") is False
    assert command_needs_continuation("put away pliers") is False


def test_clipped_putaway_fragment_merges_next_transcript() -> None:
    assert merge_voice_continuation(
        "Put away res",
        "Razer travel holder into a small bin.",
    ) == "Put away Razer travel holder into a small bin"


def test_listener_dispatches_complete_command_across_clips(monkeypatch, tmp_path: Path) -> None:
    config = VoiceConfig(
        api_url="http://127.0.0.1:8765",
        input_device="plughw:2,0",
        output_device="plughw:0,0",
        wake_word="magpie",
        clip_seconds=4,
        idle_seconds=0.4,
        stt_model=DEFAULT_STT_MODEL,
        stt_fallback_model=DEFAULT_STT_FALLBACK_MODEL,
        tts_model="tts-1",
        tts_voice="alloy",
        openai_api_key="",
        audio_threshold=500,
        dry_run=True,
    )
    transcripts = iter(
        [
            ("Hey Magpie. Put away res.", DEFAULT_STT_MODEL),
            ("Razer travel holder into a small bin.", DEFAULT_STT_MODEL),
        ]
    )
    dispatched: list[dict[str, object]] = []
    monkeypatch.setattr(voice_daemon, "clip_rms", lambda _path: 1000.0)
    monkeypatch.setattr(voice_daemon, "clip_activity_rms", lambda _path: 2500.0)
    monkeypatch.setattr(voice_daemon, "transcribe_with_fallback", lambda _path, _config: next(transcripts))
    monkeypatch.setattr(
        voice_daemon,
        "dispatch_voice_diagnosis",
        lambda diagnosis, _config: dispatched.append(diagnosis),
    )
    state = VoiceLoopState()
    clip = tmp_path / "clip.wav"

    state.awaiting_command = handle_clip(clip, config, state=state)
    assert state.awaiting_command is True
    assert dispatched == []

    state.awaiting_command = handle_clip(clip, config, state=state)
    assert state.awaiting_command is False
    assert dispatched[0]["query"] == "put away Razer travel holder into a small bin"
    assert str(dispatched[0]["client_request_id"]).startswith("voice-")


def test_incomplete_putaway_is_not_dispatched_after_quiet_continuation(
    monkeypatch,
    tmp_path: Path,
) -> None:
    config = VoiceConfig(
        api_url="http://127.0.0.1:8765",
        input_device="plughw:2,0",
        output_device="",
        wake_word="magpie",
        clip_seconds=4,
        idle_seconds=0.4,
        stt_model=DEFAULT_STT_MODEL,
        stt_fallback_model=DEFAULT_STT_FALLBACK_MODEL,
        tts_model="tts-1",
        tts_voice="alloy",
        openai_api_key="",
        audio_threshold=500,
        dry_run=True,
    )
    activity_levels = iter([2500.0, 120.0])
    dispatched: list[dict[str, object]] = []
    logged: list[dict[str, object]] = []
    states: list[tuple[str, str]] = []
    spoken: list[str] = []
    monkeypatch.setattr(voice_daemon, "clip_rms", lambda _path: 1000.0)
    monkeypatch.setattr(
        voice_daemon,
        "clip_activity_rms",
        lambda _path: next(activity_levels),
    )
    monkeypatch.setattr(
        voice_daemon,
        "transcribe_with_fallback",
        lambda _path, _config: ("Put away a", DEFAULT_STT_MODEL),
    )
    monkeypatch.setattr(
        voice_daemon,
        "retry_transcript_for_wake_word",
        lambda _path, transcript, model, _config: (transcript, model),
    )
    monkeypatch.setattr(
        voice_daemon,
        "dispatch_voice_diagnosis",
        lambda diagnosis, _config: dispatched.append(diagnosis),
    )
    monkeypatch.setattr(
        voice_daemon,
        "post_voice_attempt",
        lambda _config, attempt: logged.append(attempt),
    )
    monkeypatch.setattr(
        voice_daemon,
        "post_voice_state",
        lambda _config, state, message, **_kwargs: states.append((state, message)),
    )
    monkeypatch.setattr(
        voice_daemon,
        "speak",
        lambda message, _config: spoken.append(message),
    )
    state = VoiceLoopState()
    clip = tmp_path / "clip.wav"

    assert handle_clip(clip, config, state=state, speech_detected=True) is True
    assert state.pending_diagnosis is not None
    assert handle_clip(clip, config, state=state, speech_detected=False) is False

    assert dispatched == []
    assert state.pending_diagnosis is None
    assert logged[-1]["error"] == "incomplete_command_timeout"
    assert states[-1] == (
        "error",
        "I only heard Put away a. Please say the full command again.",
    )
    assert spoken == ["I only heard Put away a. Please say the full command again."]


def test_listener_accepts_correction_during_follow_up_window(monkeypatch, tmp_path: Path) -> None:
    config = VoiceConfig(
        api_url="http://127.0.0.1:8765",
        input_device="plughw:2,0",
        output_device="plughw:0,0",
        wake_word="magpie",
        clip_seconds=4,
        idle_seconds=0.4,
        stt_model=DEFAULT_STT_MODEL,
        stt_fallback_model=DEFAULT_STT_FALLBACK_MODEL,
        tts_model="tts-1",
        tts_voice="alloy",
        openai_api_key="",
        audio_threshold=500,
        dry_run=True,
    )
    transcripts = iter(
        [
            ("Magpie. USB", DEFAULT_STT_MODEL),
            ("Magpie, those are flash drives. Put away USB dongles.", DEFAULT_STT_MODEL),
        ]
    )
    dispatched: list[dict[str, object]] = []
    monkeypatch.setattr(voice_daemon, "clip_rms", lambda _path: 1000.0)
    monkeypatch.setattr(voice_daemon, "clip_activity_rms", lambda _path: 2500.0)
    monkeypatch.setattr(voice_daemon, "transcribe_with_fallback", lambda _path, _config: next(transcripts))
    monkeypatch.setattr(
        voice_daemon,
        "dispatch_voice_diagnosis",
        lambda diagnosis, _config: (
            dispatched.append(diagnosis)
            or ({"decision": {"plan": [{"tool": "put_item"}]}} if len(dispatched) == 1 else {})
        ),
    )
    state = VoiceLoopState()
    clip = tmp_path / "clip.wav"

    state.awaiting_command = handle_clip(clip, config, state=state)
    assert dispatched[0]["query"] == "USB"
    assert state.follow_up_until > 0

    state.awaiting_command = handle_clip(clip, config, state=state)
    assert dispatched[1]["query"] == "those are flash drives. Put away USB dongles"


def test_listener_accepts_bare_yes_during_follow_up_window(monkeypatch, tmp_path: Path) -> None:
    config = VoiceConfig(
        api_url="http://127.0.0.1:8765",
        input_device="plughw:2,0",
        output_device="",
        wake_word="magpie",
        clip_seconds=4,
        idle_seconds=0.4,
        stt_model=DEFAULT_STT_MODEL,
        stt_fallback_model=DEFAULT_STT_FALLBACK_MODEL,
        tts_model="tts-1",
        tts_voice="alloy",
        openai_api_key="",
        audio_threshold=500,
        local_wake_enabled=True,
        dry_run=True,
    )
    dispatched: list[dict[str, object]] = []
    monkeypatch.setattr(voice_daemon, "clip_rms", lambda _path: 1000.0)
    monkeypatch.setattr(voice_daemon, "clip_activity_rms", lambda _path: 2500.0)
    monkeypatch.setattr(
        voice_daemon,
        "detect_local_wake",
        lambda _path, _config: voice_daemon.LocalWakeDetection(False),
    )
    monkeypatch.setattr(
        voice_daemon,
        "transcribe_local_vosk",
        lambda _path, _config: "Yes",
    )
    monkeypatch.setattr(
        voice_daemon,
        "transcribe_with_fallback",
        lambda *_args: pytest.fail("bare confirmation must not use cloud STT"),
    )
    monkeypatch.setattr(
        voice_daemon,
        "dispatch_voice_diagnosis",
        lambda diagnosis, _config: dispatched.append(diagnosis),
    )
    state = VoiceLoopState(follow_up_until=voice_daemon.time.monotonic() + 10)

    assert handle_clip(tmp_path / "yes.wav", config, state=state, speech_detected=True) is False
    assert dispatched[0]["route"] == "/assistant/voice/confirm"
    assert dispatched[0]["query"] == "confirm"
    assert dispatched[0]["wake_heard"] is True


def test_listener_routes_bare_correction_to_pending_plan_revision(
    monkeypatch,
    tmp_path: Path,
) -> None:
    config = VoiceConfig(
        api_url="http://127.0.0.1:8765",
        input_device="plughw:2,0",
        output_device="",
        wake_word="magpie",
        clip_seconds=4,
        idle_seconds=0.4,
        stt_model=DEFAULT_STT_MODEL,
        stt_fallback_model=DEFAULT_STT_FALLBACK_MODEL,
        tts_model="tts-1",
        tts_voice="alloy",
        openai_api_key="",
        audio_threshold=500,
        local_wake_enabled=True,
        dry_run=True,
    )
    dispatched: list[dict[str, object]] = []
    monkeypatch.setattr(voice_daemon, "clip_rms", lambda _path: 1000.0)
    monkeypatch.setattr(voice_daemon, "clip_activity_rms", lambda _path: 2500.0)
    monkeypatch.setattr(
        voice_daemon,
        "detect_local_wake",
        lambda _path, _config: voice_daemon.LocalWakeDetection(False),
    )
    monkeypatch.setattr(
        voice_daemon,
        "transcribe_local_vosk",
        lambda _path, _config: "use the larger bin",
    )
    monkeypatch.setattr(
        voice_daemon,
        "transcribe_with_fallback",
        lambda *_args: pytest.fail("pending correction must not use cloud STT"),
    )
    monkeypatch.setattr(
        voice_daemon,
        "dispatch_voice_diagnosis",
        lambda diagnosis, _config: dispatched.append(diagnosis) or {},
    )
    state = VoiceLoopState(follow_up_until=voice_daemon.time.monotonic() + 10)

    assert handle_clip(
        tmp_path / "correction.wav",
        config,
        state=state,
        speech_detected=True,
    ) is False
    assert dispatched[0]["route"] == "/assistant/revise"
    assert dispatched[0]["query"] == "use the larger bin"
    assert dispatched[0]["wake_heard"] is True


def test_listener_routes_confirm_after_local_wake_is_removed_from_audio(
    monkeypatch,
    tmp_path: Path,
) -> None:
    config = VoiceConfig(
        api_url="http://127.0.0.1:8765",
        input_device="plughw:2,0",
        output_device="",
        wake_word="magpie",
        clip_seconds=4,
        idle_seconds=0.4,
        stt_model=DEFAULT_STT_MODEL,
        stt_fallback_model=DEFAULT_STT_FALLBACK_MODEL,
        tts_model="tts-1",
        tts_voice="alloy",
        openai_api_key="",
        audio_threshold=500,
        local_wake_enabled=True,
        dry_run=True,
    )
    dispatched: list[dict[str, object]] = []
    monkeypatch.setattr(voice_daemon, "clip_rms", lambda _path: 1000.0)
    monkeypatch.setattr(voice_daemon, "clip_activity_rms", lambda _path: 2500.0)
    monkeypatch.setattr(
        voice_daemon,
        "detect_local_wake",
        lambda _path, _config: voice_daemon.LocalWakeDetection(True, 2, 30, 0.9),
    )
    monkeypatch.setattr(
        voice_daemon,
        "write_command_audio_after_wake",
        lambda _source, _target, _detection: 1.0,
    )
    monkeypatch.setattr(
        voice_daemon,
        "transcribe_with_fallback",
        lambda _path, _config: ("Confirm", DEFAULT_STT_MODEL),
    )
    monkeypatch.setattr(
        voice_daemon,
        "dispatch_voice_diagnosis",
        lambda diagnosis, _config: dispatched.append(diagnosis) or {},
    )

    assert handle_clip(tmp_path / "confirm.wav", config, speech_detected=True) is False
    assert dispatched[0]["command"] == "Confirm"
    assert dispatched[0]["route"] == "/assistant/voice/confirm"
    assert dispatched[0]["query"] == "confirm"


def test_only_pending_plan_response_arms_bare_voice_confirmation() -> None:
    config = VoiceConfig(
        api_url="http://127.0.0.1:8765",
        input_device="",
        output_device="",
        wake_word="magpie",
        clip_seconds=4,
        idle_seconds=0.4,
        stt_model=DEFAULT_STT_MODEL,
        stt_fallback_model=DEFAULT_STT_FALLBACK_MODEL,
        tts_model="tts-1",
        tts_voice="alloy",
        openai_api_key="",
        audio_threshold=500,
    )
    state = VoiceLoopState()

    voice_daemon.arm_voice_follow_up(state, config, {"action": "found"})
    assert state.follow_up_until == 0

    voice_daemon.arm_voice_follow_up(
        state,
        config,
        {"decision": {"plan": [{"tool": "put_item"}]}},
    )
    assert state.follow_up_until > voice_daemon.time.monotonic()


def test_listener_does_not_dispatch_unrelated_room_speech_as_follow_up(
    monkeypatch,
    tmp_path: Path,
) -> None:
    config = VoiceConfig(
        api_url="http://127.0.0.1:8765",
        input_device="plughw:2,0",
        output_device="plughw:0,0",
        wake_word="magpie",
        clip_seconds=4,
        idle_seconds=0.4,
        stt_model=DEFAULT_STT_MODEL,
        stt_fallback_model=DEFAULT_STT_FALLBACK_MODEL,
        tts_model="tts-1",
        tts_voice="alloy",
        openai_api_key="",
        audio_threshold=500,
        local_wake_enabled=True,
        dry_run=True,
    )
    dispatched: list[dict[str, object]] = []
    logged: list[dict[str, object]] = []
    monkeypatch.setattr(voice_daemon, "clip_rms", lambda _path: 1000.0)
    monkeypatch.setattr(voice_daemon, "clip_activity_rms", lambda _path: 2500.0)
    monkeypatch.setattr(
        voice_daemon,
        "detect_local_wake",
        lambda _path, _config: voice_daemon.LocalWakeDetection(False),
    )
    monkeypatch.setattr(
        voice_daemon,
        "transcribe_local_vosk",
        lambda _path, _config: "Yeah I'm here",
    )
    monkeypatch.setattr(
        voice_daemon,
        "transcribe_with_fallback",
        lambda *_args: pytest.fail("unrelated follow-up must not use cloud STT"),
    )
    monkeypatch.setattr(
        voice_daemon,
        "dispatch_voice_diagnosis",
        lambda diagnosis, _config: dispatched.append(diagnosis),
    )
    monkeypatch.setattr(
        voice_daemon,
        "post_voice_attempt",
        lambda _config, attempt: logged.append(attempt),
    )
    state = VoiceLoopState(follow_up_until=voice_daemon.time.monotonic() + 10)
    clip = tmp_path / "clip.wav"

    follow_up_deadline = state.follow_up_until
    handle_clip(clip, config, state=state)

    assert dispatched == []
    assert logged[-1]["error"] == "unrelated_local_confirmation"
    assert state.follow_up_until == follow_up_deadline


def test_listener_ignores_filler_while_waiting_for_command(monkeypatch, tmp_path: Path) -> None:
    config = VoiceConfig(
        api_url="http://127.0.0.1:8765",
        input_device="plughw:2,0",
        output_device="plughw:0,0",
        wake_word="magpie",
        clip_seconds=4,
        idle_seconds=0.4,
        stt_model=DEFAULT_STT_MODEL,
        stt_fallback_model=DEFAULT_STT_FALLBACK_MODEL,
        tts_model="tts-1",
        tts_voice="alloy",
        openai_api_key="",
        audio_threshold=500,
        dry_run=True,
    )
    dispatched: list[dict[str, object]] = []
    logged: list[dict[str, object]] = []
    monkeypatch.setattr(voice_daemon, "clip_rms", lambda _path: 1000.0)
    monkeypatch.setattr(voice_daemon, "clip_activity_rms", lambda _path: 2500.0)
    monkeypatch.setattr(
        voice_daemon,
        "transcribe_with_fallback",
        lambda _path, _config: ("Ah", DEFAULT_STT_MODEL),
    )
    monkeypatch.setattr(
        voice_daemon,
        "dispatch_voice_diagnosis",
        lambda diagnosis, _config: dispatched.append(diagnosis),
    )
    monkeypatch.setattr(
        voice_daemon,
        "post_voice_attempt",
        lambda _config, attempt: logged.append(attempt),
    )
    state = VoiceLoopState(
        awaiting_command=True,
        awaiting_command_until=voice_daemon.time.monotonic() + 10,
    )

    assert handle_clip(tmp_path / "clip.wav", config, state=state) is True
    assert dispatched == []
    assert logged[0]["error"] == "filler_transcript"


def test_listener_expires_stale_wake_command_window(monkeypatch, tmp_path: Path) -> None:
    config = VoiceConfig(
        api_url="http://127.0.0.1:8765",
        input_device="plughw:2,0",
        output_device="plughw:0,0",
        wake_word="magpie",
        clip_seconds=4,
        idle_seconds=0.4,
        stt_model=DEFAULT_STT_MODEL,
        stt_fallback_model=DEFAULT_STT_FALLBACK_MODEL,
        tts_model="tts-1",
        tts_voice="alloy",
        openai_api_key="",
        audio_threshold=500,
        dry_run=True,
    )
    dispatched: list[dict[str, object]] = []
    monkeypatch.setattr(voice_daemon, "clip_rms", lambda _path: 1000.0)
    monkeypatch.setattr(voice_daemon, "clip_activity_rms", lambda _path: 2500.0)
    monkeypatch.setattr(
        voice_daemon,
        "transcribe_with_fallback",
        lambda _path, _config: ("I was talking about the cabinet", DEFAULT_STT_MODEL),
    )
    monkeypatch.setattr(
        voice_daemon,
        "retry_transcript_for_wake_word",
        lambda _path, transcript, model, _config: (transcript, model),
    )
    monkeypatch.setattr(
        voice_daemon,
        "dispatch_voice_diagnosis",
        lambda diagnosis, _config: dispatched.append(diagnosis),
    )
    state = VoiceLoopState(awaiting_command=True, awaiting_command_until=1.0)
    monkeypatch.setattr(voice_daemon.time, "monotonic", lambda: 2.0)

    assert handle_clip(tmp_path / "clip.wav", config, state=state) is False
    assert state.awaiting_command is False
    assert dispatched == []


def test_normalize_spoken_metric_hardware_terms() -> None:
    assert normalize_spoken_hardware_terms("m four tea nuts") == "M4 T-nuts"
    assert normalize_spoken_hardware_terms("m 3 screws") == "M3 screws"
    assert normalize_spoken_hardware_terms("solder wake") == "solder wick"
    assert normalize_spoken_hardware_terms("M14") == "M4 T-nuts"
    assert normalize_spoken_hardware_terms("M4T") == "M4 T-nuts"


def test_route_voice_normalizes_find_query() -> None:
    assert route_voice_command("where are m four tee nuts") == ("/assistant/chat", "find M4 T-nuts")
    assert route_voice_command("find M14") == ("/assistant/chat", "find M4 T-nuts")
    assert route_voice_command("find M4T") == ("/assistant/chat", "find M4 T-nuts")


def test_diagnose_voice_command_reports_route_without_backend() -> None:
    config = VoiceConfig(
        api_url="http://127.0.0.1:8765",
        input_device="plughw:2,0",
        output_device="plughw:0,0",
        wake_word="magpie",
        clip_seconds=4,
        idle_seconds=0.4,
        stt_model="whisper-1",
        stt_fallback_model="whisper-1",
        tts_model="tts-1",
        tts_voice="alloy",
        openai_api_key="",
        audio_threshold=500,
        dry_run=True,
    )

    result = diagnose_voice_command("Magpie find m four tea nuts", config, rms=700)

    assert result["rms"] == 700
    assert result["command"] == "find m four tea nuts"
    assert result["route"] == "/assistant/chat"
    assert result["query"] == "find M4 T-nuts"


def test_diagnose_voice_command_handles_bad_wake_and_find_words() -> None:
    config = VoiceConfig(
        api_url="http://127.0.0.1:8765",
        input_device="plughw:2,0",
        output_device="plughw:0,0",
        wake_word="magpie",
        clip_seconds=4,
        idle_seconds=0.4,
        stt_model=DEFAULT_STT_MODEL,
        stt_fallback_model="whisper-1",
        tts_model="tts-1",
        tts_voice="alloy",
        openai_api_key="",
        audio_threshold=500,
        dry_run=True,
    )

    result = diagnose_voice_command("Nangpai, fine stuff.", config, rms=1572)

    assert result["wake_heard"] is True
    assert result["command"] == "fine stuff"
    assert result["route"] == "/assistant/chat"
    assert result["query"] == "find stuff"


def test_diagnose_voice_command_supports_second_clip_after_wake() -> None:
    config = VoiceConfig(
        api_url="http://127.0.0.1:8765",
        input_device="plughw:2,0",
        output_device="plughw:0,0",
        wake_word="magpie",
        clip_seconds=4,
        idle_seconds=0.4,
        stt_model="whisper-1",
        stt_fallback_model="whisper-1",
        tts_model="tts-1",
        tts_voice="alloy",
        openai_api_key="",
        audio_threshold=500,
        dry_run=True,
    )

    result = diagnose_voice_command(
        "find m four tea nuts",
        config,
        awaiting_command=True,
    )

    assert result["command"] == "find m four tea nuts"
    assert result["route"] == "/assistant/chat"
    assert result["query"] == "find M4 T-nuts"


def test_attempt_from_diagnosis_shapes_record() -> None:
    config = VoiceConfig(
        api_url="http://127.0.0.1:8765",
        input_device="plughw:2,0",
        output_device="plughw:0,0",
        wake_word="magpie",
        clip_seconds=4,
        idle_seconds=0.4,
        stt_model="whisper-1",
        stt_fallback_model="whisper-1",
        tts_model="tts-1",
        tts_voice="alloy",
        openai_api_key="",
        audio_threshold=500,
        dry_run=True,
    )
    diagnosis = diagnose_voice_command("Magpie find M14", config, rms=900)

    attempt = attempt_from_diagnosis(diagnosis, config)

    assert attempt["rms"] == 900
    assert attempt["transcript"] == "Magpie find M14"
    assert attempt["wake_heard"] is True
    assert attempt["command"] == "find M14"
    assert attempt["route"] == "/assistant/chat"
    assert attempt["query"] == "find M4 T-nuts"
    assert attempt["stt_model"] == "whisper-1"


def test_voice_request_id_replays_only_immediate_duplicate_chat() -> None:
    state = VoiceLoopState()
    first = {"route": "/assistant/chat", "query": "find M4 T-nuts"}
    duplicate = {"route": "/assistant/chat", "query": "  FIND   m4 t-nuts "}
    later = {"route": "/assistant/chat", "query": "find M4 T-nuts"}

    first_id = voice_daemon.assign_voice_client_request_id(first, state, now=100)
    duplicate_id = voice_daemon.assign_voice_client_request_id(
        duplicate, state, now=110
    )
    later_id = voice_daemon.assign_voice_client_request_id(later, state, now=116)

    assert first_id.startswith("voice-")
    assert duplicate_id == first_id
    assert later_id != first_id
    assert duplicate["client_request_id"] == first_id

    follow_up = {"route": "/assistant/revise", "query": "use a bigger bin"}
    assert voice_daemon.assign_voice_client_request_id(follow_up, state, now=117) == ""
    assert "client_request_id" not in follow_up


def test_ask_magpie_sends_voice_metadata_to_chat(monkeypatch) -> None:
    config = VoiceConfig(
        api_url="http://127.0.0.1:8765",
        input_device="plughw:2,0",
        output_device="plughw:0,0",
        wake_word="magpie",
        clip_seconds=4,
        idle_seconds=0.4,
        stt_model=DEFAULT_STT_MODEL,
        stt_fallback_model="whisper-1",
        tts_model="tts-1",
        tts_voice="alloy",
        openai_api_key="",
        audio_threshold=500,
        dry_run=True,
    )
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return b'{"ok": true}'

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(voice_daemon.urllib.request, "urlopen", fake_urlopen)

    response = ask_magpie(
        "find m four tea nuts",
        config,
        transcript="Magpie find m four tea nuts",
        route="/assistant/chat",
        query="find M4 T-nuts",
        client_request_id="voice-test-1",
    )

    assert response == {"ok": True}
    assert config.runtime_stats.agent_requests == 1
    assert captured["url"] == "http://127.0.0.1:8765/assistant/chat"
    assert captured["body"] == {
        "text": "find M4 T-nuts",
        "source": "voice",
        "voice_transcript": "Magpie find m four tea nuts",
        "voice_command": "find m four tea nuts",
        "voice_query": "find M4 T-nuts",
        "stt_model": DEFAULT_STT_MODEL,
        "client_request_id": "voice-test-1",
    }


def test_voice_state_reports_listener_device_and_runtime(monkeypatch) -> None:
    config = SimpleNamespace(
        api_url="https://magpie.example.com",
        dry_run=False,
        edge_device_id="main-cabinet",
        edge_token="device-token",
    )
    captured = {}
    monkeypatch.setattr(
        voice_daemon,
        "voice_capabilities",
        lambda config: {"local_stt_available": True, "local_tts_available": False},
    )

    class FakeResponse:
        def close(self):
            return None

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(voice_daemon.urllib.request, "urlopen", fake_urlopen)

    voice_daemon.post_voice_state(config, "waiting", "Waiting for Magpie")

    assert captured["url"] == "https://magpie.example.com/voice/state"
    assert captured["timeout"] == 2
    assert captured["body"]["device_id"] == "main-cabinet"
    assert captured["body"]["runtime_version"] == voice_daemon.VOICE_RUNTIME.label()
    assert captured["body"]["capabilities"] == {
        "local_stt_available": True,
        "local_tts_available": False,
    }


def test_voice_capabilities_report_local_and_provider_availability(
    monkeypatch,
    tmp_path: Path,
) -> None:
    model = tmp_path / "vosk-model"
    (model / "conf").mkdir(parents=True)
    (model / "conf/model.conf").write_text("model", encoding="utf-8")
    config = VoiceConfig(
        api_url="http://127.0.0.1:8765",
        input_device="",
        output_device="plughw:0,0",
        wake_word="magpie",
        clip_seconds=4,
        idle_seconds=0.4,
        stt_model=DEFAULT_STT_MODEL,
        stt_fallback_model=DEFAULT_STT_FALLBACK_MODEL,
        tts_model="tts-1",
        tts_voice="alloy",
        openai_api_key="capabilities-key",
        audio_threshold=500,
        vosk_model_path=str(model),
        stt_failure_state_path=str(tmp_path / "audio-failures.json"),
        local_stt_fallback_enabled=True,
        local_tts_fallback_enabled=True,
    )
    monkeypatch.setattr(
        voice_daemon.shutil,
        "which",
        lambda name: "/usr/bin/espeak-ng" if name == "espeak-ng" else None,
    )
    cooldown = voice_daemon._stt_failure_cooldown(config.stt_failure_state_path)
    cooldown.failed(
        voice_daemon._stt_endpoint_id(config),
        voice_daemon.LlmError(
            "quota exhausted",
            category="quota_exhausted",
            status_code=429,
        ),
    )

    capabilities = voice_daemon.voice_capabilities(config)

    assert capabilities["cloud_audio_available"] is False
    assert capabilities["cloud_audio_failure"] == "quota_exhausted"
    assert capabilities["cloud_audio_retry_seconds"] > 0
    assert capabilities["local_stt_available"] is True
    assert capabilities["local_tts_available"] is True
    assert capabilities["output_device"] == "plughw:0,0"
    assert capabilities["wake_gate"] == "cloud"
    assert capabilities["audio_before_wake_uploaded"] is True
    assert capabilities["no_wake_follow_up"] == "cloud"
    assert capabilities["speech_clips"] == 0
    assert capabilities["cloud_stt_calls"] == 0



def test_ask_magpie_surfaces_api_validation_detail(monkeypatch) -> None:
    config = VoiceConfig(
        api_url="http://127.0.0.1:8765",
        input_device="plughw:2,0",
        output_device="plughw:0,0",
        wake_word="magpie",
        clip_seconds=4,
        idle_seconds=0.4,
        stt_model=DEFAULT_STT_MODEL,
        stt_fallback_model=DEFAULT_STT_FALLBACK_MODEL,
        tts_model="tts-1",
        tts_voice="alloy",
        openai_api_key="",
        audio_threshold=500,
        dry_run=True,
    )

    def failed_urlopen(request, timeout):
        raise voice_daemon.urllib.error.HTTPError(
            request.full_url,
            400,
            "Bad Request",
            {},
            io.BytesIO(b'{"detail":"pending plan is no longer valid"}'),
        )

    monkeypatch.setattr(voice_daemon.urllib.request, "urlopen", failed_urlopen)

    with pytest.raises(RuntimeError, match="pending plan is no longer valid"):
        ask_magpie("yes", config, route="/assistant/voice/confirm", query="confirm")


def test_voice_api_uses_edge_credentials_for_central_agent(monkeypatch) -> None:
    config = VoiceConfig(
        api_url="https://magpie.example.com",
        input_device="plughw:2,0",
        output_device="plughw:0,0",
        wake_word="magpie",
        clip_seconds=4,
        idle_seconds=0.4,
        stt_model=DEFAULT_STT_MODEL,
        stt_fallback_model=DEFAULT_STT_FALLBACK_MODEL,
        tts_model="tts-1",
        tts_voice="alloy",
        openai_api_key="",
        audio_threshold=500,
        edge_device_id="main-cabinet",
        edge_token="device-secret",
        edge_workspace_id="workspace-1",
        cf_access_client_id="machine-id.access",
        cf_access_client_secret="machine-secret",
    )
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return b'{"ok":true}'

    def fake_urlopen(request, timeout):
        captured["headers"] = {
            key.lower(): value for key, value in request.header_items()
        }
        captured["url"] = request.full_url
        return FakeResponse()

    monkeypatch.setattr(voice_daemon.urllib.request, "urlopen", fake_urlopen)

    assert ask_magpie("find screws", config) == {"ok": True}
    assert captured["url"] == "https://magpie.example.com/assistant/chat"
    assert captured["headers"]["authorization"] == "Bearer device-secret"
    assert captured["headers"]["x-magpie-device-id"] == "main-cabinet"
    assert captured["headers"]["x-magpie-workspace"] == "workspace-1"
    assert captured["headers"]["cf-access-client-id"] == "machine-id.access"
    assert captured["headers"]["cf-access-client-secret"] == "machine-secret"


def test_voice_api_refuses_edge_token_on_insecure_remote_origin() -> None:
    config = VoiceConfig(
        api_url="http://magpie.example.com",
        input_device="plughw:2,0",
        output_device="plughw:0,0",
        wake_word="magpie",
        clip_seconds=4,
        idle_seconds=0.4,
        stt_model=DEFAULT_STT_MODEL,
        stt_fallback_model=DEFAULT_STT_FALLBACK_MODEL,
        tts_model="tts-1",
        tts_voice="alloy",
        openai_api_key="",
        audio_threshold=500,
        edge_device_id="main-cabinet",
        edge_token="device-secret",
    )

    with pytest.raises(ValueError, match="must use HTTPS"):
        ask_magpie("find screws", config)


def test_voice_api_refuses_machine_auth_on_insecure_remote_origin() -> None:
    config = VoiceConfig(
        api_url="http://magpie.example.com",
        input_device="plughw:2,0",
        output_device="plughw:0,0",
        wake_word="magpie",
        clip_seconds=4,
        idle_seconds=0.4,
        stt_model=DEFAULT_STT_MODEL,
        stt_fallback_model=DEFAULT_STT_FALLBACK_MODEL,
        tts_model="tts-1",
        tts_voice="alloy",
        openai_api_key="",
        audio_threshold=500,
        cf_access_client_id="machine-id.access",
        cf_access_client_secret="machine-secret",
    )

    with pytest.raises(ValueError, match="must use HTTPS"):
        ask_magpie("find screws", config)


def test_voice_device_id_without_token_keeps_local_api_compatible(monkeypatch) -> None:
    config = VoiceConfig(
        api_url="http://127.0.0.1:8765",
        input_device="plughw:2,0",
        output_device="plughw:0,0",
        wake_word="magpie",
        clip_seconds=4,
        idle_seconds=0.4,
        stt_model=DEFAULT_STT_MODEL,
        stt_fallback_model=DEFAULT_STT_FALLBACK_MODEL,
        tts_model="tts-1",
        tts_voice="alloy",
        openai_api_key="",
        audio_threshold=500,
        edge_device_id="main-cabinet",
    )
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return b'{"ok":true}'

    def fake_urlopen(request, timeout):
        captured["headers"] = {
            key.lower(): value for key, value in request.header_items()
        }
        return FakeResponse()

    monkeypatch.setattr(voice_daemon.urllib.request, "urlopen", fake_urlopen)

    assert ask_magpie("find screws", config) == {"ok": True}
    assert "authorization" not in captured["headers"]
    assert "x-magpie-device-id" not in captured["headers"]


def test_once_capture_logs_voice_attempt(monkeypatch, capsys) -> None:
    logged = []

    def fake_record_clip(path, config):
        path.write_bytes(b"fake wav")

    monkeypatch.setattr(voice_daemon, "record_clip", fake_record_clip)
    monkeypatch.setattr(voice_daemon, "clip_rms", lambda path: 1100)
    monkeypatch.setattr(voice_daemon, "clip_activity_rms", lambda path: 2600)
    monkeypatch.setattr(voice_daemon, "transcribe", lambda path, config, model=None: "Magpie find M4 T-nuts")
    monkeypatch.setattr(
        voice_daemon,
        "ask_magpie",
        lambda command, config, transcript="", route="", query="": {
            "ok": True,
            "interaction_id": 44,
            "automation": {"lit": True},
        },
    )
    monkeypatch.setattr(voice_daemon, "post_voice_attempt", lambda config, attempt: logged.append(attempt))

    monkeypatch.setattr(
        "sys.argv",
        [
            "searchable-storage-voice",
            "--once",
            "--call-backend",
            "--json",
            "--openai-api-key",
            "test-key",
        ],
    )

    voice_daemon.main()

    assert len(logged) == 1
    assert logged[0]["rms"] == 1100
    assert logged[0]["activity_rms"] == 2600
    assert logged[0]["transcript"] == "Magpie find M4 T-nuts"
    assert logged[0]["route"] == "/assistant/chat"
    assert logged[0]["query"] == "find M4 T-nuts"
    assert logged[0]["backend_response"] == {
        "ok": True,
        "interaction_id": 44,
        "automation": {"lit": True},
    }
    assert logged[0]["error"] == ""
    assert json.loads(capsys.readouterr().out)["query"] == "find M4 T-nuts"


def test_once_quiet_capture_logs_quiet_attempt(monkeypatch) -> None:
    logged = []

    def fake_record_clip(path, config):
        path.write_bytes(b"fake wav")

    monkeypatch.setattr(voice_daemon, "record_clip", fake_record_clip)
    monkeypatch.setattr(voice_daemon, "clip_rms", lambda path: 100)
    monkeypatch.setattr(voice_daemon, "clip_activity_rms", lambda path: 220)
    monkeypatch.setattr(voice_daemon, "post_voice_attempt", lambda config, attempt: logged.append(attempt))
    monkeypatch.setattr(
        "sys.argv",
        [
            "searchable-storage-voice",
            "--once",
            "--json",
            "--openai-api-key",
            "test-key",
        ],
    )

    voice_daemon.main()

    assert len(logged) == 1
    assert logged[0]["rms"] == 100
    assert logged[0]["activity_rms"] == 220
    assert logged[0]["transcript"] == ""
    assert logged[0]["error"] == "quiet"


def test_once_above_threshold_empty_transcript_logs_no_transcript(monkeypatch, capsys) -> None:
    logged = []

    def fake_record_clip(path, config):
        path.write_bytes(b"fake wav")

    monkeypatch.setattr(voice_daemon, "record_clip", fake_record_clip)
    monkeypatch.setattr(voice_daemon, "clip_rms", lambda path: 950)
    monkeypatch.setattr(voice_daemon, "clip_activity_rms", lambda path: 2400)
    monkeypatch.setattr(voice_daemon, "transcribe", lambda path, config, model=None: "")
    monkeypatch.setattr(voice_daemon, "post_voice_attempt", lambda config, attempt: logged.append(attempt))
    monkeypatch.setattr(
        "sys.argv",
        [
            "searchable-storage-voice",
            "--once",
            "--json",
            "--openai-api-key",
            "test-key",
        ],
    )

    voice_daemon.main()

    assert len(logged) == 1
    assert logged[0]["rms"] == 950
    assert logged[0]["transcript"] == ""
    assert logged[0]["error"] == "no_transcript"
    body = json.loads(capsys.readouterr().out)
    assert body["error"] == "no_transcript"
    assert body["speech"] == "Audio was above threshold, but speech-to-text returned no transcript."


def test_transcribe_with_fallback_uses_secondary_model(monkeypatch, tmp_path: Path) -> None:
    config = VoiceConfig(
        api_url="http://127.0.0.1:8765",
        input_device="plughw:2,0",
        output_device="plughw:0,0",
        wake_word="magpie",
        clip_seconds=4,
        idle_seconds=0.4,
        stt_model=DEFAULT_STT_MODEL,
        stt_fallback_model="whisper-1",
        tts_model="tts-1",
        tts_voice="alloy",
        openai_api_key="test-key",
        audio_threshold=500,
        dry_run=True,
    )
    calls = []

    def fake_transcribe(path, config, model=None):
        calls.append(model or config.stt_model)
        if model is None:
            raise voice_daemon.urllib.error.URLError("primary unavailable")
        return "Magpie find M4 T-nuts"

    monkeypatch.setattr(voice_daemon, "transcribe", fake_transcribe)

    transcript, model = transcribe_with_fallback(tmp_path / "clip.wav", config)

    assert transcript == "Magpie find M4 T-nuts"
    assert model == "whisper-1"
    assert calls == [DEFAULT_STT_MODEL, "whisper-1"]


def test_transcribe_rate_limit_waits_before_single_fallback(
    monkeypatch, tmp_path: Path
) -> None:
    config = VoiceConfig(
        api_url="http://127.0.0.1:8765",
        input_device="plughw:2,0",
        output_device="",
        wake_word="magpie",
        clip_seconds=4,
        idle_seconds=0.4,
        stt_model=DEFAULT_STT_MODEL,
        stt_fallback_model="whisper-1",
        tts_model="tts-1",
        tts_voice="alloy",
        openai_api_key="test-key",
        audio_threshold=500,
        dry_run=True,
    )
    calls = []
    sleeps = []

    def fake_transcribe(path, config, model=None):
        calls.append(model or config.stt_model)
        if model is None:
            raise voice_daemon.urllib.error.HTTPError(
                "https://api.openai.com/v1/audio/transcriptions",
                429,
                "rate limited",
                {"Retry-After": "3"},
                None,
            )
        return "Magpie find M4 T-nuts"

    monkeypatch.setattr(voice_daemon, "transcribe", fake_transcribe)
    monkeypatch.setattr(voice_daemon.time, "sleep", sleeps.append)

    transcript, model = transcribe_with_fallback(tmp_path / "clip.wav", config)

    assert transcript == "Magpie find M4 T-nuts"
    assert model == "whisper-1"
    assert calls == [DEFAULT_STT_MODEL, "whisper-1"]
    assert sleeps == [3.0]


def test_transcribe_quota_failure_skips_same_provider_fallback_and_persists_cooldown(
    monkeypatch, tmp_path: Path
) -> None:
    state_path = tmp_path / "stt-failures.json"
    config = VoiceConfig(
        api_url="http://127.0.0.1:8765",
        input_device="plughw:2,0",
        output_device="",
        wake_word="magpie",
        clip_seconds=4,
        idle_seconds=0.4,
        stt_model=DEFAULT_STT_MODEL,
        stt_fallback_model=DEFAULT_STT_FALLBACK_MODEL,
        tts_model="tts-1",
        tts_voice="alloy",
        openai_api_key="test-key",
        audio_threshold=500,
        dry_run=True,
        stt_failure_state_path=str(state_path),
    )
    calls = []

    def fail_quota(path, config, model=None):
        calls.append(model or config.stt_model)
        raise voice_daemon.LlmError(
            "OpenAI transcription unavailable (quota_exhausted)",
            category="quota_exhausted",
            status_code=429,
        )

    monkeypatch.setattr(voice_daemon, "transcribe", fail_quota)

    with pytest.raises(voice_daemon.LlmError, match="quota_exhausted"):
        transcribe_with_fallback(tmp_path / "clip.wav", config)
    with pytest.raises(voice_daemon.LlmError, match="cooling down"):
        transcribe_with_fallback(tmp_path / "clip.wav", config)

    assert calls == [DEFAULT_STT_MODEL]
    persisted = json.loads(state_path.read_text())
    failure = next(iter(persisted["failures"].values()))
    assert failure["category"] == "quota_exhausted"
    assert state_path.stat().st_mode & 0o777 == 0o600


def test_transcribe_quota_failure_uses_local_stt_during_persisted_cooldown(
    monkeypatch, tmp_path: Path
) -> None:
    config = VoiceConfig(
        api_url="http://127.0.0.1:8765",
        input_device="plughw:2,0",
        output_device="",
        wake_word="magpie",
        clip_seconds=4,
        idle_seconds=0.4,
        stt_model=DEFAULT_STT_MODEL,
        stt_fallback_model=DEFAULT_STT_FALLBACK_MODEL,
        tts_model="tts-1",
        tts_voice="alloy",
        openai_api_key="local-fallback-key",
        audio_threshold=500,
        dry_run=True,
        stt_failure_state_path=str(tmp_path / "stt-failures.json"),
        local_stt_fallback_enabled=True,
    )
    cloud_calls = []
    local_calls = []

    def fail_quota(path, config, model=None):
        cloud_calls.append(model or config.stt_model)
        raise voice_daemon.LlmError(
            "OpenAI transcription unavailable (quota_exhausted)",
            category="quota_exhausted",
            status_code=429,
        )

    def transcribe_locally(path, config):
        local_calls.append(path)
        return "find M4 T-nuts"

    monkeypatch.setattr(voice_daemon, "transcribe", fail_quota)
    monkeypatch.setattr(voice_daemon, "transcribe_local_vosk", transcribe_locally)

    first = transcribe_with_fallback(tmp_path / "clip.wav", config)
    second = transcribe_with_fallback(tmp_path / "clip.wav", config)

    assert first == ("find M4 T-nuts", voice_daemon.LOCAL_STT_MODEL)
    assert second == first
    assert cloud_calls == [DEFAULT_STT_MODEL]
    assert len(local_calls) == 2


def test_transcribe_quota_failure_treats_empty_local_result_as_silence(
    monkeypatch, tmp_path: Path
) -> None:
    config = VoiceConfig(
        api_url="http://127.0.0.1:8765",
        input_device="plughw:2,0",
        output_device="",
        wake_word="magpie",
        clip_seconds=4,
        idle_seconds=0.4,
        stt_model=DEFAULT_STT_MODEL,
        stt_fallback_model=DEFAULT_STT_FALLBACK_MODEL,
        tts_model="tts-1",
        tts_voice="alloy",
        openai_api_key="empty-local-key",
        audio_threshold=500,
        dry_run=True,
        stt_failure_state_path=str(tmp_path / "stt-failures.json"),
        local_stt_fallback_enabled=True,
    )

    monkeypatch.setattr(
        voice_daemon,
        "transcribe",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            voice_daemon.LlmError(
                "OpenAI transcription unavailable (quota_exhausted)",
                category="quota_exhausted",
                status_code=429,
            )
        ),
    )
    monkeypatch.setattr(voice_daemon, "transcribe_local_vosk", lambda *args: "")

    first = transcribe_with_fallback(tmp_path / "clip.wav", config)
    second = transcribe_with_fallback(tmp_path / "clip.wav", config)

    assert first == ("", voice_daemon.LOCAL_STT_MODEL)
    assert second == first


def test_openai_transcription_error_reads_insufficient_quota_category() -> None:
    error = voice_daemon.urllib.error.HTTPError(
        "https://api.openai.com/v1/audio/transcriptions",
        429,
        "Too Many Requests",
        {},
        io.BytesIO(
            json.dumps(
                {
                    "error": {
                        "code": "insufficient_quota",
                        "type": "insufficient_quota",
                        "message": "Billing quota exhausted.",
                    }
                }
            ).encode()
        ),
    )

    converted = voice_daemon._openai_http_error(error)

    assert converted.category == "quota_exhausted"
    assert converted.status_code == 429
    assert "Billing quota exhausted" not in str(converted)


def test_voice_attempts_are_spooled_and_replayed(monkeypatch, tmp_path: Path) -> None:
    config = VoiceConfig(
        api_url="http://127.0.0.1:8765",
        input_device="",
        output_device="",
        wake_word="magpie",
        clip_seconds=4,
        idle_seconds=0.4,
        stt_model=DEFAULT_STT_MODEL,
        stt_fallback_model=DEFAULT_STT_FALLBACK_MODEL,
        tts_model="tts-1",
        tts_voice="alloy",
        openai_api_key="",
        audio_threshold=500,
    )
    spool = tmp_path / "voice-attempts.jsonl"
    monkeypatch.setenv("MAGPIE_VOICE_ATTEMPT_SPOOL", str(spool))
    sent = []

    def fail_send(_config, attempt):
        sent.append(attempt)
        raise OSError("offline")

    monkeypatch.setattr(voice_daemon, "_send_voice_attempt", fail_send)
    voice_daemon.post_voice_attempt(config, {"command": "find screws"})

    assert json.loads(spool.read_text().strip()) == {"command": "find screws"}

    sent.clear()
    monkeypatch.setattr(
        voice_daemon,
        "_send_voice_attempt",
        lambda _config, attempt: sent.append(attempt),
    )
    voice_daemon.post_voice_attempt(config, {"command": "find washers"})

    assert sent == [
        {"command": "find screws"},
        {"command": "find washers"},
    ]
    assert not spool.exists()


def test_transcribe_empty_primary_tries_fallback_model(monkeypatch, tmp_path: Path) -> None:
    config = VoiceConfig(
        api_url="http://127.0.0.1:8765",
        input_device="plughw:2,0",
        output_device="plughw:0,0",
        wake_word="magpie",
        clip_seconds=4,
        idle_seconds=0.4,
        stt_model=DEFAULT_STT_MODEL,
        stt_fallback_model="whisper-1",
        tts_model="tts-1",
        tts_voice="alloy",
        openai_api_key="test-key",
        audio_threshold=500,
        dry_run=True,
    )
    calls = []

    def fake_transcribe(path, config, model=None):
        calls.append(model or config.stt_model)
        return ""

    monkeypatch.setattr(voice_daemon, "transcribe", fake_transcribe)

    transcript, model = transcribe_with_fallback(tmp_path / "clip.wav", config)

    assert transcript == ""
    assert model == "whisper-1"
    assert calls == [DEFAULT_STT_MODEL, "whisper-1"]


def test_retry_transcript_prefers_fallback_that_preserves_wake_word(monkeypatch, tmp_path: Path) -> None:
    config = VoiceConfig(
        api_url="http://127.0.0.1:8765",
        input_device="plughw:2,0",
        output_device="plughw:0,0",
        wake_word="magpie",
        clip_seconds=4,
        idle_seconds=0.4,
        stt_model=DEFAULT_STT_MODEL,
        stt_fallback_model="whisper-1",
        tts_model="tts-1",
        tts_voice="alloy",
        openai_api_key="test-key",
        audio_threshold=500,
        dry_run=True,
    )
    monkeypatch.setattr(
        voice_daemon,
        "transcribe",
        lambda path, config, model=None: "Magpie, find M4 washers",
    )

    transcript, model = retry_transcript_for_wake_word(
        tmp_path / "clip.wav",
        "M4 washers",
        DEFAULT_STT_MODEL,
        config,
    )

    assert transcript == "Magpie, find M4 washers"
    assert model == "whisper-1"


def test_transcribe_uses_english_server_vad_for_gpt_audio(monkeypatch, tmp_path: Path) -> None:
    config = VoiceConfig(
        api_url="http://127.0.0.1:8765",
        input_device="plughw:2,0",
        output_device="plughw:0,0",
        wake_word="magpie",
        clip_seconds=4,
        idle_seconds=0.4,
        stt_model=DEFAULT_STT_MODEL,
        stt_fallback_model="whisper-1",
        tts_model="tts-1",
        tts_voice="alloy",
        openai_api_key="test-key",
        audio_threshold=500,
        dry_run=True,
    )
    path = tmp_path / "clip.wav"
    path.write_bytes(b"wav")
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return b'{"text":"Magpie find M4 T-nuts"}'

    def fake_urlopen(request, timeout):
        captured["body"] = request.data
        return FakeResponse()

    monkeypatch.setattr(voice_daemon.urllib.request, "urlopen", fake_urlopen)

    assert voice_daemon.transcribe(path, config) == "Magpie find M4 T-nuts"
    assert b'name="language"\r\n\r\nen\r\n' in captured["body"]
    assert b'name="chunking_strategy"\r\n\r\nauto\r\n' in captured["body"]
    assert b"M4 T-nuts" in captured["body"]


def test_stt_prompt_excludes_live_inventory_vocabulary_by_default(monkeypatch) -> None:
    config = VoiceConfig(
        api_url="http://127.0.0.1:8765",
        input_device="plughw:2,0",
        output_device="plughw:0,0",
        wake_word="magpie",
        clip_seconds=4,
        idle_seconds=0.4,
        stt_model=DEFAULT_STT_MODEL,
        stt_fallback_model=DEFAULT_STT_FALLBACK_MODEL,
        tts_model="tts-1",
        tts_voice="alloy",
        openai_api_key="test-key",
        audio_threshold=500,
        dry_run=True,
    )
    monkeypatch.delenv("MAGPIE_STT_PROMPT", raising=False)
    monkeypatch.delenv("MAGPIE_STT_INCLUDE_INVENTORY_VOCABULARY", raising=False)

    def unexpected_urlopen(request, timeout):
        raise AssertionError("default STT prompt must not fetch inventory")

    monkeypatch.setattr(voice_daemon.urllib.request, "urlopen", unexpected_urlopen)

    assert voice_daemon.stt_prompt(config) == voice_daemon.DEFAULT_STT_PROMPT


def test_stt_prompt_can_opt_into_live_inventory_vocabulary(monkeypatch) -> None:
    config = VoiceConfig(
        api_url="http://127.0.0.1:8765",
        input_device="plughw:2,0",
        output_device="plughw:0,0",
        wake_word="magpie",
        clip_seconds=4,
        idle_seconds=0.4,
        stt_model=DEFAULT_STT_MODEL,
        stt_fallback_model=DEFAULT_STT_FALLBACK_MODEL,
        tts_model="tts-1",
        tts_voice="alloy",
        openai_api_key="test-key",
        audio_threshold=500,
        dry_run=True,
    )
    monkeypatch.delenv("MAGPIE_STT_PROMPT", raising=False)
    monkeypatch.setenv("MAGPIE_STT_INCLUDE_INVENTORY_VOCABULARY", "true")

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps(
                {
                    "inventory": [
                        {
                            "name": "M4 T-Nuts",
                            "aliases": ["t-slot nuts", "hammer nuts"],
                        }
                    ]
                }
            ).encode()

    monkeypatch.setattr(
        voice_daemon.urllib.request,
        "urlopen",
        lambda request, timeout: FakeResponse(),
    )

    prompt = voice_daemon.stt_prompt(config)

    assert "Known inventory vocabulary:" in prompt
    assert "M4 T-Nuts" in prompt
    assert "t-slot nuts" in prompt


def test_speak_decodes_to_hdmi_safe_wav_and_tries_fallback_device(
    monkeypatch,
    tmp_path: Path,
) -> None:
    config = VoiceConfig(
        api_url="http://127.0.0.1:8765",
        input_device="plughw:2,0",
        output_device="plughw:0,0",
        wake_word="magpie",
        clip_seconds=4,
        idle_seconds=0.4,
        stt_model=DEFAULT_STT_MODEL,
        stt_fallback_model=DEFAULT_STT_FALLBACK_MODEL,
        tts_model="tts-1",
        tts_voice="alloy",
        openai_api_key="test-key",
        audio_threshold=500,
    )
    monkeypatch.setattr(
        voice_daemon,
        "synthesize",
        lambda text, path, config: path.write_bytes(b"mp3"),
    )
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[0] == "ffmpeg":
            return SimpleNamespace(returncode=0, stderr=b"")
        if command[3] == "plughw:0,0":
            return SimpleNamespace(returncode=1, stderr=b"device unavailable")
        return SimpleNamespace(returncode=0, stderr=b"")

    monkeypatch.setattr(voice_daemon.subprocess, "run", fake_run)

    voice_daemon.speak("Found it.", config)

    assert calls[0][0] == "ffmpeg"
    assert "48000" in calls[0]
    assert calls[1][3] == "plughw:0,0"
    assert calls[2][3] == "plughw:CARD=Array,DEV=0"


def test_speak_uses_local_tts_without_cloud_call_during_audio_quota_cooldown(
    monkeypatch,
    tmp_path: Path,
) -> None:
    config = VoiceConfig(
        api_url="http://127.0.0.1:8765",
        input_device="",
        output_device="default",
        wake_word="magpie",
        clip_seconds=4,
        idle_seconds=0.4,
        stt_model=DEFAULT_STT_MODEL,
        stt_fallback_model=DEFAULT_STT_FALLBACK_MODEL,
        tts_model="tts-1",
        tts_voice="alloy",
        openai_api_key="local-tts-key",
        audio_threshold=500,
        stt_failure_state_path=str(tmp_path / "audio-failures.json"),
        local_tts_fallback_enabled=True,
    )
    cooldown = voice_daemon._stt_failure_cooldown(config.stt_failure_state_path)
    cooldown.failed(
        voice_daemon._stt_endpoint_id(config),
        voice_daemon.LlmError(
            "quota exhausted",
            category="quota_exhausted",
            status_code=429,
        ),
    )
    local_calls = []
    commands = []
    monkeypatch.setattr(
        voice_daemon.urllib.request,
        "urlopen",
        lambda *args, **kwargs: pytest.fail("cloud TTS must remain in cooldown"),
    )
    monkeypatch.setattr(
        voice_daemon,
        "synthesize_local",
        lambda text, path: (local_calls.append(text), path.write_bytes(b"wav")),
    )

    def fake_run(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(voice_daemon.subprocess, "run", fake_run)

    voice_daemon.speak("Found M4 T-nuts.", config)

    assert local_calls == ["Found M4 T-nuts."]
    assert commands[0][0] == "ffmpeg"
    assert commands[1][0] == "aplay"


def test_local_tts_uses_espeak_stdout(monkeypatch, tmp_path: Path) -> None:
    path = tmp_path / "speech.wav"
    observed = []
    monkeypatch.setattr(voice_daemon.shutil, "which", lambda name: "/usr/bin/espeak-ng")

    def fake_run(command, **kwargs):
        observed.append(command)
        return SimpleNamespace(returncode=0, stdout=b"RIFF-local-wav", stderr=b"")

    monkeypatch.setattr(voice_daemon.subprocess, "run", fake_run)

    voice_daemon.synthesize_local("Found it.", path)

    assert observed == [
        ["/usr/bin/espeak-ng", "-v", "en-us", "-s", "165", "--stdout", "Found it."]
    ]
    assert path.read_bytes() == b"RIFF-local-wav"


def test_transcribe_keeps_whisper_fallback_free_of_chunking_field(
    monkeypatch,
    tmp_path: Path,
) -> None:
    config = VoiceConfig(
        api_url="http://127.0.0.1:8765",
        input_device="plughw:2,0",
        output_device="plughw:0,0",
        wake_word="magpie",
        clip_seconds=4,
        idle_seconds=0.4,
        stt_model=DEFAULT_STT_MODEL,
        stt_fallback_model="whisper-1",
        tts_model="tts-1",
        tts_voice="alloy",
        openai_api_key="test-key",
        audio_threshold=500,
        dry_run=True,
    )
    path = tmp_path / "clip.wav"
    path.write_bytes(b"wav")
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return b'{"text":""}'

    def fake_urlopen(request, timeout):
        captured["body"] = request.data
        return FakeResponse()

    monkeypatch.setattr(voice_daemon.urllib.request, "urlopen", fake_urlopen)

    assert voice_daemon.transcribe(path, config, model="whisper-1") == ""
    assert b'name="language"\r\n\r\nen\r\n' in captured["body"]
    assert b'name="chunking_strategy"' not in captured["body"]


def test_transcript_diagnostic_does_not_log_voice_attempt(monkeypatch) -> None:
    logged = []
    monkeypatch.setattr(voice_daemon, "post_voice_attempt", lambda config, attempt: logged.append(attempt))
    monkeypatch.setattr(
        "sys.argv",
        [
            "searchable-storage-voice",
            "--transcript",
            "Magpie find M4 T-nuts",
        ],
    )

    voice_daemon.main()

    assert logged == []


def test_transcript_diagnostic_can_speak_backend_response(monkeypatch) -> None:
    spoken = []
    monkeypatch.setattr(
        voice_daemon,
        "diagnose_voice_command",
        lambda transcript, config, **kwargs: {
            "speech": "M4 T-Nuts are in B046. I lit it.",
            "response": {"interaction_id": 77},
        },
    )
    monkeypatch.setattr(
        voice_daemon,
        "speak",
        lambda speech, config: spoken.append(speech),
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "searchable-storage-voice",
            "--transcript",
            "Magpie find M4 T-nuts",
            "--call-backend",
            "--speak-response",
            "--openai-api-key",
            "test-key",
            "--json",
        ],
    )

    voice_daemon.main()

    assert spoken == ["M4 T-Nuts are in B046. I lit it."]


def test_parser_defaults_to_current_transcription_model() -> None:
    args = build_parser().parse_args(["--transcript", "Magpie find screws"])

    assert args.stt_model == DEFAULT_STT_MODEL
    assert args.stt_fallback_model == DEFAULT_STT_FALLBACK_MODEL
    assert args.stt_failure_state_path == voice_daemon.DEFAULT_STT_FAILURE_STATE_PATH
    assert args.local_stt_fallback is True
    assert args.local_tts_fallback is True
    assert args.audio_threshold == DEFAULT_AUDIO_THRESHOLD
    assert args.vad_frame_threshold == DEFAULT_VAD_FRAME_THRESHOLD
    assert args.input_channels == 2
    assert args.input_channel == 1


def test_put_away_response_asks_for_spoken_confirmation() -> None:
    speech = response_to_speech(
        {
            "message": "File M4 T-nuts in Main Cabinet. Confirm to save it.",
            "decision": {
                "action": "put_away",
                "plan": [{"tool": "put_item", "arguments": {"item": "M4 T-nuts"}}],
            },
        }
    )

    assert speech == (
        "That goes in the suggested spot. "
        "I could not light that location. Say yes to confirm."
    )


def test_completed_put_away_response_does_not_request_screen_confirmation() -> None:
    speech = response_to_speech(
        {
            "action": "put_away",
            "message": "Stored M4 T-nuts in Main Cabinet.",
            "lit": True,
        }
    )

    assert speech == "Stored M4 T-nuts in Main Cabinet."
    assert "screen" not in speech.lower()


def test_put_away_speech_omits_reason_and_uses_item_and_destination() -> None:
    speech = response_to_speech(
        {
            "message": (
                "File MH Series Sensor in Main Cabinet > B053. "
                "Reason: you asked for a small bin in the main cabinet. Confirm to save it."
            ),
            "decision": {
                "action": "put_away",
                "lit": True,
                "item": {"name": "MH Series Sensor"},
                "destination": {
                    "id": "B053",
                    "path": [
                        {"id": "main-cabinet", "label": "Main Cabinet"},
                        {"id": "B053", "label": "B053"},
                    ],
                },
                "plan": [{"tool": "put_item", "arguments": {"item": "MH Series Sensor"}}],
            },
        }
    )

    assert speech == "MH Series Sensor goes in B053. I lit it. Say yes to confirm."
    assert "reason" not in speech.lower()


def test_put_away_speech_does_not_claim_failed_light_succeeded() -> None:
    speech = response_to_speech(
        {
            "decision": {
                "action": "put_away",
                "lit": False,
                "item": {"name": "LED Strip Scraps"},
                "destination": {
                    "id": "allen-keys-and-wrenches-bin",
                    "path": [
                        {"id": "main-cabinet", "label": "Main Cabinet"},
                        {
                            "id": "allen-keys-and-wrenches-bin",
                            "label": "Allen Keys and Wrenches Bin",
                        },
                    ],
                },
                "plan": [{"tool": "put_item", "arguments": {"item": "LED Strip Scraps"}}],
            },
        }
    )

    assert speech == (
        "LED Strip Scraps goes in Allen Keys and Wrenches Bin. "
        "I could not light that location. Say yes to confirm."
    )


def test_clip_rms_reads_quiet_wav(tmp_path: Path) -> None:
    path = tmp_path / "quiet.wav"
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\x00\x00" * 160)

    assert clip_rms(path) == 0


def test_activity_rms_detects_sustained_speech_diluted_by_clip_average(tmp_path: Path) -> None:
    path = tmp_path / "speech.wav"
    samples = array.array("h", [200] * (4 * 16000))
    samples[16000:32000] = array.array("h", [1200] * 16000)
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(samples.tobytes())

    assert 600 < clip_rms(path) < 700
    assert clip_activity_rms(path) == 1200


def test_activity_rms_ignores_one_short_spike(tmp_path: Path) -> None:
    path = tmp_path / "spike.wav"
    samples = array.array("h", [200] * (4 * 16000))
    samples[:1600] = array.array("h", [3000] * 1600)
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(samples.tobytes())

    assert clip_activity_rms(path) == 200


def test_vad_capture_waits_for_speech_and_stops_after_silence() -> None:
    quiet = b"\x00\x00" * 480
    speech = b"\x01\x00" * 480

    class FakeVad:
        def is_speech(self, frame, sample_rate):
            assert sample_rate == 16000
            return frame == speech

    pcm, detected = voice_daemon.collect_vad_utterance(
        [quiet] * 4 + [speech] * 8 + [quiet] * 6,
        FakeVad(),
        frame_ms=30,
        pre_roll_ms=90,
        start_timeout_seconds=1,
        silence_seconds=0.09,
        max_utterance_seconds=2,
    )

    assert detected is True
    assert pcm.startswith(quiet)
    assert speech in pcm
    assert len(pcm) < 18 * len(quiet)


def test_vad_capture_times_out_without_speech() -> None:
    quiet = b"\x00\x00" * 480

    class FakeVad:
        def is_speech(self, frame, sample_rate):
            return False

    pcm, detected = voice_daemon.collect_vad_utterance(
        [quiet] * 20,
        FakeVad(),
        frame_ms=30,
        pre_roll_ms=90,
        start_timeout_seconds=0.15,
    )

    assert detected is False
    assert pcm == quiet * 5


def test_vad_capture_rejects_low_energy_false_positive() -> None:
    low_noise = b"\xc8\x00" * 480

    class FalsePositiveVad:
        def is_speech(self, frame, sample_rate):
            return True

    pcm, detected = voice_daemon.collect_vad_utterance(
        [low_noise] * 10,
        FalsePositiveVad(),
        frame_ms=30,
        start_timeout_seconds=0.15,
        min_frame_rms=500,
    )

    assert detected is False
    assert pcm == low_noise * 5


def test_record_clip_uses_lower_speech_start_threshold(monkeypatch, tmp_path: Path) -> None:
    observed: dict[str, float] = {}

    class FakeProcess:
        stdout = io.BytesIO(b"")
        stderr = io.BytesIO(b"")

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    monkeypatch.setattr(voice_daemon.subprocess, "Popen", lambda *args, **kwargs: FakeProcess())

    def fake_collect(frames, vad, **kwargs):
        observed["min_frame_rms"] = kwargs["min_frame_rms"]
        return b"\x00\x00" * 480, True

    monkeypatch.setattr(voice_daemon, "collect_vad_utterance", fake_collect)
    config = VoiceConfig(
        api_url="http://127.0.0.1:8765",
        input_device="plughw:2,0",
        output_device="",
        wake_word="magpie",
        clip_seconds=4,
        idle_seconds=0.4,
        stt_model=DEFAULT_STT_MODEL,
        stt_fallback_model=DEFAULT_STT_FALLBACK_MODEL,
        tts_model="tts-1",
        tts_voice="alloy",
        openai_api_key="",
        audio_threshold=1900,
        vad_frame_threshold=800,
        dry_run=True,
    )

    detected = voice_daemon.record_clip(tmp_path / "clip.wav", config)

    assert detected is True
    assert observed["min_frame_rms"] == 800


def test_record_clip_selects_asr_channel_from_stereo_input(
    monkeypatch, tmp_path: Path
) -> None:
    mono_samples = 16000 * voice_daemon.DEFAULT_VAD_FRAME_MS // 1000
    stereo = array.array("h")
    for index in range(mono_samples):
        stereo.extend((100 + index, 1000 + index))
    observed: dict[str, object] = {}

    class FakeProcess:
        stdout = io.BytesIO(stereo.tobytes())
        stderr = io.BytesIO(b"")

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    def fake_popen(args, **kwargs):
        observed["args"] = args
        return FakeProcess()

    monkeypatch.setattr(voice_daemon.subprocess, "Popen", fake_popen)

    def fake_collect(frames, vad, **kwargs):
        observed["frame"] = next(iter(frames))
        return observed["frame"], True

    monkeypatch.setattr(voice_daemon, "collect_vad_utterance", fake_collect)
    config = VoiceConfig(
        api_url="http://127.0.0.1:8765",
        input_device="plughw:2,0",
        output_device="",
        wake_word="magpie",
        clip_seconds=4,
        idle_seconds=0.4,
        stt_model=DEFAULT_STT_MODEL,
        stt_fallback_model=DEFAULT_STT_FALLBACK_MODEL,
        tts_model="tts-1",
        tts_voice="alloy",
        openai_api_key="",
        audio_threshold=1900,
        input_channels=2,
        input_channel=1,
        dry_run=True,
    )

    assert voice_daemon.record_clip(tmp_path / "clip.wav", config) is True
    assert observed["args"][observed["args"].index("-c") + 1] == "2"
    selected = array.array("h")
    selected.frombytes(observed["frame"])
    assert selected.tolist() == [1000 + index for index in range(mono_samples)]


def test_local_wake_timing_trims_keyword_audio(monkeypatch, tmp_path: Path) -> None:
    source = tmp_path / "speech.wav"
    target = tmp_path / "command.wav"
    with wave.open(str(source), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\x01\x00" * 16000)

    model = tmp_path / "vosk-model"
    (model / "conf").mkdir(parents=True)
    (model / "conf/model.conf").write_text("model", encoding="utf-8")

    class FakeRecognizer:
        def __init__(self, vosk_model, sample_rate, grammar):
            assert vosk_model == "loaded-model"
            assert sample_rate == 16000
            assert json.loads(grammar) == [
                "magpie",
                "hey magpie",
                "mag pie",
                "hey mag pie",
                "magpi",
                "hey magpi",
                "maggie",
                "hey maggie",
                "nangpai",
                "hey nangpai",
                "[unk]",
            ]

        def SetWords(self, enabled):
            assert enabled is True

        def AcceptWaveform(self, pcm):
            return False

        def Result(self):
            return "{}"

        def FinalResult(self):
            return json.dumps(
                {
                    "result": [
                        {"word": "hey", "start": 0.05, "end": 0.15, "conf": 0.95},
                        {"word": "magpie", "start": 0.15, "end": 0.4, "conf": 0.9},
                    ],
                    "text": "hey magpie",
                }
            )

    monkeypatch.setattr(voice_daemon, "load_vosk_model", lambda path: "loaded-model")
    monkeypatch.setitem(
        sys.modules,
        "vosk",
        SimpleNamespace(KaldiRecognizer=FakeRecognizer),
    )
    config = VoiceConfig(
        api_url="http://127.0.0.1:8765",
        input_device="plughw:2,0",
        output_device="",
        wake_word="magpie",
        clip_seconds=4,
        idle_seconds=0.4,
        stt_model=DEFAULT_STT_MODEL,
        stt_fallback_model=DEFAULT_STT_FALLBACK_MODEL,
        tts_model="tts-1",
        tts_voice="alloy",
        openai_api_key="",
        audio_threshold=500,
        local_wake_enabled=True,
        vosk_model_path=str(model),
        dry_run=True,
    )

    detection = voice_daemon.detect_local_wake(source, config)
    duration = voice_daemon.write_command_audio_after_wake(source, target, detection)

    assert detection.detected is True
    assert detection.end_frame == 40
    assert 0.5 < duration < 0.7


@pytest.mark.parametrize(
    "recognized_words",
    [
        [
            {"word": "hey", "start": 0.02, "end": 0.12, "conf": 0.8},
            {"word": "magpie", "start": 0.13, "end": 0.35, "conf": 0.9},
        ],
        [{"word": "magpie", "start": 0.13, "end": 0.35, "conf": 0.9}],
        [{"word": "maggie", "start": 0.13, "end": 0.35, "conf": 0.9}],
        [
            {"word": "mag", "start": 0.13, "end": 0.22, "conf": 0.85},
            {"word": "pie", "start": 0.23, "end": 0.35, "conf": 0.9},
        ],
    ],
    ids=["hey-magpie", "magpie", "maggie", "mag-pie"],
)
def test_local_wake_accepts_supported_phrase(
    monkeypatch,
    tmp_path: Path,
    recognized_words: list[dict[str, object]],
) -> None:
    source = tmp_path / "speech.wav"
    with wave.open(str(source), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\x01\x00" * 16000)
    model = tmp_path / "vosk-model"
    (model / "conf").mkdir(parents=True)
    (model / "conf/model.conf").write_text("model", encoding="utf-8")
    observed: dict[str, object] = {}

    class FakeRecognizer:
        def __init__(self, vosk_model, sample_rate, grammar):
            observed.update(model=vosk_model, sample_rate=sample_rate, grammar=grammar)

        def SetWords(self, enabled):
            pass

        def AcceptWaveform(self, pcm):
            return False

        def FinalResult(self):
            return json.dumps({"result": recognized_words})

    monkeypatch.setattr(voice_daemon, "load_vosk_model", lambda path: "loaded-model")
    monkeypatch.setitem(sys.modules, "vosk", SimpleNamespace(KaldiRecognizer=FakeRecognizer))
    config = VoiceConfig(
        api_url="http://127.0.0.1:8765",
        input_device="plughw:2,0",
        output_device="",
        wake_word="magpie",
        clip_seconds=4,
        idle_seconds=0.4,
        stt_model=DEFAULT_STT_MODEL,
        stt_fallback_model=DEFAULT_STT_FALLBACK_MODEL,
        tts_model="tts-1",
        tts_voice="alloy",
        openai_api_key="",
        audio_threshold=500,
        local_wake_enabled=True,
        vosk_model_path=str(model),
        dry_run=True,
    )

    detection = voice_daemon.detect_local_wake(source, config)

    assert detection.detected is True
    assert detection.end_frame == 35
    assert observed["model"] == "loaded-model"
    assert observed["sample_rate"] == 16000
    assert json.loads(str(observed["grammar"])) == [
        "magpie",
        "hey magpie",
        "mag pie",
        "hey mag pie",
        "magpi",
        "hey magpi",
        "maggie",
        "hey maggie",
        "nangpai",
        "hey nangpai",
        "[unk]",
    ]


def test_local_wake_rejects_low_confidence_guess(
    monkeypatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "speech.wav"
    with wave.open(str(source), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\x01\x00" * 16000)
    model = tmp_path / "vosk-model"
    (model / "conf").mkdir(parents=True)
    (model / "conf/model.conf").write_text("model", encoding="utf-8")

    class FakeRecognizer:
        def __init__(self, _model, _sample_rate, _grammar):
            pass

        def SetWords(self, _enabled):
            pass

        def AcceptWaveform(self, _pcm):
            return False

        def FinalResult(self):
            return json.dumps(
                {
                    "result": [
                        {
                            "word": "magpie",
                            "start": 0.1,
                            "end": 0.4,
                            "conf": 0.62,
                        }
                    ]
                }
            )

    monkeypatch.setattr(voice_daemon, "load_vosk_model", lambda _path: "model")
    monkeypatch.setitem(sys.modules, "vosk", SimpleNamespace(KaldiRecognizer=FakeRecognizer))
    config = VoiceConfig(
        api_url="http://127.0.0.1:8765",
        input_device="plughw:2,0",
        output_device="",
        wake_word="magpie",
        clip_seconds=4,
        idle_seconds=0.4,
        stt_model=DEFAULT_STT_MODEL,
        stt_fallback_model=DEFAULT_STT_FALLBACK_MODEL,
        tts_model="tts-1",
        tts_voice="alloy",
        openai_api_key="",
        audio_threshold=500,
        local_wake_enabled=True,
        local_wake_min_confidence=0.8,
        vosk_model_path=str(model),
        dry_run=True,
    )

    assert voice_daemon.detect_local_wake(source, config).detected is False


def test_local_vosk_transcription_normalizes_hardware_terms(monkeypatch, tmp_path: Path) -> None:
    source = tmp_path / "command.wav"
    with wave.open(str(source), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\x01\x00" * 8000)
    model = tmp_path / "vosk-model"
    (model / "conf").mkdir(parents=True)
    (model / "conf/model.conf").write_text("model", encoding="utf-8")

    class FakeRecognizer:
        def __init__(self, vosk_model, sample_rate):
            assert vosk_model == "loaded-model"
            assert sample_rate == 16000

        def AcceptWaveform(self, pcm):
            return False

        def FinalResult(self):
            return json.dumps({"text": "find m four tea nuts"})

    monkeypatch.setattr(voice_daemon, "load_vosk_model", lambda path: "loaded-model")
    monkeypatch.setitem(sys.modules, "vosk", SimpleNamespace(KaldiRecognizer=FakeRecognizer))
    config = VoiceConfig(
        api_url="http://127.0.0.1:8765",
        input_device="",
        output_device="",
        wake_word="magpie",
        clip_seconds=4,
        idle_seconds=0.4,
        stt_model=DEFAULT_STT_MODEL,
        stt_fallback_model=DEFAULT_STT_FALLBACK_MODEL,
        tts_model="tts-1",
        tts_voice="alloy",
        openai_api_key="",
        audio_threshold=500,
        vosk_model_path=str(model),
    )

    assert voice_daemon.transcribe_local_vosk(source, config) == "find M4 T-nuts"


def test_local_wake_configuration_requires_key_and_model(tmp_path: Path) -> None:
    config = VoiceConfig(
        api_url="http://127.0.0.1:8765",
        input_device="plughw:2,0",
        output_device="",
        wake_word="magpie",
        clip_seconds=4,
        idle_seconds=0.4,
        stt_model=DEFAULT_STT_MODEL,
        stt_fallback_model=DEFAULT_STT_FALLBACK_MODEL,
        tts_model="tts-1",
        tts_voice="alloy",
        openai_api_key="",
        audio_threshold=500,
        local_wake_enabled=True,
        vosk_model_path=str(tmp_path / "missing-model"),
        dry_run=True,
    )

    with pytest.raises(ValueError, match="Vosk wake model not found"):
        voice_daemon.validate_local_wake_configuration(config)


def test_local_wake_configuration_rejects_invalid_confidence(tmp_path: Path) -> None:
    config = VoiceConfig(
        api_url="http://127.0.0.1:8765",
        input_device="plughw:2,0",
        output_device="",
        wake_word="magpie",
        clip_seconds=4,
        idle_seconds=0.4,
        stt_model=DEFAULT_STT_MODEL,
        stt_fallback_model=DEFAULT_STT_FALLBACK_MODEL,
        tts_model="tts-1",
        tts_voice="alloy",
        openai_api_key="",
        audio_threshold=500,
        local_wake_enabled=True,
        local_wake_min_confidence=1.2,
        vosk_model_path=str(tmp_path),
    )

    with pytest.raises(ValueError, match="between 0 and 1"):
        voice_daemon.validate_local_wake_configuration(config)


def test_listener_uses_local_wake_and_transcribes_command_only_audio(
    monkeypatch,
    tmp_path: Path,
) -> None:
    config = VoiceConfig(
        api_url="http://127.0.0.1:8765",
        input_device="plughw:2,0",
        output_device="",
        wake_word="magpie",
        clip_seconds=4,
        idle_seconds=0.4,
        stt_model=DEFAULT_STT_MODEL,
        stt_fallback_model=DEFAULT_STT_FALLBACK_MODEL,
        tts_model="tts-1",
        tts_voice="alloy",
        openai_api_key="",
        audio_threshold=500,
        local_wake_enabled=True,
        dry_run=True,
    )
    source = tmp_path / "speech.wav"
    source.write_bytes(b"source")
    dispatched = []
    monkeypatch.setattr(voice_daemon, "clip_rms", lambda path: 900)
    monkeypatch.setattr(voice_daemon, "clip_activity_rms", lambda path: 1200)
    monkeypatch.setattr(
        voice_daemon,
        "detect_local_wake",
        lambda path, config: voice_daemon.LocalWakeDetection(True, 2, 30, 0.9),
    )

    def fake_trim(source_path, target_path, detection):
        target_path.write_bytes(b"command only")
        return 1.2

    monkeypatch.setattr(voice_daemon, "write_command_audio_after_wake", fake_trim)

    def fake_transcribe(path, config):
        assert path == source.with_name("command.wav")
        return "put away M4 screws", DEFAULT_STT_MODEL

    monkeypatch.setattr(voice_daemon, "transcribe_with_fallback", fake_transcribe)
    monkeypatch.setattr(
        voice_daemon,
        "dispatch_voice_diagnosis",
        lambda diagnosis, config: dispatched.append(diagnosis),
    )

    result = handle_clip(source, config, speech_detected=True)

    assert result is False
    assert dispatched[0]["command"] == "put away M4 screws"
    assert dispatched[0]["wake_heard"] is True
    assert dispatched[0]["query"] == "put away M4 screws"
    assert config.runtime_stats.speech_clips == 1
    assert config.runtime_stats.wake_detections == 1
    capabilities = voice_daemon.voice_capabilities(config)
    assert capabilities["wake_gate"] == "local"
    assert capabilities["audio_before_wake_uploaded"] is False


@pytest.mark.parametrize(
    "transcript, expected",
    [
        (
            "Let's go ahead and put away LED strip scraps",
            "put away LED strip scraps",
        ),
        (
            "Let's put away LED strip scraps in a small bin in the main cabinet.",
            "put away LED strip scraps in a small bin in the main cabinet",
        ),
        (
            "Can you please find the M4 T-nuts?",
            "find the M4 T-nuts",
        ),
    ],
)
def test_listener_routes_conversational_command_after_local_wake(
    monkeypatch,
    tmp_path: Path,
    transcript: str,
    expected: str,
) -> None:
    config = VoiceConfig(
        api_url="http://127.0.0.1:8765",
        input_device="plughw:2,0",
        output_device="",
        wake_word="magpie",
        clip_seconds=4,
        idle_seconds=0.4,
        stt_model=DEFAULT_STT_MODEL,
        stt_fallback_model=DEFAULT_STT_FALLBACK_MODEL,
        tts_model="tts-1",
        tts_voice="alloy",
        openai_api_key="",
        audio_threshold=500,
        local_wake_enabled=True,
        dry_run=True,
    )
    source = tmp_path / "speech.wav"
    source.write_bytes(b"source")
    dispatched = []
    monkeypatch.setattr(voice_daemon, "clip_rms", lambda _path: 1300)
    monkeypatch.setattr(voice_daemon, "clip_activity_rms", lambda _path: 2500)
    monkeypatch.setattr(
        voice_daemon,
        "detect_local_wake",
        lambda _path, _config: voice_daemon.LocalWakeDetection(True, 2, 30, 0.9),
    )
    monkeypatch.setattr(
        voice_daemon,
        "write_command_audio_after_wake",
        lambda _source, target, _detection: (
            target.write_bytes(b"command only") and 1.2
        ),
    )
    monkeypatch.setattr(
        voice_daemon,
        "transcribe_with_fallback",
        lambda _path, _config: (transcript, DEFAULT_STT_MODEL),
    )
    monkeypatch.setattr(
        voice_daemon,
        "dispatch_voice_diagnosis",
        lambda diagnosis, _config: dispatched.append(diagnosis),
    )

    assert handle_clip(source, config, speech_detected=True) is False
    assert dispatched[0]["command"] == expected
    assert dispatched[0]["query"] == expected
    assert dispatched[0]["route"] == "/assistant/chat"
    assert dispatched[0]["wake_heard"] is True


def test_listener_preserves_local_wake_metadata_when_transcription_fails(
    monkeypatch, tmp_path: Path
) -> None:
    config = VoiceConfig(
        api_url="http://127.0.0.1:8765",
        input_device="plughw:2,0",
        output_device="",
        wake_word="magpie",
        clip_seconds=4,
        idle_seconds=0.4,
        stt_model=DEFAULT_STT_MODEL,
        stt_fallback_model=DEFAULT_STT_FALLBACK_MODEL,
        tts_model="tts-1",
        tts_voice="alloy",
        openai_api_key="",
        audio_threshold=500,
        local_wake_enabled=True,
        dry_run=True,
        edge_device_id="main-cabinet",
    )
    source = tmp_path / "speech.wav"
    source.write_bytes(b"source")
    attempts = []
    monkeypatch.setattr(voice_daemon, "clip_rms", lambda _path: 900)
    monkeypatch.setattr(voice_daemon, "clip_activity_rms", lambda _path: 1200)
    monkeypatch.setattr(
        voice_daemon,
        "detect_local_wake",
        lambda _path, _config: voice_daemon.LocalWakeDetection(True, 2, 30, 0.9),
    )
    monkeypatch.setattr(
        voice_daemon,
        "write_command_audio_after_wake",
        lambda _source, _target, _detection: 1.2,
    )
    monkeypatch.setattr(
        voice_daemon,
        "transcribe_with_fallback",
        lambda _path, _config: (_ for _ in ()).throw(OSError("rate limited")),
    )
    monkeypatch.setattr(
        voice_daemon,
        "post_voice_attempt",
        lambda _config, attempt: attempts.append(attempt),
    )

    with pytest.raises(OSError, match="rate limited"):
        handle_clip(source, config, speech_detected=True)

    assert attempts[0]["wake_heard"] is True
    assert attempts[0]["device_id"] == "main-cabinet"
    assert attempts[0]["runtime_version"] == voice_daemon.VOICE_RUNTIME.label()


def test_listener_arms_when_wake_has_no_command_audio(monkeypatch, tmp_path: Path) -> None:
    config = VoiceConfig(
        api_url="http://127.0.0.1:8765",
        input_device="plughw:2,0",
        output_device="",
        wake_word="magpie",
        clip_seconds=4,
        idle_seconds=0.4,
        stt_model=DEFAULT_STT_MODEL,
        stt_fallback_model=DEFAULT_STT_FALLBACK_MODEL,
        tts_model="tts-1",
        tts_voice="alloy",
        openai_api_key="",
        audio_threshold=500,
        local_wake_enabled=True,
        dry_run=True,
    )
    source = tmp_path / "speech.wav"
    source.write_bytes(b"ambient speech")
    attempts = []
    states = []
    monkeypatch.setattr(voice_daemon, "clip_rms", lambda path: 900)
    monkeypatch.setattr(voice_daemon, "clip_activity_rms", lambda path: 1200)
    monkeypatch.setattr(
        voice_daemon,
        "detect_local_wake",
        lambda path, config: voice_daemon.LocalWakeDetection(True, 2, 30, 0.9),
    )
    monkeypatch.setattr(
        voice_daemon,
        "write_command_audio_after_wake",
        lambda source_path, target_path, detection: 0.0,
    )
    monkeypatch.setattr(
        voice_daemon,
        "transcribe_with_fallback",
        lambda path, config: (_ for _ in ()).throw(
            AssertionError("wake-only audio must not call cloud STT")
        ),
    )
    monkeypatch.setattr(
        voice_daemon,
        "retry_transcript_for_wake_word",
        lambda path, transcript, model, config: (transcript, model),
    )
    monkeypatch.setattr(
        voice_daemon,
        "post_voice_attempt",
        lambda config, attempt: attempts.append(attempt),
    )
    monkeypatch.setattr(
        voice_daemon,
        "post_voice_state",
        lambda _config, state, message, **_kwargs: states.append((state, message)),
    )

    state = VoiceLoopState()
    result = handle_clip(source, config, state=state, speech_detected=True)

    assert result is True
    assert attempts == []
    assert state.wake_diagnosis is not None
    assert state.wake_diagnosis["wake_heard"] is True
    assert states[-1] == (
        "listening",
        "Magpie heard. Say what to find or put away.",
    )


def test_listener_records_one_failure_when_wake_command_window_expires(
    monkeypatch, tmp_path: Path
) -> None:
    config = VoiceConfig(
        api_url="http://127.0.0.1:8765",
        input_device="plughw:2,0",
        output_device="",
        wake_word="magpie",
        clip_seconds=4,
        idle_seconds=0.4,
        stt_model=DEFAULT_STT_MODEL,
        stt_fallback_model=DEFAULT_STT_FALLBACK_MODEL,
        tts_model="tts-1",
        tts_voice="alloy",
        openai_api_key="",
        audio_threshold=500,
        local_wake_enabled=True,
        dry_run=True,
    )
    attempts = []
    states = []
    monkeypatch.setattr(voice_daemon, "clip_rms", lambda _path: 100)
    monkeypatch.setattr(voice_daemon, "clip_activity_rms", lambda _path: 120)
    monkeypatch.setattr(
        voice_daemon,
        "post_voice_attempt",
        lambda _config, attempt: attempts.append(attempt),
    )
    monkeypatch.setattr(
        voice_daemon,
        "post_voice_state",
        lambda _config, state, message, **_kwargs: states.append((state, message)),
    )
    state = VoiceLoopState(
        awaiting_command=True,
        awaiting_command_until=1.0,
        wake_diagnosis={
            "rms": 900,
            "activity_rms": 1200,
            "transcript": "",
            "wake_word": "magpie",
            "wake_heard": True,
            "awaiting_command": True,
            "command": None,
            "route": "",
            "query": "",
            "stt_model": DEFAULT_STT_MODEL,
        },
    )
    monkeypatch.setattr(voice_daemon.time, "monotonic", lambda: 2.0)

    result = handle_clip(
        tmp_path / "quiet.wav", config, state=state, speech_detected=False
    )

    assert result is False
    assert state.wake_diagnosis is None
    assert len(attempts) == 1
    assert attempts[0]["error"] == "awaiting_command_timeout"
    assert attempts[0]["awaiting_command"] is False
    assert states[-1] == (
        "error",
        "I did not hear a command. Say Magpie and try again.",
    )


def test_listener_rejects_unrelated_command_audio_after_local_wake(
    monkeypatch,
    tmp_path: Path,
) -> None:
    config = VoiceConfig(
        api_url="http://127.0.0.1:8765",
        input_device="plughw:2,0",
        output_device="",
        wake_word="magpie",
        clip_seconds=4,
        idle_seconds=0.4,
        stt_model=DEFAULT_STT_MODEL,
        stt_fallback_model=DEFAULT_STT_FALLBACK_MODEL,
        tts_model="tts-1",
        tts_voice="alloy",
        openai_api_key="",
        audio_threshold=500,
        local_wake_enabled=True,
        dry_run=True,
    )
    source = tmp_path / "speech.wav"
    source.write_bytes(b"ambient speech")
    attempts = []
    monkeypatch.setattr(voice_daemon, "clip_rms", lambda path: 900)
    monkeypatch.setattr(voice_daemon, "clip_activity_rms", lambda path: 1200)
    monkeypatch.setattr(
        voice_daemon,
        "detect_local_wake",
        lambda path, config: voice_daemon.LocalWakeDetection(True, 2, 30, 0.9),
    )
    monkeypatch.setattr(
        voice_daemon,
        "write_command_audio_after_wake",
        lambda source_path, target_path, detection: 1.2,
    )
    monkeypatch.setattr(
        voice_daemon,
        "transcribe_with_fallback",
        lambda path, config: (
            "I wanted to buy a Super Mario watch at Walmart",
            DEFAULT_STT_MODEL,
        ),
    )
    monkeypatch.setattr(
        voice_daemon,
        "retry_transcript_for_wake_word",
        lambda path, transcript, model, config: (transcript, model),
    )
    monkeypatch.setattr(
        voice_daemon,
        "post_voice_attempt",
        lambda config, attempt: attempts.append(attempt),
    )

    result = handle_clip(source, config, speech_detected=True)

    assert result is False
    assert attempts[0]["error"] == "unrecognized_command"
    assert attempts[0]["wake_heard"] is True
    assert attempts[0]["command"] == ""


def test_listener_arms_cloud_confirmed_wake_only_clip(monkeypatch, tmp_path: Path) -> None:
    config = VoiceConfig(
        api_url="http://127.0.0.1:8765",
        input_device="plughw:2,0",
        output_device="",
        wake_word="magpie",
        clip_seconds=4,
        idle_seconds=0.4,
        stt_model=DEFAULT_STT_MODEL,
        stt_fallback_model=DEFAULT_STT_FALLBACK_MODEL,
        tts_model="tts-1",
        tts_voice="alloy",
        openai_api_key="",
        audio_threshold=500,
        local_wake_enabled=True,
        dry_run=True,
    )
    source = tmp_path / "speech.wav"
    source.write_bytes(b"magpie")
    monkeypatch.setattr(voice_daemon, "clip_rms", lambda path: 900)
    monkeypatch.setattr(voice_daemon, "clip_activity_rms", lambda path: 1200)
    monkeypatch.setattr(
        voice_daemon,
        "detect_local_wake",
        lambda path, config: voice_daemon.LocalWakeDetection(True, 2, 30, 0.9),
    )
    monkeypatch.setattr(
        voice_daemon,
        "write_command_audio_after_wake",
        lambda source_path, target_path, detection: 0.0,
    )
    monkeypatch.setattr(
        voice_daemon,
        "transcribe_with_fallback",
        lambda path, config: ("Magpie", DEFAULT_STT_MODEL),
    )

    result = handle_clip(source, config, speech_detected=True)

    assert result is True


def test_listener_cloud_fallback_rejects_ambient_speech_without_wake_word(
    monkeypatch,
    tmp_path: Path,
) -> None:
    config = VoiceConfig(
        api_url="http://127.0.0.1:8765",
        input_device="plughw:2,0",
        output_device="",
        wake_word="magpie",
        clip_seconds=4,
        idle_seconds=0.4,
        stt_model=DEFAULT_STT_MODEL,
        stt_fallback_model=DEFAULT_STT_FALLBACK_MODEL,
        tts_model="tts-1",
        tts_voice="alloy",
        openai_api_key="",
        audio_threshold=500,
        local_wake_enabled=True,
        dry_run=True,
    )
    source = tmp_path / "speech.wav"
    source.write_bytes(b"ambient speech")
    attempts = []
    monkeypatch.setattr(voice_daemon, "clip_rms", lambda path: 900)
    monkeypatch.setattr(voice_daemon, "clip_activity_rms", lambda path: 1200)
    monkeypatch.setattr(
        voice_daemon,
        "detect_local_wake",
        lambda path, config: voice_daemon.LocalWakeDetection(False),
    )
    monkeypatch.setattr(
        voice_daemon,
        "transcribe_with_fallback",
        lambda path, config: (_ for _ in ()).throw(
            AssertionError("passive speech must not reach cloud STT")
        ),
    )
    monkeypatch.setattr(
        voice_daemon,
        "retry_transcript_for_wake_word",
        lambda path, transcript, model, config: (transcript, model),
    )
    monkeypatch.setattr(
        voice_daemon,
        "post_voice_attempt",
        lambda config, attempt: attempts.append(attempt),
    )

    result = handle_clip(source, config, speech_detected=True)

    assert result is False
    assert attempts == []
    assert config.runtime_stats.speech_clips == 1
    assert config.runtime_stats.passive_speech_ignored == 1
    assert config.runtime_stats.cloud_stt_calls == 0


def test_listener_skips_continuous_noise_before_cloud_stt(monkeypatch, tmp_path: Path) -> None:
    config = VoiceConfig(
        api_url="http://127.0.0.1:8765",
        input_device="plughw:2,0",
        output_device="",
        wake_word="magpie",
        clip_seconds=4,
        idle_seconds=0.4,
        stt_model=DEFAULT_STT_MODEL,
        stt_fallback_model=DEFAULT_STT_FALLBACK_MODEL,
        tts_model="tts-1",
        tts_voice="alloy",
        openai_api_key="",
        audio_threshold=500,
        max_utterance_seconds=8.0,
        local_wake_enabled=True,
        dry_run=True,
    )
    source = tmp_path / "continuous-noise.wav"
    source.write_bytes(b"noise")
    monkeypatch.setattr(voice_daemon, "clip_rms", lambda path: 3000)
    monkeypatch.setattr(voice_daemon, "clip_activity_rms", lambda path: 5000)
    monkeypatch.setattr(voice_daemon, "clip_duration_seconds", lambda path: 8.0)
    monkeypatch.setattr(
        voice_daemon,
        "detect_local_wake",
        lambda path, config: voice_daemon.LocalWakeDetection(False),
    )
    monkeypatch.setattr(
        voice_daemon,
        "transcribe_with_fallback",
        lambda path, config: (_ for _ in ()).throw(
            AssertionError("continuous noise must not reach cloud STT")
        ),
    )

    assert handle_clip(source, config, speech_detected=True) is False


def test_listener_applies_audio_threshold_to_passive_cloud_fallback(
    monkeypatch, tmp_path: Path
) -> None:
    config = VoiceConfig(
        api_url="http://127.0.0.1:8765",
        input_device="plughw:2,0",
        output_device="",
        wake_word="magpie",
        clip_seconds=4,
        idle_seconds=0.4,
        stt_model=DEFAULT_STT_MODEL,
        stt_fallback_model=DEFAULT_STT_FALLBACK_MODEL,
        tts_model="tts-1",
        tts_voice="alloy",
        openai_api_key="",
        audio_threshold=1900,
        vad_frame_threshold=800,
        local_wake_enabled=True,
        dry_run=True,
    )
    source = tmp_path / "passive-low-audio.wav"
    source.write_bytes(b"noise")
    monkeypatch.setattr(voice_daemon, "clip_rms", lambda path: 600)
    monkeypatch.setattr(voice_daemon, "clip_activity_rms", lambda path: 900)
    monkeypatch.setattr(
        voice_daemon,
        "detect_local_wake",
        lambda path, config: voice_daemon.LocalWakeDetection(False),
    )
    monkeypatch.setattr(
        voice_daemon,
        "transcribe_with_fallback",
        lambda path, config: (_ for _ in ()).throw(
            AssertionError("sub-threshold passive audio must not reach cloud STT")
        ),
    )

    assert handle_clip(source, config, speech_detected=True) is False


def test_listener_does_not_call_cloud_stt_when_local_wake_misses(
    monkeypatch,
    tmp_path: Path,
) -> None:
    config = VoiceConfig(
        api_url="http://127.0.0.1:8765",
        input_device="plughw:2,0",
        output_device="",
        wake_word="magpie",
        clip_seconds=4,
        idle_seconds=0.4,
        stt_model=DEFAULT_STT_MODEL,
        stt_fallback_model=DEFAULT_STT_FALLBACK_MODEL,
        tts_model="tts-1",
        tts_voice="alloy",
        openai_api_key="",
        audio_threshold=500,
        local_wake_enabled=True,
        dry_run=True,
    )
    source = tmp_path / "speech.wav"
    source.write_bytes(b"speech")
    monkeypatch.setattr(voice_daemon, "clip_rms", lambda path: 900)
    monkeypatch.setattr(voice_daemon, "clip_activity_rms", lambda path: 1200)
    monkeypatch.setattr(
        voice_daemon,
        "detect_local_wake",
        lambda path, config: voice_daemon.LocalWakeDetection(False),
    )
    monkeypatch.setattr(
        voice_daemon,
        "transcribe_with_fallback",
        lambda path, config: (_ for _ in ()).throw(
            AssertionError("idle room speech must not reach cloud STT")
        ),
    )

    result = handle_clip(source, config, speech_detected=True)

    assert result is False


def test_listener_does_not_log_normal_vad_timeout(monkeypatch, tmp_path: Path) -> None:
    config = VoiceConfig(
        api_url="http://127.0.0.1:8765",
        input_device="plughw:2,0",
        output_device="",
        wake_word="magpie",
        clip_seconds=4,
        idle_seconds=0.4,
        stt_model=DEFAULT_STT_MODEL,
        stt_fallback_model=DEFAULT_STT_FALLBACK_MODEL,
        tts_model="tts-1",
        tts_voice="alloy",
        openai_api_key="",
        audio_threshold=500,
        local_wake_enabled=True,
        dry_run=True,
    )
    attempts = []
    monkeypatch.setattr(voice_daemon, "clip_rms", lambda path: 100)
    monkeypatch.setattr(voice_daemon, "clip_activity_rms", lambda path: 120)
    monkeypatch.setattr(
        voice_daemon,
        "post_voice_attempt",
        lambda _config, attempt: attempts.append(attempt),
    )

    result = handle_clip(tmp_path / "quiet.wav", config, speech_detected=False)

    assert result is False
    assert attempts == []


def test_listener_keeps_silence_after_wake_as_diagnostic(monkeypatch, tmp_path: Path) -> None:
    config = VoiceConfig(
        api_url="http://127.0.0.1:8765",
        input_device="plughw:2,0",
        output_device="",
        wake_word="magpie",
        clip_seconds=4,
        idle_seconds=0.4,
        stt_model=DEFAULT_STT_MODEL,
        stt_fallback_model=DEFAULT_STT_FALLBACK_MODEL,
        tts_model="tts-1",
        tts_voice="alloy",
        openai_api_key="",
        audio_threshold=500,
        local_wake_enabled=True,
        dry_run=True,
    )
    attempts = []
    monkeypatch.setattr(voice_daemon, "clip_rms", lambda path: 100)
    monkeypatch.setattr(voice_daemon, "clip_activity_rms", lambda path: 120)
    monkeypatch.setattr(
        voice_daemon,
        "post_voice_attempt",
        lambda _config, attempt: attempts.append(attempt),
    )
    state = VoiceLoopState(
        awaiting_command=True,
        awaiting_command_until=voice_daemon.time.monotonic() + 10,
        wake_diagnosis={
            "rms": 900,
            "activity_rms": 1200,
            "transcript": "",
            "wake_word": "magpie",
            "wake_heard": True,
            "awaiting_command": True,
            "command": None,
            "route": "",
            "query": "",
            "stt_model": DEFAULT_STT_MODEL,
        },
    )

    result = handle_clip(tmp_path / "quiet.wav", config, state=state, speech_detected=False)

    assert result is True
    assert attempts == []
