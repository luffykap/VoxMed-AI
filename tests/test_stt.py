"""
tests/test_stt.py
Run with: python -m pytest tests/test_stt.py -v
"""
import pytest
import sys
from unittest.mock import MagicMock, patch

# Mock faster_whisper and pyaudio to avoid ModuleNotFoundError in test environment
mock_faster_whisper = MagicMock()
sys.modules['faster_whisper'] = mock_faster_whisper
sys.modules['pyaudio'] = MagicMock()

# Now we can safely import stt
from processing.stt import transcribe

@patch('processing.stt._model.transcribe')
@patch('processing.stt.record_audio')
def test_transcribe_success(mock_record, mock_transcribe):
    # Mock Whisper Info object
    mock_info = MagicMock()
    mock_info.language = "en"
    mock_info.language_probability = 0.95
    
    # Mock Whisper Segment object
    mock_seg = MagicMock()
    mock_seg.text = "Hello world"
    mock_seg.avg_logprob = -0.1 # roughly 0.90 confidence
    
    mock_transcribe.return_value = ([mock_seg], mock_info)
    
    result = transcribe()
    
    assert result["text"] == "Hello world"
    assert result["language"] == "en"
    assert result["confidence"] > 0.8
    assert mock_record.called

@patch('processing.stt._model.transcribe')
@patch('processing.stt.record_audio')
def test_transcribe_low_confidence(mock_record, mock_transcribe):
    # Mock Whisper Info object
    mock_info = MagicMock()
    mock_info.language = "en"
    mock_info.language_probability = 0.4
    
    # Mock Whisper Segment object
    mock_seg = MagicMock()
    mock_seg.text = "mumble mumble"
    mock_seg.avg_logprob = -2.3 # roughly 0.1 confidence
    
    mock_transcribe.return_value = ([mock_seg], mock_info)
    
    result = transcribe()
    
    assert result["text"] == "mumble mumble"
    assert result["language"] == "en"
    assert result["confidence"] < 0.4

@patch('processing.stt._model.transcribe')
@patch('processing.stt.record_audio')
def test_transcribe_error(mock_record, mock_transcribe):
    mock_transcribe.side_effect = Exception("Whisper crashed")
    
    result = transcribe()
    
    assert result["text"] == ""
    assert result["language"] == "unknown"
    assert result["confidence"] == 0.0
