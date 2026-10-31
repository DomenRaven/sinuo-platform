"""Read-only view of one finished fake chain.

The page and the JSON route both call ``read_fake_chain``. Confirm and reject
on the page change only the browser's local text. They do not call
``consume_candidate`` and they do not create a page version.
"""

import html

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy.orm import Session

from sinuo import __version__
from sinuo.api.auth.dependencies import get_engine, get_settings, session_token
from sinuo.api.auth.errors import request_id_for
from sinuo.application.agents.read_chain import FakeChainReading, read_fake_chain
from sinuo.application.auth.authorizer import SessionAuthorizer
from sinuo.application.auth.sessions import resolve_principal
from sinuo.domain.public_contract import ErrorCode

router = APIRouter(prefix="/api/v1")
_JSON_PATH = "/projects/{project_id}/agent-runs/{parent_run_id}/fake-chain"
_PAGE_PATH = "/projects/{project_id}/agent-runs/{parent_run_id}/fake-chain/view"
_NOTICE = "页快照和检索来自已测后端。这次运行用的是 FakeModelProvider，不是真实模型。"
_ROLE_LABELS = {
    "clarification": "澄清",
    "evidence": "查证据",
    "method": "问方法",
    "critic": "批评",
}


def _reading(request: Request, project_id: str, parent_run_id: str) -> FakeChainReading:
    settings = get_settings(request)
    with Session(get_engine(request)) as session:
        principal = resolve_principal(session, session_token(request, settings))
        return read_fake_chain(
            session,
            SessionAuthorizer(session),
            principal,
            project_id,
            parent_run_id,
        )


def _payload(reading: FakeChainReading) -> dict[str, object]:
    return {
        "found": reading.found,
        "parent_run_id": reading.parent_run_id,
        "parent_status": reading.parent_status,
        "critic_run_id": reading.critic_run_id,
        "critic_status": reading.critic_status,
        "steps": [
            {"run_id": step.run_id, "role": step.role, "status": step.status}
            for step in reading.steps
        ],
        "hits": [
            {"artifact_version_id": hit.artifact_version_id, "excerpt": hit.excerpt}
            for hit in reading.hits
        ],
        "page_title": reading.page_title,
        "page_body": reading.page_body,
        "method_question": reading.method_question,
        "candidate_status": reading.candidate_status,
        "candidate_body": reading.candidate_body,
    }


@router.get(_JSON_PATH)
def get_fake_chain(project_id: str, parent_run_id: str, request: Request) -> JSONResponse:
    try:
        reading = _reading(request, project_id, parent_run_id)
    except ValueError:
        body = {
            "error": {
                "code": ErrorCode.INVALID_SCHEMA.value,
                "message": "Invalid schema.",
                "details": {},
                "retryable": False,
                "request_id": request_id_for(request),
            }
        }
        response = JSONResponse(body, status_code=422)
        response.headers["Cache-Control"] = "no-store"
        return response
    response = JSONResponse(
        {
            "data": _payload(reading),
            "meta": {"request_id": request_id_for(request), "version": __version__},
        }
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get(_PAGE_PATH, response_class=HTMLResponse)
def view_fake_chain(project_id: str, parent_run_id: str, request: Request) -> HTMLResponse:
    try:
        reading = _reading(request, project_id, parent_run_id)
    except ValueError:
        response = HTMLResponse("没有可显示的运行。", status_code=422)
        response.headers["Cache-Control"] = "no-store"
        return response
    response = HTMLResponse(_page(reading))
    response.headers["Cache-Control"] = "no-store"
    return response


def _page(reading: FakeChainReading) -> str:
    steps = "".join(
        "<li>{label} · {status}</li>".format(
            label=html.escape(_ROLE_LABELS.get(step.role, step.role)),
            status=html.escape(step.status),
        )
        for step in reading.steps
    )
    if not steps:
        steps = "<li>没有可显示的步骤。</li>"
    cards = "".join(
        "<article class='card'><p>{excerpt}</p><p>作品版本 {version}</p></article>".format(
            excerpt=html.escape(hit.excerpt),
            version=html.escape(hit.artifact_version_id),
        )
        for hit in reading.hits
    )
    if reading.page_title or reading.page_body:
        cards += "<article class='card'><p>{title}</p><p>{body}</p></article>".format(
            title=html.escape(reading.page_title or ""),
            body=html.escape(reading.page_body or ""),
        )
    if not cards:
        cards = "<p>没有可显示的证据。</p>"
    question = html.escape(reading.method_question or "没有可显示的方法问句。")
    if reading.candidate_body:
        candidate = "<pre>{body}</pre>".format(body=html.escape(reading.candidate_body))
    else:
        candidate = "<p>没有待确认候选。</p>"
    parent = html.escape(reading.parent_status or "没有父运行")
    critic = html.escape(reading.critic_status or "没有批评运行")
    notice = html.escape(_NOTICE)
    return (
        "<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        "<title>假模型链</title><style>"
        "body{font-family:Segoe UI,sans-serif;margin:2rem auto;max-width:40rem;line-height:1.5}"
        ".notice{background:#f4f1ea;padding:.75rem 1rem}"
        ".card{border:1px solid #ccc;padding:.75rem 1rem;margin:.5rem 0}"
        "pre{white-space:pre-wrap;background:#f7f7f7;padding:.75rem}"
        "</style></head><body>"
        f"<p class=\"notice\">{notice}</p>"
        "<h1>已跑完的假模型链</h1>"
        f"<p>父运行 {parent}。批评运行 {critic}。</p>"
        "<h2>步骤</h2><ol>"
        + steps
        + "</ol><h2>证据</h2>"
        + cards
        + "<h2>方法问句</h2><p id=\"method-question\">"
        + question
        + "</p><h2>待确认候选</h2>"
        + candidate
        + "<p><button type=\"button\" id=\"confirm-local\">确认</button> "
        "<button type=\"button\" id=\"reject-local\">拒绝</button></p>"
        "<p id=\"local-choice\">尚未在本页选择。</p>"
        "<script>"
        "const choice=document.getElementById('local-choice');"
        "document.getElementById('confirm-local').addEventListener('click',function(){"
        "choice.textContent='本页已标为确认。没有提交到后端。';"
        "});"
        "document.getElementById('reject-local').addEventListener('click',function(){"
        "choice.textContent='本页已标为拒绝。没有提交到后端。';"
        "});"
        "</script></body></html>"
    )
