#
# Copyright (c) 2024–2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""LMNT text-to-speech service implementation."""

import json
from typing import AsyncGenerator, Optional

from loguru import logger

from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    ErrorFrame,
    Frame,
    StartFrame,
    StartInterruptionFrame,
    TTSAudioRawFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.tts_service import InterruptibleTTSService
from pipecat.transcriptions.language import Language
from pipecat.utils.tracing.service_decorators import traced_tts

# See .env.example for LMNT configuration needed
try:
    from websockets.asyncio.client import connect as websocket_connect
    from websockets.protocol import State
except ModuleNotFoundError as e:
    logger.error(f"Exception: {e}")
    logger.error("In order to use LMNT, you need to `pip install pipecat-ai[lmnt]`.")
    raise Exception(f"Missing module: {e}")


def language_to_lmnt_language(language: Language) -> Optional[str]:
    """Convert a Language enum to LMNT language code.

    Args:
        language: The Language enum value to convert.

    Returns:
        The corresponding LMNT language code, or None if not supported.
    """
    BASE_LANGUAGES = {
        Language.DE: "de",
        Language.EN: "en",
        Language.ES: "es",
        Language.FR: "fr",
        Language.HI: "hi",
        Language.ID: "id",
        Language.IT: "it",
        Language.JA: "ja",
        Language.KO: "ko",
        Language.NL: "nl",
        Language.PL: "pl",
        Language.PT: "pt",
        Language.RU: "ru",
        Language.SV: "sv",
        Language.TH: "th",
        Language.TR: "tr",
        Language.UK: "uk",
        Language.VI: "vi",
        Language.ZH: "zh",
    }

    result = BASE_LANGUAGES.get(language)

    # If not found in base languages, try to find the base language from a variant
    if not result:
        # Convert enum value to string and get the base language part (e.g. es-ES -> es)
        lang_str = str(language.value)
        base_code = lang_str.split("-")[0].lower()
        # Look up the base code in our supported languages
        result = base_code if base_code in BASE_LANGUAGES.values() else None

    return result


class LmntTTSService(InterruptibleTTSService):
    """LMNT real-time text-to-speech service.

    Provides real-time text-to-speech synthesis using LMNT's WebSocket API.
    Supports streaming audio generation with configurable voice models and
    language settings.
    """

    def __init__(
        self,
        *,
        api_key: str,
        voice_id: str,
        sample_rate: Optional[int] = None,
        language: Language = Language.EN,
        model: str = "blizzard",
        **kwargs,
    ):
        """Initialize the LMNT TTS service.

        Args:
            api_key: LMNT API key for authentication.
            voice_id: ID of the voice to use for synthesis.
            sample_rate: Audio sample rate. If None, uses default.
            language: Language for synthesis. Defaults to English.
            model: TTS model to use. Defaults to "blizzard".
            **kwargs: Additional arguments passed to parent InterruptibleTTSService.
        """
        super().__init__(
            push_stop_frames=True,
            pause_frame_processing=True,
            sample_rate=sample_rate,
            **kwargs,
        )

        self._api_key = api_key
        self.set_voice(voice_id)
        self.set_model_name(model)
        self._settings = {
            "language": self.language_to_service_language(language),
            "format": "raw",  # Use raw format for direct PCM data
        }
        self._started = False
        self._receive_task = None

    def can_generate_metrics(self) -> bool:
        """Check if this service can generate processing metrics.

        Returns:
            True, as LMNT service supports metrics generation.
        """
        return True

    def language_to_service_language(self, language: Language) -> Optional[str]:
        """Convert a Language enum to LMNT service language format.

        Args:
            language: The language to convert.

        Returns:
            The LMNT-specific language code, or None if not supported.
        """
        return language_to_lmnt_language(language)

    async def start(self, frame: StartFrame):
        """Start the LMNT TTS service.

        Args:
            frame: The start frame containing initialization parameters.
        """
        await super().start(frame)
        await self._connect()

    async def stop(self, frame: EndFrame):
        """Stop the LMNT TTS service.

        Args:
            frame: The end frame.
        """
        await super().stop(frame)
        await self._disconnect()

    async def cancel(self, frame: CancelFrame):
        """Cancel the LMNT TTS service.

        Args:
            frame: The cancel frame.
        """
        await super().cancel(frame)
        await self._disconnect()

    async def push_frame(self, frame: Frame, direction: FrameDirection = FrameDirection.DOWNSTREAM):
        """Push a frame downstream with special handling for stop conditions.

        Args:
            frame: The frame to push.
            direction: The direction to push the frame.
        """
        await super().push_frame(frame, direction)
        if isinstance(frame, (TTSStoppedFrame, StartInterruptionFrame)):
            self._started = False

    async def _connect(self):
        """Connect to LMNT WebSocket and start receive task."""
        await self._connect_websocket()

        if self._websocket and not self._receive_task:
            self._receive_task = self.create_task(self._receive_task_handler(self._report_error))

    async def _disconnect(self):
        """Disconnect from LMNT WebSocket and clean up tasks."""
        if self._receive_task:
            await self.cancel_task(self._receive_task)
            self._receive_task = None

        await self._disconnect_websocket()

    async def _connect_websocket(self):
        """Connect to LMNT websocket."""
        try:
            if self._websocket and self._websocket.state is State.OPEN:
                return

            logger.debug("Connecting to LMNT")

            # Build initial connection message
            init_msg = {
                "X-API-Key": self._api_key,
                "voice": self._voice_id,
                "format": self._settings["format"],
                "sample_rate": self.sample_rate,
                "language": self._settings["language"],
                "model": self.model_name,
            }

            # Connect to LMNT's websocket directly
            self._websocket = await websocket_connect("wss://api.lmnt.com/v1/ai/speech/stream")

            # Send initialization message
            await self._websocket.send(json.dumps(init_msg))

        except Exception as e:
            logger.error(f"{self} initialization error: {e}")
            self._websocket = None
            await self._call_event_handler("on_connection_error", f"{e}")

    async def _disconnect_websocket(self):
        """Disconnect from LMNT websocket."""
        try:
            await self.stop_all_metrics()

            if self._websocket:
                logger.debug("Disconnecting from LMNT")
                # NOTE(aleix): sending EOF message before closing is causing
                # errors on the websocket, so we just skip it for now.
                # await self._websocket.send(json.dumps({"eof": True}))
                await self._websocket.close()
        except Exception as e:
            logger.error(f"{self} error closing websocket: {e}")
        finally:
            self._started = False
            self._websocket = None

    def _get_websocket(self):
        """Get the WebSocket connection if available."""
        if self._websocket:
            return self._websocket
        raise Exception("Websocket not connected")

    async def flush_audio(self):
        """Flush any pending audio synthesis."""
        if not self._websocket or self._websocket.state is State.CLOSED:
            return
        await self._get_websocket().send(json.dumps({"flush": True}))

    async def _receive_messages(self):
        """Receive messages from LMNT websocket."""
        async for message in self._get_websocket():
            if isinstance(message, bytes):
                # Raw audio data
                await self.stop_ttfb_metrics()
                frame = TTSAudioRawFrame(
                    audio=message,
                    sample_rate=self.sample_rate,
                    num_channels=1,
                )
                await self.push_frame(frame)
            else:
                try:
                    msg = json.loads(message)
                    if "error" in msg:
                        logger.error(f"{self} error: {msg['error']}")
                        await self.push_frame(TTSStoppedFrame())
                        await self.stop_all_metrics()
                        await self.push_error(ErrorFrame(f"{self} error: {msg['error']}"))
                        return
                except json.JSONDecodeError:
                    logger.error(f"Invalid JSON message: {message}")

    @traced_tts
    async def run_tts(self, text: str) -> AsyncGenerator[Frame, None]:
        """Generate TTS audio from text using LMNT's streaming API.

        Args:
            text: The text to synthesize into speech.

        Yields:
            Frame: Audio frames containing the synthesized speech.
        """
        logger.debug(f"{self}: Generating TTS [{text}]")

        try:
            if not self._websocket or self._websocket.state is State.CLOSED:
                await self._connect()

            try:
                if not self._started:
                    await self.start_ttfb_metrics()
                    yield TTSStartedFrame()
                    self._started = True

                # Send text to LMNT
                await self._get_websocket().send(json.dumps({"text": text}))
                # Force synthesis
                await self._get_websocket().send(json.dumps({"flush": True}))
                await self.start_tts_usage_metrics(text)
            except Exception as e:
                logger.error(f"{self} error sending message: {e}")
                yield TTSStoppedFrame()
                await self._disconnect()
                await self._connect()
                return
            yield None
        except Exception as e:
            logger.error(f"{self} exception: {e}")
