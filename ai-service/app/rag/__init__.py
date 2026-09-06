"""Day 17 — RAG generation.

Question + retrieved context → grounded answer, with an honest "I don't know"
guardrail and bounded context. Lives in the AI service (the service that owns
LLM calls, mirroring the embedding layer); the backend retrieves tenant-scoped
chunks and ships them here. See app/rag/{schemas,provider,generator}.py.
"""