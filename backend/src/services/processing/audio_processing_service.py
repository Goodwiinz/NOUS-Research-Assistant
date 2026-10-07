"""
Audio processing and transcription service for multimodal documents
"""

import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class AudioProcessingService:
    """Service for processing and transcribing audio files"""

    def __init__(self):
        """Initialize the audio processing service"""
        self.supported_formats = {
            ".mp3",
            ".wav",
            ".m4a",
            ".flac",
            ".aac",
            ".ogg",
            ".wma",
            ".mp4",
            ".webm",
        }
        self.whisper_model = None
        self.load_whisper_model()

    def load_whisper_model(self):
        """Load the Whisper model for speech transcription"""
        try:
            import whisper

            # Start with base model, can be configured for different sizes
            self.whisper_model = whisper.load_model("base")
            logger.info("Successfully loaded Whisper base model")
        except Exception as e:
            logger.error(f"Failed to load Whisper model: {str(e)}")
            self.whisper_model = None

    def process_audio(self, audio_path: str) -> Dict[str, Any]:
        """Comprehensive audio processing and transcription"""
        try:
            if not os.path.exists(audio_path):
                raise FileNotFoundError(f"Audio file not found: {audio_path}")

            # Validate audio format
            file_ext = Path(audio_path).suffix.lower()
            if file_ext not in self.supported_formats:
                raise ValueError(f"Unsupported audio format: {file_ext}")

            logger.info(f"Processing audio file: {audio_path}")

            # Extract basic metadata
            metadata = self._extract_audio_metadata(audio_path)

            # Perform transcription if Whisper is available
            transcription_results = (
                self._transcribe_audio(audio_path)
                if self.whisper_model
                else {
                    "text": "",
                    "language": "unknown",
                    "confidence": 0.0,
                    "segments": [],
                    "note": "Whisper model not available",
                }
            )

            # Audio quality analysis
            quality_analysis = self._analyze_audio_quality(audio_path, metadata)

            results = {
                "metadata": metadata,
                "transcription": transcription_results,
                "quality_analysis": quality_analysis,
                "processing_timestamp": datetime.now().isoformat(),
                "transcription_available": self.whisper_model is not None,
            }

            logger.info(f"Successfully processed audio: {audio_path}")
            return results

        except Exception as e:
            logger.error(f"Audio processing failed for {audio_path}: {str(e)}")
            raise

    def _extract_audio_metadata(self, audio_path: str) -> Dict[str, Any]:
        """Extract comprehensive audio metadata"""
        metadata = {
            "file_path": audio_path,
            "file_size": os.path.getsize(audio_path),
            "file_format": Path(audio_path).suffix.lower(),
            "duration_seconds": self._get_audio_duration(audio_path),
        }

        try:
            # Try to extract more detailed metadata using mutagen if available
            from mutagen import File as MutagenFile

            audio_file = MutagenFile(audio_path)

            if audio_file is not None:
                info = audio_file.info

                metadata.update(
                    {
                        "bitrate": getattr(info, "bitrate", 0),
                        "sample_rate": getattr(info, "sample_rate", 0),
                        "channels": getattr(info, "channels", 0),
                        "length": getattr(info, "length", 0),
                        "title": (
                            audio_file.tags.get("TIT2", [""])[0]
                            if "TIT2" in audio_file.tags
                            else ""
                        ),
                        "artist": (
                            audio_file.tags.get("TPE1", [""])[0]
                            if "TPE1" in audio_file.tags
                            else ""
                        ),
                        "album": (
                            audio_file.tags.get("TALB", [""])[0]
                            if "TALB" in audio_file.tags
                            else ""
                        ),
                        "date": (
                            audio_file.tags.get("TDRC", [""])[0]
                            if "TDRC" in audio_file.tags
                            else ""
                        ),
                        "genre": (
                            audio_file.tags.get("TCON", [""])[0]
                            if "TCON" in audio_file.tags
                            else ""
                        ),
                    }
                )

                # Add any other available tags
                other_tags = {}
                for key, value in audio_file.tags.items():
                    if isinstance(value, list) and value:
                        other_tags[key] = value[0]
                    elif isinstance(value, str):
                        other_tags[key] = value
                metadata["tags"] = other_tags

        except ImportError:
            logger.warning("Mutagen not available, limited metadata extraction")
        except Exception as e:
            logger.warning(f"Failed to extract detailed metadata: {str(e)}")

        return metadata

    def _get_audio_duration(self, audio_path: str) -> float:
        """Get audio duration in seconds"""
        try:
            # Try different methods to get duration
            import subprocess

            # Use ffprobe if available
            try:
                result = subprocess.run(
                    [
                        "ffprobe",
                        "-protocol_whitelist",
                        "file,pipe",
                        "-v",
                        "error",
                        "-show_entries",
                        "format=duration",
                        "-of",
                        "csv=p=0",
                        audio_path,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )

                if result.returncode == 0 and result.stdout.strip():
                    return float(result.stdout.strip())
            except (
                subprocess.TimeoutExpired,
                subprocess.CalledProcessError,
                FileNotFoundError,
            ):
                pass

            # Fallback to python-based methods
            try:
                from mutagen import File as MutagenFile

                audio_file = MutagenFile(audio_path)
                if audio_file and audio_file.info:
                    return getattr(audio_file.info, "length", 0.0)
            except ImportError:
                pass

            # Very rough estimate based on file size (last resort)
            file_size = os.path.getsize(audio_path)
            # Assume average bitrate of 128 kbps = 16KB/s
            estimated_duration = file_size / (16 * 1024)
            logger.warning(
                f"Using estimated duration: {estimated_duration:.2f}s for {audio_path}"
            )
            return estimated_duration

        except Exception as e:
            logger.warning(f"Failed to get audio duration: {str(e)}")
            return 0.0

    def _transcribe_audio(self, audio_path: str) -> Dict[str, Any]:
        """Transcribe audio using Whisper"""
        try:
            if not self.whisper_model:
                return {
                    "text": "",
                    "language": "unknown",
                    "confidence": 0.0,
                    "segments": [],
                    "note": "Whisper model not available",
                }

            logger.info(f"Starting transcription with Whisper model")

            # Perform transcription
            result = self.whisper_model.transcribe(
                audio_path,
                fp16=False,  # Use 32-bit for compatibility
                verbose=False,
                word_timestamps=True,
                task="transcribe",
            )

            # Process results
            segments = []
            for segment in result.get("segments", []):
                segments.append(
                    {
                        "start": segment["start"],
                        "end": segment["end"],
                        "text": segment["text"].strip(),
                        "confidence": segment.get(
                            "avg_logprob", 0.0
                        ),  # Whisper doesn't provide direct confidence
                        "words": segment.get("words", []),
                    }
                )

            # Calculate overall confidence (based on average logprob)
            avg_logprob = sum(
                s.get("avg_logprob", 0.0) for s in result.get("segments", [])
            )
            avg_logprob = (
                avg_logprob / len(result.get("segments", [1]))
                if result.get("segments")
                else 0
            )
            # Convert logprob to a rough confidence score (0-1 scale)
            confidence = max(0.0, min(1.0, (avg_logprob + 1.0) / 2.0))

            return {
                "text": result["text"].strip(),
                "language": result.get("language", "unknown"),
                "confidence": confidence,
                "segments": segments,
                "word_count": len(result["text"].split()) if result["text"] else 0,
                "detected_language_confidence": result.get("language_probability", 0.0),
            }

        except Exception as e:
            logger.error(f"Audio transcription failed: {str(e)}")
            return {
                "text": "",
                "language": "unknown",
                "confidence": 0.0,
                "segments": [],
                "error": str(e),
            }

    def _analyze_audio_quality(self, audio_path: str, metadata: Dict) -> Dict[str, Any]:
        """Analyze audio quality characteristics"""
        quality_analysis = {
            "file_size_category": self._categorize_file_size(metadata["file_size"]),
            "duration_category": self._categorize_duration(
                metadata["duration_seconds"]
            ),
            "format_quality": self._assess_format_quality(
                Path(audio_path).suffix.lower()
            ),
            "estimated_bitrate": self._estimate_bitrate(
                metadata["file_size"], metadata["duration_seconds"]
            ),
            "is_large_file": metadata["file_size"] > 50 * 1024 * 1024,  # > 50MB
            "is_long_audio": metadata["duration_seconds"] > 1800,  # > 30 minutes
        }

        return quality_analysis

    def _categorize_file_size(self, file_size_bytes: int) -> str:
        """Categorize file size"""
        size_mb = file_size_bytes / (1024 * 1024)

        if size_mb < 1:
            return "tiny"
        elif size_mb < 5:
            return "small"
        elif size_mb < 20:
            return "medium"
        elif size_mb < 50:
            return "large"
        else:
            return "very_large"

    def _categorize_duration(self, duration_seconds: float) -> str:
        """Categorize audio duration"""
        if duration_seconds < 30:
            return "very_short"
        elif duration_seconds < 120:
            return "short"
        elif duration_seconds < 600:
            return "medium"
        elif duration_seconds < 1800:
            return "long"
        else:
            return "very_long"

    def _assess_format_quality(self, file_format: str) -> str:
        """Assess audio format quality"""
        format_quality = {
            ".flac": "lossless",
            ".wav": "lossless",
            ".m4a": "high",
            ".aac": "high",
            ".mp3": "medium",
            ".ogg": "medium",
            ".wma": "low",
            ".webm": "variable",
        }
        return format_quality.get(file_format, "unknown")

    def _estimate_bitrate(self, file_size_bytes: int, duration_seconds: float) -> float:
        """Estimate audio bitrate in kbps"""
        if duration_seconds > 0:
            bitrate_bps = (file_size_bytes * 8) / duration_seconds
            return bitrate_bps / 1000  # Convert to kbps
        return 0.0

    def generate_audio_summary(self, audio_results: Dict[str, Any]) -> str:
        """Generate a natural language summary of audio analysis"""
        try:
            summary_parts = []

            # Basic information
            metadata = audio_results["metadata"]
            duration = metadata["duration_seconds"]
            summary_parts.append(
                f"Audio duration: {duration:.1f} seconds ({timedelta(seconds=int(duration))})"
            )

            # Transcription results
            transcription = audio_results["transcription"]
            if transcription["text"].strip():
                word_count = transcription["word_count"]
                confidence = transcription["confidence"]
                language = transcription["language"]
                summary_parts.append(
                    f"Transcribed {word_count} words in {language} with {confidence:.1%} confidence"
                )
            else:
                if not audio_results.get("transcription_available", False):
                    summary_parts.append("Audio transcription not available")
                else:
                    summary_parts.append("No speech detected in audio")

            # Audio quality
            quality = audio_results["quality_analysis"]
            summary_parts.append(
                f"File size: {quality['file_size_category']}, duration: {quality['duration_category']}"
            )

            if quality["estimated_bitrate"] > 0:
                summary_parts.append(
                    f"Estimated bitrate: {quality['estimated_bitrate']:.0f} kbps"
                )

            # Format information
            format_quality = quality["format_quality"]
            summary_parts.append(f"Format quality: {format_quality}")

            # Special notes
            if quality["is_large_file"]:
                summary_parts.append("Large file size may impact processing time")
            if quality["is_long_audio"]:
                summary_parts.append("Long audio may require分段 processing")

            return ". ".join(summary_parts) + "."

        except Exception as e:
            logger.error(f"Failed to generate audio summary: {str(e)}")
            return "Audio analysis completed with limited details."

    def segment_long_audio(
        self, audio_path: str, segment_duration: int = 300
    ) -> List[str]:
        """Split long audio into segments for processing"""
        try:
            # This is a placeholder for audio segmentation
            # In a real implementation, you would use pydub or ffmpeg to split the audio
            duration = self._get_audio_duration(audio_path)

            if duration <= segment_duration:
                return [audio_path]

            segments = []
            num_segments = int(duration // segment_duration) + 1

            logger.info(
                f"Would split {duration:.1f}s audio into {num_segments} segments"
            )

            # Return placeholder segment paths
            for i in range(num_segments):
                segment_path = f"{audio_path}.segment_{i:03d}.wav"
                segments.append(segment_path)

            return segments

        except Exception as e:
            logger.error(f"Audio segmentation failed: {str(e)}")
            return [audio_path]
