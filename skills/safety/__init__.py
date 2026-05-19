"""Safety skill — PII masking, content/topic safety, deepfake detection.

Pure helpers live in :mod:`skills.safety._internal`. The project-internal
``mask`` wrapper used by ``runtime.conversation_logger`` and
``runtime.clipboard`` lives in :mod:`skills.safety.pii` and is a no-op when
the skill is not loaded.
"""
