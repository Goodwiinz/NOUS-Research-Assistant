#!/usr/bin/env python3
"""
Test script for video processing integration
Tests the complete video upload and processing pipeline
"""

import requests
import json
import time
from pathlib import Path
import sys

# API Configuration
API_BASE_URL = "http://localhost:8000"
API_V1 = f"{API_BASE_URL}/api/v1"


def print_section(title):
    """Print a formatted section header"""
    print("\n" + "=" * 60)
    print(f"  {title}")
    print("=" * 60)


def test_health():
    """Test API health endpoint"""
    print_section("Testing API Health")

    try:
        response = requests.get(f"{API_BASE_URL}/health")
        response.raise_for_status()

        health = response.json()
        print(f"✓ API Status: {health['status']}")
        return True

    except Exception as e:
        print(f"✗ Health check failed: {str(e)}")
        return False


def test_video_upload(video_path=None):
    """Test video file upload"""
    print_section("Testing Video Upload")

    if not video_path:
        print("⚠ No video file provided. Skipping upload test.")
        print("  Usage: python test_video_integration.py /path/to/video.mp4")
        return None

    video_file = Path(video_path)
    if not video_file.exists():
        print(f"✗ Video file not found: {video_path}")
        return None

    try:
        print(f"📤 Uploading video: {video_file.name}")
        print(f"   Size: {video_file.stat().st_size / (1024*1024):.2f} MB")

        with open(video_file, 'rb') as f:
            files = {
                'file': (video_file.name, f, 'video/mp4')
            }
            data = {
                'title': f'Test Video: {video_file.stem}',
                'description': 'Integration test video upload'
            }

            response = requests.post(
                f"{API_V1}/files/upload",
                files=files,
                data=data
            )
            response.raise_for_status()

            upload_result = response.json()
            print(f"✓ Upload successful!")
            print(f"  Document ID: {upload_result['id']}")
            print(f"  Type: {upload_result['document_type']}")
            print(f"  Status: {upload_result['processing_status']}")

            return upload_result

    except Exception as e:
        print(f"✗ Upload failed: {str(e)}")
        if hasattr(e, 'response'):
            print(f"  Response: {e.response.text}")
        return None


def test_processing_status(document_id):
    """Test processing status check"""
    print_section("Testing Processing Status")

    try:
        max_retries = 30
        retry_interval = 2

        for i in range(max_retries):
            response = requests.get(f"{API_V1}/documents/{document_id}")
            response.raise_for_status()

            doc = response.json()
            status = doc['processing_status']

            print(f"  [{i+1}/{max_retries}] Status: {status}")

            if status == 'completed':
                print("✓ Processing completed successfully!")
                return doc
            elif status == 'failed':
                print(f"✗ Processing failed: {doc.get('error_message', 'Unknown error')}")
                return doc

            time.sleep(retry_interval)

        print("⚠ Processing timeout - still in progress")
        return None

    except Exception as e:
        print(f"✗ Status check failed: {str(e)}")
        return None


def test_video_results(document_id):
    """Test retrieval of video processing results"""
    print_section("Testing Video Processing Results")

    try:
        response = requests.get(f"{API_V1}/documents/{document_id}/processing-results")
        response.raise_for_status()

        results = response.json()

        print("✓ Processing results retrieved!")
        print("\n📊 Video Analysis Summary:")

        # Metadata
        if 'metadata' in results:
            metadata = results['metadata']
            print(f"\n  📹 Video Metadata:")
            print(f"     Duration: {metadata.get('duration', 0):.1f} seconds")
            print(f"     Format: {metadata.get('format_name', 'unknown')}")
            print(f"     Size: {metadata.get('size', 0) / (1024*1024):.2f} MB")
            print(f"     Bit rate: {metadata.get('bit_rate', 0) / 1000000:.2f} Mbps")

            video_streams = metadata.get('video_streams', [])
            if video_streams:
                stream = video_streams[0]
                print(f"     Resolution: {stream.get('width')}x{stream.get('height')}")
                print(f"     Codec: {stream.get('codec_name', 'unknown')}")

        # Content analysis
        if 'content_analysis' in results:
            content = results['content_analysis']
            print(f"\n  🎬 Content Analysis:")
            print(f"     Video type: {content.get('video_type', 'unknown')}")
            print(f"     Duration category: {content.get('duration_category', 'unknown')}")
            print(f"     Has audio: {content.get('has_audio', False)}")
            print(f"     Has subtitles: {content.get('has_subtitles', False)}")

        # Audio analysis
        if 'audio_analysis' in results:
            audio = results['audio_analysis']
            print(f"\n  🔊 Audio Analysis:")
            print(f"     Extracted: {audio.get('extracted', False)}")
            print(f"     Transcribed: {audio.get('transcribed', False)}")

            if audio.get('transcribed') and 'transcription_results' in audio:
                trans = audio['transcription_results']
                print(f"     Language: {trans.get('language', 'unknown')}")
                print(f"     Word count: {trans.get('word_count', 0)}")
                print(f"     Confidence: {trans.get('confidence', 0):.1%}")

        # Frame analysis
        if 'frame_analysis' in results:
            frames = results['frame_analysis']
            print(f"\n  🖼️  Frame Analysis:")
            print(f"     Keyframes extracted: {frames.get('keyframe_count', 0)}")

        # Quality score
        if 'quality_score' in results:
            print(f"\n  ⭐ Quality Score: {results['quality_score']:.1%}")

        return results

    except Exception as e:
        print(f"✗ Results retrieval failed: {str(e)}")
        if hasattr(e, 'response'):
            print(f"  Response: {e.response.text}")
        return None


def test_search_video(query="video"):
    """Test searching for video documents"""
    print_section("Testing Video Search")

    try:
        response = requests.get(
            f"{API_V1}/search",
            params={'query': query, 'document_type': 'video'}
        )
        response.raise_for_status()

        results = response.json()
        print(f"✓ Found {len(results.get('results', []))} video documents")

        for i, doc in enumerate(results.get('results', [])[:3], 1):
            print(f"\n  {i}. {doc.get('title', 'Untitled')}")
            print(f"     ID: {doc.get('id')}")
            print(f"     Type: {doc.get('document_type')}")
            print(f"     Score: {doc.get('score', 0):.3f}")

        return results

    except Exception as e:
        print(f"✗ Search failed: {str(e)}")
        return None


def main():
    """Main test execution"""
    print("\n" + "🎥" * 30)
    print("  VIDEO PROCESSING INTEGRATION TEST")
    print("🎥" * 30)

    # Parse command line arguments
    video_path = sys.argv[1] if len(sys.argv) > 1 else None

    # Test 1: Health check
    if not test_health():
        print("\n❌ API is not healthy. Aborting tests.")
        return 1

    # Test 2: Upload video
    upload_result = test_video_upload(video_path)
    if not upload_result:
        if video_path:
            print("\n⚠️  Upload failed. Cannot test processing.")
            return 1
        else:
            print("\n⚠️  Skipping upload tests (no video file provided)")

    # Test 3: Check processing status
    if upload_result:
        document_id = upload_result['id']
        doc_result = test_processing_status(document_id)

        # Test 4: Get processing results
        if doc_result and doc_result.get('processing_status') == 'completed':
            test_video_results(document_id)

    # Test 5: Search for videos
    test_search_video("test video")

    # Summary
    print_section("Test Summary")
    print("✓ All available tests completed!")
    print("\nTo test video upload and processing:")
    print(f"  python {sys.argv[0]} /path/to/your/video.mp4")

    return 0


if __name__ == "__main__":
    try:
        exit(main())
    except KeyboardInterrupt:
        print("\n\n⚠️  Test interrupted by user")
        exit(130)
    except Exception as e:
        print(f"\n\n❌ Test failed with error: {str(e)}")
        import traceback
        traceback.print_exc()
        exit(1)
