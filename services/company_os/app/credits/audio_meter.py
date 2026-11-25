"""Confirmed mini-TTS billing receipt for the R3-owned greeting integration."""
from __future__ import annotations

import base64
import json

from app.credits.errors import CreditsUnavailable
from app.credits.metered import price_micro
from llm_gateway.usage import extract_confirmed_token_usage


def speech_receipt(response) -> tuple[bytes, int]:
    """Return the emitted audio and cost from a complete SSE usage receipt.

    The owner requests ``stream_format='sse'`` and calls this inside the paid
    callback. Missing or malformed audio/usage retains the protected hold.
    """
    chunks = []
    usage = None
    try:
        for line in response.text.splitlines():
            if not line.startswith("data:"):
                continue
            event = json.loads(line[5:].strip())
            if event.get("type") == "speech.audio.delta":
                chunks.append(base64.b64decode(event["audio"], validate=True))
            elif event.get("type") == "speech.audio.done":
                usage = event.get("usage")
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise CreditsUnavailable("speech usage needs reconciliation") from exc
    counts = extract_confirmed_token_usage(usage)
    if counts is None or not chunks:
        raise CreditsUnavailable("speech usage needs reconciliation")
    actual = (price_micro("tts_input_token", counts["prompt_tokens"])
              + price_micro("tts_output_audio_token", counts["completion_tokens"]))
    return b"".join(chunks), actual
