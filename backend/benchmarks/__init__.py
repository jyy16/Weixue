"""Weixue performance benchmark suite.

Measures the real (LLM/ASR-backed) pipeline end to end:
  - speech transcription wall-clock / RTF / transcript size
  - batch assessment latency & reliability (9 students x 3 topics = 27)
  - comment-draft generation latency & output compliance
  - AI-vs-teacher score agreement + rough token/cost estimate

The suite runs against a dedicated SQLite database and a temporary uvicorn
server; application code is never modified.
"""

