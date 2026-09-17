"""看板 Web 服务（FastAPI）。

启动:
    uv run research-agent-dashboard --port 8000
打开 http://127.0.0.1:8000 查看动态本体图谱与智能体工作状态。
"""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Optional

from fastapi import Body, FastAPI, Query, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from research_agent import __version__
from research_agent.config import Settings
from research_agent.config import settings as default_settings
from research_agent.dashboard import api as dbapi
from research_agent.dashboard import library_api as libapi
from research_agent.dashboard import writing_api as wrtapi
from research_agent.dashboard import section_api as secapi

STATIC_DIR = Path(__file__).resolve().parent / "static"


def create_app(db_path: str | Path | None = None,
               inject_llms: bool = True,
               settings: Settings | None = None) -> FastAPI:
    """构建看板应用；生产默认注入四个 DeepSeek V4 Pro 研究模型。

    ``settings`` 承载节级写作的阈值等配置；**征求意见与实际撰写必须共用同一个
    Settings**，否则会出现"预览说不足、撰写却通过"的自相矛盾判定。
    """
    _db = str(db_path) if db_path else str(default_settings.db_path)

    app = FastAPI(
        title="research-agent 看板",
        description="动态本体图谱 + 智能体工作状态 + 输入输出",
        version=__version__,
    )
    app.state.db_path = _db
    app.state.inject_llms = bool(inject_llms)
    app.state.settings = settings or Settings(db_path=_db)

    @app.get("/api/health")
    def health(request: Request) -> dict:
        return {"ok": True, "db": request.app.state.db_path}

    @app.get("/api/databases")
    def databases() -> list[dict]:
        return dbapi.list_databases()

    @app.post("/api/db/select")
    def select_db(payload: dict = Body(...)) -> dict:
        path = str(payload.get("path") or "").strip()
        if not path:
            return {"ok": False, "error": "path 为空"}
        if not Path(path).is_file():
            return {"ok": False, "error": f"数据库不存在: {path}"}
        app.state.db_path = path
        return {"ok": True, "db": path}

    @app.get("/api/status/nodes")
    def status_nodes(request: Request = None) -> dict:
        return dbapi.node_status(request.app.state.db_path)

    @app.get("/api/reviews")
    def reviews(request: Request = None,
                history: bool = Query(False)) -> dict:
        return dbapi.human_review_items(
            request.app.state.db_path, include_history=history)

    @app.post("/api/reviews/submit")
    def reviews_submit(payload: dict = Body(...),
                       request: Request = None) -> dict:
        return dbapi.submit_human_review(
            request.app.state.db_path, payload)

    @app.post("/api/planner/run")
    def planner_run(payload: dict = Body(...),
                    request: Request = None) -> dict:
        text = str(payload.get("request") or "").strip()
        if not text:
            return {"ok": False, "error": "指令为空"}
        db_path = payload.get("db") or request.app.state.db_path
        # 缺 Key / 模型构建失败时**不再直接失败**：planner 节点本身支持
        # model=None 走确定性任务单（planner.py 的 offline_fallback 分支）。
        # 原实现在这里抛异常，被 except 吞成 ok=False，用户只看到"执行失败"，
        # 既拿不到契约、也看不出是不是 Key 的问题。
        model = None
        model_error = ""
        if app.state.inject_llms:
            try:
                from research_agent.models import build_role_model
                model = build_role_model("planner")
            except Exception as exc:  # noqa: BLE001 —— 无 Key 是预期情况
                model_error = f"{type(exc).__name__}: {exc}"
        else:
            model_error = "服务以离线模式启动（inject_llms=False），未注入模型"
        try:
            result = dbapi.run_planner_request(text, db_path, model=model)
            plan = result.get("plan") or {}
            return {
                "ok": True,
                **result,
                "planner_mode": plan.get("planner_mode"),
                "planner_model_error": plan.get("planner_model_error") or model_error,
                "model_used": model is not None,
            }
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}

    @app.post("/api/study/run")
    def study_run(payload: dict = Body(...),
                  request: Request = None) -> dict:
        """后台启动一次 Planner-first 研究流程，生产环境注入真实 LLM。"""
        text = str(payload.get("request") or "").strip()
        if not text:
            return {"ok": False, "error": "指令为空"}
        db_path = payload.get("db") or request.app.state.db_path
        import threading
        import uuid
        job_id = uuid.uuid4().hex[:8]

        def _run() -> None:
            from research_agent.config import Settings
            from research_agent.study.graph import StudyServices, run_study

            try:
                settings = Settings(db_path=Path(db_path))
                if app.state.inject_llms:
                    from research_agent.models import build_role_model
                    from research_agent.pipeline import Services as PipelineServices
                    from research_agent.retrieval.api_clients import ApiHub
                    from research_agent.study.collection import collect_mission

                    pipeline_services = PipelineServices(
                        api=ApiHub(source="fulltext"),
                        retriever_model=build_role_model("retriever"),
                        quality_model=build_role_model("quality"),
                        knowledge_model=build_role_model("knowledge"),
                        settings=settings,
                    )
                    collector = lambda req: collect_mission(
                        req, services=pipeline_services)
                    services = StudyServices(
                        planner_model=build_role_model("planner"),
                        consumer_model=build_role_model("consumer"),
                        content_model=build_role_model("content"),
                        review_model=build_role_model("review"),
                        fact_check_model=build_role_model("fact_check"),
                        settings=settings,
                        collector=collector,
                    )
                else:
                    services = StudyServices(settings=settings)
                run_study(
                    text,
                    services,
                    max_results_override=None,
                )
            except Exception as exc:  # noqa: BLE001
                app.state.last_study_error = {
                    "job_id": job_id, "error": str(exc),
                }

        thread = threading.Thread(target=_run, daemon=True, name="study-run")
        thread.start()
        return {"ok": True, "job_id": job_id, "db": str(db_path)}

    @app.get("/api/overview")
    def overview(request: Request) -> dict:
        return dbapi.overview(request.app.state.db_path)

    @app.get("/api/papers")
    def papers(request: Request) -> list[dict]:
        return dbapi.list_papers(request.app.state.db_path)

    @app.get("/api/papers/{key}")
    def paper_detail(key: str, request: Request) -> dict | None:
        return dbapi.get_paper_detail(key, request.app.state.db_path)

    @app.get("/api/agents")
    def agents(recent: int = Query(40, ge=1, le=200),
               request: Request = None) -> dict:
        agents_list, events = dbapi.agent_status(
            request.app.state.db_path, recent=recent)
        return {"agents": agents_list, "recent": events}

    @app.get("/api/study/status")
    def study_status(request: Request) -> dict:
        return dbapi.study_status(request.app.state.db_path)

    @app.get("/api/logs")
    def logs(limit: int = Query(80, ge=1, le=500),
             node: str | None = Query(None),
             event: str | None = Query(None),
             search: str | None = Query(None),
             request: Request = None) -> list[dict]:
        return dbapi.activity_log(limit=limit, db_path=request.app.state.db_path,
                                  node=node, event=event, search=search)

    @app.get("/api/ontology")
    def ontology(
        min_confidence: float = Query(0.0, ge=0.0, le=1.0),
        types: Optional[str] = Query(None, description="逗号分隔的节点类型"),
        q: Optional[str] = Query(None, description="名称搜索"),
        domain: Optional[str] = Query(None, description="语义节点域 key 或名称"),
        limit: int = Query(800, ge=1, le=5000),
        request: Request = None,
    ) -> dict:
        type_list = [t.strip() for t in types.split(",") if t.strip()] if types else None
        return dbapi.ontology_graph(
            request.app.state.db_path,
            min_confidence=min_confidence, types=type_list,
            query=q, domain=domain, limit=limit,
        )

    @app.get("/api/ontology/nodes/{node_id}")
    def node_detail(node_id: int, request: Request) -> dict | None:
        return dbapi.ontology_node_detail(node_id, request.app.state.db_path)

    # ------------------------------------------------------------ 文献库（合并新增）
    @app.get("/api/library/papers")
    def library_papers(
        request: Request,
        q: str | None = Query(None, description="标题/摘要/期刊/DOI 模糊搜索"),
        status: str | None = Query(None),
        source: str | None = Query(None),
        source_type: str | None = Query(None),
        decision: str | None = Query(None, description="direct/flagged/human"),
        year_from: int | None = Query(None),
        year_to: int | None = Query(None),
        year_mode: str | None = Query(None, description="传 unknown 只看年份缺失"),
        tag: str | None = Query(None),
        folder_id: int | None = Query(None),
        favorite: bool = Query(False),
        low_signal: str | None = Query(None, description="yes/no，按检索门控标注过滤"),
        has_knowledge: bool | None = Query(None),
        sort_by: str = Query("quality"),
        sort_desc: bool = Query(True),
        limit: int = Query(50, ge=1, le=500),
        offset: int = Query(0, ge=0),
    ) -> dict:
        return libapi.list_papers(
            request.app.state.db_path, q=q, status=status, source=source,
            source_type=source_type, decision=decision, year_from=year_from,
            year_to=year_to, year_mode=year_mode, tag=tag, folder_id=folder_id,
            favorite=favorite, low_signal=low_signal, has_knowledge=has_knowledge,
            sort_by=sort_by, sort_desc=sort_desc, limit=limit, offset=offset,
        )

    @app.get("/api/library/facets")
    def library_facets(request: Request) -> dict:
        return libapi.facets(request.app.state.db_path)

    @app.get("/api/library/stats")
    def library_stats(request: Request) -> dict:
        return libapi.stats(request.app.state.db_path)

    @app.get("/api/library/paper/{key}")
    def library_paper_sidecar(key: str, request: Request) -> dict:
        data = libapi.paper_sidecar(request.app.state.db_path, key)
        detail = dbapi.get_paper_detail(key, request.app.state.db_path)
        data["detail"] = detail
        return data

    @app.post("/api/library/tags")
    def library_add_tag(payload: dict = Body(...),
                        request: Request = None) -> dict:
        key = str(payload.get("paper_key") or "")
        tag = str(payload.get("tag") or "")
        if not key or not tag.strip():
            return {"ok": False, "error": "paper_key 与 tag 均为必填"}
        added = libapi.add_tag(request.app.state.db_path, key, tag)
        return {"ok": True, "added": added,
                "tags": libapi.paper_sidecar(request.app.state.db_path, key)["tags"]}

    @app.delete("/api/library/tags")
    def library_remove_tag(paper_key: str = Query(...), tag: str = Query(...),
                           request: Request = None) -> dict:
        removed = libapi.remove_tag(request.app.state.db_path, paper_key, tag)
        return {"ok": True, "removed": removed,
                "tags": libapi.paper_sidecar(request.app.state.db_path, paper_key)["tags"]}

    @app.post("/api/library/folders")
    def library_create_folder(payload: dict = Body(...),
                              request: Request = None) -> dict:
        name = str(payload.get("name") or "").strip()
        if not name:
            return {"ok": False, "error": "文件夹名不能为空"}
        try:
            folder_id = libapi.create_folder(request.app.state.db_path, name)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "folder_id": folder_id, "name": name}

    @app.delete("/api/library/folders/{folder_id}")
    def library_delete_folder(folder_id: int, request: Request) -> dict:
        libapi.delete_folder(request.app.state.db_path, folder_id)
        return {"ok": True, "folder_id": folder_id}

    @app.post("/api/library/folders/{folder_id}/papers")
    def library_add_to_folder(folder_id: int, payload: dict = Body(...),
                              request: Request = None) -> dict:
        keys = [str(k) for k in payload.get("paper_keys") or [] if k]
        if not keys:
            return {"ok": False, "error": "paper_keys 为空"}
        changed = libapi.add_to_folder(request.app.state.db_path, folder_id, keys)
        return {"ok": True, "folder_id": folder_id, "changed": changed}

    @app.delete("/api/library/folders/{folder_id}/papers")
    def library_remove_from_folder(folder_id: int, payload: dict = Body(...),
                                   request: Request = None) -> dict:
        keys = [str(k) for k in payload.get("paper_keys") or [] if k]
        if not keys:
            return {"ok": False, "error": "paper_keys 为空"}
        changed = libapi.remove_from_folder(request.app.state.db_path, folder_id, keys)
        return {"ok": True, "folder_id": folder_id, "changed": changed}

    @app.post("/api/library/favorites")
    def library_favorites(payload: dict = Body(...), request: Request = None) -> dict:
        keys = [str(k) for k in payload.get("paper_keys") or [] if k]
        value = bool(payload.get("value", True))
        if not keys:
            return {"ok": False, "error": "paper_keys 为空"}
        changed = libapi.set_favorites(request.app.state.db_path, keys, value)
        return {"ok": True, "changed": changed, "value": value}

    @app.post("/api/library/batch")
    def library_batch(payload: dict = Body(...), request: Request = None) -> dict:
        action = str(payload.get("action") or "")
        keys = [str(k) for k in payload.get("paper_keys") or [] if k]
        if not action:
            return {"ok": False, "error": "action 必填"}
        if not keys:
            return {"ok": False, "error": "paper_keys 为空"}
        return libapi.start_batch(
            request.app.state.db_path, action, keys,
            payload.get("payload") if isinstance(payload.get("payload"), dict) else {},
        )

    @app.get("/api/library/jobs")
    def library_jobs(request: Request,
                     limit: int = Query(20, ge=1, le=100)) -> list[dict]:
        return libapi.list_jobs(request.app.state.db_path, limit=limit)

    @app.get("/api/library/jobs/{job_id}")
    def library_job(job_id: str, request: Request) -> dict | None:
        return libapi.job_status(request.app.state.db_path, job_id)

    @app.post("/api/library/jobs/{job_id}/cancel")
    def library_job_cancel(job_id: str, request: Request) -> dict:
        ok = libapi.cancel_job(request.app.state.db_path, job_id)
        return {"ok": ok, "job_id": job_id}

    @app.get("/api/library/citation/{key}")
    def library_citation(key: str, request: Request,
                         style: str | None = Query(None)) -> dict:
        return libapi.citation_for(request.app.state.db_path, key, style)

    @app.get("/api/library/export")
    def library_export(request: Request,
                       keys: str = Query(..., description="逗号分隔的 paper_key"),
                       style: str | None = Query(None)) -> dict:
        wanted = [k.strip() for k in keys.split(",") if k.strip()]
        if not wanted:
            return {"ok": False, "error": "keys 为空"}
        return libapi.export_records(request.app.state.db_path, wanted, style)

    # ------------------------------------------------------------ 写作台（合并新增）
    @app.get("/api/writing/genres")
    def writing_genres() -> list[dict]:
        return wrtapi.genres()

    @app.get("/api/writing/projects")
    def writing_projects(request: Request) -> list[dict]:
        return wrtapi.list_projects(request.app.state.db_path)

    @app.post("/api/writing/projects")
    def writing_create_project(payload: dict = Body(...),
                               request: Request = None) -> dict:
        return wrtapi.create_project(
            request.app.state.db_path,
            str(payload.get("title") or ""),
            str(payload.get("topic") or ""),
            payload.get("genre"),
        )

    @app.get("/api/writing/projects/{project_id}")
    def writing_project_detail(project_id: int, request: Request) -> dict:
        return wrtapi.project_detail(request.app.state.db_path, project_id)

    @app.delete("/api/writing/projects/{project_id}")
    def writing_delete_project(project_id: int, request: Request) -> dict:
        return wrtapi.delete_project(request.app.state.db_path, project_id)

    @app.post("/api/writing/projects/{project_id}/outline")
    def writing_outline(project_id: int, request: Request,
                        payload: dict | None = Body(default=None)) -> dict:
        payload = payload or {}
        return wrtapi.generate_outline(
            request.app.state.db_path, project_id,
            topic=str(payload.get("topic") or ""),
            use_model=bool(payload.get("use_model", True)),
        )

    @app.post("/api/writing/projects/{project_id}/sections")
    def writing_generate_section(project_id: int, payload: dict = Body(...),
                                 request: Request = None) -> dict:
        return wrtapi.generate_section(
            request.app.state.db_path, project_id,
            str(payload.get("section_key") or ""),
            str(payload.get("heading") or ""),
            topic=str(payload.get("topic") or ""),
            use_model=bool(payload.get("use_model", True)),
        )

    @app.put("/api/writing/projects/{project_id}/sections")
    def writing_save_section(project_id: int, payload: dict = Body(...),
                             request: Request = None) -> dict:
        return wrtapi.save_section(
            request.app.state.db_path, project_id,
            str(payload.get("section_key") or ""),
            str(payload.get("heading") or ""),
            str(payload.get("content") or ""),
            payload.get("citation_ids"),
        )

    @app.post("/api/writing/projects/{project_id}/polish")
    def writing_polish(project_id: int, payload: dict = Body(...),
                       request: Request = None) -> dict:
        return wrtapi.polish_section(
            request.app.state.db_path, project_id,
            str(payload.get("section_key") or ""),
            str(payload.get("content") or ""),
            use_model=bool(payload.get("use_model", True)),
        )

    @app.get("/api/writing/projects/{project_id}/export")
    def writing_export(project_id: int, request: Request) -> dict:
        return wrtapi.export_project(request.app.state.db_path, project_id)

    # ------------------------------------------ 大纲节点指令工作流（合并新增）
    # ------------------------------------------ 工作规划（唯一的规划节点）
    @app.post("/api/writing/projects/{project_id}/plan")
    def writing_project_plan(project_id: int, request: Request,
                             payload: dict | None = Body(default=None)) -> dict:
        payload = payload or {}
        return secapi.project_plan(
            request.app.state.db_path, project_id,
            topic=str(payload.get("topic") or ""),
            instruction=str(payload.get("instruction") or ""),
            settings=request.app.state.settings,
            persist=bool(payload.get("persist", True)),
            use_model=bool(payload.get("use_model", True)))

    @app.get("/api/writing/templates")
    def writing_templates(genre: str = "", request: Request = None) -> dict:
        return secapi.section_templates(
            request.app.state.db_path, genre or None)

    # ------------------------------------------ 访谈（唯一的交互节点）
    @app.post("/api/writing/projects/{project_id}/interview/start")
    def interview_start(project_id: int, request: Request,
                        payload: dict | None = Body(default=None)) -> dict:
        payload = payload or {}
        return secapi.interview_start(
            request.app.state.db_path, project_id,
            reset=bool(payload.get("reset", False)),
            genre=str(payload.get("genre") or ""),
            topic=str(payload.get("topic") or ""),
            sections_list=payload.get("sections"))

    @app.get("/api/writing/projects/{project_id}/interview")
    def interview_get(project_id: int, request: Request) -> dict:
        return secapi.interview_snapshot(
            request.app.state.db_path, project_id)

    @app.post("/api/writing/projects/{project_id}/interview/answer")
    def interview_answer(project_id: int, request: Request,
                         payload: dict = Body(...)) -> dict:
        return secapi.interview_answer(
            request.app.state.db_path, project_id, payload)

    @app.post("/api/writing/projects/{project_id}/interview/step")
    def interview_step(project_id: int, request: Request,
                       payload: dict | None = Body(default=None)) -> dict:
        payload = payload or {}
        return secapi.interview_step(
            request.app.state.db_path, project_id,
            settings=request.app.state.settings,
            use_model=bool(payload.get("use_model", True)))

    @app.post("/api/writing/projects/{project_id}/sections/{section_key}/plan")
    def section_plan(project_id: int, section_key: str, request: Request,
                     payload: dict | None = Body(default=None)) -> dict:
        payload = payload or {}
        return secapi.plan_section(
            request.app.state.db_path, project_id, section_key,
            str(payload.get("instruction") or ""),
            settings=request.app.state.settings,
            user_fields=payload.get("fields"),
            use_model=bool(payload.get("use_model", True)))

    @app.post("/api/writing/projects/{project_id}/sections/{section_key}/compose")
    def section_compose(project_id: int, section_key: str, request: Request,
                        payload: dict | None = Body(default=None)) -> dict:
        payload = payload or {}
        return secapi.compose_section(
            request.app.state.db_path, project_id, section_key,
            str(payload.get("instruction") or ""),
            settings=request.app.state.settings,
            user_fields=payload.get("fields"),
            use_model=bool(payload.get("use_model", True)))

    @app.get("/api/writing/section-jobs/{job_id}")
    def section_job(job_id: str, request: Request) -> dict:
        return secapi.job_status(request.app.state.db_path, job_id)

    @app.post("/api/writing/section-jobs/{job_id}/cancel")
    def section_job_cancel(job_id: str, request: Request) -> dict:
        return secapi.cancel_job(request.app.state.db_path, job_id)

    @app.get("/api/writing/projects/{project_id}/sections/{section_key}/trace")
    def section_trace(project_id: int, section_key: str, request: Request,
                      limit: int = 20) -> dict:
        return secapi.section_trace(
            request.app.state.db_path, project_id, section_key, limit)

    @app.get("/api/writing/projects/{project_id}/sections/{section_key}/content")
    def section_content(project_id: int, section_key: str, request: Request) -> dict:
        return secapi.section_content(
            request.app.state.db_path, project_id, section_key)

    @app.get("/api/writing/projects/{project_id}/section-states")
    def section_states(project_id: int, request: Request) -> dict:
        return secapi.section_states(request.app.state.db_path, project_id)

    # ------------------------------------------------------------ 系统状态（合并新增）
    @app.get("/api/system/packs")
    def system_packs(request: Request) -> dict:
        return libapi.system_packs(request.app.state.db_path)

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    return app


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="research-agent 图形化看板")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--db", help="SQLite 数据库路径（默认 data/research_agent.db）")
    args = ap.parse_args(argv)

    import uvicorn

    app = create_app(args.db)
    print(f"看板已启动: http://{args.host}:{args.port}  (DB: {app.state.db_path})")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
