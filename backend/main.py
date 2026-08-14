"""FastAPI application: wires routers, lifecycle, health and static hosting."""

import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from database import SessionLocal, init_db
from feishu.routes import close_client as close_feishu_router_client
from feishu.routes import router as feishu_router
from feishu.sync import bitable_status
from grading.llm import LLMClient  # kept so tests can patch main.LLMClient

from api import state
from api.assessment import router as assessment_router
from api.comments import router as comments_router
from api.companion import router as companion_router
from api.courses import router as courses_router
from api.prep import router as prep_router
from api.recordings import router as recordings_router
from api.reports import router as reports_router
from api.settings import router as settings_router
from api.tags import router as tags_router

app = FastAPI(title="思辨星 · 少儿思辨能力认知自适应评估系统", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(feishu_router)
app.include_router(settings_router)
app.include_router(courses_router)
app.include_router(assessment_router)
app.include_router(companion_router)
app.include_router(recordings_router)
app.include_router(comments_router)
app.include_router(prep_router)
app.include_router(tags_router)
app.include_router(reports_router)

# Backward-compatible alias for tests that referenced main.UPLOAD_DIR.
UPLOAD_DIR = state.UPLOAD_DIR


def __getattr__(name):
    # Runtime singletons live in api.state so that reload_runtime_settings()
    # can rebuild them; expose the current object through main.<name> for
    # tests and any external tooling that still references main.llm etc.
    if name in (
        "llm", "evaluator", "companion", "feishu_client",
        "_assessment_progress", "_progress_lock",
    ):
        return getattr(state, name)
    if name in ("_prep_topic_rows", "_prep_insights", "_build_prep_plan_card_content"):
        from api import prep as _prep_module
        return getattr(_prep_module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


@app.on_event("startup")
def on_startup():
    init_db()
    db = SessionLocal()
    try:
        state.reload_runtime_settings(db)
    finally:
        db.close()


@app.on_event("shutdown")
async def on_shutdown():
    await state.feishu_client.close()
    await close_feishu_router_client()


@app.get("/api/health")
async def health_check():
    feishu = await state.feishu_client.health_check()
    return {
        "status": "ok" if feishu["status"] == "auth_ok" else "degraded",
        "database": "ready",
        "feishu": feishu,
        "bitable": bitable_status(state.feishu_client.config),
    }

# Serve built frontend (production mode)

_frontend_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "frontend", "dist")

if os.path.isdir(_frontend_dir):
    @app.get("/")
    def _serve_index():
        return FileResponse(os.path.join(_frontend_dir, "index.html"))

    # Must come AFTER all other routes
    app.mount("/assets", StaticFiles(directory=os.path.join(_frontend_dir, "assets")), name="static-assets")

    @app.get("/{full_path:path}")
    def _spa_fallback(full_path: str):
        """SPA fallback: serve index.html for any non-API route."""
        return FileResponse(os.path.join(_frontend_dir, "index.html"))
