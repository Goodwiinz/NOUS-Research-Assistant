"""
Video processing and analysis service for multimodal documents
"""

import json
import logging
import os
import subprocess
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class VideoProcessingService:
    """Service for processing and analyzing video files"""

    def __init__(self):
        """Initialize the video processing service"""
        self.supported_formats = {
            ".mp4",
            ".avi",
            ".mov",
            ".mkv",
            ".wmv",
            ".flv",
            ".webm",
            ".m4v",
            ".3gp",
        }
        self.max_video_duration = 3600  # 1 hour max for processing

    def process_video(self, video_path: str) -> Dict[str, Any]:
        """Comprehensive video processing and analysis"""
        try:
            if not os.path.exists(video_path):
                raise FileNotFoundError(f"Video file not found: {video_path}")

            # Validate video format
            file_ext = Path(video_path).suffix.lower()
            if file_ext not in self.supported_formats:
                raise ValueError(f"Unsupported video format: {file_ext}")

            logger.info(f"Processing video: {video_path}")

            # Extract comprehensive metadata
            metadata = self._extract_video_metadata(video_path)

            # Analyze video content
            content_analysis = self._analyze_video_content(video_path, metadata)

            # Extract audio for transcription
            audio_analysis = self._extract_and_analyze_audio(video_path)

            # Extract keyframes
            frame_analysis = self._extract_keyframes(video_path)

            # Video quality assessment
            quality_score = self._assess_video_quality(
                video_path, metadata, content_analysis
            )

            results = {
                "metadata": metadata,
                "content_analysis": content_analysis,
                "audio_analysis": audio_analysis,
                "frame_analysis": frame_analysis,
                "quality_score": quality_score,
                "processing_timestamp": datetime.now().isoformat(),
                "audio_transcribed": audio_analysis.get("transcribed", False),
                "keyframes_extracted": len(frame_analysis.get("keyframes", [])) > 0,
            }

            logger.info(f"Successfully processed video: {video_path}")
            return results

        except Exception as e:
            logger.error(f"Video processing failed for {video_path}: {str(e)}")
            raise

    def _extract_video_metadata(self, video_path: str) -> Dict[str, Any]:
        """Extract comprehensive video metadata using ffprobe"""
        metadata = {
            "file_path": video_path,
            "file_size": os.path.getsize(video_path),
            "file_format": Path(video_path).suffix.lower(),
        }

        try:
            # Use ffprobe for detailed metadata extraction
            cmd = [
                "ffprobe",
                "-protocol_whitelist",
                "file,pipe",
                "-v",
                "error",
                "-show_format",
                "-show_streams",
                "-show_chapters",
                "-print_format",
                "json",
                video_path,
            ]

            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)

            if result.returncode == 0:
                probe_data = json.loads(result.stdout)

                # Format information
                if "format" in probe_data:
                    format_info = probe_data["format"]
                    metadata.update(
                        {
                            "duration": float(format_info.get("duration", 0)),
                            "format_name": format_info.get("format_name", "unknown"),
                            "bit_rate": int(format_info.get("bit_rate", 0)),
                            "size": int(format_info.get("size", 0)),
                        }
                    )

                # Stream information
                if "streams" in probe_data:
                    video_streams = [
                        s
                        for s in probe_data["streams"]
                        if s.get("codec_type") == "video"
                    ]
                    audio_streams = [
                        s
                        for s in probe_data["streams"]
                        if s.get("codec_type") == "audio"
                    ]
                    subtitle_streams = [
                        s
                        for s in probe_data["streams"]
                        if s.get("codec_type") == "subtitle"
                    ]

                    metadata.update(
                        {
                            "video_streams": self._process_video_streams(video_streams),
                            "audio_streams": self._process_audio_streams(audio_streams),
                            "subtitle_streams": self._process_subtitle_streams(
                                subtitle_streams
                            ),
                            "total_streams": len(probe_data["streams"]),
                        }
                    )

                # Chapter information
                if "chapters" in probe_data:
                    metadata["chapters"] = self._process_chapters(
                        probe_data["chapters"]
                    )

            else:
                logger.warning(f"ffprobe failed for {video_path}: {result.stderr}")
                # Fallback to basic metadata
                metadata["duration"] = self._estimate_video_duration(video_path)

        except subprocess.TimeoutExpired:
            logger.error(f"ffprobe timeout for {video_path}")
            metadata["duration"] = self._estimate_video_duration(video_path)
        except Exception as e:
            logger.error(f"Metadata extraction failed: {str(e)}")
            metadata["duration"] = self._estimate_video_duration(video_path)

        return metadata

    def _process_video_streams(self, video_streams: List[Dict]) -> List[Dict]:
        """Process video stream information"""
        processed_streams = []

        for stream in video_streams:
            processed_stream = {
                "index": stream.get("index"),
                "codec_name": stream.get("codec_name"),
                "codec_long_name": stream.get("codec_long_name"),
                "width": stream.get("width", 0),
                "height": stream.get("height", 0),
                "bit_rate": stream.get("bit_rate", 0),
                "frame_rate": stream.get("r_frame_rate", 0),
                "frames": stream.get("nb_frames", 0),
                "duration": stream.get("duration", 0),
                "pixel_format": stream.get("pix_fmt", "unknown"),
                "profile": stream.get("profile", "unknown"),
                "level": stream.get("level", 0),
            }

            # Calculate aspect ratio
            width = processed_stream["width"]
            height = processed_stream["height"]
            if width > 0 and height > 0:
                processed_stream["aspect_ratio"] = width / height
                processed_stream["resolution_category"] = self._categorize_resolution(
                    width, height
                )

            processed_streams.append(processed_stream)

        return processed_streams

    def _process_audio_streams(self, audio_streams: List[Dict]) -> List[Dict]:
        """Process audio stream information"""
        processed_streams = []

        for stream in audio_streams:
            processed_stream = {
                "index": stream.get("index"),
                "codec_name": stream.get("codec_name"),
                "codec_long_name": stream.get("codec_long_name"),
                "sample_rate": stream.get("sample_rate", 0),
                "channels": stream.get("channels", 0),
                "bit_rate": stream.get("bit_rate", 0),
                "duration": stream.get("duration", 0),
                "language": stream.get("tags", {}).get("language", "unknown"),
                "title": stream.get("tags", {}).get("title", ""),
            }

            # Categorize audio quality
            sample_rate = processed_stream["sample_rate"]
            if sample_rate >= 48000:
                processed_stream["quality"] = "high"
            elif sample_rate >= 22050:
                processed_stream["quality"] = "medium"
            else:
                processed_stream["quality"] = "low"

            processed_streams.append(processed_stream)

        return processed_streams

    def _process_subtitle_streams(self, subtitle_streams: List[Dict]) -> List[Dict]:
        """Process subtitle stream information"""
        processed_streams = []

        for stream in subtitle_streams:
            processed_stream = {
                "index": stream.get("index"),
                "codec_name": stream.get("codec_name"),
                "codec_long_name": stream.get("codec_long_name"),
                "language": stream.get("tags", {}).get("language", "unknown"),
                "title": stream.get("tags", {}).get("title", ""),
                "duration": stream.get("duration", 0),
            }

            processed_streams.append(processed_stream)

        return processed_streams

    def _process_chapters(self, chapters: List[Dict]) -> List[Dict]:
        """Process chapter information"""
        processed_chapters = []

        for chapter in chapters:
            processed_chapter = {
                "id": chapter.get("id"),
                "title": chapter.get("tags", {}).get("title", ""),
                "start": chapter.get("start", 0),
                "end": chapter.get("end", 0),
                "duration": chapter.get("end", 0) - chapter.get("start", 0),
            }

            processed_chapters.append(processed_chapter)

        return processed_chapters

    def _analyze_video_content(self, video_path: str, metadata: Dict) -> Dict[str, Any]:
        """Analyze video content characteristics"""
        analysis = {
            "video_type": self._classify_video_type(metadata),
            "duration_category": self._categorize_duration(metadata["duration"]),
            "has_audio": len(metadata.get("audio_streams", [])) > 0,
            "has_subtitles": len(metadata.get("subtitle_streams", [])) > 0,
            "has_chapters": len(metadata.get("chapters", [])) > 0,
            "estimated_content_category": self._estimate_content_category(metadata),
        }

        # Analyze video streams
        video_streams = metadata.get("video_streams", [])
        if video_streams:
            main_stream = video_streams[0]  # Assume first stream is main
            analysis.update(
                {
                    "resolution": f"{main_stream['width']}x{main_stream['height']}",
                    "frame_rate": main_stream["frame_rate"],
                    "aspect_ratio": main_stream.get("aspect_ratio", 0),
                    "video_quality": self._assess_video_quality_stream(main_stream),
                }
            )

        return analysis

    def _extract_and_analyze_audio(self, video_path: str) -> Dict[str, Any]:
        """Extract audio from video for transcription"""
        audio_analysis = {
            "extracted": False,
            "transcribed": False,
            "transcription_available": False,
            "error": None,
        }

        audio_streams = self._extract_video_metadata(video_path).get(
            "audio_streams", []
        )

        if not audio_streams:
            audio_analysis["error"] = "No audio stream found in video"
            return audio_analysis

        try:
            # Import audio processing service for transcription
            from .audio_processing_service import AudioProcessingService

            # Create temporary audio file
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as temp_audio:
                # Extract audio using ffmpeg
                cmd = [
                    "ffmpeg",
                    "-protocol_whitelist",
                    "file,pipe",
                    "-i",
                    video_path,
                    "-vn",
                    "-acodec",
                    "pcm_s16le",
                    "-ar",
                    "16000",
                    "-ac",
                    "1",
                    temp_audio.name,
                    "-y",
                ]

                result = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=120
                )

                if result.returncode == 0:
                    audio_analysis["extracted"] = True
                    logger.info(f"Successfully extracted audio from {video_path}")

                    # Transcribe the extracted audio using audio processing service
                    try:
                        audio_processor = AudioProcessingService()
                        audio_results = audio_processor.process_audio(temp_audio.name)

                        audio_analysis.update(
                            {
                                "transcribed": True,
                                "transcription_available": True,
                                "transcription_results": audio_results["transcription"],
                                "audio_metadata": audio_results["metadata"],
                                "quality_analysis": audio_results["quality_analysis"],
                            }
                        )

                        logger.info(f"Successfully transcribed audio from {video_path}")

                    except Exception as transcribe_error:
                        audio_analysis["transcription_error"] = str(transcribe_error)
                        logger.warning(
                            f"Transcription failed for {video_path}: {str(transcribe_error)}"
                        )

                    # Clean up temporary file
                    os.unlink(temp_audio.name)

                else:
                    audio_analysis["error"] = (
                        f"Audio extraction failed: {result.stderr}"
                    )
                    logger.error(f"ffmpeg failed for {video_path}: {result.stderr}")

        except Exception as e:
            audio_analysis["error"] = str(e)
            logger.error(f"Audio extraction failed for {video_path}: {str(e)}")

        return audio_analysis

    def _extract_keyframes(self, video_path: str) -> Dict[str, Any]:
        """Extract keyframes from video"""
        frame_analysis = {
            "keyframes": [],
            "extraction_method": "ffmpeg",
            "keyframe_count": 0,
            "error": None,
        }

        try:
            # Extract keyframes using ffmpeg
            cmd = [
                "ffmpeg",
                "-protocol_whitelist",
                "file,pipe",
                "-i",
                video_path,
                "-vf",
                "select=keyframe",
                "-frames:v",
                "1",
                "-vsync",
                "drop",
                "-q:v",
                "2",
                "-y",
                "-f",
                "image2",
                "-",
            ]

            with tempfile.TemporaryDirectory() as temp_dir:
                result = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=120, cwd=temp_dir
                )

                if result.returncode == 0:
                    # Find extracted frames
                    frame_files = [
                        f for f in os.listdir(temp_dir) if f.endswith(".png")
                    ]
                    frame_files.sort()

                    for i, frame_file in enumerate(frame_files):
                        frame_path = os.path.join(temp_dir, frame_file)
                        frame_info = self._analyze_frame(frame_path, i)
                        frame_analysis["keyframes"].append(frame_info)

                    frame_analysis["keyframe_count"] = len(frame_files)
                    logger.info(
                        f"Extracted {len(frame_files)} keyframes from {video_path}"
                    )

                else:
                    frame_analysis["error"] = (
                        f"Keyframe extraction failed: {result.stderr}"
                    )
                    logger.warning(
                        f"Keyframe extraction failed for {video_path}: {result.stderr}"
                    )

        except subprocess.TimeoutExpired:
            frame_analysis["error"] = "Keyframe extraction timeout"
            logger.error(f"Keyframe extraction timeout for {video_path}")
        except Exception as e:
            frame_analysis["error"] = str(e)
            logger.error(f"Keyframe extraction failed for {video_path}: {str(e)}")

        return frame_analysis

    def _analyze_frame(self, frame_path: str, frame_index: int) -> Dict[str, Any]:
        """Analyze individual frame"""
        try:
            from PIL import Image

            # Get frame dimensions
            with Image.open(frame_path) as img:
                width, height = img.size

                return {
                    "index": frame_index,
                    "path": frame_path,
                    "width": width,
                    "height": height,
                    "pixels": width * height,
                    "aspect_ratio": width / height if height > 0 else 0,
                    "timestamp": None,  # Would need video time extraction
                }

        except Exception as e:
            logger.warning(f"Frame analysis failed for {frame_path}: {str(e)}")
            return {
                "index": frame_index,
                "path": frame_path,
                "width": 0,
                "height": 0,
                "pixels": 0,
                "aspect_ratio": 0,
                "error": str(e),
            }

    def _estimate_video_duration(self, video_path: str) -> float:
        """Estimate video duration when ffprobe fails"""
        try:
            # Use file size as rough estimate (assume 1 Mbps average bitrate)
            file_size_bytes = os.path.getsize(video_path)
            # Rough estimate: 1 hour = 3600 MB, so 1 MB = 1 second
            return file_size_bytes / (1024 * 1024)
        except Exception:
            return 0.0

    def _classify_video_type(self, metadata: Dict) -> str:
        """Classify video type based on characteristics"""
        duration = metadata.get("duration", 0)
        video_streams = metadata.get("video_streams", [])

        if not video_streams:
            return "unknown"

        main_stream = video_streams[0]
        width = main_stream.get("width", 0)
        height = main_stream.get("height", 0)
        frame_rate = main_stream.get("frame_rate", 0)

        # Classification logic
        if width >= 3840 or height >= 2160:
            if frame_rate >= 50:
                return "4K_high_framerate"
            else:
                return "4K_standard"
        elif width >= 1920 or height >= 1080:
            if frame_rate >= 50:
                return "1080p_high_framerate"
            else:
                return "1080p_standard"
        elif width >= 1280 or height >= 720:
            return "720p"
        elif width >= 854 or height >= 480:
            return "480p"
        else:
            return "low_resolution"

    def _categorize_duration(self, duration: float) -> str:
        """Categorize video duration"""
        if duration < 60:
            return "very_short"
        elif duration < 300:
            return "short"
        elif duration < 600:
            return "medium"
        elif duration < 1800:
            return "long"
        else:
            return "very_long"

    def _categorize_resolution(self, width: int, height: int) -> str:
        """Categorize video resolution"""
        pixels = width * height

        if pixels >= 8294400:  # 4K+
            return "4K"
        elif pixels >= 2073600:  # 2K+
            return "2K"
        elif pixels >= 921600:  # 720p+
            return "HD"
        elif pixels >= 409600:  # 480p+
            return "SD"
        else:
            return "low_resolution"

    def _assess_video_quality_stream(self, stream: Dict) -> str:
        """Assess video quality based on stream characteristics"""
        bit_rate = stream.get("bit_rate", 0)
        frame_rate = stream.get("frame_rate", 0)

        if bit_rate >= 8000000 or frame_rate >= 60:
            return "high_quality"
        elif bit_rate >= 4000000 or frame_rate >= 30:
            return "medium_quality"
        else:
            return "standard_quality"

    def _estimate_content_category(self, metadata: Dict) -> str:
        """Estimate video content category based on metadata"""
        # Use heuristics based on common video patterns
        duration = metadata.get("duration", 0)
        has_audio = len(metadata.get("audio_streams", [])) > 0
        has_chapters = len(metadata.get("chapters", [])) > 0

        if has_chapters and duration > 900:
            return "educational_content"
        elif duration > 1800:
            return "long_form_content"
        elif has_audio and duration > 300:
            return "presentation_lecture"
        elif duration < 30:
            return "short_clip"
        else:
            return "general_content"

    def _assess_video_quality(
        self, video_path: str, metadata: Dict, content_analysis: Dict
    ) -> float:
        """Assess overall video quality (0.0 to 1.0)"""
        quality_score = 0.5  # Base score

        # Duration factor
        duration = metadata.get("duration", 0)
        if 60 <= duration <= 1800:  # 1-30 minutes
            quality_score += 0.2
        elif duration < 30 or duration > 3600:
            quality_score -= 0.1

        # Audio quality factor
        audio_streams = metadata.get("audio_streams", [])
        if audio_streams:
            main_audio = audio_streams[0]
            if main_audio.get("quality") == "high":
                quality_score += 0.2
            elif main_audio.get("sample_rate") >= 48000:
                quality_score += 0.1

        # Video quality factor
        video_streams = metadata.get("video_streams", [])
        if video_streams:
            main_video = video_streams[0]
            video_quality = main_video.get("quality", "standard_quality")
            if video_quality == "high_quality":
                quality_score += 0.2
            elif main_video.get("bit_rate", 0) >= 5000000:
                quality_score += 0.1

        # Content factors
        if content_analysis.get("has_subtitles", False):
            quality_score += 0.1
        if content_analysis.get("has_chapters", False):
            quality_score += 0.1

        return max(0.0, min(1.0, quality_score))

    def generate_video_summary(self, video_results: Dict[str, Any]) -> str:
        """Generate a natural language summary of video analysis"""
        try:
            summary_parts = []

            # Basic information
            metadata = video_results["metadata"]
            duration = metadata["duration"]
            summary_parts.append(
                f"Video duration: {duration:.1f} seconds ({timedelta(seconds=int(duration))})"
            )

            # Video characteristics
            content = video_results["content_analysis"]
            summary_parts.append(
                f"Video type: {content['video_type']}, duration: {content['duration_category']}"
            )

            # Audio information
            audio = video_results["audio_analysis"]
            if audio["extracted"]:
                summary_parts.append("Audio track extracted successfully")
                if audio["transcribed"]:
                    transcription = audio["transcription_results"]
                    word_count = transcription.get("word_count", 0)
                    confidence = transcription.get("confidence", 0.0)
                    language = transcription.get("language", "unknown")
                    summary_parts.append(
                        f"Transcribed {word_count} words in {language} with {confidence:.1%} confidence"
                    )
                else:
                    summary_parts.append("Audio transcription not available")
            else:
                summary_parts.append("No audio track found in video")

            # Frame information
            frames = video_results["frame_analysis"]
            if frames["keyframe_count"] > 0:
                summary_parts.append(
                    f"Extracted {frames['keyframe_count']} keyframes for visual analysis"
                )

            # Quality assessment
            summary_parts.append(
                f"Overall video quality: {video_results['quality_score']:.1%}"
            )

            return ". ".join(summary_parts) + "."

        except Exception as e:
            logger.error(f"Failed to generate video summary: {str(e)}")
            return "Video analysis completed with limited details."

    def segment_long_video(
        self, video_path: str, segment_duration: int = 600
    ) -> List[str]:
        """Split long video into segments for processing"""
        try:
            duration = self._extract_video_metadata(video_path)["duration"]

            if duration <= segment_duration:
                return [video_path]

            num_segments = int(duration // segment_duration) + 1
            logger.info(
                f"Would split {duration:.1f}s video into {num_segments} segments"
            )

            # Return placeholder segment paths
            segments = []
            for i in range(num_segments):
                segment_path = f"{video_path}.segment_{i:03d}.mp4"
                segments.append(segment_path)

            return segments

        except Exception as e:
            logger.error(f"Video segmentation failed: {str(e)}")
            return [video_path]
