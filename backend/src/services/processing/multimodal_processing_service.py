"""
Multimodal Processing Service with async pipeline for OCR, transcription, and AI analysis
"""

import asyncio
import io
import logging
import os
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

# OCR and document processing
try:
    import fitz  # PyMuPDF
    import pdfplumber
    import pytesseract

    OCR_AVAILABLE = True
except ImportError:
    OCR_AVAILABLE = False

# Image processing
try:
    import cv2
    import numpy as np
    from PIL import Image

    IMAGE_PROCESSING_AVAILABLE = True
except ImportError:
    IMAGE_PROCESSING_AVAILABLE = False

# Audio processing
try:
    import librosa
    import whisper

    AUDIO_PROCESSING_AVAILABLE = True
except ImportError:
    AUDIO_PROCESSING_AVAILABLE = False

# Video processing
try:
    import cv2
    import ffmpeg

    VIDEO_PROCESSING_AVAILABLE = True
except ImportError:
    VIDEO_PROCESSING_AVAILABLE = False

# AI processing
try:
    import anthropic
    import openai

    AI_PROCESSING_AVAILABLE = True
except ImportError:
    AI_PROCESSING_AVAILABLE = False

from fastapi import Depends

from src.core.config import settings
from src.core.database import get_db
from src.models.document import Document, DocumentType, ProcessingStatus
from src.models.processing import JobStatus, JobType, ProcessingJob
from src.services.documents.storage_utils import local_file_for_document
from src.services.embedding.embedding_service import EmbeddingService
from src.services.processing.entity_extraction_service import EntityExtractionService

logger = logging.getLogger(__name__)


class ProcessingStep:
    """Represents a processing step in the pipeline"""

    def __init__(self, name: str, function: callable, required: bool = True):
        self.name = name
        self.function = function
        self.required = required
        self.start_time = None
        self.end_time = None
        self.success = False
        self.error_message = None


class MultimodalProcessingService:
    """Service for processing multimodal documents with async pipeline"""

    def __init__(self, db: Session):
        self.db = db
        self.upload_dir = Path(settings.UPLOAD_DIR)
        self.processed_dir = self.upload_dir / "processed"
        self.processed_dir.mkdir(parents=True, exist_ok=True)

        # Initialize AI clients
        self.openai_client = None
        self.anthropic_client = None

        if settings.OPENAI_API_KEY and AI_PROCESSING_AVAILABLE:
            openai.api_key = settings.OPENAI_API_KEY
            self.openai_client = openai

        if settings.ANTHROPIC_API_KEY and AI_PROCESSING_AVAILABLE:
            self.anthropic_client = anthropic.Anthropic(
                api_key=settings.ANTHROPIC_API_KEY
            )

        # Initialize AI models
        self.whisper_model = None
        if AUDIO_PROCESSING_AVAILABLE:
            try:
                self.whisper_model = whisper.load_model("base")
            except Exception as e:
                logger.warning(f"Failed to load Whisper model: {str(e)}")

        # Initialize processing services
        self.entity_service = None
        self.embedding_service = None

    async def process_document(
        self, document: Document, job: ProcessingJob, upload_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Main processing pipeline for multimodal documents
        """
        start_time = time.time()
        processing_results = {
            "text_extraction": {},
            "entity_extraction": {},
            "embedding_generation": {},
            "ai_analysis": {},
            "quality_assessment": {},
            "processing_time": 0,
            "success": False,
            "errors": [],
        }

        try:
            # Update job status
            job.start_job()
            job.update_progress("Starting document processing", 10.0)
            self.db.commit()

            # Define processing pipeline based on document type
            processing_steps = self.get_processing_pipeline(document.document_type)

            # Execute processing pipeline
            for i, step in enumerate(processing_steps):
                try:
                    step.start_time = time.time()
                    progress_percentage = 10.0 + (i * 80.0 / len(processing_steps))

                    job.update_progress(f"Executing: {step.name}", progress_percentage)
                    self.db.commit()

                    # Execute step
                    result = await step.function(document, job)

                    step.success = True
                    step.end_time = time.time()

                    # Store result
                    step_name = step.name.lower().replace(" ", "_")
                    processing_results[step_name] = result

                    logger.info(f"Processing step '{step.name}' completed successfully")

                except Exception as e:
                    step.success = False
                    step.error_message = str(e)
                    step.end_time = time.time()

                    error_msg = f"Processing step '{step.name}' failed: {str(e)}"
                    logger.error(error_msg)
                    processing_results["errors"].append(error_msg)

                    if step.required:
                        # Fail the job AND the document if a required step fails.
                        # This branch returns before update_document_with_results
                        # (the only other place that sets doc status), so without
                        # this the document would be stranded in PROCESSING.
                        job.fail_job(error_msg)
                        try:
                            document.update_processing_status(
                                ProcessingStatus.FAILED, error_msg
                            )
                        except Exception:
                            # Fall back to setting the columns directly so we
                            # never commit a FAILED job alongside a doc still in
                            # PROCESSING (state divergence).
                            logger.warning(
                                "update_processing_status failed for document %s "
                                "after required step '%s'; setting status directly",
                                getattr(document, "id", "?"),
                                step.name,
                                exc_info=True,
                            )
                            document.processing_status = ProcessingStatus.FAILED
                            document.processing_error = error_msg
                        self.db.commit()
                        return processing_results

            # Finalize processing
            processing_results["processing_time"] = time.time() - start_time
            processing_results["success"] = len(processing_results["errors"]) == 0

            # Update document with processing results
            await self.update_document_with_results(document, processing_results)

            # Finalize the job to MATCH the document outcome.
            self._finalize_job(job, document, processing_results)
            self.db.commit()

            return processing_results

        except Exception as e:
            error_msg = f"Document processing failed: {str(e)}"
            logger.error(error_msg)

            job.fail_job(error_msg)
            self.db.commit()

            processing_results["errors"].append(error_msg)
            return processing_results

    def get_processing_pipeline(
        self, document_type: DocumentType
    ) -> List[ProcessingStep]:
        """Get processing pipeline based on document type"""

        # Common steps for all document types
        common_steps = [
            ProcessingStep("Text Extraction", self.extract_text_content),
            ProcessingStep("Entity Extraction", self.extract_entities),
            ProcessingStep(
                "Knowledge Graph Storage",
                self.store_entities_in_knowledge_graph,
                required=False,
            ),
            ProcessingStep("Embedding Generation", self.generate_embeddings),
            ProcessingStep("AI Analysis", self.perform_ai_analysis, required=False),
            ProcessingStep("Quality Assessment", self.assess_quality, required=False),
        ]

        # Document type-specific steps
        if document_type == DocumentType.PDF:
            return [
                ProcessingStep("PDF Processing", self.process_pdf),
                ProcessingStep(
                    "OCR Extraction", self.extract_text_with_ocr, required=False
                ),
                ProcessingStep(
                    "Figure Extraction", self.extract_figures, required=False
                ),
                *common_steps,
            ]
        elif document_type == DocumentType.IMAGE:
            return [
                ProcessingStep("Image Processing", self.process_image),
                ProcessingStep(
                    "Image OCR", self.extract_text_from_image, required=False
                ),
                ProcessingStep(
                    "Image Analysis", self.analyze_image_content, required=False
                ),
                *common_steps,
            ]
        elif document_type == DocumentType.AUDIO:
            return [
                ProcessingStep("Audio Processing", self.process_audio),
                ProcessingStep("Audio Transcription", self.transcribe_audio),
                *common_steps,
            ]
        elif document_type == DocumentType.VIDEO:
            return [
                ProcessingStep("Video Processing", self.process_video),
                ProcessingStep("Frame Extraction", self.extract_video_frames),
                ProcessingStep(
                    "Audio Extraction", self.extract_audio_from_video, required=False
                ),
                *common_steps,
            ]
        else:
            return common_steps

    async def process_pdf(
        self, document: Document, job: ProcessingJob
    ) -> Dict[str, Any]:
        """Process PDF document"""
        try:
            if not OCR_AVAILABLE:
                return {"error": "OCR libraries not available"}

            results = {
                "text_content": "",
                "page_count": 0,
                "images_extracted": 0,
                "metadata": {},
            }

            with local_file_for_document(document) as file_path:
                # Extract text using PyMuPDF
                try:
                    pdf_document = fitz.open(file_path)
                    results["page_count"] = len(pdf_document)

                    text_content = []
                    for page_num in range(len(pdf_document)):
                        page = pdf_document.load_page(page_num)
                        text_content.append(page.get_text())

                    results["text_content"] = "\n".join(text_content)

                    # Extract metadata
                    metadata = pdf_document.metadata
                    results["metadata"] = {
                        "title": metadata.get("title", ""),
                        "author": metadata.get("author", ""),
                        "subject": metadata.get("subject", ""),
                        "creator": metadata.get("creator", ""),
                        "producer": metadata.get("producer", ""),
                        "creation_date": metadata.get("creationDate", ""),
                        "modification_date": metadata.get("modDate", ""),
                    }

                    pdf_document.close()

                except Exception as e:
                    logger.error(f"PyMuPDF processing failed: {str(e)}")

                    # Fallback to pdfplumber
                    try:
                        import pdfplumber

                        with pdfplumber.open(file_path) as pdf:
                            text_content = []
                            for page in pdf.pages:
                                text_content.append(page.extract_text())

                            results["text_content"] = "\n".join(text_content)
                            results["page_count"] = len(pdf.pages)

                    except Exception as e2:
                        logger.error(f"pdfplumber fallback failed: {str(e2)}")
                        raise e

            return results

        except Exception as e:
            logger.error(f"PDF processing failed: {str(e)}")
            raise

    async def extract_text_with_ocr(
        self, document: Document, job: ProcessingJob
    ) -> Dict[str, Any]:
        """Extract text from PDF using OCR"""
        try:
            if not OCR_AVAILABLE:
                return {"error": "OCR not available"}

            results = {"ocr_text": "", "confidence_scores": [], "processing_time": 0}

            start_time = time.time()

            with local_file_for_document(document) as file_path:
                # Use pytesseract for OCR
                try:
                    import fitz
                    import pytesseract
                    from PIL import Image

                    pdf_document = fitz.open(file_path)
                    ocr_text_pages = []

                    for page_num in range(len(pdf_document)):
                        page = pdf_document.load_page(page_num)

                        # Convert page to image
                        pix = page.get_pixmap(
                            matrix=fitz.Matrix(2, 2)
                        )  # 2x zoom for better OCR
                        img_data = pix.tobytes("png")

                        # Perform OCR
                        image = Image.open(io.BytesIO(img_data))
                        text = pytesseract.image_to_string(image)
                        confidence = pytesseract.image_to_data(
                            image, output_type=pytesseract.Output.DICT
                        )

                        ocr_text_pages.append(text)

                        # Calculate average confidence
                        if confidence.get("conf"):
                            avg_confidence = sum(
                                conf["conf"] for conf in confidence["conf"] if conf > 0
                            ) / len([c for c in confidence["conf"] if c > 0])
                            results["confidence_scores"].append(avg_confidence)

                    results["ocr_text"] = "\n".join(ocr_text_pages)
                    results["processing_time"] = time.time() - start_time

                    pdf_document.close()

                except Exception as e:
                    logger.error(f"OCR processing failed: {str(e)}")
                    raise

            return results

        except Exception as e:
            logger.error(f"OCR extraction failed: {str(e)}")
            raise

    async def extract_figures(
        self, document: Document, job: ProcessingJob
    ) -> Dict[str, Any]:
        """Extract embedded raster figures + caption heuristics (flag-gated).

        Reclaim-safe: ``extract_figures_for_document`` is idempotent — it
        delete-before-inserts its ``MultimodalContent`` rows scoped to
        (document_id, organization_id, extraction_method), so a stale-reclaim
        replay of ``process_document`` (see ``process_document_upload``) REPLACES
        the prior run's figure rows rather than appending duplicates (audit
        #1138). This is the only SQL-row-persisting step in the pipeline.
        """
        from src.services.processing.figure_extraction_service import (
            extract_figures_for_document,
        )

        if not settings.FIGURE_EXTRACTION_ENABLED:
            return {"skipped": "disabled"}
        result = extract_figures_for_document(self.db, document)
        self.db.commit()
        return result

    async def process_image(
        self, document: Document, job: ProcessingJob
    ) -> Dict[str, Any]:
        """Process image document"""
        try:
            if not IMAGE_PROCESSING_AVAILABLE:
                return {"error": "Image processing libraries not available"}

            # ponytail: same s3 file_path bug as process_pdf/extract_text_with_ocr
            # (and the 7 other file_path sites below); fix when image/audio/video
            # pipelines are actually live.
            file_path = document.file_path
            results = {
                "width": 0,
                "height": 0,
                "format": "",
                "mode": "",
                "size_bytes": 0,
                "has_transparency": False,
                "color_analysis": {},
            }

            # Analyze image using PIL
            with Image.open(file_path) as img:
                results.update(
                    {
                        "width": img.width,
                        "height": img.height,
                        "format": img.format,
                        "mode": img.mode,
                        "has_transparency": img.mode in ("RGBA", "LA")
                        or "transparency" in img.info,
                    }
                )

                # Basic color analysis
                if img.mode == "RGB":
                    # Convert to numpy array for analysis
                    img_array = np.array(img)
                    results["color_analysis"] = {
                        "mean_color": img_array.mean(axis=(0, 1)).tolist(),
                        "brightness": np.mean(img_array),
                        "contrast": np.std(img_array),
                    }

            results["size_bytes"] = os.path.getsize(file_path)

            return results

        except Exception as e:
            logger.error(f"Image processing failed: {str(e)}")
            raise

    async def extract_text_from_image(
        self, document: Document, job: ProcessingJob
    ) -> Dict[str, Any]:
        """Extract text from image using OCR"""
        try:
            if not OCR_AVAILABLE:
                return {"error": "OCR not available"}

            file_path = document.file_path
            results = {
                "extracted_text": "",
                "confidence_score": 0,
                "processing_time": 0,
            }

            start_time = time.time()

            # Perform OCR using pytesseract
            with Image.open(file_path) as img:
                # Preprocess image for better OCR
                if img.mode != "RGB":
                    img = img.convert("RGB")

                # Extract text
                text = pytesseract.image_to_string(img)

                # Get confidence scores
                try:
                    data = pytesseract.image_to_data(
                        img, output_type=pytesseract.Output.DICT
                    )
                    confidences = [int(conf) for conf in data["conf"] if int(conf) > 0]
                    avg_confidence = (
                        sum(confidences) / len(confidences) if confidences else 0
                    )
                    results["confidence_score"] = avg_confidence
                except Exception:
                    logger.warning("OCR confidence extraction failed", exc_info=True)
                    results["confidence_score"] = 0

                results["extracted_text"] = text
                results["processing_time"] = time.time() - start_time

            return results

        except Exception as e:
            logger.error(f"Image OCR failed: {str(e)}")
            raise

    async def analyze_image_content(
        self, document: Document, job: ProcessingJob
    ) -> Dict[str, Any]:
        """Analyze image content using AI"""
        try:
            if not self.openai_client:
                return {"error": "AI processing not available"}

            file_path = document.file_path
            results = {
                "description": "",
                "objects": [],
                "scene_analysis": {},
                "confidence_scores": [],
            }

            # This would require vision model integration
            # For now, return placeholder
            results["description"] = "Image content analysis not yet implemented"
            results["objects"] = []
            results["scene_analysis"] = {"type": "unknown"}

            return results

        except Exception as e:
            logger.error(f"Image analysis failed: {str(e)}")
            raise

    async def process_audio(
        self, document: Document, job: ProcessingJob
    ) -> Dict[str, Any]:
        """Process audio document"""
        try:
            if not AUDIO_PROCESSING_AVAILABLE:
                return {"error": "Audio processing libraries not available"}

            file_path = document.file_path
            results = {
                "duration": 0,
                "sample_rate": 0,
                "channels": 0,
                "format": "",
                "bitrate": 0,
                "file_size": 0,
            }

            # Analyze audio using librosa
            try:
                y, sr = librosa.load(file_path, sr=None)
                duration = librosa.get_duration(y=y, sr=sr)

                results.update(
                    {
                        "duration": duration,
                        "sample_rate": sr,
                        "channels": 1 if y.ndim == 1 else y.shape[0],
                    }
                )

            except Exception as e:
                logger.error(f"Librosa analysis failed: {str(e)}")

                # Fallback basic analysis
                import mutagen

                try:
                    audio_file = mutagen.File(file_path)
                    if audio_file is not None:
                        results["duration"] = audio_file.info.length
                        results["bitrate"] = audio_file.info.bitrate
                except Exception:
                    logger.warning("Audio fallback analysis failed", exc_info=True)

            results["format"] = Path(file_path).suffix[1:].upper()
            results["file_size"] = os.path.getsize(file_path)

            return results

        except Exception as e:
            logger.error(f"Audio processing failed: {str(e)}")
            raise

    async def transcribe_audio(
        self, document: Document, job: ProcessingJob
    ) -> Dict[str, Any]:
        """Transcribe audio using Whisper"""
        try:
            if not self.whisper_model:
                return {"error": "Whisper model not available"}

            file_path = document.file_path
            results = {
                "transcription": "",
                "language": "",
                "confidence": 0,
                "processing_time": 0,
            }

            start_time = time.time()

            # Transcribe using Whisper
            result = self.whisper_model.transcribe(file_path)

            results["transcription"] = result["text"]
            results["language"] = result.get("language", "unknown")
            results["processing_time"] = time.time() - start_time

            return results

        except Exception as e:
            logger.error(f"Audio transcription failed: {str(e)}")
            raise

    async def process_video(
        self, document: Document, job: ProcessingJob
    ) -> Dict[str, Any]:
        """Process video document"""
        try:
            if not VIDEO_PROCESSING_AVAILABLE:
                return {"error": "Video processing libraries not available"}

            file_path = document.file_path
            results = {
                "duration": 0,
                "fps": 0,
                "width": 0,
                "height": 0,
                "frames_count": 0,
                "format": "",
                "file_size": 0,
            }

            # Analyze video using OpenCV
            try:
                cap = cv2.VideoCapture(file_path)

                if cap.isOpened():
                    results["fps"] = cap.get(cv2.CAP_PROP_FPS)
                    results["width"] = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                    results["height"] = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                    results["frames_count"] = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
                    results["duration"] = (
                        results["frames_count"] / results["fps"]
                        if results["fps"] > 0
                        else 0
                    )

                cap.release()

            except Exception as e:
                logger.error(f"OpenCV analysis failed: {str(e)}")

            results["format"] = Path(file_path).suffix[1:].upper()
            results["file_size"] = os.path.getsize(file_path)

            return results

        except Exception as e:
            logger.error(f"Video processing failed: {str(e)}")
            raise

    async def extract_video_frames(
        self, document: Document, job: ProcessingJob
    ) -> Dict[str, Any]:
        """Extract key frames from video"""
        try:
            if not VIDEO_PROCESSING_AVAILABLE:
                return {"error": "Video processing not available"}

            file_path = document.file_path
            results = {
                "extracted_frames": [],
                "frame_count": 0,
                "extraction_method": "uniform",
            }

            # Create output directory for frames
            frames_dir = self.processed_dir / f"{document.id}_frames"
            frames_dir.mkdir(exist_ok=True)

            # Extract frames using OpenCV
            try:
                cap = cv2.VideoCapture(file_path)

                if not cap.isOpened():
                    raise Exception("Cannot open video file")

                fps = cap.get(cv2.CAP_PROP_FPS)
                total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

                # Extract 10 frames uniformly distributed
                frames_to_extract = min(10, total_frames)
                frame_interval = (
                    total_frames // frames_to_extract if frames_to_extract > 0 else 1
                )

                extracted_frames = []
                for i in range(0, total_frames, frame_interval):
                    cap.set(cv2.CAP_PROP_POS_FRAMES, i)
                    ret, frame = cap.read()

                    if ret:
                        frame_filename = f"frame_{i:06d}.jpg"
                        frame_path = frames_dir / frame_filename

                        cv2.imwrite(str(frame_path), frame)
                        extracted_frames.append(
                            {
                                "frame_number": i,
                                "timestamp": i / fps if fps > 0 else 0,
                                "filename": frame_filename,
                                "path": str(frame_path),
                            }
                        )

                        if len(extracted_frames) >= frames_to_extract:
                            break

                cap.release()

                results["extracted_frames"] = extracted_frames
                results["frame_count"] = len(extracted_frames)

            except Exception as e:
                logger.error(f"Frame extraction failed: {str(e)}")
                raise

            return results

        except Exception as e:
            logger.error(f"Video frame extraction failed: {str(e)}")
            raise

    async def extract_audio_from_video(
        self, document: Document, job: ProcessingJob
    ) -> Dict[str, Any]:
        """Extract audio from video"""
        try:
            file_path = document.file_path
            results = {"audio_extracted": False, "audio_path": "", "audio_duration": 0}

            # Extract audio using ffmpeg-python
            try:
                import ffmpeg

                audio_path = self.processed_dir / f"{document.id}_audio.wav"

                (
                    ffmpeg.input(file_path, protocol_whitelist="file,pipe")
                    .audio.output(str(audio_path))
                    .overwrite_output()
                    .run(capture_stdout=True, capture_stderr=True)
                )

                if audio_path.exists():
                    results["audio_extracted"] = True
                    results["audio_path"] = str(audio_path)

                    # Get audio duration
                    try:
                        import librosa

                        y, sr = librosa.load(str(audio_path), sr=None)
                        results["audio_duration"] = librosa.get_duration(y=y, sr=sr)
                    except:
                        pass

            except Exception as e:
                logger.error(f"Audio extraction failed: {str(e)}")
                # Don't raise error as this is optional

            return results

        except Exception as e:
            logger.error(f"Video audio extraction failed: {str(e)}")
            raise

    async def extract_text_content(
        self, document: Document, job: ProcessingJob
    ) -> Dict[str, Any]:
        """Extract text content from document"""
        try:
            results = {
                "text_content": "",
                "extraction_method": "",
                "confidence": 0,
                "word_count": 0,
                "character_count": 0,
            }

            # Use existing file_service for text extraction
            from src.services.documents.file_service import FileService

            file_service = FileService(self.db)

            text_content = file_service.extract_text_content(document)

            results["text_content"] = text_content
            results["extraction_method"] = "basic"
            results["word_count"] = len(text_content.split()) if text_content else 0
            results["character_count"] = len(text_content)

            return results

        except Exception as e:
            logger.error(f"Text extraction failed: {str(e)}")
            raise

    async def extract_entities(
        self, document: Document, job: ProcessingJob
    ) -> Dict[str, Any]:
        """Extract entities from document text.

        Reclaim-safe by construction: this step returns transient ``Entity``
        objects in its result payload and never ``db.add``s them, so a
        stale-reclaim replay of ``process_document`` persists no duplicate
        entity rows here. The row-persisting spaCy/regex entity pipeline lives
        in ``processing_tasks.process_document_ingestion``, which is made
        idempotent separately via ``_reset_pipeline_entities``
        (delete-before-insert on the same document).
        """
        try:
            if not self.entity_service:
                self.entity_service = EntityExtractionService(self.db)

            text_content = document.content_text or ""
            if not text_content:
                return {"entities": [], "entity_count": 0, "extraction_method": "none"}

            results = {
                "entities": [],
                "entity_count": 0,
                "extraction_method": "spacy",
                "processing_time": 0,
            }

            start_time = time.time()

            # Extract entities
            entities = await self.entity_service.extract_entities_from_text(
                text_content,
                document_id=document.id,
                organization_id=document.organization_id,
            )

            results["entities"] = [
                {
                    "text": entity.name,
                    "label": entity.entity_type.value,
                    "confidence": entity.confidence_score,
                    "start": entity.metadata.get("start", 0) if entity.metadata else 0,
                    "end": entity.metadata.get("end", 0) if entity.metadata else 0,
                }
                for entity in entities
            ]

            results["entity_count"] = len(entities)
            results["processing_time"] = time.time() - start_time

            return results

        except Exception as e:
            logger.error(f"Entity extraction failed: {str(e)}")
            raise

    async def store_entities_in_knowledge_graph(
        self, document: Document, job: ProcessingJob
    ) -> Dict[str, Any]:
        """Store extracted entities and relationships in the knowledge graph.

        Reclaim-safe: writes land in Neo4j via
        ``knowledge_graph_service.create_entity`` / ``create_relationship``,
        both of which MERGE on a canonical identity key (not CREATE), so a
        stale-reclaim replay converges onto the same nodes/edges instead of
        duplicating them. This step writes no SQL rows.
        """
        try:
            import spacy

            from src.models.graph import CreateEntityRequest, CreateRelationshipRequest
            from src.models.graph import EntityType as GraphEntityType
            from src.models.graph import ExtractionMethod as GraphExtractionMethod
            from src.models.graph import RelationshipType as GraphRelationshipType
            from src.services.knowledge_graph.knowledge_graph_service import (
                knowledge_graph_service,
            )
            from src.services.processing.entity_extraction_service import (
                EntityExtractionService as SpacyEntityService,
            )

            text_content = document.content_text or ""
            if not text_content:
                return {
                    "entities_stored": 0,
                    "relationships_stored": 0,
                    "reason": "No text content",
                }

            results = {
                "entities_stored": 0,
                "relationships_stored": 0,
                "processing_time": 0,
                "errors": [],
            }

            start_time = time.time()

            # Initialize entity extraction service
            entity_service = SpacyEntityService()

            # Process text with spaCy to get the doc object for relationship extraction
            spacy_doc = entity_service.nlp(text_content)

            # Extract entities from text
            entities = entity_service.extract_entities_from_text(document, text_content)

            if not entities:
                return {
                    **results,
                    "processing_time": time.time() - start_time,
                    "reason": "No entities extracted",
                }

            # Store entities in knowledge graph
            entity_map = {}  # Map local entity IDs to graph entity IDs
            local_entity_map = {}  # Map local entity names to entity objects

            for entity in entities:
                try:
                    # Map entity types
                    entity_type_str = (
                        entity.entity_type.value
                        if hasattr(entity.entity_type, "value")
                        else str(entity.entity_type)
                    )

                    # Get confidence score - handle different attribute names
                    confidence = 0.8
                    if hasattr(entity, "confidence_score"):
                        confidence = entity.confidence_score
                    elif hasattr(entity, "confidence"):
                        confidence = entity.confidence
                    elif hasattr(entity, "relevance_score"):
                        confidence = entity.relevance_score

                    # Get context
                    context = None
                    if hasattr(entity, "context") and entity.context:
                        context = entity.context
                    elif (
                        hasattr(entity, "properties")
                        and entity.properties
                        and "context_window" in entity.properties
                    ):
                        context = entity.properties["context_window"]
                    else:
                        context = text_content[:200]

                    # Create entity request
                    entity_request = CreateEntityRequest(
                        name=entity.name,
                        entity_type=GraphEntityType(entity_type_str.upper()),
                        confidence_score=confidence,
                        extraction_method=GraphExtractionMethod.SPACY_NER,
                        context=context,
                        metadata={
                            "document_id": str(document.id),
                            "document_title": document.title,
                            "extraction_date": datetime.utcnow().isoformat(),
                            "source": "document_processing",
                        },
                        source_document_id=str(document.id),
                        organization_id=(
                            str(document.organization_id)
                            if document.organization_id
                            else None
                        ),
                    )

                    # Store in knowledge graph
                    graph_entity = knowledge_graph_service.create_entity(entity_request)
                    entity_map[entity.id] = graph_entity.id
                    local_entity_map[entity.name.lower()] = (entity, graph_entity.id)
                    results["entities_stored"] += 1

                    logger.info(
                        f"Stored entity in knowledge graph: {entity.name} ({entity_type_str})"
                    )

                except Exception as e:
                    error_msg = f"Failed to store entity {entity.name}: {str(e)}"
                    logger.warning(error_msg)
                    results["errors"].append(error_msg)

            # Extract and store relationships
            try:
                relationships = entity_service._extract_relationships(
                    spacy_doc, entities
                )

                logger.info(
                    f"Extracted {len(relationships)} relationships from document"
                )

                for rel in relationships:
                    try:
                        source_entity = rel.get("source_entity")
                        target_entity = rel.get("target_entity")
                        relationship_type = rel.get("relationship_type", "related_to")

                        # Get graph IDs for source and target entities
                        source_graph_id = entity_map.get(source_entity.id)
                        target_graph_id = entity_map.get(target_entity.id)

                        if not source_graph_id or not target_graph_id:
                            continue

                        # Map relationship type to GraphRelationshipType
                        relationship_type_mapping = {
                            "works_for": GraphRelationshipType.WORKS_FOR,
                            "located_in": GraphRelationshipType.LOCATED_IN,
                            "part_of": GraphRelationshipType.PART_OF,
                            "related_to": GraphRelationshipType.RELATED_TO,
                            "owns": GraphRelationshipType.OWNS,
                            "created_by": GraphRelationshipType.CREATED_BY,
                            "manages": GraphRelationshipType.MANAGES,
                            "knows": GraphRelationshipType.KNOWS,
                            "collaborates_with": GraphRelationshipType.COLLABORATES_WITH,
                        }

                        graph_rel_type = relationship_type_mapping.get(
                            relationship_type.lower(), GraphRelationshipType.RELATED_TO
                        )

                        # Create relationship request
                        rel_request = CreateRelationshipRequest(
                            source_entity_id=source_graph_id,
                            target_entity_id=target_graph_id,
                            relationship_type=graph_rel_type,
                            confidence_score=rel.get("confidence", 0.7),
                            context=rel.get("evidence", ""),
                            evidence=(
                                [rel.get("evidence", "")] if rel.get("evidence") else []
                            ),
                            metadata={
                                "document_id": str(document.id),
                                "pattern_matched": rel.get("pattern_matched", ""),
                                "extraction_date": datetime.utcnow().isoformat(),
                                "source": "document_processing",
                            },
                            source_document_id=str(document.id),
                            organization_id=(
                                str(document.organization_id)
                                if document.organization_id
                                else None
                            ),
                        )

                        # Store relationship in knowledge graph
                        knowledge_graph_service.create_relationship(rel_request)
                        results["relationships_stored"] += 1

                        logger.info(
                            f"Stored relationship: {source_entity.name} --[{graph_rel_type.value}]--> {target_entity.name}"
                        )

                    except Exception as e:
                        error_msg = f"Failed to store relationship: {str(e)}"
                        logger.warning(error_msg)
                        results["errors"].append(error_msg)

            except Exception as e:
                error_msg = f"Failed to extract relationships: {str(e)}"
                logger.warning(error_msg)
                results["errors"].append(error_msg)

            results["processing_time"] = time.time() - start_time

            logger.info(
                f"Stored {results['entities_stored']} entities and {results['relationships_stored']} relationships in knowledge graph for document {document.id}"
            )

            return results

        except Exception as e:
            logger.error(f"Failed to store entities in knowledge graph: {str(e)}")
            return {
                "entities_stored": 0,
                "relationships_stored": 0,
                "processing_time": 0,
                "errors": [str(e)],
            }

    async def generate_embeddings(
        self, document: Document, job: ProcessingJob
    ) -> Dict[str, Any]:
        """Generate embeddings for document"""
        try:
            if not self.embedding_service:
                self.embedding_service = EmbeddingService(self.db)

            text_content = document.content_text or ""
            if not text_content:
                return {"embedding_generated": False, "reason": "No text content"}

            results = {
                "embedding_generated": False,
                "embedding_id": None,
                "vector_dimension": 0,
                "processing_time": 0,
            }

            start_time = time.time()

            # Phase 5: DO KB owns retrieval. Push the document directly.
            ds_uuid: Optional[str] = None
            if getattr(settings, "DO_KB_ENABLED", False):
                try:
                    from src.services.do_kb import sync_document_to_kb

                    ds_uuid = await sync_document_to_kb(self.db, document)
                except Exception as kb_err:  # noqa: BLE001
                    logger.warning("do_kb sync failed: %s", kb_err)

            if ds_uuid:
                results.update(
                    {
                        "embedding_generated": True,
                        "do_kb_data_source_uuid": ds_uuid,
                        "processing_time": time.time() - start_time,
                    }
                )
                document.is_embedded = True
                self.db.commit()

            return results

        except Exception as e:
            logger.error(f"Embedding generation failed: {str(e)}")
            raise

    async def perform_ai_analysis(
        self, document: Document, job: ProcessingJob
    ) -> Dict[str, Any]:
        """Perform AI analysis on document"""
        try:
            if not (self.openai_client or self.anthropic_client):
                return {
                    "analysis_performed": False,
                    "reason": "AI clients not available",
                }

            text_content = document.content_text or ""
            if not text_content:
                return {"analysis_performed": False, "reason": "No text content"}

            results = {
                "analysis_performed": False,
                "summary": "",
                "key_topics": [],
                "sentiment": {},
                "insights": [],
                "processing_time": 0,
            }

            start_time = time.time()

            # Use OpenAI for analysis
            if self.openai_client:
                try:
                    response = self.openai_client.ChatCompletion.create(
                        model="gpt-3.5-turbo",
                        messages=[
                            {
                                "role": "system",
                                "content": "Analyze the following document and provide a summary, key topics, sentiment, and insights.",
                            },
                            {
                                "role": "user",
                                "content": text_content[:4000],  # Limit text length
                            },
                        ],
                        max_tokens=500,
                        temperature=0.3,
                    )

                    analysis_text = response.choices[0].message.content

                    # Parse analysis (simplified)
                    results["summary"] = analysis_text
                    results["analysis_performed"] = True

                except Exception as e:
                    logger.error(f"OpenAI analysis failed: {str(e)}")

            results["processing_time"] = time.time() - start_time

            return results

        except Exception as e:
            logger.error(f"AI analysis failed: {str(e)}")
            raise

    async def assess_quality(
        self, document: Document, job: ProcessingJob
    ) -> Dict[str, Any]:
        """Assess document quality"""
        try:
            from src.services.documents.document_quality_service import (
                DocumentQualityService,
            )

            if not hasattr(self, "quality_service"):
                self.quality_service = DocumentQualityService(self.db)

            results = await self.quality_service.comprehensive_quality_assessment(
                document
            )
            results["quality_assessment_performed"] = True

            return results

        except Exception as e:
            logger.error(f"Quality assessment failed: {str(e)}")
            return {
                "quality_assessment_performed": False,
                "error": str(e),
                "overall_score": 0.0,
            }

    def _finalize_job(
        self, job, document: Document, processing_results: Dict[str, Any]
    ) -> None:
        """Mark the ProcessingJob to match the document outcome.

        Completing the job unconditionally marked it COMPLETED even when a step
        failed and ``update_document_with_results`` set the document FAILED —
        leaving a job=COMPLETED / document=FAILED split, so a client polling the
        job status saw a false success. Fail the job when processing did not
        succeed so the two statuses agree.
        """
        if processing_results["success"]:
            job.complete_job(
                result={"processing_results": processing_results},
                artifacts={"processed_files": self.get_processed_files(document)},
                metrics={
                    "processing_time_seconds": processing_results["processing_time"]
                },
            )
            logger.info(f"Document processing completed for {document.id}")
        else:
            job.fail_job(
                error_message="; ".join(processing_results["errors"])
                or "Document processing failed",
                error_type="processing_error",
            )
            logger.warning(
                f"Document processing failed for {document.id}: "
                f"{processing_results['errors']}"
            )

    async def update_document_with_results(
        self, document: Document, results: Dict[str, Any]
    ):
        """Update document with processing results"""
        try:
            # Update text content
            if "text_extraction" in results and results["text_extraction"].get(
                "text_content"
            ):
                document.content_text = results["text_extraction"]["text_content"]

            # Merge figure captions in — must run after content_text is set
            # above (pipeline A assigns content_text last, not inside the
            # figure-extraction step itself).
            captions = (results.get("figure_extraction") or {}).get("captions_text")
            if captions:
                from src.services.processing.figure_extraction_service import (
                    merge_captions_into_text,
                )

                document.content_text = merge_captions_into_text(
                    document.content_text, captions
                )

            # Update processing status
            if results["success"]:
                document.processing_status = ProcessingStatus.COMPLETED
            else:
                document.processing_status = ProcessingStatus.FAILED
                document.processing_error = "; ".join(results["errors"])

            # Update processing timestamps
            document.processing_completed_at = datetime.utcnow()

            # Store processing results in metadata
            document.add_metadata("processing_results", results)
            document.add_metadata("processed_at", datetime.utcnow().isoformat())

            # Mark as indexed if successful
            if results["success"]:
                document.is_indexed = True

            self.db.commit()

        except Exception as e:
            logger.error(f"Failed to update document with results: {str(e)}")
            raise

    def get_processed_files(self, document: Document) -> List[str]:
        """Get list of processed files for document"""
        processed_files = []
        document_id = str(document.id)

        # Check for extracted frames
        frames_dir = self.processed_dir / f"{document_id}_frames"
        if frames_dir.exists():
            processed_files.extend([str(f) for f in frames_dir.glob("*.jpg")])

        # Check for extracted audio
        audio_file = self.processed_dir / f"{document_id}_audio.wav"
        if audio_file.exists():
            processed_files.append(str(audio_file))

        return processed_files

    def estimate_processing_time(
        self, document_type: DocumentType, file_size_bytes: int
    ) -> int:
        """Estimate processing time in seconds"""
        base_times = {
            DocumentType.TEXT: 5,
            DocumentType.PDF: 30,
            DocumentType.IMAGE: 15,
            DocumentType.AUDIO: 60,
            DocumentType.VIDEO: 120,
            DocumentType.SPREADSHEET: 10,
            DocumentType.PRESENTATION: 20,
            DocumentType.MULTIMODAL: 45,
        }

        base_time = base_times.get(document_type, 30)

        # Adjust based on file size (in MB)
        size_mb = file_size_bytes / (1024 * 1024)
        size_factor = max(1.0, size_mb / 10.0)  # Normalize to 10MB

        estimated_time = int(base_time * size_factor)
        return min(estimated_time, 600)  # Cap at 10 minutes


# Dependency injection
def get_multimodal_processing_service(
    db: Session = Depends(get_db),
) -> MultimodalProcessingService:
    """Get multimodal processing service instance"""
    return MultimodalProcessingService(db)
