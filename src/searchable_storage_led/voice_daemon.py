from __future__ import annotations

import argparse
import array
from collections import deque
from functools import lru_cache
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
from urllib.parse import urlsplit
import urllib.request
import uuid
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import webrtcvad

from .build import build_info
from .interaction_contract import VOICE_SCOPE_MESSAGE
from .llm_client import LlmError, LlmFailureCooldown, llm_error_category
from .machine_auth import cloudflare_access_headers


DEFAULT_API_URL = "http://127.0.0.1:8765"
DEFAULT_WAKE_WORD = "magpie"
DEFAULT_STT_MODEL = "gpt-4o-transcribe"
DEFAULT_STT_FALLBACK_MODEL = "gpt-4o-mini-transcribe"
LOCAL_STT_MODEL = "vosk-local"
# The ReSpeaker's observed quiet floor is below 200 RMS, while normal speech can
# begin below 800 RMS. Local Vosk wake detection remains the cloud-call gate.
DEFAULT_AUDIO_THRESHOLD = 500.0
DEFAULT_VAD_FRAME_THRESHOLD = 350.0
DEFAULT_STT_PROMPT = (
    "Transcribe the spoken audio exactly. Do not add, infer, or complete words that are not clearly audible. "
    "Preserve exact hardware names, metric sizes, letters, numbers, and hyphens. "
    "Examples include M3 screws, M4 T-nuts, T-slot nuts, solder wick, Allen keys, "
    "lock washers, and heat shrink."
)
DEFAULT_STT_LANGUAGE = "en"
DEFAULT_STT_CHUNKING_STRATEGY = "auto"
DEFAULT_FOLLOW_UP_SECONDS = 20.0
DEFAULT_VAD_START_TIMEOUT_SECONDS = 10.0
DEFAULT_VAD_SILENCE_SECONDS = 1.2
DEFAULT_MAX_UTTERANCE_SECONDS = 12.0
DEFAULT_VAD_FRAME_MS = 30
DEFAULT_VAD_PRE_ROLL_MS = 300
DEFAULT_VOSK_MODEL_PATH = "state/vosk-model-small-en-us-0.15"
DEFAULT_LOCAL_WAKE_MIN_CONFIDENCE = 0.8
DEFAULT_VOICE_ATTEMPT_SPOOL_PATH = "state/voice-attempt-spool.jsonl"
DEFAULT_STT_FAILURE_STATE_PATH = "state/stt-failures.json"
MAX_SPOOLED_VOICE_ATTEMPTS = 500
DEFAULT_STT_RETRY_SECONDS = 2.0
MAX_STT_RETRY_SECONDS = 8.0
MAX_STT_VOCABULARY_CHARS = 6000
MAX_VOICE_TRANSCRIPT_CHARS = 800
MAX_VOICE_TRANSCRIPT_WORDS = 80
VOICE_SCOPE_ROUTE = "local:voice-scope"
VOICE_REQUEST_REPLAY_SECONDS = 15.0
VOICE_RUNTIME = build_info()


@dataclass
class VoiceRuntimeStats:
    started_at: float = field(default_factory=time.time)
    speech_clips: int = 0
    passive_speech_ignored: int = 0
    wake_detections: int = 0
    cloud_stt_calls: int = 0
    local_stt_calls: int = 0
    agent_requests: int = 0
    cloud_tts_calls: int = 0
    local_tts_calls: int = 0

    def snapshot(self) -> dict[str, int | float]:
        return {
            "started_at": self.started_at,
            "speech_clips": self.speech_clips,
            "passive_speech_ignored": self.passive_speech_ignored,
            "wake_detections": self.wake_detections,
            "cloud_stt_calls": self.cloud_stt_calls,
            "local_stt_calls": self.local_stt_calls,
            "assistant_requests": self.agent_requests,
            "agent_requests": self.agent_requests,
            "cloud_tts_calls": self.cloud_tts_calls,
            "local_tts_calls": self.local_tts_calls,
        }


@dataclass(frozen=True)
class VoiceConfig:
    api_url: str
    input_device: str
    output_device: str
    wake_word: str
    clip_seconds: float
    idle_seconds: float
    stt_model: str
    stt_fallback_model: str
    tts_model: str
    tts_voice: str
    openai_api_key: str
    audio_threshold: float
    input_channels: int = 2
    input_channel: int = 1
    vad_frame_threshold: float = DEFAULT_VAD_FRAME_THRESHOLD
    follow_up_seconds: float = DEFAULT_FOLLOW_UP_SECONDS
    vad_aggressiveness: int = 2
    vad_start_timeout_seconds: float = DEFAULT_VAD_START_TIMEOUT_SECONDS
    vad_silence_seconds: float = DEFAULT_VAD_SILENCE_SECONDS
    max_utterance_seconds: float = DEFAULT_MAX_UTTERANCE_SECONDS
    local_wake_enabled: bool = False
    local_wake_min_confidence: float = DEFAULT_LOCAL_WAKE_MIN_CONFIDENCE
    vosk_model_path: str = DEFAULT_VOSK_MODEL_PATH
    log_attempts: bool = True
    dry_run: bool = False
    edge_device_id: str = ""
    edge_token: str = ""
    edge_workspace_id: str = ""
    cf_access_client_id: str = ""
    cf_access_client_secret: str = ""
    stt_failure_state_path: str = ""
    local_stt_fallback_enabled: bool = False
    local_tts_fallback_enabled: bool = False
    runtime_stats: VoiceRuntimeStats = field(
        default_factory=VoiceRuntimeStats,
        compare=False,
    )


@dataclass(frozen=True)
class TranscriptCommand:
    command: str | None
    wake_heard: bool
    awaiting_command: bool = False


@dataclass
class VoiceLoopState:
    awaiting_command: bool = False
    awaiting_command_until: float = 0.0
    wake_diagnosis: dict[str, Any] | None = None
    pending_diagnosis: dict[str, Any] | None = None
    follow_up_until: float = 0.0
    last_request_signature: str = ""
    last_client_request_id: str = ""
    last_request_at: float = 0.0


@dataclass(frozen=True)
class LocalWakeDetection:
    detected: bool
    start_frame: int = 0
    end_frame: int = 0
    probability: float = 0.0


NUMBER_WORDS = {
    "zero": "0",
    "oh": "0",
    "o": "0",
    "one": "1",
    "two": "2",
    "three": "3",
    "four": "4",
    "for": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "ate": "8",
    "nine": "9",
}


WAKE_WORD_ALIASES = {
    "magpie": ("magpie", "mag pie", "magpi", "maggie", "nangpai"),
}


def wake_word_pattern(wake_word: str) -> str:
    aliases = WAKE_WORD_ALIASES.get(wake_word.lower(), (wake_word,))
    return "|".join(re.escape(alias) for alias in aliases)


def wake_word_phrases(wake_word: str) -> list[str]:
    aliases = WAKE_WORD_ALIASES.get(wake_word.casefold(), (wake_word,))
    phrases: list[str] = []
    for alias in aliases:
        clean_alias = re.sub(r"\s+", " ", alias).strip().casefold()
        if not clean_alias:
            continue
        phrases.extend((clean_alias, f"hey {clean_alias}"))
    return list(dict.fromkeys(phrases))


def normalize_spoken_hardware_terms(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", text).strip()
    if not cleaned:
        return ""
    if re.fullmatch(r"m\s*(?:14|fourteen)", cleaned, re.I):
        return "M4 T-nuts"
    cleaned = re.sub(
        r"\bm(\d)t\b",
        r"M\1 T-nuts",
        cleaned,
        flags=re.I,
    )

    def normalize_metric(match: re.Match[str]) -> str:
        digit = NUMBER_WORDS.get(match.group(1).lower(), match.group(1))
        return f"M{digit}"

    cleaned = re.sub(
        r"\bm\s*(zero|oh|o|one|two|three|four|for|five|six|seven|eight|ate|nine|\d)\b",
        normalize_metric,
        cleaned,
        flags=re.I,
    )
    cleaned = re.sub(r"\b(?:tee|tea|t)\s*[- ]?\s*nuts?\b", "T-nuts", cleaned, flags=re.I)
    cleaned = re.sub(r"\b(?:tee|tea|t)\s*[- ]?\s*slots?\b", "T-slots", cleaned, flags=re.I)
    cleaned = re.sub(r"\bsolder\s+(?:wake|wicke)\b", "solder wick", cleaned, flags=re.I)
    cleaned = re.sub(r"\bheat\s+shrinks?\b", "heat shrink", cleaned, flags=re.I)
    cleaned = re.sub(r"\bzip\s+ties?\b", "zip ties", cleaned, flags=re.I)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ?.")
    return cleaned


def strip_wake_word(transcript: str, wake_word: str = DEFAULT_WAKE_WORD) -> str | None:
    cleaned = re.sub(r"\s+", " ", transcript).strip()
    if not cleaned:
        return None
    matches = list(re.finditer(rf"\b(?:{wake_word_pattern(wake_word)})\b", cleaned, re.I))
    if not matches:
        return None
    command = clean_voice_command(cleaned[matches[-1].end() :])
    return command or None


def clean_voice_command(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", text).strip()
    cleaned = cleaned.strip(" \t\r\n,;:-.!?")
    if not re.search(r"[A-Za-z0-9]", cleaned):
        return ""
    return cleaned


def recover_storage_command_without_wake(transcript: str) -> str | None:
    """Recover an explicit command when STT drops or mangles the wake word."""
    cleaned = re.sub(r"\s+", " ", transcript).strip()
    if not cleaned:
        return None
    command_pattern = (
        r"(?:"
        r"(?:find|where\s+(?:is|are)|show\s+me|locate|get|grab|"
        r"put\s+away|put|store|file|stash|stow|confirm|apply|cancel|undo|revert|"
        r"light|illuminate)\b.+|"
        r"(?:move|relocate)\s+(?:this|that|these|those)\b.+"
        r")"
    )
    direct = re.fullmatch(command_pattern, clean_voice_command(cleaned), re.I)
    if direct:
        return clean_voice_command(direct.group(0))
    wrapped = re.fullmatch(
        rf"(?:"
        rf"let(?:'s| us)(?:\s+go ahead and)?|"
        rf"(?:can|could|would|will) you(?:\s+please)?|"
        rf"please|go ahead and"
        rf")\s+({command_pattern})",
        clean_voice_command(cleaned),
        re.I,
    )
    if wrapped:
        return clean_voice_command(wrapped.group(1))
    direct_storage_command = re.fullmatch(
        r"(?:clear(?: all| the)? lights?|"
        r"(?:turn|shut)(?: all| the)? lights? off)",
        clean_voice_command(cleaned),
        re.I,
    )
    if direct_storage_command:
        return clean_voice_command(direct_storage_command.group(0))
    # A mangled wake word is often emitted as its own short sentence, for
    # example "bag five. Put away M4 button head screw."
    prefixed = re.fullmatch(
        rf"[A-Za-z][A-Za-z ]{{0,24}}[.!?]\s*({command_pattern})[.!?]?",
        cleaned,
        re.I,
    )
    return clean_voice_command(prefixed.group(1)) if prefixed else None


def is_prompt_echo(transcript: str) -> bool:
    cleaned = re.sub(r"\s+", " ", transcript).strip().lower()
    return (
        len(cleaned) > MAX_VOICE_TRANSCRIPT_CHARS
        or len(cleaned.split()) > MAX_VOICE_TRANSCRIPT_WORDS
        or cleaned.count(",") > 20
        or "storage cabinet voice commands" in cleaned
        or ("common phrases:" in cleaned and "hardware terms:" in cleaned)
        or ("storage cabinet assistant" in cleaned and "preserve exact hardware names" in cleaned)
        or "known inventory vocabulary:" in cleaned
    )


def is_filler_transcript(transcript: str) -> bool:
    cleaned = clean_voice_command(transcript).lower()
    return bool(
        cleaned
        and re.fullmatch(
            r"(?:ah+|uh+|um+|hmm+|hm+|er+|erm+|ha(?:\s+ha)*)",
            cleaned,
        )
    )


def transcript_command(
    transcript: str,
    wake_word: str = DEFAULT_WAKE_WORD,
    *,
    awaiting_command: bool = False,
) -> TranscriptCommand:
    cleaned = re.sub(r"\s+", " ", transcript).strip()
    if not cleaned:
        return TranscriptCommand(command=None, wake_heard=False, awaiting_command=awaiting_command)
    if is_prompt_echo(cleaned):
        return TranscriptCommand(command=None, wake_heard=False, awaiting_command=False)
    if awaiting_command:
        wake_re = rf"\b(?:{wake_word_pattern(wake_word)})\b"
        wake_heard = bool(re.search(wake_re, cleaned, re.I))
        command = strip_wake_word(cleaned, wake_word) if wake_heard else clean_voice_command(cleaned)
        return TranscriptCommand(
            command=command or None,
            wake_heard=wake_heard,
            awaiting_command=False,
        )
    wake_re = rf"\b(?:{wake_word_pattern(wake_word)})\b"
    wake_heard = bool(re.search(wake_re, cleaned, re.I))
    command = (
        strip_wake_word(cleaned, wake_word)
        if wake_heard
        else recover_storage_command_without_wake(cleaned)
    )
    # This is an inferred activation rather than a literal wake-word match,
    # but downstream routing and diagnostics should treat it as intentional.
    wake_heard = wake_heard or bool(command)
    return TranscriptCommand(
        command=command,
        wake_heard=wake_heard,
        awaiting_command=wake_heard and not command,
    )


def route_voice_command(command: str) -> tuple[str, str]:
    cleaned = normalize_spoken_hardware_terms(command)
    if re.fullmatch(
        r"(?:confirm|apply|yes|yep|yeah|sure|do it|looks good|go ahead|"
        r"that(?:'s| is) right|correct|okay|ok)",
        cleaned,
        re.I,
    ):
        return "/assistant/voice/confirm", "confirm"
    if re.fullmatch(
        r"(?:cancel(?: it| that)?|no|nope|never mind|nevermind|stop|"
        r"don(?:'t| not) do it|do not do it)",
        cleaned,
        re.I,
    ):
        return "/assistant/voice/cancel", "cancel"
    if re.fullmatch(r"(?:undo|undo that|revert|go back)", cleaned, re.I):
        return "/assistant/undo", "undo"
    mount_putaway = re.match(
        r"^mount\s+(.+\b(?:in|into)\b.+\b(?:bin|drawer|box|container|cabinet))$",
        cleaned,
        re.I,
    )
    if mount_putaway:
        return "/assistant/chat", f"put away {mount_putaway.group(1)}"
    find_match = re.match(
        r"^(?:finds?|fine|where(?:'s| is| are)?|show me|locate|get me|grab(?: me)?|i need)\s+(.+)$",
        cleaned,
        re.I,
    )
    if find_match:
        return "/assistant/chat", f"find {normalize_spoken_hardware_terms(find_match.group(1))}"
    put_match = re.match(
        r"^(?:put away|put|store|file|stash|stow)\s+(.+)$",
        cleaned,
        re.I,
    )
    if put_match:
        return "/assistant/chat", f"put away {normalize_spoken_hardware_terms(put_match.group(1))}"
    move_putaway = re.match(
        r"^(?:(?:let(?:'s| us)|please|can you|could you)\s+)?"
        r"(?:move|relocate)\s+(?:this|that|these|those)\s+(.+)$",
        cleaned,
        re.I,
    )
    if move_putaway:
        return (
            "/assistant/chat",
            f"put away {normalize_spoken_hardware_terms(move_putaway.group(1))}",
        )
    if re.match(r"^(?:move|relocate|consolidate|organize|sort)\b", cleaned, re.I):
        return VOICE_SCOPE_ROUTE, cleaned
    if re.match(r"^(?:light|illuminate)\b", cleaned, re.I):
        return "/assistant/chat", cleaned
    if re.match(
        r"^(?:use|actually|instead|put (?:it|them)|"
        r"(?:the )?(?:bigger|smaller|larger|large|medium|small)\b)",
        cleaned,
        re.I,
    ):
        return "/assistant/chat", cleaned
    if re.fullmatch(
        r"(?:clear(?: all| the)? lights?|(?:turn|shut)(?: all| the)? lights? off)",
        cleaned,
        re.I,
    ):
        return "/assistant/chat", cleaned
    return VOICE_SCOPE_ROUTE, cleaned


def is_plausible_voice_follow_up(transcript: str) -> bool:
    """Keep an open follow-up window from routing unrelated room conversation."""
    cleaned = clean_voice_command(transcript).lower()
    if not cleaned:
        return False
    if is_voice_confirmation(cleaned) or is_voice_revision(cleaned):
        return True
    if re.match(
        r"^(?:confirm|apply|cancel|never mind|nevermind|stop|undo|revert|"
        r"find|where|show|locate|get|grab|put|store|file|stash|stow|move|relocate|"
        r"use|light|illuminate|clear)\b",
        cleaned,
        re.I,
    ):
        return True
    if re.match(r"^(?:no|actually|instead)\b", cleaned, re.I) and re.search(
        r"\b(?:bin|drawer|cabinet|box|container|location|item|photo|rule|light|"
        r"bigger|smaller|large|medium|small|put|store|move|find|use)\b",
        cleaned,
        re.I,
    ):
        return True
    return bool(
        re.fullmatch(
            r"(?:the\s+)?(?:bigger|smaller|larger|large|medium|small)\s+(?:one|bin|drawer|box)",
            cleaned,
            re.I,
        )
    )


def is_voice_confirmation(transcript: str) -> bool:
    """Accept only the short responses expected after a proposed plan."""
    route, _query = route_voice_command(clean_voice_command(transcript))
    return route in {"/assistant/voice/confirm", "/assistant/voice/cancel"}


def is_voice_revision(transcript: str) -> bool:
    """Accept a narrow correction vocabulary while a proposal is pending."""
    cleaned = clean_voice_command(transcript).lower()
    if not cleaned or is_voice_confirmation(cleaned):
        return False
    return bool(
        re.match(
            r"^(?:no[,.]?\s+|actually\s+|instead\s+|"
            r"use\s+|put\s+(?:it|them)\s+|"
            r"(?:the\s+)?(?:bigger|smaller|larger|large|medium|small)\b)",
            cleaned,
            re.I,
        )
        or re.fullmatch(
            r"(?:(?:not|wrong)\s+(?:(?:that|this|the)\s+)?"
            r"(?:one|bin|drawer|box|container|location|item)|"
            r"(?:different|another|wrong)\s+"
            r"(?:one|bin|drawer|box|container|location|item)|"
            r"(?:pick|choose|try|find)\s+(?:a\s+)?(?:different|another)\s+"
            r"(?:one|bin|drawer|box|container|location)|"
            r"(?:that|this|the)\s+(?:bin|drawer|box|container|location)\s+"
            r"(?:is\s+)?(?:full|occupied|wrong|too\s+(?:small|big)))",
            cleaned,
            re.I,
        )
    )


def command_needs_continuation(command: str) -> bool:
    """Hold likely clipped storage commands for one more recording window."""
    match = re.match(
        r"^(put away|put|store|file|stash|stow)\s+(.+)$",
        clean_voice_command(command),
        re.I,
    )
    if not match:
        return False
    target = clean_voice_command(match.group(2))
    words = target.split()
    if not words:
        return True
    if words[-1].lower() in {"a", "an", "the", "this", "that", "these", "those", "in", "into", "to"}:
        return True
    return len(words) == 1 and len(words[0]) <= 3 and not re.fullmatch(r"M\d+", words[0], re.I)


def merge_voice_continuation(command: str, continuation: str) -> str:
    cleaned_continuation = clean_voice_command(continuation)
    if not cleaned_continuation:
        return clean_voice_command(command)
    match = re.match(
        r"^(put away|put|store|file|stash|stow)\s+(.+)$",
        clean_voice_command(command),
        re.I,
    )
    if not match:
        return clean_voice_command(f"{command} {cleaned_continuation}")
    target = clean_voice_command(match.group(2))
    # A tiny trailing token is usually the first clipped syllable of the next clip.
    if len(target.split()) == 1 and len(target) <= 3 and not re.fullmatch(r"M\d+", target, re.I):
        return f"{match.group(1)} {cleaned_continuation}"
    return clean_voice_command(f"{command} {cleaned_continuation}")


def response_to_speech(response: dict[str, Any]) -> str:
    message = str(response.get("message") or "").strip()
    has_plan = _response_has_pending_plan(response)
    putaway_proposal = _putaway_proposal_speech(response) if has_plan else ""
    if putaway_proposal:
        return putaway_proposal
    if message:
        if has_plan:
            return (
                f"{message} Do you want me to confirm it? "
                "Say yes, no, or what to change."
            )
        if response.get("action") == "found":
            return f"{message} I lit it."
        return message

    results = response.get("results")
    if isinstance(results, list) and results:
        first = results[0]
        if isinstance(first, dict):
            name = first.get("name") or "that"
            path = first.get("path")
            if isinstance(path, list):
                labels = [str(p.get("label")) for p in path if isinstance(p, dict) and p.get("label")]
                if labels:
                    return f"{name} is in {'; '.join(labels)}. I lit it."
            return f"I found {name}. I lit it."

    if has_plan:
        return (
            "I have a proposed plan. Do you want me to confirm it? "
            "Say yes, no, or what to change."
        )

    return "I heard you, but I do not have a clear answer yet."


def _putaway_proposal_speech(response: dict[str, Any]) -> str:
    candidates: list[dict[str, Any]] = [response]
    for key in ("decision", "raw"):
        nested = response.get(key)
        if isinstance(nested, dict):
            candidates.append(nested)
            decision = nested.get("decision")
            if isinstance(decision, dict):
                candidates.append(decision)
    proposal = next(
        (candidate for candidate in candidates if candidate.get("action") == "put_away"),
        None,
    )
    if proposal is None:
        return ""
    item = proposal.get("item")
    item_name = str(item.get("name") or "That") if isinstance(item, dict) else "That"
    destination = proposal.get("destination")
    destination_name = "the suggested spot"
    if isinstance(destination, dict):
        path = destination.get("path")
        if isinstance(path, list) and path:
            leaf = path[-1]
            if isinstance(leaf, dict) and leaf.get("label"):
                destination_name = str(leaf["label"])
        elif destination.get("id"):
            destination_name = str(destination["id"])
    light_status = (
        "I lit it."
        if _response_lit(response)
        else "I could not light that location."
    )
    return f"{item_name} goes in {destination_name}. {light_status} Say yes to confirm."


def _response_lit(response: dict[str, Any]) -> bool:
    candidates: list[dict[str, Any]] = [response]
    for key in ("decision", "raw", "automation"):
        nested = response.get(key)
        if isinstance(nested, dict):
            candidates.append(nested)
            for nested_key in ("decision", "automation"):
                deeper = nested.get(nested_key)
                if isinstance(deeper, dict):
                    candidates.append(deeper)
    return any(candidate.get("lit") is True for candidate in candidates)


def _response_has_pending_plan(response: dict[str, Any]) -> bool:
    candidates: list[dict[str, Any]] = [response]
    for key in ("decision", "raw"):
        nested = response.get(key)
        if not isinstance(nested, dict):
            continue
        candidates.append(nested)
        decision = nested.get("decision")
        if isinstance(decision, dict):
            candidates.append(decision)
    return any(isinstance(candidate.get("plan"), list) and candidate["plan"] for candidate in candidates)


def collect_vad_utterance(
    frames: Any,
    vad: Any,
    *,
    frame_ms: int = DEFAULT_VAD_FRAME_MS,
    pre_roll_ms: int = DEFAULT_VAD_PRE_ROLL_MS,
    start_timeout_seconds: float = DEFAULT_VAD_START_TIMEOUT_SECONDS,
    silence_seconds: float = DEFAULT_VAD_SILENCE_SECONDS,
    max_utterance_seconds: float = DEFAULT_MAX_UTTERANCE_SECONDS,
    min_frame_rms: float = 0.0,
    on_speech_start: Callable[[], None] | None = None,
) -> tuple[bytes, bool]:
    """Collect one speech-shaped utterance from fixed-size PCM frames."""
    pre_roll = deque(maxlen=max(1, math.ceil(pre_roll_ms / frame_ms)) + 5)
    recent_voice = deque(maxlen=5)
    start_limit = max(1, math.ceil(start_timeout_seconds * 1000 / frame_ms))
    silence_limit = max(1, math.ceil(silence_seconds * 1000 / frame_ms))
    utterance_limit = max(1, math.ceil(max_utterance_seconds * 1000 / frame_ms))
    captured: list[bytes] = []
    started = False
    trailing_silence = 0
    frames_seen = 0

    for frame in frames:
        if not frame:
            break
        frames_seen += 1
        samples = array.array("h")
        samples.frombytes(frame)
        frame_rms = (
            math.sqrt(sum(sample * sample for sample in samples) / len(samples))
            if samples
            else 0.0
        )
        voiced = bool(vad.is_speech(frame, 16000)) and frame_rms >= min_frame_rms
        if not started:
            pre_roll.append(frame)
            recent_voice.append(voiced)
            if sum(recent_voice) >= 3:
                started = True
                captured.extend(pre_roll)
                if on_speech_start is not None:
                    on_speech_start()
            elif frames_seen >= start_limit:
                break
            continue

        captured.append(frame)
        trailing_silence = 0 if voiced else trailing_silence + 1
        if trailing_silence >= silence_limit or len(captured) >= utterance_limit:
            break

    if not captured:
        captured.extend(pre_roll)
    return b"".join(captured), started


def record_clip(path: Path, config: VoiceConfig) -> bool:
    """Record one complete utterance, waiting for speech and stopping on silence."""
    sample_rate = 16000
    frame_ms = DEFAULT_VAD_FRAME_MS
    input_channels = max(1, int(config.input_channels))
    input_channel = max(0, min(int(config.input_channel), input_channels - 1))
    mono_frame_bytes = sample_rate * frame_ms // 1000 * 2
    input_frame_bytes = mono_frame_bytes * input_channels
    process = subprocess.Popen(
        [
            "arecord",
            "-q",
            "-D",
            config.input_device,
            "-f",
            "S16_LE",
            "-r",
            str(sample_rate),
            "-c",
            str(input_channels),
            "-t",
            "raw",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdout is not None
    vad = webrtcvad.Vad(max(0, min(3, int(config.vad_aggressiveness))))

    def frames():
        while True:
            frame = process.stdout.read(input_frame_bytes)
            if len(frame) != input_frame_bytes:
                break
            if input_channels == 1:
                yield frame
                continue
            samples = array.array("h")
            samples.frombytes(frame)
            yield array.array("h", samples[input_channel::input_channels]).tobytes()

    try:
        pcm, speech_detected = collect_vad_utterance(
            frames(),
            vad,
            frame_ms=frame_ms,
            start_timeout_seconds=config.vad_start_timeout_seconds,
            silence_seconds=config.vad_silence_seconds,
            max_utterance_seconds=config.max_utterance_seconds,
            min_frame_rms=max(0.0, config.vad_frame_threshold),
            on_speech_start=lambda: post_voice_state(
                config,
                "listening",
                "Listening",
            ),
        )
    finally:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)

    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(sample_rate)
        audio.writeframes(pcm)
    return speech_detected


def resolve_wake_model_path(config: VoiceConfig) -> Path:
    path = Path(config.vosk_model_path).expanduser()
    return path if path.is_absolute() else Path.cwd() / path


def validate_vosk_model(config: VoiceConfig) -> None:
    model_path = resolve_wake_model_path(config)
    if not (model_path / "conf/model.conf").is_file():
        raise ValueError(
            f"Vosk wake model not found: {model_path}. "
            "Run scripts/install-pi5-voice-service.sh to install it."
        )


def validate_local_wake_configuration(config: VoiceConfig) -> None:
    if config.local_wake_enabled:
        if not 0.0 <= config.local_wake_min_confidence <= 1.0:
            raise ValueError("local wake confidence must be between 0 and 1")
        validate_vosk_model(config)


@lru_cache(maxsize=2)
def load_vosk_model(model_path: str) -> Any:
    try:
        import vosk

        vosk.SetLogLevel(-1)
        return vosk.Model(model_path)
    except (ImportError, OSError, RuntimeError) as exc:
        raise RuntimeError(f"could not initialize Vosk wake detection: {exc}") from exc


def detect_local_wake(path: Path, config: VoiceConfig) -> LocalWakeDetection:
    if not config.local_wake_enabled:
        return LocalWakeDetection(False)
    validate_local_wake_configuration(config)
    try:
        with wave.open(str(path), "rb") as audio:
            if (
                audio.getsampwidth() != 2
                or audio.getnchannels() != 1
                or audio.getframerate() != 16000
            ):
                return LocalWakeDetection(False)
            samples = array.array("h")
            samples.frombytes(audio.readframes(audio.getnframes()))
    except (OSError, EOFError, wave.Error):
        return LocalWakeDetection(False)
    if not samples:
        return LocalWakeDetection(False)
    try:
        import vosk

        model = load_vosk_model(str(resolve_wake_model_path(config)))
        phrases = wake_word_phrases(config.wake_word)
        recognizer = vosk.KaldiRecognizer(
            model,
            16000,
            json.dumps([*phrases, "[unk]"]),
        )
        recognizer.SetWords(True)
        payloads: list[dict[str, Any]] = []
        pcm = samples.tobytes()
        for start in range(0, len(pcm), 8000):
            if recognizer.AcceptWaveform(pcm[start : start + 8000]):
                payloads.append(json.loads(recognizer.Result()))
        payloads.append(json.loads(recognizer.FinalResult()))
    except (ImportError, OSError, RuntimeError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Vosk wake detection failed: {exc}") from exc
    phrase_words = sorted(
        (phrase.split() for phrase in phrases),
        key=len,
        reverse=True,
    )
    for payload in payloads:
        words = payload.get("result") or []
        spoken = [str(item.get("word") or "").casefold() for item in words]
        for target_words in phrase_words:
            for start in range(0, len(spoken) - len(target_words) + 1):
                if spoken[start : start + len(target_words)] != target_words:
                    continue
                matched = words[start : start + len(target_words)]
                start_frame = math.floor(float(matched[0].get("start") or 0) * 100)
                end_frame = math.ceil(float(matched[-1].get("end") or 0) * 100)
                confidence = min(float(item.get("conf") or 0) for item in matched)
                if confidence < config.local_wake_min_confidence:
                    continue
                return LocalWakeDetection(True, start_frame, end_frame, confidence)
    return LocalWakeDetection(False)


def transcribe_local_vosk(path: Path, config: VoiceConfig) -> str:
    config.runtime_stats.local_stt_calls += 1
    validate_vosk_model(config)
    try:
        import vosk

        with wave.open(str(path), "rb") as audio:
            if (
                audio.getsampwidth() != 2
                or audio.getnchannels() != 1
                or audio.getframerate() != 16000
            ):
                raise ValueError("local transcription requires 16 kHz mono PCM audio")
            model = load_vosk_model(str(resolve_wake_model_path(config)))
            recognizer = vosk.KaldiRecognizer(model, 16000)
            transcripts: list[str] = []
            while chunk := audio.readframes(4000):
                if recognizer.AcceptWaveform(chunk):
                    result = json.loads(recognizer.Result())
                    if text := str(result.get("text") or "").strip():
                        transcripts.append(text)
            result = json.loads(recognizer.FinalResult())
            if text := str(result.get("text") or "").strip():
                transcripts.append(text)
    except (ImportError, OSError, RuntimeError, ValueError, json.JSONDecodeError, wave.Error) as exc:
        raise RuntimeError(f"Vosk local transcription failed: {exc}") from exc
    return normalize_spoken_hardware_terms(" ".join(transcripts))


def write_command_audio_after_wake(
    source: Path,
    target: Path,
    detection: LocalWakeDetection,
) -> float:
    """Write command-only WAV audio and return its duration in seconds."""
    with wave.open(str(source), "rb") as audio:
        channels = audio.getnchannels()
        sample_width = audio.getsampwidth()
        sample_rate = audio.getframerate()
        frames = audio.readframes(audio.getnframes())
    frame_bytes = max(1, channels * sample_width)
    # Wake timing uses 100 frames/second. Move one frame beyond the keyword so
    # its final phoneme does not leak into command transcription.
    start_sample = max(0, math.ceil((detection.end_frame + 1) * sample_rate / 100))
    command_pcm = frames[start_sample * frame_bytes :]
    with wave.open(str(target), "wb") as audio:
        audio.setnchannels(channels)
        audio.setsampwidth(sample_width)
        audio.setframerate(sample_rate)
        audio.writeframes(command_pcm)
    return len(command_pcm) / frame_bytes / sample_rate if sample_rate else 0.0


def clip_rms(path: Path) -> float:
    with wave.open(str(path), "rb") as audio:
        if audio.getsampwidth() != 2:
            return 0.0
        frames = audio.readframes(audio.getnframes())
    if not frames:
        return 0.0
    sample_count = len(frames) // 2
    total = 0
    for index in range(0, len(frames), 2):
        sample = int.from_bytes(frames[index : index + 2], byteorder="little", signed=True)
        total += sample * sample
    return math.sqrt(total / sample_count)


def clip_duration_seconds(path: Path) -> float:
    try:
        with wave.open(str(path), "rb") as audio:
            frame_rate = audio.getframerate()
            return audio.getnframes() / frame_rate if frame_rate > 0 else 0.0
    except (OSError, EOFError, wave.Error):
        return 0.0


def clip_activity_rms(
    path: Path,
    *,
    window_ms: int = 250,
    percentile: float = 0.9,
) -> float:
    """Return sustained short-window energy without letting one click trigger STT."""
    with wave.open(str(path), "rb") as audio:
        if audio.getsampwidth() != 2 or audio.getnchannels() != 1:
            return 0.0
        frame_rate = audio.getframerate()
        frames = audio.readframes(audio.getnframes())
    if not frames or frame_rate <= 0:
        return 0.0
    samples = array.array("h")
    samples.frombytes(frames)
    if os.sys.byteorder != "little":
        samples.byteswap()
    window_samples = max(1, round(frame_rate * max(1, window_ms) / 1000))
    window_levels: list[float] = []
    for start in range(0, len(samples), window_samples):
        window = samples[start : start + window_samples]
        if len(window) < window_samples // 2:
            continue
        window_levels.append(math.sqrt(sum(sample * sample for sample in window) / len(window)))
    if not window_levels:
        return 0.0
    window_levels.sort()
    clean_percentile = min(1.0, max(0.0, percentile))
    index = max(0, math.ceil(clean_percentile * len(window_levels)) - 1)
    return window_levels[index]


def clip_has_audio(path: Path, config: VoiceConfig) -> bool:
    if config.audio_threshold <= 0:
        return True
    activity_rms = clip_activity_rms(path)
    if activity_rms >= config.audio_threshold:
        return True
    print(f"quiet: rms={clip_rms(path):.0f} activity_rms={activity_rms:.0f}", flush=True)
    return False


def inventory_stt_vocabulary(config: VoiceConfig) -> str:
    request = urllib.request.Request(
        f"{config.api_url.rstrip('/')}/export/inventory.json",
        headers={
            "Accept": "application/json",
            **_magpie_api_headers(config, content_type=False),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=2) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        return ""
    inventory = payload.get("inventory") if isinstance(payload, dict) else None
    if not isinstance(inventory, list):
        return ""
    terms: list[str] = []
    seen: set[str] = set()
    for item in inventory:
        if not isinstance(item, dict):
            continue
        candidates = [item.get("name")]
        aliases = item.get("aliases")
        if isinstance(aliases, list):
            candidates.extend(aliases[:3])
        for candidate in candidates:
            term = re.sub(r"\s+", " ", str(candidate or "")).strip(" ,;.")
            key = term.casefold()
            if not term or key in seen:
                continue
            seen.add(key)
            terms.append(term)
    return "; ".join(terms)[:MAX_STT_VOCABULARY_CHARS].rstrip(" ;")


def stt_prompt(config: VoiceConfig) -> str:
    configured = os.environ.get("MAGPIE_STT_PROMPT")
    if configured is not None:
        return configured.strip()
    if os.environ.get("MAGPIE_STT_INCLUDE_INVENTORY_VOCABULARY", "").strip().lower() not in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return DEFAULT_STT_PROMPT
    vocabulary = inventory_stt_vocabulary(config)
    if not vocabulary:
        return DEFAULT_STT_PROMPT
    return f"{DEFAULT_STT_PROMPT} Known inventory vocabulary: {vocabulary}."


@lru_cache(maxsize=8)
def _persistent_stt_failure_cooldown(state_path: str) -> LlmFailureCooldown:
    return LlmFailureCooldown(state_path=state_path)


def _stt_failure_cooldown(state_path: str) -> LlmFailureCooldown:
    if not state_path:
        return LlmFailureCooldown()
    return _persistent_stt_failure_cooldown(state_path)


def _stt_endpoint_id(config: VoiceConfig) -> str:
    key_fingerprint = hashlib.sha256(config.openai_api_key.encode()).hexdigest()[:12]
    return f"openai-audio:{key_fingerprint}"


def voice_capabilities(config: VoiceConfig) -> dict[str, object]:
    model_path = resolve_wake_model_path(config)
    cooldown = _stt_failure_cooldown(config.stt_failure_state_path)
    provider_failure = cooldown.snapshot().get(_stt_endpoint_id(config)) or {}
    runtime = config.runtime_stats.snapshot()
    return {
        "wake_gate": "local" if config.local_wake_enabled else "cloud",
        "local_wake_min_confidence": config.local_wake_min_confidence,
        "audio_before_wake_uploaded": not config.local_wake_enabled,
        "no_wake_follow_up": (
            "local_confirmation_only" if config.local_wake_enabled else "cloud"
        ),
        "metrics_since": runtime["started_at"],
        **{key: value for key, value in runtime.items() if key != "started_at"},
        "cloud_audio_available": not bool(provider_failure),
        "cloud_audio_failure": str(provider_failure.get("category") or ""),
        "cloud_audio_retry_seconds": int(provider_failure.get("retry_in_seconds") or 0),
        "local_stt_enabled": bool(config.local_stt_fallback_enabled),
        "local_stt_available": bool(
            config.local_stt_fallback_enabled
            and (model_path / "conf/model.conf").is_file()
        ),
        "local_tts_enabled": bool(config.local_tts_fallback_enabled),
        "local_tts_available": bool(
            config.local_tts_fallback_enabled
            and (shutil.which("espeak-ng") or shutil.which("espeak"))
        ),
        "stt_model": config.stt_model,
        "stt_fallback_model": config.stt_fallback_model,
        "output_device": config.output_device,
    }


def _openai_http_error(exc: urllib.error.HTTPError) -> LlmError:
    detail = ""
    try:
        raw = exc.read().decode("utf-8", errors="replace")
        payload = json.loads(raw)
        error = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(error, dict):
            detail = " ".join(
                str(error.get(field) or "").strip()
                for field in ("code", "type", "message")
            ).strip()
        else:
            detail = raw.strip()
    except (OSError, json.JSONDecodeError):
        detail = str(exc.reason or "")
    category = llm_error_category(exc.code, detail)
    error = LlmError(
        f"OpenAI transcription unavailable ({category})",
        category=category,
        status_code=exc.code,
    )
    retry_after = exc.headers.get("Retry-After") if exc.headers else None
    try:
        error.retry_after = float(retry_after) if retry_after else None
    except ValueError:
        error.retry_after = None
    return error


def _local_stt_or_raise(
    path: Path,
    config: VoiceConfig,
    cloud_error: BaseException,
) -> tuple[str, str]:
    if config.local_stt_fallback_enabled:
        try:
            transcript = transcribe_local_vosk(path, config)
        except RuntimeError as exc:
            print(f"local STT fallback unavailable: {exc}", flush=True)
        else:
            # An empty local result is valid silence. Do not turn background
            # noise during the follow-up window into a visible cloud-quota error.
            return transcript, LOCAL_STT_MODEL
    raise cloud_error


def transcribe(path: Path, config: VoiceConfig, *, model: str | None = None) -> str:
    boundary = f"----magpie-{uuid.uuid4().hex}"
    parts: list[bytes] = []

    def add_field(name: str, value: str) -> None:
        parts.extend(
            [
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                value.encode(),
                b"\r\n",
            ]
        )

    def add_file(name: str, filename: str, data: bytes) -> None:
        parts.extend(
            [
                f"--{boundary}\r\n".encode(),
                (
                    f'Content-Disposition: form-data; name="{name}"; '
                    f'filename="{filename}"\r\n'
                ).encode(),
                b"Content-Type: audio/wav\r\n\r\n",
                data,
                b"\r\n",
            ]
        )

    selected_model = model or config.stt_model
    add_field("model", selected_model)
    if language := os.environ.get("MAGPIE_STT_LANGUAGE", DEFAULT_STT_LANGUAGE).strip():
        add_field("language", language)
    if selected_model.startswith("gpt-4o-"):
        chunking_strategy = os.environ.get(
            "MAGPIE_STT_CHUNKING_STRATEGY",
            DEFAULT_STT_CHUNKING_STRATEGY,
        ).strip()
        if chunking_strategy:
            add_field("chunking_strategy", chunking_strategy)
    if prompt := stt_prompt(config):
        add_field("prompt", prompt)
    add_file("file", path.name, path.read_bytes())
    parts.append(f"--{boundary}--\r\n".encode())
    body = b"".join(parts)
    request = urllib.request.Request(
        "https://api.openai.com/v1/audio/transcriptions",
        data=body,
        headers={
            "Authorization": f"Bearer {config.openai_api_key}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        method="POST",
    )
    config.runtime_stats.cloud_stt_calls += 1
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise _openai_http_error(exc) from exc
    return str(payload.get("text") or "").strip()


def transcribe_with_fallback(path: Path, config: VoiceConfig) -> tuple[str, str]:
    cooldown = _stt_failure_cooldown(config.stt_failure_state_path)
    endpoint_id = _stt_endpoint_id(config)
    if blocked := cooldown.blocked(endpoint_id):
        return _local_stt_or_raise(path, config, blocked)
    try:
        transcript = transcribe(path, config)
    except (LlmError, urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        if isinstance(exc, LlmError):
            cooldown.failed(endpoint_id, exc)
            if exc.category in {"authentication", "quota_exhausted"}:
                return _local_stt_or_raise(path, config, exc)
        if not config.stt_fallback_model or config.stt_fallback_model == config.stt_model:
            raise
        status_code = exc.status_code if isinstance(exc, LlmError) else getattr(exc, "code", None)
        if status_code == 429 or (status_code or 0) >= 500:
            retry_after = getattr(exc, "retry_after", None)
            if retry_after is None and isinstance(exc, urllib.error.HTTPError):
                header = exc.headers.get("Retry-After") if exc.headers else None
                try:
                    retry_after = float(header) if header else None
                except ValueError:
                    retry_after = None
            delay = retry_after if retry_after is not None else DEFAULT_STT_RETRY_SECONDS
            time.sleep(max(0.0, min(delay, MAX_STT_RETRY_SECONDS)))
        try:
            fallback_transcript = transcribe(
                path,
                config,
                model=config.stt_fallback_model,
            )
        except LlmError as fallback_error:
            cooldown.failed(endpoint_id, fallback_error)
            return _local_stt_or_raise(path, config, fallback_error)
        cooldown.succeeded(endpoint_id)
        return fallback_transcript, config.stt_fallback_model
    cooldown.succeeded(endpoint_id)
    if transcript or not config.stt_fallback_model or config.stt_fallback_model == config.stt_model:
        return transcript, config.stt_model
    try:
        fallback_transcript = transcribe(path, config, model=config.stt_fallback_model)
    except LlmError as fallback_error:
        cooldown.failed(endpoint_id, fallback_error)
        return _local_stt_or_raise(path, config, fallback_error)
    cooldown.succeeded(endpoint_id)
    return fallback_transcript, config.stt_fallback_model


def transcript_contains_wake_word(transcript: str, wake_word: str) -> bool:
    return bool(re.search(rf"\b{re.escape(wake_word)}\b", transcript, re.IGNORECASE))


def retry_transcript_for_wake_word(
    path: Path,
    transcript: str,
    stt_model: str,
    config: VoiceConfig,
) -> tuple[str, str]:
    """Retry a command clip when the primary STT may have dropped its wake word."""
    fallback = config.stt_fallback_model
    if (
        not transcript
        or transcript_contains_wake_word(transcript, config.wake_word)
        or not fallback
        or fallback in {config.stt_model, stt_model}
    ):
        return transcript, stt_model
    alternate = transcribe(path, config, model=fallback)
    if transcript_contains_wake_word(alternate, config.wake_word):
        return alternate, fallback
    return transcript, stt_model


def _magpie_api_headers(
    config: VoiceConfig,
    *,
    content_type: bool,
) -> dict[str, str]:
    machine_headers = cloudflare_access_headers(
        str(getattr(config, "cf_access_client_id", "")),
        str(getattr(config, "cf_access_client_secret", "")),
    )
    headers = {
        **machine_headers,
        **({"Content-Type": "application/json"} if content_type else {}),
    }
    token = config.edge_token.strip()
    device_id = config.edge_device_id.strip().lower()
    parsed = urlsplit(config.api_url)
    if (token or machine_headers) and parsed.scheme != "https" and parsed.hostname not in {
        "127.0.0.1",
        "localhost",
        "::1",
    }:
        raise ValueError("the authenticated Magpie voice API must use HTTPS")
    if not token:
        return headers
    if not device_id:
        raise ValueError("MAGPIE_EDGE_DEVICE_ID is required with MAGPIE_EDGE_TOKEN")
    headers.update(
        {
            "Authorization": f"Bearer {token}",
            "X-Magpie-Device-ID": device_id,
        }
    )
    edge_workspace_id = str(getattr(config, "edge_workspace_id", "")).strip()
    if edge_workspace_id:
        headers["X-Magpie-Workspace"] = edge_workspace_id
    return headers


def ask_magpie(
    command: str,
    config: VoiceConfig,
    *,
    transcript: str = "",
    route: str = "",
    query: str = "",
    client_request_id: str = "",
) -> dict[str, Any]:
    if not route or not query:
        route, query = route_voice_command(command)
    scope_handoff = route == VOICE_SCOPE_ROUTE
    if scope_handoff:
        # The chat endpoint rejects out-of-scope voice work before model dispatch
        # and records the visual handoff so a connected kiosk can display it.
        route = "/assistant/chat"
    if not query:
        return {"message": "What should I look for?"}
    if route == "/assistant/chat" and not client_request_id:
        client_request_id = f"voice-{uuid.uuid4().hex}"
    payload = (
        None
        if route.startswith("/assistant/voice/") or route == "/assistant/undo"
        else json.dumps(
            {
                "text": query,
                "source": "voice",
                "voice_transcript": transcript,
                "voice_command": command,
                "voice_query": query,
                "stt_model": config.stt_model,
                **(
                    {"client_request_id": client_request_id}
                    if route == "/assistant/chat"
                    else {}
                ),
            }
        ).encode("utf-8")
    )
    request = urllib.request.Request(
        f"{config.api_url.rstrip('/')}{route}",
        data=payload,
        headers=_magpie_api_headers(config, content_type=payload is not None),
        method="POST",
    )
    config.runtime_stats.agent_requests += 1
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            error_payload = json.loads(exc.read().decode("utf-8"))
            detail = str(error_payload.get("detail") or error_payload.get("message") or "")
        except (AttributeError, UnicodeDecodeError, json.JSONDecodeError):
            pass
        if scope_handoff:
            return {
                "ok": True,
                "action": "voice_scope_ui_required",
                "message": VOICE_SCOPE_MESSAGE,
                "query": query,
                "storage_url": "/storage?view=places",
                "handoff_recorded": False,
            }
        raise RuntimeError(detail or f"Magpie API returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError):
        if scope_handoff:
            return {
                "ok": True,
                "action": "voice_scope_ui_required",
                "message": VOICE_SCOPE_MESSAGE,
                "query": query,
                "storage_url": "/storage?view=places",
                "handoff_recorded": False,
            }
        raise


def _voice_attempt_spool_path() -> Path:
    return Path(
        os.environ.get(
            "MAGPIE_VOICE_ATTEMPT_SPOOL",
            DEFAULT_VOICE_ATTEMPT_SPOOL_PATH,
        )
    ).expanduser()


def _read_spooled_voice_attempts(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    attempts: list[dict[str, Any]] = []
    for line in lines:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            attempts.append(value)
    return attempts[-MAX_SPOOLED_VOICE_ATTEMPTS:]


def _write_spooled_voice_attempts(path: Path, attempts: list[dict[str, Any]]) -> None:
    if not attempts:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    retained = attempts[-MAX_SPOOLED_VOICE_ATTEMPTS:]
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    serialized = (
        f"{json.dumps(attempt, separators=(',', ':'))}\n" for attempt in retained
    )
    temporary.write_text(
        "".join(serialized),
        encoding="utf-8",
    )
    temporary.replace(path)


def _send_voice_attempt(config: VoiceConfig, attempt: dict[str, Any]) -> None:
    payload = json.dumps(attempt).encode("utf-8")
    request = urllib.request.Request(
        f"{config.api_url.rstrip('/')}/voice/attempts",
        data=payload,
        headers=_magpie_api_headers(config, content_type=True),
        method="POST",
    )
    urllib.request.urlopen(request, timeout=5).close()


def post_voice_attempt(config: VoiceConfig, attempt: dict[str, Any]) -> None:
    if config.dry_run or not config.log_attempts:
        return
    spool_path = _voice_attempt_spool_path()
    attempts = [*_read_spooled_voice_attempts(spool_path), attempt]
    for index, pending in enumerate(attempts):
        try:
            _send_voice_attempt(config, pending)
        except Exception as exc:
            _write_spooled_voice_attempts(spool_path, attempts[index:])
            print(
                f"voice log error: {exc}; queued={len(attempts) - index}",
                flush=True,
            )
            return
    _write_spooled_voice_attempts(spool_path, [])


def post_voice_state(
    config: VoiceConfig,
    state: str,
    message: str,
    *,
    transcript: str = "",
    command: str = "",
) -> None:
    if config.dry_run:
        return
    payload = json.dumps(
        {
            "state": state,
            "message": message,
            "transcript": transcript,
            "command": command,
            "device_id": config.edge_device_id,
            "runtime_version": VOICE_RUNTIME.label(),
            "capabilities": voice_capabilities(config),
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{config.api_url.rstrip('/')}/voice/state",
        data=payload,
        headers=_magpie_api_headers(config, content_type=True),
        method="POST",
    )
    try:
        urllib.request.urlopen(request, timeout=2).close()
    except Exception as exc:
        print(f"voice state error: {exc}", flush=True)


def diagnose_voice_command(
    transcript: str,
    config: VoiceConfig,
    *,
    rms: float | None = None,
    activity_rms: float | None = None,
    call_backend: bool = False,
    awaiting_command: bool = False,
) -> dict[str, Any]:
    parsed = transcript_command(
        transcript,
        config.wake_word,
        awaiting_command=awaiting_command,
    )
    command = parsed.command
    route = ""
    query = ""
    response: dict[str, Any] | None = None
    speech = ""
    if command:
        route, query = route_voice_command(command)
        if call_backend and query:
            response = ask_magpie(
                command,
                config,
                transcript=transcript,
                route=route,
                query=query,
            )
            speech = response_to_speech(response)
        elif route == VOICE_SCOPE_ROUTE:
            speech = VOICE_SCOPE_MESSAGE
        elif query:
            speech = f"I would send {query} to {route}."
    return {
        "rms": rms,
        "activity_rms": activity_rms,
        "transcript": transcript,
        "wake_word": config.wake_word,
        "command": command,
        "wake_heard": parsed.wake_heard,
        "awaiting_command": parsed.awaiting_command,
        "route": route,
        "query": query,
        "would_call_backend": bool(call_backend and query),
        "response": response,
        "speech": speech,
    }


def attempt_from_diagnosis(
    diagnosis: dict[str, Any],
    config: VoiceConfig,
    *,
    error: str = "",
) -> dict[str, Any]:
    return {
        "rms": diagnosis.get("rms"),
        "activity_rms": diagnosis.get("activity_rms"),
        "transcript": str(diagnosis.get("transcript") or ""),
        "wake_word": config.wake_word,
        "wake_heard": bool(diagnosis.get("wake_heard")),
        "awaiting_command": bool(diagnosis.get("awaiting_command")),
        "command": str(diagnosis.get("command") or ""),
        "route": str(diagnosis.get("route") or ""),
        "query": str(diagnosis.get("query") or ""),
        "client_request_id": str(diagnosis.get("client_request_id") or ""),
        "stt_model": str(diagnosis.get("stt_model") or config.stt_model),
        "audio_threshold": config.audio_threshold,
        "device_id": config.edge_device_id,
        "runtime_version": VOICE_RUNTIME.label(),
        "backend_response": diagnosis.get("response") if isinstance(diagnosis.get("response"), dict) else None,
        "error": error,
    }


def assign_voice_client_request_id(
    diagnosis: dict[str, Any],
    state: VoiceLoopState,
    *,
    now: float | None = None,
) -> str:
    """Reuse one server idempotency key for an immediate repeated voice request."""
    if str(diagnosis.get("route") or "") != "/assistant/chat":
        diagnosis.pop("client_request_id", None)
        return ""
    signature = " ".join(str(diagnosis.get("query") or "").lower().split())
    if not signature:
        diagnosis.pop("client_request_id", None)
        return ""
    observed_at = time.monotonic() if now is None else float(now)
    replay = bool(
        state.last_client_request_id
        and state.last_request_signature == signature
        and observed_at - state.last_request_at <= VOICE_REQUEST_REPLAY_SECONDS
    )
    request_id = (
        state.last_client_request_id
        if replay
        else f"voice-{uuid.uuid4().hex}"
    )
    if not replay:
        state.last_request_signature = signature
        state.last_client_request_id = request_id
        state.last_request_at = observed_at
    diagnosis["client_request_id"] = request_id
    return request_id


def synthesize(text: str, path: Path, config: VoiceConfig) -> None:
    cooldown = _stt_failure_cooldown(config.stt_failure_state_path)
    endpoint_id = _stt_endpoint_id(config)
    if blocked := cooldown.blocked(endpoint_id):
        raise blocked
    payload = json.dumps(
        {
            "model": config.tts_model,
            "voice": config.tts_voice,
            "input": text,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        "https://api.openai.com/v1/audio/speech",
        data=payload,
        headers={
            "Authorization": f"Bearer {config.openai_api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    config.runtime_stats.cloud_tts_calls += 1
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            path.write_bytes(response.read())
    except urllib.error.HTTPError as exc:
        error = _openai_http_error(exc)
        cooldown.failed(endpoint_id, error)
        raise error from exc
    cooldown.succeeded(endpoint_id)


def synthesize_local(text: str, path: Path) -> None:
    executable = shutil.which("espeak-ng") or shutil.which("espeak")
    if not executable:
        raise RuntimeError("local speech requires espeak-ng or espeak")
    result = subprocess.run(
        [executable, "-v", "en-us", "-s", "165", "--stdout", text],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0 or not result.stdout:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"local speech synthesis failed: {detail or result.returncode}")
    path.write_bytes(result.stdout)


def play_acknowledgement(config: VoiceConfig) -> None:
    """Play a short local cue after wake detection without calling a service."""
    if config.dry_run:
        return
    sample_rate = 48000
    duration_seconds = 0.12
    amplitude = 3200
    samples = array.array("h")
    for index in range(round(sample_rate * duration_seconds)):
        envelope = min(1.0, index / 400, (sample_rate * duration_seconds - index) / 600)
        value = round(amplitude * envelope * math.sin(2 * math.pi * 880 * index / sample_rate))
        samples.extend((value, value))
    with tempfile.TemporaryDirectory(prefix="magpie-cue-") as tmp:
        path = Path(tmp) / "ack.wav"
        with wave.open(str(path), "wb") as audio:
            audio.setnchannels(2)
            audio.setsampwidth(2)
            audio.setframerate(sample_rate)
            audio.writeframes(samples.tobytes())
        devices = [config.output_device, "plughw:CARD=Array,DEV=0", "default"]
        for device in dict.fromkeys(device for device in devices if device):
            result = subprocess.run(
                ["aplay", "-q", "-D", device, str(path)],
                capture_output=True,
                check=False,
            )
            if result.returncode == 0:
                return
    print("voice acknowledgement playback failed", flush=True)


def speak(text: str, config: VoiceConfig) -> None:
    if config.dry_run:
        print(f"say: {text}", flush=True)
        return
    with tempfile.TemporaryDirectory(prefix="magpie-voice-") as tmp:
        audio_path = Path(tmp) / "speech-source"
        wav_path = Path(tmp) / "speech.wav"
        try:
            synthesize(text, audio_path, config)
        except (LlmError, urllib.error.URLError, TimeoutError, OSError) as cloud_error:
            if not config.local_tts_fallback_enabled:
                raise
            try:
                config.runtime_stats.local_tts_calls += 1
                synthesize_local(text, audio_path)
            except RuntimeError:
                raise cloud_error
        decode = subprocess.run(
            [
                "ffmpeg",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(audio_path),
                "-ar",
                "48000",
                "-ac",
                "2",
                "-c:a",
                "pcm_s16le",
                str(wav_path),
            ],
            capture_output=True,
            check=False,
        )
        if decode.returncode != 0:
            error = decode.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"TTS decode failed: {error or decode.returncode}")

        devices = [
            config.output_device,
            "plughw:CARD=Array,DEV=0",
            "plughw:0,0",
            "plughw:1,0",
            "default",
        ]
        errors: list[str] = []
        for device in dict.fromkeys(device for device in devices if device):
            playback = subprocess.run(
                ["aplay", "-q", "-D", device, str(wav_path)],
                capture_output=True,
                check=False,
            )
            if playback.returncode == 0:
                return
            detail = playback.stderr.decode("utf-8", errors="replace").strip()
            errors.append(f"{device}: {detail or f'exit {playback.returncode}'}")
        raise RuntimeError(f"audio playback failed ({'; '.join(errors)})")


def dispatch_voice_diagnosis(
    diagnosis: dict[str, Any],
    config: VoiceConfig,
) -> dict[str, Any]:
    command = str(diagnosis.get("command") or "")
    query = str(diagnosis.get("query") or "")
    print(f"command: {command}", flush=True)
    print(f"route: {diagnosis['route']} query: {query}", flush=True)
    post_voice_state(
        config,
        "processing",
        f"Processing: {command}",
        transcript=str(diagnosis.get("transcript") or ""),
        command=command,
    )
    response = (
        {"message": f"I heard: {command}"}
        if config.dry_run
        else ask_magpie(
            command,
            config,
            transcript=str(diagnosis.get("transcript") or ""),
            route=str(diagnosis["route"]),
            query=query,
            client_request_id=str(diagnosis.get("client_request_id") or ""),
        )
    )
    diagnosis["response"] = response
    speech = response_to_speech(response)
    diagnosis["speech"] = speech
    post_voice_attempt(config, attempt_from_diagnosis(diagnosis, config))
    post_voice_state(
        config,
        "done",
        speech,
        transcript=str(diagnosis.get("transcript") or ""),
        command=command,
    )
    speak(speech, config)
    return response


def arm_voice_follow_up(
    state: VoiceLoopState,
    config: VoiceConfig,
    response: dict[str, Any] | None,
) -> None:
    state.follow_up_until = (
        time.monotonic() + max(0.0, config.follow_up_seconds)
        if isinstance(response, dict) and _response_has_pending_plan(response)
        else 0.0
    )


def arm_command_window(state: VoiceLoopState, config: VoiceConfig) -> None:
    state.awaiting_command = True
    state.awaiting_command_until = time.monotonic() + max(0.0, config.follow_up_seconds)


def handle_clip(
    path: Path,
    config: VoiceConfig,
    *,
    awaiting_command: bool = False,
    state: VoiceLoopState | None = None,
    speech_detected: bool = False,
) -> bool:
    loop_state = state or VoiceLoopState(awaiting_command=awaiting_command)
    loop_state.awaiting_command = loop_state.awaiting_command or awaiting_command
    if (
        loop_state.awaiting_command
        and loop_state.awaiting_command_until > 0
        and time.monotonic() >= loop_state.awaiting_command_until
    ):
        print("wake command window expired", flush=True)
        wake_diagnosis = loop_state.wake_diagnosis
        loop_state.awaiting_command = False
        loop_state.awaiting_command_until = 0.0
        loop_state.wake_diagnosis = None
        loop_state.pending_diagnosis = None
        if wake_diagnosis is not None:
            timeout_attempt = attempt_from_diagnosis(
                wake_diagnosis,
                config,
                error="awaiting_command_timeout",
            )
            timeout_attempt["awaiting_command"] = False
            post_voice_attempt(config, timeout_attempt)
            post_voice_state(
                config,
                "error",
                "I did not hear a command. Say Magpie and try again.",
            )
    rms = clip_rms(path)
    activity_rms = clip_activity_rms(path)
    if (
        not speech_detected
        and config.audio_threshold > 0
        and activity_rms < config.audio_threshold
    ):
        print(f"quiet: rms={rms:.0f} activity_rms={activity_rms:.0f}", flush=True)
        if loop_state.pending_diagnosis is not None:
            pending = loop_state.pending_diagnosis
            loop_state.pending_diagnosis = None
            loop_state.awaiting_command = False
            loop_state.awaiting_command_until = 0.0
            command = str(pending.get("command") or "").strip()
            retry_message = (
                f"I only heard {command}. Please say the full command again."
                if command
                else "I did not hear the full command. Please try again."
            )
            print(f"incomplete command timed out: {command}", flush=True)
            post_voice_attempt(
                config,
                attempt_from_diagnosis(
                    pending,
                    config,
                    error="incomplete_command_timeout",
                ),
            )
            post_voice_state(
                config,
                "error",
                retry_message,
                transcript=str(pending.get("transcript") or ""),
                command=command,
            )
            speak(retry_message, config)
            return False
        # A normal VAD timeout is a listener heartbeat. The activation is
        # recorded once when a command arrives or the command window expires.
        return loop_state.awaiting_command
    config.runtime_stats.speech_clips += 1
    local_wake = LocalWakeDetection(False)
    try:
        local_wake = detect_local_wake(path, config)
        follow_up_active = time.monotonic() < loop_state.follow_up_until
        cloud_wake_required = bool(
            config.local_wake_enabled
            and not local_wake.detected
            and not loop_state.awaiting_command
            and loop_state.pending_diagnosis is None
            and not follow_up_active
        )
        if cloud_wake_required:
            config.runtime_stats.passive_speech_ignored += 1
            print("passive speech ignored: local wake not detected", flush=True)
            post_voice_state(config, "waiting", "Waiting for Magpie")
            return False
        transcription_path = path
        command_audio_seconds = 0.0
        if local_wake.detected:
            config.runtime_stats.wake_detections += 1
            transcription_path = path.with_name("command.wav")
            command_audio_seconds = write_command_audio_after_wake(
                path,
                transcription_path,
                local_wake,
            )
            print(
                f"local wake heard: frames={local_wake.start_frame}-{local_wake.end_frame} "
                f"command_audio={command_audio_seconds:.2f}s",
                flush=True,
            )
            post_voice_state(config, "heard", "Magpie heard. Processing command.")
            play_acknowledgement(config)
            if command_audio_seconds < 0.2:
                print("local wake heard; awaiting command", flush=True)
                diagnosis = {
                    "rms": rms,
                    "activity_rms": activity_rms,
                    "transcript": "",
                    "wake_word": config.wake_word,
                    "command": None,
                    "wake_heard": True,
                    "awaiting_command": True,
                    "route": "",
                    "query": "",
                    "stt_model": config.stt_model,
                }
                loop_state.wake_diagnosis = diagnosis
                post_voice_state(
                    config,
                    "listening",
                    "Magpie heard. Say what to find or put away.",
                )
                arm_command_window(loop_state, config)
                return True
        local_confirmation_active = bool(
            follow_up_active
            and config.local_wake_enabled
            and not local_wake.detected
            and not loop_state.awaiting_command
            and loop_state.pending_diagnosis is None
        )
        if local_confirmation_active:
            try:
                transcript = transcribe_local_vosk(transcription_path, config)
            except (RuntimeError, ValueError) as exc:
                print(f"local confirmation unavailable: {exc}", flush=True)
                config.runtime_stats.passive_speech_ignored += 1
                post_voice_state(
                    config,
                    "listening",
                    "Say yes, no, or describe what should change.",
                )
                return False
            stt_model = LOCAL_STT_MODEL
            if not (
                is_voice_confirmation(transcript)
                or is_voice_revision(transcript)
            ):
                config.runtime_stats.passive_speech_ignored += 1
                print(f"confirmation speech ignored locally: {transcript}", flush=True)
                post_voice_attempt(
                    config,
                    {
                        "rms": rms,
                        "activity_rms": activity_rms,
                        "transcript": transcript,
                        "wake_word": config.wake_word,
                        "wake_heard": False,
                        "awaiting_command": False,
                        "command": "",
                        "route": "",
                        "query": "",
                        "stt_model": stt_model,
                        "audio_threshold": config.audio_threshold,
                        "backend_response": None,
                        "error": "unrelated_local_confirmation",
                    },
                )
                post_voice_state(
                    config,
                    "listening",
                    "Say yes, no, or describe what should change.",
                )
                return False
        else:
            transcript, stt_model = transcribe_with_fallback(
                transcription_path,
                config,
            )
        if cloud_wake_required:
            transcript, stt_model = retry_transcript_for_wake_word(
                transcription_path,
                transcript,
                stt_model,
                config,
            )
        if (
            transcript
            and cloud_wake_required
            and not transcript_contains_wake_word(transcript, config.wake_word)
        ):
            print(f"speech ignored: cloud transcript has no wake word: {transcript}", flush=True)
            post_voice_state(config, "waiting", "Waiting for Magpie")
            return False
        if transcript and cloud_wake_required:
            print("cloud wake fallback heard Magpie", flush=True)
            post_voice_state(config, "heard", "Magpie heard. Processing command.")
            play_acknowledgement(config)
        if transcript:
            print(f"heard ({stt_model}): {transcript}", flush=True)
            post_voice_state(
                config,
                "heard",
                f"Heard: {transcript}",
                transcript=transcript,
            )
        elif local_wake.detected:
            print("local wake heard; awaiting command", flush=True)
            diagnosis = {
                "rms": rms,
                "activity_rms": activity_rms,
                "transcript": "",
                "wake_word": config.wake_word,
                "command": None,
                "wake_heard": True,
                "awaiting_command": True,
                "route": "",
                "query": "",
                "stt_model": stt_model,
            }
            loop_state.wake_diagnosis = diagnosis
            post_voice_state(
                config,
                "listening",
                "Magpie heard. Say what to find or put away.",
            )
            arm_command_window(loop_state, config)
            return True
        elif speech_detected or activity_rms >= config.audio_threshold:
            print(f"no transcript: rms={rms:.0f} activity_rms={activity_rms:.0f}", flush=True)
            return loop_state.awaiting_command
        if is_prompt_echo(transcript):
            print("ignored prompt echo", flush=True)
            post_voice_attempt(
                config,
                {
                    "rms": rms,
                    "transcript": transcript,
                    "wake_word": config.wake_word,
                    "wake_heard": False,
                    "awaiting_command": False,
                    "command": "",
                    "route": "",
                    "query": "",
                    "stt_model": config.stt_model,
                    "audio_threshold": config.audio_threshold,
                    "backend_response": None,
                    "error": "prompt_echo",
                },
            )
            return False
        if is_filler_transcript(transcript):
            print(f"ignored filler transcript: {transcript}", flush=True)
            post_voice_attempt(
                config,
                {
                    "rms": rms,
                    "activity_rms": activity_rms,
                    "transcript": transcript,
                    "wake_word": config.wake_word,
                    "wake_heard": False,
                    "awaiting_command": loop_state.awaiting_command,
                    "command": "",
                    "route": "",
                    "query": "",
                    "stt_model": stt_model,
                    "audio_threshold": config.audio_threshold,
                    "backend_response": None,
                    "error": "filler_transcript",
                },
            )
            return loop_state.awaiting_command
        if (
            follow_up_active
            and not loop_state.awaiting_command
            and loop_state.pending_diagnosis is None
            and not transcript_contains_wake_word(transcript, config.wake_word)
            and not is_plausible_voice_follow_up(transcript)
        ):
            print(f"ignored unrelated follow-up: {transcript}", flush=True)
            post_voice_attempt(
                config,
                {
                    "rms": rms,
                    "activity_rms": activity_rms,
                    "transcript": transcript,
                    "wake_word": config.wake_word,
                    "wake_heard": False,
                    "awaiting_command": False,
                    "command": "",
                    "route": "",
                    "query": "",
                    "stt_model": stt_model,
                    "audio_threshold": config.audio_threshold,
                    "backend_response": None,
                    "error": "unrelated_follow_up",
                },
            )
            return False
        if loop_state.pending_diagnosis is not None:
            pending = loop_state.pending_diagnosis
            merged_command = merge_voice_continuation(
                str(pending.get("command") or ""),
                transcript,
            )
            merged_transcript = " ".join(
                part
                for part in (
                    str(pending.get("transcript") or "").strip(),
                    transcript.strip(),
                )
                if part
            )
            diagnosis = diagnose_voice_command(
                f"{config.wake_word} {merged_command}",
                config,
                rms=rms,
                activity_rms=activity_rms,
            )
            diagnosis["transcript"] = merged_transcript
            diagnosis["stt_model"] = stt_model
            loop_state.pending_diagnosis = None
            loop_state.awaiting_command = False
            loop_state.wake_diagnosis = None
            assign_voice_client_request_id(diagnosis, loop_state)
            response = dispatch_voice_diagnosis(diagnosis, config)
            arm_voice_follow_up(loop_state, config, response)
            return False
        armed_by_prior_turn = bool(loop_state.awaiting_command or follow_up_active)
        diagnosis = diagnose_voice_command(
            transcript,
            config,
            rms=rms,
            activity_rms=activity_rms,
            awaiting_command=(
                loop_state.awaiting_command
                or follow_up_active
                or (local_wake.detected and is_voice_confirmation(transcript))
            ),
        )
        diagnosis["stt_model"] = stt_model
        if local_wake.detected or armed_by_prior_turn:
            diagnosis["wake_heard"] = True
        if follow_up_active and is_voice_revision(str(diagnosis.get("command") or "")):
            diagnosis["route"] = "/assistant/revise"
            diagnosis["query"] = str(diagnosis["command"])
        if diagnosis["awaiting_command"]:
            print("wake heard; awaiting command", flush=True)
            post_voice_attempt(config, attempt_from_diagnosis(diagnosis, config))
            arm_command_window(loop_state, config)
            return True
        command = diagnosis["command"]
        query = diagnosis["query"]
        if not command or not query:
            unrecognized = bool(local_wake.detected and transcript)
            post_voice_attempt(
                config,
                attempt_from_diagnosis(
                    diagnosis,
                    config,
                    error="unrecognized_command" if unrecognized else "",
                ),
            )
            if unrecognized:
                post_voice_state(
                    config,
                    "error",
                    "I heard you, but I could not identify a storage request. "
                    "Say Magpie and try again.",
                    transcript=transcript,
                )
            loop_state.awaiting_command = False
            loop_state.wake_diagnosis = None
            return False
        if command_needs_continuation(str(command)):
            print(f"holding clipped command: {command}", flush=True)
            loop_state.wake_diagnosis = None
            loop_state.pending_diagnosis = diagnosis
            arm_command_window(loop_state, config)
            post_voice_attempt(
                config,
                attempt_from_diagnosis(diagnosis, config, error="awaiting_continuation"),
            )
            return True
        loop_state.awaiting_command = False
        loop_state.awaiting_command_until = 0.0
        loop_state.wake_diagnosis = None
        assign_voice_client_request_id(diagnosis, loop_state)
        response = dispatch_voice_diagnosis(diagnosis, config)
        arm_voice_follow_up(loop_state, config, response)
        return False
    except Exception as exc:
        post_voice_attempt(
            config,
            {
                "rms": rms,
                "activity_rms": activity_rms,
                "transcript": "",
                "wake_word": config.wake_word,
                "wake_heard": local_wake.detected,
                "awaiting_command": loop_state.awaiting_command,
                "command": "",
                "route": "",
                "query": "",
                "stt_model": config.stt_model,
                "audio_threshold": config.audio_threshold,
                "device_id": config.edge_device_id,
                "runtime_version": VOICE_RUNTIME.label(),
                "backend_response": None,
                "error": str(exc),
            },
        )
        raise


def run_once(
    config: VoiceConfig,
    *,
    transcript: str = "",
    call_backend: bool = False,
    compare_stt_models: list[str] | None = None,
) -> dict[str, Any]:
    if transcript:
        return diagnose_voice_command(transcript, config, call_backend=call_backend)
    with tempfile.TemporaryDirectory(prefix="magpie-voice-") as tmp:
        clip_path = Path(tmp) / "clip.wav"
        speech_detected = record_clip(clip_path, config) is True
        rms = clip_rms(clip_path)
        activity_rms = clip_activity_rms(clip_path)
        if (
            not speech_detected
            and config.audio_threshold > 0
            and activity_rms < config.audio_threshold
        ):
            return {
                "rms": rms,
                "activity_rms": activity_rms,
                "transcript": "",
                "wake_word": config.wake_word,
                "command": None,
                "route": "",
                "query": "",
                "would_call_backend": False,
                "response": None,
                "speech": "Skipped as quiet.",
            }
        heard, stt_model = transcribe_with_fallback(clip_path, config)
        if not heard:
            return {
                "rms": rms,
                "activity_rms": activity_rms,
                "transcript": "",
                "wake_word": config.wake_word,
                "command": None,
                "route": "",
                "query": "",
                "would_call_backend": False,
                "response": None,
                "speech": "Audio was above threshold, but speech-to-text returned no transcript.",
                "error": "no_transcript",
                "stt_model": stt_model,
            }
        result = diagnose_voice_command(
            heard,
            config,
            rms=rms,
            activity_rms=activity_rms,
            call_backend=call_backend,
        )
        result["stt_model"] = stt_model
        comparisons = []
        for model in compare_stt_models or []:
            alt_transcript = transcribe(clip_path, config, model=model)
            alt = diagnose_voice_command(
                alt_transcript,
                config,
                rms=rms,
                activity_rms=activity_rms,
                call_backend=False,
            )
            alt["stt_model"] = model
            comparisons.append(alt)
        if comparisons:
            result["stt_model"] = config.stt_model
            result["comparisons"] = comparisons
        return result


def run_loop(config: VoiceConfig) -> None:
    validate_local_wake_configuration(config)
    print(
        f"listening for '{config.wake_word}' or 'hey {config.wake_word}' with Vosk "
        f"on {config.input_device}; "
        f"speaking on {config.output_device or 'default'}; "
        f"audio gate {config.audio_threshold:g} RMS, "
        f"speech start {config.vad_frame_threshold:g} RMS",
        flush=True,
    )
    state = VoiceLoopState()
    post_voice_state(config, "waiting", "Waiting for Magpie")
    while True:
        try:
            with tempfile.TemporaryDirectory(prefix="magpie-voice-") as tmp:
                clip_path = Path(tmp) / "clip.wav"
                speech_detected = record_clip(clip_path, config) is True
                post_voice_state(
                    config,
                    "processing" if speech_detected else "waiting",
                    "Processing speech" if speech_detected else "Waiting for Magpie",
                )
                state.awaiting_command = handle_clip(
                    clip_path,
                    config,
                    state=state,
                    speech_detected=speech_detected,
                )
        except Exception as exc:
            print(f"voice error: {exc}", flush=True)
            post_voice_state(config, "error", f"Voice error: {exc}")
            time.sleep(max(config.idle_seconds, 2.0))
        # Reopen the microphone immediately while waiting for a split command;
        # otherwise the normal idle delay can swallow the continuation.
        time.sleep(0.0 if state.awaiting_command else config.idle_seconds)


def config_from_args(args: argparse.Namespace) -> VoiceConfig:
    api_key = args.openai_api_key or os.environ.get("OPENAI_API_KEY", "")
    needs_key = not (args.transcript and not args.call_backend)
    if not api_key and needs_key:
        raise SystemExit("OPENAI_API_KEY is required for Magpie voice mode")
    return VoiceConfig(
        api_url=args.api_url,
        input_device=args.input_device,
        output_device=args.output_device,
        wake_word=args.wake_word,
        clip_seconds=args.clip_seconds,
        idle_seconds=args.idle_seconds,
        stt_model=args.stt_model,
        stt_fallback_model=args.stt_fallback_model,
        tts_model=args.tts_model,
        tts_voice=args.tts_voice,
        openai_api_key=api_key,
        audio_threshold=args.audio_threshold,
        input_channels=args.input_channels,
        input_channel=args.input_channel,
        vad_frame_threshold=args.vad_frame_threshold,
        follow_up_seconds=args.follow_up_seconds,
        vad_aggressiveness=args.vad_aggressiveness,
        vad_start_timeout_seconds=args.vad_start_timeout_seconds,
        vad_silence_seconds=args.vad_silence_seconds,
        max_utterance_seconds=args.max_utterance_seconds,
        local_wake_enabled=args.local_wake,
        local_wake_min_confidence=args.local_wake_min_confidence,
        vosk_model_path=args.vosk_model_path,
        log_attempts=not args.no_log_attempts,
        dry_run=args.dry_run,
        edge_device_id=args.edge_device_id,
        edge_token=args.edge_token,
        edge_workspace_id=args.edge_workspace_id,
        cf_access_client_id=args.cf_access_client_id,
        cf_access_client_secret=args.cf_access_client_secret,
        stt_failure_state_path=args.stt_failure_state_path,
        local_stt_fallback_enabled=args.local_stt_fallback,
        local_tts_fallback_enabled=args.local_tts_fallback,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="searchable-storage-voice")
    parser.add_argument("--api-url", default=os.environ.get("MAGPIE_API_URL", DEFAULT_API_URL))
    parser.add_argument(
        "--edge-device-id",
        default=os.environ.get("MAGPIE_EDGE_DEVICE_ID", ""),
    )
    parser.add_argument(
        "--edge-token",
        default=os.environ.get("MAGPIE_EDGE_TOKEN", ""),
    )
    parser.add_argument(
        "--edge-workspace-id",
        default=os.environ.get("MAGPIE_EDGE_WORKSPACE_ID", ""),
    )
    parser.add_argument(
        "--cf-access-client-id",
        default=os.environ.get("MAGPIE_CF_ACCESS_CLIENT_ID", ""),
    )
    parser.add_argument(
        "--cf-access-client-secret",
        default=os.environ.get("MAGPIE_CF_ACCESS_CLIENT_SECRET", ""),
    )
    parser.add_argument("--input-device", default=os.environ.get("MAGPIE_VOICE_INPUT_DEVICE", "plughw:2,0"))
    parser.add_argument(
        "--output-device",
        default=os.environ.get("MAGPIE_VOICE_OUTPUT_DEVICE", "plughw:CARD=vc4hdmi0,DEV=0"),
    )
    parser.add_argument("--wake-word", default=os.environ.get("MAGPIE_WAKE_WORD", DEFAULT_WAKE_WORD))
    parser.add_argument(
        "--input-channels",
        type=int,
        default=int(os.environ.get("MAGPIE_VOICE_INPUT_CHANNELS", "2")),
    )
    parser.add_argument(
        "--input-channel",
        type=int,
        default=int(os.environ.get("MAGPIE_VOICE_INPUT_CHANNEL", "1")),
        help="Zero-based channel passed to wake detection and speech recognition.",
    )
    parser.add_argument("--clip-seconds", type=float, default=float(os.environ.get("MAGPIE_VOICE_CLIP_SECONDS", "4")))
    parser.add_argument("--idle-seconds", type=float, default=float(os.environ.get("MAGPIE_VOICE_IDLE_SECONDS", "0.4")))
    parser.add_argument("--stt-model", default=os.environ.get("MAGPIE_STT_MODEL", DEFAULT_STT_MODEL))
    parser.add_argument(
        "--stt-fallback-model",
        default=os.environ.get("MAGPIE_STT_FALLBACK_MODEL", DEFAULT_STT_FALLBACK_MODEL),
    )
    parser.add_argument(
        "--stt-failure-state-path",
        default=os.environ.get(
            "MAGPIE_STT_FAILURE_STATE_PATH",
            DEFAULT_STT_FAILURE_STATE_PATH,
        ),
    )
    local_stt_fallback_default = os.environ.get(
        "MAGPIE_LOCAL_STT_FALLBACK_ENABLED",
        "true",
    ).strip().lower() in {"1", "true", "yes", "on"}
    parser.add_argument(
        "--local-stt-fallback",
        action=argparse.BooleanOptionalAction,
        default=local_stt_fallback_default,
    )
    local_tts_fallback_default = os.environ.get(
        "MAGPIE_LOCAL_TTS_FALLBACK_ENABLED",
        "true",
    ).strip().lower() in {"1", "true", "yes", "on"}
    parser.add_argument(
        "--local-tts-fallback",
        action=argparse.BooleanOptionalAction,
        default=local_tts_fallback_default,
    )
    parser.add_argument("--tts-model", default=os.environ.get("MAGPIE_TTS_MODEL", "tts-1"))
    parser.add_argument("--tts-voice", default=os.environ.get("MAGPIE_TTS_VOICE", "alloy"))
    parser.add_argument(
        "--audio-threshold",
        type=float,
        default=float(os.environ.get("MAGPIE_VOICE_AUDIO_THRESHOLD", str(DEFAULT_AUDIO_THRESHOLD))),
    )
    parser.add_argument(
        "--vad-frame-threshold",
        type=float,
        default=float(
            os.environ.get(
                "MAGPIE_VOICE_VAD_FRAME_THRESHOLD",
                str(DEFAULT_VAD_FRAME_THRESHOLD),
            )
        ),
        help=(
            "Minimum RMS for a WebRTC-VAD frame to start an utterance. "
            "Keep this below --audio-threshold so quiet speech onsets are retained."
        ),
    )
    parser.add_argument(
        "--follow-up-seconds",
        type=float,
        default=float(
            os.environ.get("MAGPIE_VOICE_FOLLOW_UP_SECONDS", str(DEFAULT_FOLLOW_UP_SECONDS))
        ),
        help="Accept conversational follow-ups without the wake word for this many seconds.",
    )
    parser.add_argument(
        "--vad-aggressiveness",
        type=int,
        choices=range(4),
        default=int(os.environ.get("MAGPIE_VOICE_VAD_AGGRESSIVENESS", "2")),
    )
    parser.add_argument(
        "--vad-start-timeout-seconds",
        type=float,
        default=float(
            os.environ.get(
                "MAGPIE_VOICE_VAD_START_TIMEOUT_SECONDS",
                str(DEFAULT_VAD_START_TIMEOUT_SECONDS),
            )
        ),
    )
    parser.add_argument(
        "--vad-silence-seconds",
        type=float,
        default=float(
            os.environ.get(
                "MAGPIE_VOICE_VAD_SILENCE_SECONDS",
                str(DEFAULT_VAD_SILENCE_SECONDS),
            )
        ),
    )
    parser.add_argument(
        "--max-utterance-seconds",
        type=float,
        default=float(
            os.environ.get(
                "MAGPIE_VOICE_MAX_UTTERANCE_SECONDS",
                str(DEFAULT_MAX_UTTERANCE_SECONDS),
            )
        ),
    )
    local_wake_default = os.environ.get("MAGPIE_LOCAL_WAKE_ENABLED", "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    parser.add_argument(
        "--local-wake",
        action=argparse.BooleanOptionalAction,
        default=local_wake_default,
    )
    parser.add_argument(
        "--vosk-model-path",
        default=os.environ.get(
            "MAGPIE_VOSK_MODEL_PATH",
            DEFAULT_VOSK_MODEL_PATH,
        ),
    )
    parser.add_argument(
        "--local-wake-min-confidence",
        type=float,
        default=float(
            os.environ.get(
                "MAGPIE_LOCAL_WAKE_MIN_CONFIDENCE",
                str(DEFAULT_LOCAL_WAKE_MIN_CONFIDENCE),
            )
        ),
    )
    parser.add_argument("--openai-api-key", default="")
    parser.add_argument("--no-log-attempts", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--once", action="store_true", help="Run one diagnostic capture/transcript cycle and exit.")
    parser.add_argument("--transcript", default="", help="Diagnose a transcript without recording audio.")
    parser.add_argument("--awaiting-command", action="store_true", help="Treat --transcript/--once input as the command after a wake-only clip.")
    parser.add_argument("--call-backend", action="store_true", help="In --once mode, call the routed Magpie API.")
    parser.add_argument(
        "--speak-response",
        action="store_true",
        help="Speak the diagnostic response after a successful backend call.",
    )
    parser.add_argument("--compare-stt-model", action="append", default=[], help="In --once mode, also transcribe the same clip with another STT model.")
    parser.add_argument("--json", action="store_true", help="Print --once diagnostics as JSON.")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    config = config_from_args(args)
    if args.once or args.transcript:
        if args.transcript:
            result = diagnose_voice_command(
                args.transcript,
                config,
                call_backend=args.call_backend and not args.dry_run,
                awaiting_command=args.awaiting_command,
            )
        else:
            result = run_once(
                config,
                call_backend=args.call_backend and not args.dry_run,
                compare_stt_models=args.compare_stt_model,
            )
            if config.log_attempts and not args.dry_run:
                error = str(result.get("error") or "")
                if result.get("speech") == "Skipped as quiet.":
                    error = "quiet"
                post_voice_attempt(config, attempt_from_diagnosis(result, config, error=error))
        if args.speak_response:
            speech = str(result.get("speech") or "").strip()
            if not speech:
                raise SystemExit("--speak-response requires a routed backend response")
            speak(speech, config)
        if args.json:
            print(json.dumps(result, indent=2, sort_keys=True))
        else:
            for key in ("rms", "transcript", "command", "route", "query", "speech"):
                value = result.get(key)
                if value is not None:
                    print(f"{key}: {value}")
            if result.get("response") is not None:
                print(f"response: {json.dumps(result['response'], sort_keys=True)}")
        return
    run_loop(config)


if __name__ == "__main__":
    main()
