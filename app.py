from __future__ import annotations

from collections import defaultdict
from datetime import date
from functools import wraps
from collections import Counter
from pathlib import Path
import os
import re
from typing import Callable
from uuid import uuid4
import math

from flask import Flask, abort, flash, jsonify, redirect, render_template, request, send_from_directory, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

from db import execute, get_connection, init_db, query_all, query_one

app = Flask(__name__)
app.secret_key = os.environ.get("JEROCOIN_SECRET_KEY") or os.environ.get("FLASK_SECRET_KEY") or "jerocoin-local-dev-key-change-this"

ROLE_BY_TEAM_TYPE = {
    "robotica": "robotica_team",
    "desarrollo": "desarrollo_team",
}

ROLE_OPTIONS_BY_TEAM_TYPE = {
    "robotica": ("robotica",),
    "desarrollo": ("COO", "contable", "programador"),
}
INTERNAL_ROLES = tuple(dict.fromkeys(role for roles in ROLE_OPTIONS_BY_TEAM_TYPE.values() for role in roles))
SERVICE_TRACK_OPTIONS = {
    "robotica": "Hardware / Robótica",
    "programacion": "Programación",
    "web_html": "Web / HTML",
}
MARKET_ROLE_OPTIONS = {
    "client_only": "Solo contrata",
    "provider_only": "Solo ofrece",
    "both": "Puede contratar y ofrecer",
}
SERVICE_TRACK_DEFAULT_BY_TEAM_TYPE = {
    "robotica": "robotica",
    "desarrollo": "programacion",
}
PORTFOLIO_SERVICE_CATEGORY_OPTIONS = {
    "programacion_robotica": "Programación para robótica",
    "pagina_web_simple": "Página web simple",
    "landing_html": "Landing HTML",
    "automatizacion": "Automatización",
    "otro": "Otro servicio",
}
SERVICE_TRACK_TO_PORTFOLIO_CATEGORY = {
    "robotica": "programacion_robotica",
    "programacion": "programacion_robotica",
    "web_html": "pagina_web_simple",
}
BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads" / "deliveries"
REQUEST_UPLOAD_DIR = BASE_DIR / "uploads" / "requests"
TEAM_LOGO_DIR = BASE_DIR / "uploads" / "team_logos"
TEAM_GALLERY_DIR = BASE_DIR / "uploads" / "team_gallery"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
REQUEST_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
TEAM_LOGO_DIR.mkdir(parents=True, exist_ok=True)
TEAM_GALLERY_DIR.mkdir(parents=True, exist_ok=True)

ALLOWED_UPLOAD_EXTENSIONS = {
    "py", "txt", "md", "json", "csv", "zip", "rar", "7z", "ino", "c", "cpp", "h", "java", "js", "ts"
}
ALLOWED_IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "webp", "gif"}
AI_SUPPORTED_TEXT_EXTENSIONS = {"py", "txt", "md", "json", "csv", "ino", "c", "cpp", "h", "java", "js", "ts"}
AI_MAX_QUESTION_CHARS = 500
AI_MAX_CODE_CHARS = 12000
AI_RESPONSE_MAX_CHARS = 2200

OPEN_CONTRACT_STATUSES = ('pending_interventor_activation', 'active', 'in_development', 'submitted_for_review', 'correction_required')
CLIENT_OPEN_CONTRACT_LIMIT = 2


def bootstrap() -> None:
    init_db()


bootstrap()


def current_user():
    user_id = session.get("user_id")
    if not user_id:
        return None
    return query_one("SELECT * FROM users WHERE id = ? AND active = 1", (user_id,))


def allowed_upload(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_UPLOAD_EXTENSIONS


def allowed_image_upload(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_IMAGE_EXTENSIONS


def get_ai_api_key() -> str | None:
    return os.environ.get("JEROCOIN_OPENAI_API_KEY") or os.environ.get("PANCHICOIN_OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY")


def get_ai_model() -> str:
    return os.environ.get("JEROCOIN_AI_MODEL") or os.environ.get("PANCHICOIN_AI_MODEL", "gpt-5.2")


def ai_feature_enabled() -> bool:
    return bool(get_ai_api_key())


def read_text_file_for_ai(path: Path) -> str | None:
    if not path.exists() or not path.is_file():
        return None
    if path.suffix.lower().lstrip(".") not in AI_SUPPORTED_TEXT_EXTENSIONS:
        return None
    try:
        data = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        try:
            data = path.read_text(encoding="latin-1")
        except Exception:
            return None
    except Exception:
        return None
    data = data.strip()
    return data[:AI_MAX_CODE_CHARS] if data else None


def latest_contract_code_context(conn, contract_id: int) -> tuple[str | None, str | None, str | None]:
    delivery = conn.execute(
        """
        SELECT *
        FROM deliveries
        WHERE contract_id = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (contract_id,),
    ).fetchone()
    if not delivery:
        return None, None, None
    code_text = (delivery["code_text"] or "").strip()
    if code_text:
        return "latest_delivery_code", code_text[:AI_MAX_CODE_CHARS], "último código pegado en una entrega"
    stored_filename = delivery["stored_filename"]
    if stored_filename:
        file_text = read_text_file_for_ai(UPLOAD_DIR / stored_filename)
        if file_text:
            label = delivery["original_filename"] or stored_filename
            return "latest_delivery_file", file_text, f"último archivo de entrega ({label})"
    return None, None, None


def response_looks_like_code(text: str) -> bool:
    if not text:
        return False
    if "```" in text:
        return True
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    if not lines:
        return False
    code_like = 0
    for line in lines:
        stripped = line.strip()
        if re.match(r"^(def |class |import |from |if |elif |else:|for |while |try:|except|return |const |let |var |function |public |private |#include)", stripped):
            code_like += 1
        elif stripped.endswith(("{", "}", ":", ";")) and len(stripped.split()) <= 8:
            code_like += 1
        elif stripped.count("(") and stripped.count(")") and any(tok in stripped for tok in ["=", "==", "=>"]):
            code_like += 1
    return code_like >= max(2, len(lines) // 3)


def request_ai_code_explanation(question: str, code_text: str, context: dict[str, str | None]) -> tuple[str, str, str]:
    api_key = get_ai_api_key()
    if not api_key:
        raise RuntimeError("La API key no está configurada en el servidor.")
    try:
        from openai import OpenAI
    except Exception as exc:
        raise RuntimeError("Falta instalar la librería openai en el servidor.") from exc

    client = OpenAI(api_key=api_key)
    model = get_ai_model()
    context_lines = []
    if context.get("team_name"):
        context_lines.append(f"Equipo: {context['team_name']}")
    if context.get("contract_id"):
        context_lines.append(f"Contrato: #{context['contract_id']}")
    if context.get("request_message"):
        context_lines.append(f"Pedido del cliente: {context['request_message']}")
    context_block = "\n".join(context_lines)

    instructions = (
        "Sos un asistente docente de apoyo para estudiantes de desarrollo de secundaria. "
        "Tu tarea es explicar el código que el equipo ya escribió o pegó. "
        "No generes código nuevo, no propongas scripts completos, no devuelvas bloques de código, "
        "no uses markdown con triple tilde y no reescribas funciones. "
        "Respondé en español claro, breve y útil. "
        "Podés explicar qué hace, señalar errores conceptuales, marcar riesgos y sugerir qué revisar, "
        "pero siempre sin escribir código. Si la pregunta pide que programes o completes el código, "
        "negate y redirigí la respuesta a una explicación conceptual."
    )
    prompt = (
        f"{context_block}\n\n"
        f"Pregunta puntual del equipo:\n{question[:AI_MAX_QUESTION_CHARS]}\n\n"
        "Código del equipo para analizar:\n"
        f"{code_text[:AI_MAX_CODE_CHARS]}\n\n"
        "Respondé con una explicación breve, en viñetas o párrafos cortos, sin escribir código."
    )
    response = client.responses.create(model=model, instructions=instructions, input=prompt)
    output = (response.output_text or "").strip()
    if not output:
        output = "No hubo una respuesta útil del asistente. Intenten reformular la pregunta."
    output = output[:AI_RESPONSE_MAX_CHARS]
    status = "answered"
    if response_looks_like_code(output):
        output = "La respuesta fue bloqueada porque se parecía demasiado a código. Reformulen la pregunta para pedir una explicación más conceptual y puntual."
        status = "blocked"
    return model, output, status



def team_type_from_service_track(requested_track: str | None) -> str:
    requested_track = (requested_track or "").strip()
    return "robotica" if requested_track == "robotica" else "desarrollo"


def normalize_service_track(team_type: str, requested_track: str | None) -> str:
    requested_track = (requested_track or "").strip()
    if team_type == "robotica":
        return "robotica"
    if requested_track in SERVICE_TRACK_OPTIONS and requested_track != "robotica":
        return requested_track
    return SERVICE_TRACK_DEFAULT_BY_TEAM_TYPE.get(team_type, "programacion")


def normalize_market_role(team_type: str, service_track: str | None, requested_role: str | None) -> str:
    requested_role = (requested_role or "").strip()
    if requested_role in MARKET_ROLE_OPTIONS:
        return requested_role
    if team_type == "robotica":
        return "client_only"
    if (service_track or "") == "web_html":
        return "provider_only"
    return "both"


def normalize_service_category(requested_category: str | None, fallback_track: str | None = None) -> str:
    requested_category = (requested_category or "").strip()
    if requested_category in PORTFOLIO_SERVICE_CATEGORY_OPTIONS:
        return requested_category
    return SERVICE_TRACK_TO_PORTFOLIO_CATEGORY.get(fallback_track or "programacion", "programacion_robotica")


def market_portfolio_rows(conn, *, active_cycle=None, service_category: str | None = None, service_track: str | None = None):
    filters = ["p.status = 'published'", "t.active = 1"]
    params: list = []
    joins = []
    if active_cycle:
        joins.append("JOIN cycle_teams ct ON ct.team_id = t.id AND ct.cycle_id = ?")
        params.append(active_cycle["id"])
    if service_category and service_category in PORTFOLIO_SERVICE_CATEGORY_OPTIONS:
        filters.append("p.service_category = ?")
        params.append(service_category)
    if service_track and service_track in SERVICE_TRACK_OPTIONS:
        filters.append("t.service_track = ?")
        params.append(service_track)
    sql = f"""
        SELECT p.*, t.name AS team_name, t.team_type, t.service_track,
               t.logo_stored_filename, t.profile_blurb,
               (SELECT stored_filename FROM team_gallery tg WHERE tg.team_id = t.id ORDER BY tg.id DESC LIMIT 1) AS preview_gallery_image
        FROM portfolios p
        JOIN teams t ON p.team_id = t.id
        {' '.join(joins)}
        WHERE {' AND '.join(filters)}
        ORDER BY p.created_at DESC, p.id DESC
    """
    return conn.execute(sql, tuple(params)).fetchall()


def market_offer_rows(conn, *, active_cycle=None, service_category: str | None = None):
    filters = ["ao.status IN ('open', 'taken')"]
    params: list = []
    if active_cycle:
        filters.append("(ao.cycle_id IS NULL OR ao.cycle_id = ?)")
        params.append(active_cycle["id"])
    if service_category and service_category in PORTFOLIO_SERVICE_CATEGORY_OPTIONS:
        filters.append("ao.service_category = ?")
        params.append(service_category)
    sql = f"""
        SELECT ao.*, u.username AS created_by_username, t.name AS taken_by_team_name
        FROM admin_offers ao
        LEFT JOIN users u ON u.id = ao.created_by_user_id
        LEFT JOIN teams t ON t.id = ao.taken_by_team_id
        WHERE {' AND '.join(filters)}
        ORDER BY ao.created_at DESC, ao.id DESC
    """
    return conn.execute(sql, tuple(params)).fetchall()


def offer_allowed_tracks(service_category: str) -> set[str]:
    if service_category in {"pagina_web_simple", "landing_html"}:
        return {"web_html"}
    if service_category in {"programacion_robotica", "automatizacion"}:
        return {"programacion"}
    return {"programacion", "web_html"}


def team_can_take_offer(team, offer) -> bool:
    if not team or team["team_type"] != "desarrollo" or not team["active"]:
        return False
    return (team["service_track"] or "programacion") in offer_allowed_tracks(offer["service_category"])


def team_can_request_portfolio(client_team, portfolio) -> tuple[bool, str | None]:
    if not client_team or not portfolio:
        return False, "No se pudo validar el equipo o el portfolio."
    if not client_team["active"]:
        return False, "Tu equipo está inactivo en este momento."
    if client_team["id"] == portfolio["team_id"]:
        return False, "Tu equipo no puede contratar su propio portfolio."
    provider_track = portfolio["service_track"] or "programacion"
    client_track = client_team["service_track"] or normalize_service_track(client_team["team_type"], None)
    if client_team["team_type"] == "robotica":
        if portfolio["team_type"] != "desarrollo":
            return False, "Ese portfolio no pertenece a un equipo proveedor disponible."
        return True, None
    if client_team["team_type"] == "desarrollo":
        if client_track != "programacion":
            return False, "En esta fase los equipos web / HTML funcionan como proveedores, no como clientes."
        if provider_track != "web_html":
            return False, "En esta fase los equipos de desarrollo solo pueden contratar servicios web / HTML."
        return True, None
    return False, "Tu equipo no puede contratar portfolios desde esta vista."


def count_open_client_contracts(conn, team_id: int, cycle_id: int | None = None) -> int:
    filters = [f"COALESCE(client_team_id, robotics_team_id) = ?", f"status IN ({','.join(['?'] * len(OPEN_CONTRACT_STATUSES))})"]
    params: list = [team_id, *OPEN_CONTRACT_STATUSES]
    if cycle_id is not None:
        filters.insert(1, "cycle_id = ?")
        params.insert(1, cycle_id)
    row = conn.execute(
        f"SELECT COUNT(*) AS total FROM contracts WHERE {' AND '.join(filters)}",
        tuple(params),
    ).fetchone()
    return row["total"] if row else 0


def count_open_provider_contracts(conn, team_id: int, cycle_id: int | None = None) -> int:
    filters = [f"COALESCE(provider_team_id, development_team_id) = ?", f"status IN ({','.join(['?'] * len(OPEN_CONTRACT_STATUSES))})"]
    params: list = [team_id, *OPEN_CONTRACT_STATUSES]
    if cycle_id is not None:
        filters.insert(1, "cycle_id = ?")
        params.insert(1, cycle_id)
    row = conn.execute(
        f"SELECT COUNT(*) AS total FROM contracts WHERE {' AND '.join(filters)}",
        tuple(params),
    ).fetchone()
    return row["total"] if row else 0


def team_dashboard_endpoint(user) -> str:
    if not user:
        return "dashboard"
    if user["role"] == "robotica_team":
        return "robotics_dashboard"
    if user["role"] == "desarrollo_team":
        return "development_dashboard"
    if user["role"] == "interventor":
        return "interventor_dashboard"
    return "admin_dashboard"


def safe_redirect_target(target: str | None, fallback_endpoint: str, **values):
    if target and isinstance(target, str) and target.startswith('/') and not target.startswith('//'):
        return redirect(target)
    return redirect(url_for(fallback_endpoint, **values))


def save_team_logo_file(upload, team_id: int) -> tuple[str, str] | tuple[None, None]:
    if not upload or not upload.filename:
        return None, None
    original_name = secure_filename(upload.filename)
    if not original_name:
        return None, None
    if not allowed_image_upload(original_name):
        raise ValueError("Tipo de imagen no permitido para el logo.")
    stored_name = f"teamlogo_{team_id}_{uuid4().hex}_{original_name}"
    destination = TEAM_LOGO_DIR / stored_name
    upload.save(destination)
    return original_name, stored_name


def save_team_gallery_file(upload, team_id: int) -> tuple[str, str] | tuple[None, None]:
    if not upload or not upload.filename:
        return None, None
    original_name = secure_filename(upload.filename)
    if not original_name:
        return None, None
    if not allowed_image_upload(original_name):
        raise ValueError("Tipo de imagen no permitido para la galería.")
    stored_name = f"teamgallery_{team_id}_{uuid4().hex}_{original_name}"
    destination = TEAM_GALLERY_DIR / stored_name
    upload.save(destination)
    return original_name, stored_name


def save_delivery_file(upload, contract_id: int) -> tuple[str, str, int] | tuple[None, None, int]:
    if not upload or not upload.filename:
        return None, None, 0
    original_name = secure_filename(upload.filename)
    if not original_name:
        return None, None, 0
    if not allowed_upload(original_name):
        raise ValueError("Tipo de archivo no permitido.")
    stored_name = f"contract_{contract_id}_{uuid4().hex}_{original_name}"
    destination = UPLOAD_DIR / stored_name
    upload.save(destination)
    return original_name, stored_name, destination.stat().st_size


def save_request_file(upload, portfolio_id: int) -> tuple[str, str, int] | tuple[None, None, int]:
    if not upload or not upload.filename:
        return None, None, 0
    original_name = secure_filename(upload.filename)
    if not original_name:
        return None, None, 0
    if not allowed_upload(original_name):
        raise ValueError("Tipo de archivo no permitido.")
    stored_name = f"request_{portfolio_id}_{uuid4().hex}_{original_name}"
    destination = REQUEST_UPLOAD_DIR / stored_name
    upload.save(destination)
    return original_name, stored_name, destination.stat().st_size


CONTRACT_CLIENT_SQL = "COALESCE(c.client_team_id, c.robotics_team_id)"
CONTRACT_PROVIDER_SQL = "COALESCE(c.provider_team_id, c.development_team_id)"


def _row_has_key(row, key: str) -> bool:
    return row is not None and hasattr(row, "keys") and key in row.keys()


def contract_client_team_id(row) -> int | None:
    if row is None:
        return None
    if _row_has_key(row, "client_team_id") and row["client_team_id"] is not None:
        return row["client_team_id"]
    if _row_has_key(row, "robotics_team_id"):
        return row["robotics_team_id"]
    return None


def contract_provider_team_id(row) -> int | None:
    if row is None:
        return None
    if _row_has_key(row, "provider_team_id") and row["provider_team_id"] is not None:
        return row["provider_team_id"]
    if _row_has_key(row, "development_team_id"):
        return row["development_team_id"]
    return None


def sync_contract_party_fields(conn, contract_id: int) -> None:
    conn.execute(
        """
        UPDATE contracts
        SET client_team_id = COALESCE(client_team_id, robotics_team_id),
            provider_team_id = COALESCE(provider_team_id, development_team_id),
            client_team_type = COALESCE(NULLIF(client_team_type, ''), (SELECT t.team_type FROM teams t WHERE t.id = COALESCE(contracts.client_team_id, contracts.robotics_team_id))),
            provider_team_type = COALESCE(NULLIF(provider_team_type, ''), (SELECT t.team_type FROM teams t WHERE t.id = COALESCE(contracts.provider_team_id, contracts.development_team_id))),
            provider_service_track = COALESCE(NULLIF(provider_service_track, ''), (SELECT t.service_track FROM teams t WHERE t.id = COALESCE(contracts.provider_team_id, contracts.development_team_id)))
        WHERE id = ?
        """,
        (contract_id,),
    )


def can_access_contract(user, contract_row) -> bool:
    if not user or not contract_row:
        return False
    if user["role"] in {"admin", "interventor"}:
        return True
    if user["role"] in {"robotica_team", "desarrollo_team"}:
        team_id = user.get("team_id")
        return team_id in {contract_client_team_id(contract_row), contract_provider_team_id(contract_row)}
    return False


def can_access_delivery(user, delivery_row) -> bool:
    if not user or not delivery_row:
        return False
    if user["role"] in {"admin", "interventor"}:
        return True
    if user["role"] in {"robotica_team", "desarrollo_team"}:
        team_id = user.get("team_id")
        return team_id in {contract_client_team_id(delivery_row), contract_provider_team_id(delivery_row)}
    return False


def fetch_team_gallery(conn, team_id: int, limit: int | None = None):
    sql = "SELECT * FROM team_gallery WHERE team_id = ? ORDER BY id DESC"
    params = [team_id]
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    return conn.execute(sql, tuple(params)).fetchall()


def team_gallery_preview(conn, team_id: int):
    return conn.execute(
        "SELECT stored_filename FROM team_gallery WHERE team_id = ? ORDER BY id DESC LIMIT 1",
        (team_id,),
    ).fetchone()


def log_action(conn, user_id: int | None, action: str, entity_type: str, entity_id: int | None, details: str = "") -> None:
    conn.execute(
        "INSERT INTO audit_log (user_id, action, entity_type, entity_id, details) VALUES (?, ?, ?, ?, ?)",
        (user_id, action, entity_type, entity_id, details),
    )


def allowed_roles_for_team_type(team_type: str) -> tuple[str, ...]:
    return ROLE_OPTIONS_BY_TEAM_TYPE.get(team_type, ())


def validate_member_role(conn, team_id: int, internal_role: str, active: int = 1, exclude_member_id: int | None = None) -> None:
    team = conn.execute("SELECT * FROM teams WHERE id = ?", (team_id,)).fetchone()
    if not team:
        raise ValueError("Equipo no encontrado.")
    allowed_roles = allowed_roles_for_team_type(team["team_type"])
    if internal_role not in allowed_roles:
        raise ValueError(f"El rol {internal_role} no corresponde a un equipo de {team['team_type']}.")
    if active and team["team_type"] == "desarrollo" and internal_role == "COO":
        sql = "SELECT COUNT(*) AS c FROM team_members WHERE team_id = ? AND internal_role = 'COO' AND active = 1"
        params = [team_id]
        if exclude_member_id is not None:
            sql += " AND id != ?"
            params.append(exclude_member_id)
        current_coos = conn.execute(sql, tuple(params)).fetchone()["c"]
        if current_coos:
            raise ValueError("Ese equipo ya tiene un COO activo. Solo puede haber uno por equipo.")


def calculate_maintenance(member_count: int, active_projects: int, settings) -> int:
    base_cost = settings["base_cost"]
    per_member = settings["cost_per_member"]
    standard_members = min(member_count, 4)
    total = base_cost + standard_members * per_member
    extra_members = max(member_count - 4, 0)
    if extra_members:
        for extra_index in range(1, extra_members + 1):
            total += round(per_member * (1.3 ** extra_index))
    if active_projects >= 2:
        total += settings["second_project_surcharge"]
    return int(total)


def has_active_interventor_assignment(user_id: int) -> bool:
    row = query_one("SELECT id FROM interventor_assignments WHERE user_id = ? AND active = 1 ORDER BY id DESC LIMIT 1", (user_id,))
    return bool(row)


def fetch_contract_reviews_map(conn, contract_ids: list[int]) -> dict[int, list]:
    review_map: dict[int, list] = defaultdict(list)
    if not contract_ids:
        return review_map
    placeholders = ",".join(["?"] * len(contract_ids))
    rows = conn.execute(
        f"""
        SELECT cr.*, u.username AS interventor_username
        FROM contract_reviews cr
        LEFT JOIN users u ON u.id = cr.interventor_user_id
        WHERE cr.contract_id IN ({placeholders})
        ORDER BY cr.id DESC
        """,
        tuple(contract_ids),
    ).fetchall()
    for row in rows:
        review_map[row["contract_id"]].append(row)
    return review_map


def latest_review_lookup(review_map: dict[int, list], *, review_stage: str | None = None, decisions: set[str] | None = None) -> dict[int, dict]:
    latest: dict[int, dict] = {}
    for contract_id, reviews in review_map.items():
        for review in reviews:
            if review_stage and review["review_stage"] != review_stage:
                continue
            if decisions and review["decision"] not in decisions:
                continue
            latest[contract_id] = review
            break
    return latest


def fetch_contract_detail_bundle(contract_id: int):
    contract = query_one(
        f"""
        SELECT c.*,
               {CONTRACT_CLIENT_SQL} AS client_team_id,
               {CONTRACT_PROVIDER_SQL} AS provider_team_id,
               rt.name AS robotics_name, dt.name AS development_name,
               rt.name AS client_name, dt.name AS provider_name,
               rt.team_type AS client_team_type_resolved,
               dt.team_type AS provider_team_type_resolved,
               dt.service_track AS provider_service_track_resolved,
               p.title AS portfolio_title, u.username AS requested_by_username,
               cyc.name AS cycle_name, cyc.status AS cycle_status, cyc.started AS cycle_started,
               iu.username AS last_interventor_username
        FROM contracts c
        JOIN teams rt ON rt.id = {CONTRACT_CLIENT_SQL}
        JOIN teams dt ON dt.id = {CONTRACT_PROVIDER_SQL}
        LEFT JOIN portfolios p ON p.id = c.portfolio_id
        LEFT JOIN users u ON u.id = c.requested_by_user_id
        LEFT JOIN cycles cyc ON cyc.id = c.cycle_id
        LEFT JOIN users iu ON iu.id = c.last_interventor_user_id
        WHERE c.id = ?
        """,
        (contract_id,),
    )
    if not contract:
        return None

    deliveries = query_all(
        """
        SELECT d.*, u.username AS submitted_by,
               CASE WHEN COALESCE(d.code_text, '') != '' THEN 1 ELSE 0 END AS has_code,
               CASE WHEN COALESCE(d.repository_link, '') != '' THEN 1 ELSE 0 END AS has_link,
               CASE WHEN COALESCE(d.stored_filename, '') != '' THEN 1 ELSE 0 END AS has_file
        FROM deliveries d
        LEFT JOIN users u ON u.id = d.submitted_by_user_id
        WHERE d.contract_id = ?
        ORDER BY d.id DESC
        """,
        (contract_id,),
    )
    reviews = query_all(
        """
        SELECT cr.*, u.username AS interventor_username
        FROM contract_reviews cr
        LEFT JOIN users u ON u.id = cr.interventor_user_id
        WHERE cr.contract_id = ?
        ORDER BY cr.id DESC
        """,
        (contract_id,),
    )
    transactions = query_all(
        """
        SELECT tr.*, u.username AS actor_username,
               fw.owner_type AS from_owner_type, fw.owner_id AS from_owner_id,
               tw.owner_type AS to_owner_type, tw.owner_id AS to_owner_id,
               ft.name AS from_team_name, tt.name AS to_team_name
        FROM transactions tr
        LEFT JOIN users u ON u.id = tr.created_by_user_id
        LEFT JOIN wallets fw ON fw.id = tr.from_wallet_id
        LEFT JOIN wallets tw ON tw.id = tr.to_wallet_id
        LEFT JOIN teams ft ON fw.owner_type = 'team' AND fw.owner_id = ft.id
        LEFT JOIN teams tt ON tw.owner_type = 'team' AND tw.owner_id = tt.id
        WHERE tr.description LIKE ?
        ORDER BY tr.id DESC
        """,
        (f"%contrato #{contract_id}%",),
    )
    latest_return_review = None
    for review in reviews:
        if review["review_stage"] == "final_delivery" and review["decision"] in {"correction_required", "rejected"}:
            latest_return_review = review
            break
    cycle_state = None
    if contract["cycle_id"]:
        cycle_state = cycle_state_label({"status": contract["cycle_status"], "started": contract["cycle_started"]})
    return {
        "contract": contract,
        "deliveries": deliveries,
        "reviews": reviews,
        "transactions": transactions,
        "latest_return_review": latest_return_review,
        "cycle_state": cycle_state,
    }


def fetch_ai_messages_map(conn, contract_ids: list[int]) -> dict[int, list]:
    message_map: dict[int, list] = defaultdict(list)
    if not contract_ids:
        return message_map
    placeholders = ",".join(["?"] * len(contract_ids))
    rows = conn.execute(
        f"""
        SELECT am.*, u.username AS asked_by_username
        FROM ai_assistant_messages am
        LEFT JOIN users u ON u.id = am.asked_by_user_id
        WHERE am.contract_id IN ({placeholders})
        ORDER BY am.id DESC
        """,
        tuple(contract_ids),
    ).fetchall()
    for row in rows:
        message_map[row["contract_id"]].append(row)
    return message_map


def contract_columns_available(conn) -> bool:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(contracts)").fetchall()}
    required = {
        "requested_delivery_date",
        "request_message",
        "request_file_path",
        "request_original_filename",
        "request_stored_filename",
        "request_file_size",
        "paused_by_deadline",
        "paused_at",
        "pause_reason",
    }
    return required.issubset(columns)


def refresh_overdue_contracts() -> None:
    with get_connection() as conn:
        if not contract_columns_available(conn):
            return
        overdue_contracts = conn.execute(
            """
            SELECT id
            FROM contracts
            WHERE COALESCE(requested_delivery_date, '') != ''
              AND date(requested_delivery_date) < date('now', 'localtime')
              AND paused_by_deadline = 0
              AND payment_released = 0
              AND status IN ('pending_interventor_activation', 'active', 'in_development', 'submitted_for_review', 'correction_required')
            """
        ).fetchall()
        for contract in overdue_contracts:
            conn.execute(
                "UPDATE contracts SET paused_by_deadline = 1, paused_at = CURRENT_TIMESTAMP, pause_reason = COALESCE(pause_reason, 'Se venció la fecha comprometida de entrega.') WHERE id = ?",
                (contract["id"],),
            )
            log_action(conn, None, "deadline_pause", "contract", contract["id"], "pause_by_deadline")
        if overdue_contracts:
            conn.commit()


@app.before_request
def apply_deadline_pauses():
    refresh_overdue_contracts()


def cycle_state_label(cycle) -> str | None:
    if not cycle:
        return None
    if cycle["status"] == "closed":
        return "finalizado"
    return "activo" if cycle["started"] else "borrador"


def get_open_cycle(conn):
    return conn.execute(
        "SELECT * FROM cycles WHERE status = 'open' ORDER BY id DESC LIMIT 1"
    ).fetchone()


def get_active_cycle(conn):
    return conn.execute(
        "SELECT * FROM cycles WHERE status = 'open' AND started = 1 ORDER BY id DESC LIMIT 1"
    ).fetchone()


def get_draft_cycle(conn):
    return conn.execute(
        "SELECT * FROM cycles WHERE status = 'open' AND started = 0 ORDER BY id DESC LIMIT 1"
    ).fetchone()


def any_open_cycle(conn):
    return get_open_cycle(conn)


def team_in_open_cycle(conn, team_id: int):
    return conn.execute(
        """
        SELECT c.id, c.name, c.status, c.started
        FROM cycle_teams ct
        JOIN cycles c ON c.id = ct.cycle_id
        WHERE ct.team_id = ? AND c.status = 'open'
        ORDER BY c.id DESC
        LIMIT 1
        """,
        (team_id,),
    ).fetchone()


def cycle_has_team(conn, cycle_id: int, team_id: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM cycle_teams WHERE cycle_id = ? AND team_id = ? LIMIT 1",
        (cycle_id, team_id),
    ).fetchone()
    return bool(row)


def cycle_team_rows(conn, cycle_id: int, team_type: str | None = None):
    sql = """
        SELECT t.id, t.name, t.team_type, t.active, t.max_contracts, t.notes,
               COALESCE(w.balance, 0) AS wallet_balance,
               COUNT(DISTINCT CASE WHEN tm.active = 1 AND s.active = 1 THEN tm.id END) AS member_count,
               SUM(CASE WHEN tm.active = 1 AND tm.internal_role = 'COO' THEN 1 ELSE 0 END) AS coo_count
        FROM cycle_teams ct
        JOIN teams t ON t.id = ct.team_id
        LEFT JOIN wallets w ON w.owner_type = 'team' AND w.owner_id = t.id
        LEFT JOIN team_members tm ON tm.team_id = t.id
        LEFT JOIN students s ON s.id = tm.student_id
        WHERE ct.cycle_id = ?
    """
    params: list = [cycle_id]
    if team_type:
        sql += " AND t.team_type = ?"
        params.append(team_type)
    sql += " GROUP BY t.id, w.balance ORDER BY t.team_type, t.name"
    return conn.execute(sql, tuple(params)).fetchall()




def cycle_overview_rows(conn, cycle_id: int):
    cycle = conn.execute(
        """
        SELECT c.*,
               COALESCE((SELECT COUNT(*) FROM cycle_teams ct WHERE ct.cycle_id = c.id), 0) AS linked_team_count,
               COALESCE((SELECT COUNT(*) FROM cycle_teams ct JOIN teams t ON t.id = ct.team_id WHERE ct.cycle_id = c.id AND t.active = 1), 0) AS active_linked_team_count,
               COALESCE((SELECT COUNT(*) FROM cycle_runs cr WHERE cr.cycle_id = c.id), 0) AS run_count,
               COALESCE((SELECT COUNT(*) FROM rewards r WHERE r.cycle_id = c.id), 0) AS reward_count,
               COALESCE((SELECT COUNT(*) FROM contracts k WHERE k.cycle_id = c.id), 0) AS contract_count
        FROM cycles c
        WHERE c.id = ?
        """,
        (cycle_id,),
    ).fetchone()
    if not cycle:
        return None

    participant_groups = {
        "robotica": cycle_team_rows(conn, cycle_id, "robotica"),
        "desarrollo": cycle_team_rows(conn, cycle_id, "desarrollo"),
    }

    run = conn.execute(
        """
        SELECT cr.*, u.username AS executed_by_username
        FROM cycle_runs cr
        LEFT JOIN users u ON u.id = cr.executed_by_user_id
        WHERE cr.cycle_id = ?
        """,
        (cycle_id,),
    ).fetchone()

    charges = conn.execute(
        """
        SELECT cc.*, t.name AS team_name
        FROM cycle_custom_charges cc
        JOIN teams t ON t.id = cc.team_id
        WHERE cc.cycle_id = ?
        ORDER BY cc.id DESC
        """,
        (cycle_id,),
    ).fetchall()

    rewards = conn.execute(
        """
        SELECT r.*, t.name AS team_name, u.username AS actor_username
        FROM rewards r
        JOIN teams t ON t.id = r.robotics_team_id
        LEFT JOIN users u ON u.id = r.created_by_user_id
        WHERE r.cycle_id = ?
        ORDER BY r.id DESC
        """,
        (cycle_id,),
    ).fetchall()

    contracts = conn.execute(
        """
        SELECT c.*, rt.name AS robotics_name, dt.name AS development_name
        FROM contracts c
        JOIN teams rt ON rt.id = COALESCE(c.client_team_id, c.robotics_team_id)
        JOIN teams dt ON dt.id = COALESCE(c.provider_team_id, c.development_team_id)
        WHERE c.cycle_id = ?
        ORDER BY c.id DESC
        """,
        (cycle_id,),
    ).fetchall()

    development_projection = []
    settings = conn.execute("SELECT * FROM economic_settings ORDER BY effective_from DESC, id DESC LIMIT 1").fetchone()
    if cycle["status"] == "open" and cycle["started"]:
        development_projection = [dict(row) for row in development_team_cycle_rows(conn, cycle_id)]
        pending_map = {
            row["team_id"]: row["total"]
            for row in conn.execute(
                "SELECT team_id, SUM(amount) AS total FROM cycle_custom_charges WHERE cycle_id = ? AND status = 'pending' GROUP BY team_id",
                (cycle_id,),
            ).fetchall()
        }
        for row in development_projection:
            row["maintenance_amount"] = calculate_maintenance(row["member_count"], row["active_projects"], settings) if row["active"] and settings else 0
            row["pending_extra"] = pending_map.get(row["id"], 0)
            row["projected_total"] = row["maintenance_amount"] + row["pending_extra"]

    return {
        "cycle": cycle,
        "cycle_state": cycle_state_label(cycle),
        "participant_groups": participant_groups,
        "run": run,
        "charges": charges,
        "rewards": rewards,
        "contracts": contracts,
        "development_projection": development_projection,
        "can_delete": (cycle["status"] == "open" and not cycle["started"]) or (cycle["status"] == "closed" and cycle["active_linked_team_count"] == 0),
    }


def development_team_cycle_rows(conn, cycle_id: int | None = None):
    sql = """
        SELECT t.id, t.name, t.active, COALESCE(w.balance, 0) AS wallet_balance,
               COUNT(DISTINCT CASE WHEN tm.active = 1 AND s.active = 1 THEN tm.id END) AS member_count,
               COUNT(DISTINCT CASE WHEN c.status IN ('pending_interventor_activation','active','in_development','submitted_for_review','correction_required') THEN c.id END) AS active_projects
        FROM teams t
    """
    params: list = []
    if cycle_id is not None:
        sql += " JOIN cycle_teams ct ON ct.team_id = t.id AND ct.cycle_id = ?"
        params.append(cycle_id)
    sql += """
        LEFT JOIN wallets w ON w.owner_type = 'team' AND w.owner_id = t.id
        LEFT JOIN team_members tm ON tm.team_id = t.id
        LEFT JOIN students s ON s.id = tm.student_id
        LEFT JOIN contracts c ON c.development_team_id = t.id
    """
    if cycle_id is not None:
        sql += " AND c.cycle_id = ?"
        params.append(cycle_id)
    sql += " WHERE t.team_type = 'desarrollo' GROUP BY t.id, w.balance ORDER BY t.active DESC, t.name"
    return conn.execute(sql, tuple(params)).fetchall()


def redirect_back(default_endpoint: str, **kwargs):
    next_url = request.form.get("next") or request.args.get("next")
    if next_url:
        return redirect(next_url)
    return redirect(url_for(default_endpoint, **kwargs))


def login_required(view: Callable):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user():
            flash("Iniciá sesión para continuar.", "warning")
            return redirect(url_for("login"))
        return view(*args, **kwargs)

    return wrapped


def role_required(*roles: str):
    def decorator(view: Callable):
        @wraps(view)
        def wrapped(*args, **kwargs):
            user = current_user()
            if not user:
                flash("Iniciá sesión para continuar.", "warning")
                return redirect(url_for("login"))
            if user["role"] not in roles:
                flash("No tenés permisos para entrar ahí.", "danger")
                return redirect(url_for("dashboard"))
            if user["role"] == "interventor" and "interventor" in roles and not has_active_interventor_assignment(user["id"]):
                flash("Tu cuenta de interventor no tiene una asignación activa en este momento.", "warning")
                session.clear()
                return redirect(url_for("login"))
            return view(*args, **kwargs)

        return wrapped

    return decorator


POINTS_PER_PANCHICOIN = 10
SUCCESS_PROJECT_BONUS = 50
RETURN_PENALTY = 15
CANCELLATION_PENALTY = 30


def panchicoin_points(balance: int | None) -> int:
    balance = balance or 0
    return int(balance) * POINTS_PER_PANCHICOIN


def build_points_rule_text() -> str:
    return (
        f"Puntaje = saldo disponible × {POINTS_PER_PANCHICOIN}"
        f" + inversión robótica en proyectos cerrados × {POINTS_PER_PANCHICOIN}"
        f" + {SUCCESS_PROJECT_BONUS} por proyecto exitoso"
        f" - {RETURN_PENALTY} por devolución"
        f" - {CANCELLATION_PENALTY} por cancelación"
    )


def compute_team_scores(conn, team_rows):
    team_ids = [row["id"] for row in team_rows]
    score_map = {}
    if not team_ids:
        return score_map

    successful_contracts = conn.execute(
        """
        SELECT id,
               COALESCE(client_team_id, robotics_team_id) AS client_team_id,
               COALESCE(provider_team_id, development_team_id) AS provider_team_id,
               requested_amount
        FROM contracts
        WHERE status = 'closed' AND payment_released = 1
        """
    ).fetchall()
    successful_count = defaultdict(int)
    client_invested = defaultdict(int)
    for contract in successful_contracts:
        successful_count[contract["client_team_id"]] += 1
        successful_count[contract["provider_team_id"]] += 1
        client_invested[contract["client_team_id"]] += int(contract["requested_amount"] or 0)

    return_reviews = conn.execute(
        """
        SELECT DISTINCT cr.id,
               COALESCE(c.client_team_id, c.robotics_team_id) AS client_team_id,
               COALESCE(c.provider_team_id, c.development_team_id) AS provider_team_id
        FROM contract_reviews cr
        JOIN contracts c ON c.id = cr.contract_id
        WHERE cr.review_stage = 'final_delivery' AND cr.decision = 'correction_required'
        """
    ).fetchall()
    return_count = defaultdict(int)
    for review in return_reviews:
        return_count[review["client_team_id"]] += 1
        return_count[review["provider_team_id"]] += 1

    cancelled_contracts = conn.execute(
        """
        SELECT DISTINCT id,
               COALESCE(client_team_id, robotics_team_id) AS client_team_id,
               COALESCE(provider_team_id, development_team_id) AS provider_team_id
        FROM contracts
        WHERE status = 'cancelled'
        """
    ).fetchall()
    cancellation_count = defaultdict(int)
    for contract in cancelled_contracts:
        cancellation_count[contract["client_team_id"]] += 1
        cancellation_count[contract["provider_team_id"]] += 1

    for row in team_rows:
        team_id = row["id"]
        base_points = panchicoin_points(row["wallet_balance"])
        invested_points = 0
        if row["team_type"] == "robotica":
            invested_points = panchicoin_points(client_invested[team_id])
        success_bonus = successful_count[team_id] * SUCCESS_PROJECT_BONUS
        returns_penalty = return_count[team_id] * RETURN_PENALTY
        cancellations_penalty = cancellation_count[team_id] * CANCELLATION_PENALTY
        total = max(0, base_points + invested_points + success_bonus - returns_penalty - cancellations_penalty)
        score_map[team_id] = {
            "score": total,
            "base_points": base_points,
            "invested_points": invested_points,
            "success_count": successful_count[team_id],
            "success_bonus": success_bonus,
            "return_count": return_count[team_id],
            "return_penalty": returns_penalty,
            "cancellation_count": cancellation_count[team_id],
            "cancellation_penalty": cancellations_penalty,
        }
    return score_map


def build_public_home_context():
    with get_connection() as conn:
        teams = conn.execute(
            """
            SELECT t.id, t.name, t.team_type, t.profile_blurb, t.logo_stored_filename,
                   COALESCE(w.balance, 0) AS wallet_balance,
                   COALESCE(NULLIF(t.course_label, ''), (
                       SELECT s.course
                       FROM team_members tm
                       JOIN students s ON s.id = tm.student_id
                       WHERE tm.team_id = t.id AND tm.active = 1 AND s.active = 1
                       ORDER BY s.course, tm.id
                       LIMIT 1
                   ), 'Sin curso') AS course_label,
                   (
                       SELECT tg.stored_filename
                       FROM team_gallery tg
                       WHERE tg.team_id = t.id
                       ORDER BY tg.id DESC
                       LIMIT 1
                   ) AS preview_gallery_image
            FROM teams t
            LEFT JOIN wallets w ON w.owner_type = 'team' AND w.owner_id = t.id
            WHERE t.active = 1
            ORDER BY course_label, t.name
            """
        ).fetchall()
        score_map = compute_team_scores(conn, teams)

    teams_by_course = defaultdict(list)
    ranking = []
    for row in teams:
        item = dict(row)
        item.update(score_map.get(item["id"], {"score": 0, "base_points": 0, "invested_points": 0, "success_count": 0, "success_bonus": 0, "return_count": 0, "return_penalty": 0, "cancellation_count": 0, "cancellation_penalty": 0}))
        teams_by_course[item["course_label"]].append(item)
        ranking.append(item)

    course_sections = []
    def course_sort_key(label: str):
        import re
        match = re.match(r"(\d+)", label or "")
        if match:
            return (0, int(match.group(1)), label)
        return (1, 999, label or "Sin curso")

    for course_label in sorted(teams_by_course.keys(), key=course_sort_key):
        course_sections.append({
            "course_label": course_label,
            "teams": sorted(teams_by_course[course_label], key=lambda x: (-x["score"], x["name"].lower())),
        })

    ranking = sorted(ranking, key=lambda x: (-x["score"], x["name"].lower()))
    return {
        "course_sections": course_sections,
        "ranking": ranking,
        "points_rule": build_points_rule_text(),
        "points_per_panchicoin": POINTS_PER_PANCHICOIN,
        "success_project_bonus": SUCCESS_PROJECT_BONUS,
        "return_penalty": RETURN_PENALTY,
        "cancellation_penalty": CANCELLATION_PENALTY,
    }


@app.context_processor
def inject_globals():
    return {"current_user": current_user(), "panchicoin_points": panchicoin_points, "points_rule_text": build_points_rule_text}


@app.route("/")
def home():
    if current_user():
        return redirect(url_for("dashboard"))
    return render_template("home.html", **build_public_home_context())




@app.route("/reglas")
def project_rules():
    settings = query_one("SELECT * FROM economic_settings ORDER BY effective_from DESC, id DESC LIMIT 1")
    return render_template(
        "project_rules.html",
        contract_price=settings["contract_price"] if settings else 30,
        points_rule=build_points_rule_text(),
        points_per_panchicoin=POINTS_PER_PANCHICOIN,
        success_project_bonus=SUCCESS_PROJECT_BONUS,
        return_penalty=RETURN_PENALTY,
        cancellation_penalty=CANCELLATION_PENALTY,
    )


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = query_one("SELECT * FROM users WHERE username = ? AND active = 1", (username,))
        if user and check_password_hash(user["password_hash"], password):
            session.clear()
            session["user_id"] = user["id"]
            flash("Sesión iniciada.", "success")
            return redirect(url_for("dashboard"))
        flash("Usuario o contraseña incorrectos.", "danger")
        return render_template("home.html", **build_public_home_context())
    return redirect(url_for("home", _anchor="acceso"))


@app.route("/logout")
@login_required
def logout():
    session.clear()
    flash("Sesión cerrada.", "info")
    return redirect(url_for("login"))


@app.route("/dashboard")
@login_required
def dashboard():
    user = current_user()
    if user["role"] == "admin":
        return redirect(url_for("admin_dashboard"))
    if user["role"] == "robotica_team":
        return redirect_back("robotics_dashboard")
    if user["role"] == "desarrollo_team":
        return redirect_back("development_dashboard")
    if user["role"] == "interventor":
        return redirect(url_for("interventor_dashboard"))
    flash("Rol no reconocido.", "danger")
    return redirect(url_for("logout"))


@app.route("/admin")
@role_required("admin")
def admin_dashboard():
    teams = query_all(
        """
        SELECT t.*, COALESCE(w.balance, 0) AS wallet_balance,
               COUNT(CASE WHEN tm.active = 1 THEN 1 END) AS member_count,
               u.username AS login_username
        FROM teams t
        LEFT JOIN wallets w ON w.owner_type = 'team' AND w.owner_id = t.id
        LEFT JOIN team_members tm ON tm.team_id = t.id
        LEFT JOIN users u ON u.team_id = t.id AND u.role IN ('robotica_team', 'desarrollo_team')
        GROUP BY t.id, w.balance, u.username
        ORDER BY COALESCE(NULLIF(t.course_label, ''), 'Sin curso'), t.service_track, t.name
        """
    )
    settings = query_one(
        "SELECT * FROM economic_settings ORDER BY effective_from DESC, id DESC LIMIT 1"
    )
    recent_transactions = query_all(
        """
        SELECT tr.*, fu.username AS actor_username,
               fw.owner_type AS from_owner_type, tw.owner_type AS to_owner_type,
               ft.name AS from_team_name, tt.name AS to_team_name
        FROM transactions tr
        LEFT JOIN users fu ON tr.created_by_user_id = fu.id
        LEFT JOIN wallets fw ON tr.from_wallet_id = fw.id
        LEFT JOIN wallets tw ON tr.to_wallet_id = tw.id
        LEFT JOIN teams ft ON fw.owner_type = 'team' AND fw.owner_id = ft.id
        LEFT JOIN teams tt ON tw.owner_type = 'team' AND tw.owner_id = tt.id
        ORDER BY tr.id DESC
        LIMIT 12
        """
    )
    recent_reviews = query_all(
        """
        SELECT cr.*, u.username AS interventor_username,
               rt.name AS robotics_name, dt.name AS development_name,
               rt.name AS client_name, dt.name AS provider_name,
               rt.team_type AS client_team_type, dt.team_type AS provider_team_type,
               dt.service_track AS provider_service_track
        FROM contract_reviews cr
        JOIN contracts c ON c.id = cr.contract_id
        JOIN teams rt ON rt.id = COALESCE(c.client_team_id, c.robotics_team_id)
        JOIN teams dt ON dt.id = COALESCE(c.provider_team_id, c.development_team_id)
        JOIN users u ON cr.interventor_user_id = u.id
        ORDER BY cr.id DESC
        LIMIT 8
        """
    )
    recent_deliveries = query_all(
        """
        SELECT d.*, u.username AS submitted_by,
               rt.name AS robotics_name, dt.name AS development_name,
               rt.name AS client_name, dt.name AS provider_name,
               dt.service_track AS provider_service_track,
               CASE WHEN COALESCE(d.code_text, '') != '' THEN 1 ELSE 0 END AS has_code,
               CASE WHEN COALESCE(d.repository_link, '') != '' THEN 1 ELSE 0 END AS has_link,
               CASE WHEN COALESCE(d.stored_filename, '') != '' THEN 1 ELSE 0 END AS has_file
        FROM deliveries d
        JOIN users u ON d.submitted_by_user_id = u.id
        JOIN contracts c ON d.contract_id = c.id
        JOIN teams rt ON rt.id = COALESCE(c.client_team_id, c.robotics_team_id)
        JOIN teams dt ON dt.id = COALESCE(c.provider_team_id, c.development_team_id)
        ORDER BY d.id DESC
        LIMIT 8
        """
    )
    audit_items = query_all(
        """
        SELECT a.*, u.username
        FROM audit_log a
        LEFT JOIN users u ON a.user_id = u.id
        ORDER BY a.id DESC
        LIMIT 12
        """
    )
    interventor_users = query_all(
        """
        SELECT u.id AS user_id, u.username, u.active AS user_active,
               ia.id AS assignment_id, ia.active AS assignment_active, ia.start_date, ia.end_date,
               s.id AS student_id, s.full_name AS student_name, s.course AS student_course,
               COALESCE(rv.review_count, 0) AS review_count,
               COALESCE(ls.last_signature_refs, 0) AS last_signature_refs
        FROM users u
        LEFT JOIN interventor_assignments ia ON ia.id = (
            SELECT ia2.id FROM interventor_assignments ia2 WHERE ia2.user_id = u.id ORDER BY ia2.id DESC LIMIT 1
        )
        LEFT JOIN students s ON s.id = ia.student_id
        LEFT JOIN (
            SELECT interventor_user_id, COUNT(*) AS review_count
            FROM contract_reviews
            GROUP BY interventor_user_id
        ) rv ON rv.interventor_user_id = u.id
        LEFT JOIN (
            SELECT last_interventor_user_id, COUNT(*) AS last_signature_refs
            FROM contracts
            WHERE last_interventor_user_id IS NOT NULL
            GROUP BY last_interventor_user_id
        ) ls ON ls.last_interventor_user_id = u.id
        WHERE u.role = 'interventor'
        ORDER BY COALESCE(ia.active, 0) DESC, u.username
        """
    )
    available_interventor_students = query_all(
        """
        SELECT s.id, s.full_name, s.course
        FROM students s
        WHERE s.active = 1
        ORDER BY s.full_name
        """
    )
    active_interventor_count = query_one(
        "SELECT COUNT(*) AS c FROM interventor_assignments WHERE active = 1"
    )["c"]
    recent_admin_offers = query_all(
        """
        SELECT ao.*, u.username AS created_by_username, t.name AS taken_by_team_name
        FROM admin_offers ao
        LEFT JOIN users u ON u.id = ao.created_by_user_id
        LEFT JOIN teams t ON t.id = ao.taken_by_team_id
        ORDER BY ao.id DESC
        LIMIT 10
        """
    )
    stats = {
        "team_total": len(teams),
        "student_total": query_one("SELECT COUNT(*) AS c FROM students WHERE active = 1")["c"],
        "active_contracts": query_one(
            "SELECT COUNT(*) AS c FROM contracts WHERE status IN ('pending_interventor_activation','active','in_development','submitted_for_review','correction_required')"
        )["c"],
        "treasury_balance": query_one("SELECT balance AS c FROM wallets WHERE owner_type='treasury' LIMIT 1")["c"],
    }
    return render_template(
        "admin_dashboard.html",
        teams=teams,
        settings=settings,
        recent_transactions=recent_transactions,
        recent_reviews=recent_reviews,
        recent_deliveries=recent_deliveries,
        audit_items=audit_items,
        stats=stats,
        interventor_users=interventor_users,
        available_interventor_students=available_interventor_students,
        active_interventor_count=active_interventor_count,
        recent_admin_offers=recent_admin_offers,
        portfolio_service_category_options=PORTFOLIO_SERVICE_CATEGORY_OPTIONS,
    )


@app.route("/admin/interventors/create", methods=["POST"])
@role_required("admin")
def admin_create_interventor():
    user = current_user()
    student_id_raw = request.form.get("student_id", "").strip()
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    active_now = 1 if request.form.get("active_now") == "on" else 0
    if not username or not password:
        flash("El interventor necesita usuario y contraseña.", "danger")
        return redirect(url_for("admin_dashboard"))
    student_id = int(student_id_raw) if student_id_raw else None
    with get_connection() as conn:
        if active_now:
            active_count = conn.execute("SELECT COUNT(*) AS c FROM interventor_assignments WHERE active = 1").fetchone()["c"]
            if active_count >= 2:
                flash("Ya hay dos interventores activos. Desactivá uno antes de activar otro.", "warning")
                return redirect(url_for("admin_dashboard"))
        try:
            user_id = conn.execute(
                "INSERT INTO users (username, password_hash, role, team_id, active) VALUES (?, ?, 'interventor', NULL, 1)",
                (username, generate_password_hash(password)),
            ).lastrowid
            conn.execute(
                "INSERT INTO interventor_assignments (user_id, student_id, active) VALUES (?, ?, ?)",
                (user_id, student_id, active_now),
            )
            detail = f"username={username} student={student_id or 'sin vínculo'} active={active_now}"
            log_action(conn, user["id"], "create_interventor", "user", user_id, detail)
            conn.commit()
            flash("Cuenta de interventor creada correctamente.", "success")
        except Exception as exc:
            conn.rollback()
            flash(f"No se pudo crear el interventor: {exc}", "danger")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/interventors/<int:user_id>/delete", methods=["POST"])
@role_required("admin")
def admin_delete_interventor(user_id: int):
    user = current_user()
    with get_connection() as conn:
        target = conn.execute("SELECT * FROM users WHERE id = ? AND role = 'interventor'", (user_id,)).fetchone()
        if not target:
            flash("Interventor no encontrado.", "danger")
            return redirect(url_for("admin_dashboard"))

        review_count = conn.execute("SELECT COUNT(*) AS c FROM contract_reviews WHERE interventor_user_id = ?", (user_id,)).fetchone()["c"]
        signature_refs = conn.execute("SELECT COUNT(*) AS c FROM contracts WHERE last_interventor_user_id = ?", (user_id,)).fetchone()["c"]
        if review_count or signature_refs:
            flash("No se puede borrar esta cuenta porque ya tiene firmas o historial asociado. Primero desactivala y conservá su trazabilidad.", "warning")
            return redirect(url_for("admin_dashboard"))

        conn.execute("DELETE FROM interventor_assignments WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM users WHERE id = ? AND role = 'interventor'", (user_id,))
        log_action(conn, user["id"], "delete_interventor", "user", user_id, f"username={target['username']}")
        conn.commit()
    flash("Cuenta de interventor eliminada.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/interventors/<int:user_id>/update", methods=["POST"])
@role_required("admin")
def admin_update_interventor(user_id: int):
    user = current_user()
    student_id_raw = request.form.get("student_id", "").strip()
    active_now = 1 if request.form.get("assignment_active") == "on" else 0
    student_id = int(student_id_raw) if student_id_raw else None
    with get_connection() as conn:
        existing = conn.execute("SELECT * FROM users WHERE id = ? AND role = 'interventor'", (user_id,)).fetchone()
        if not existing:
            flash("Interventor no encontrado.", "danger")
            return redirect(url_for("admin_dashboard"))
        assignment = conn.execute("SELECT * FROM interventor_assignments WHERE user_id = ? ORDER BY id DESC LIMIT 1", (user_id,)).fetchone()
        if active_now and (not assignment or not assignment["active"]):
            active_count = conn.execute("SELECT COUNT(*) AS c FROM interventor_assignments WHERE active = 1").fetchone()["c"]
            if active_count >= 2:
                flash("Ya hay dos interventores activos. Desactivá uno antes de activar otro.", "warning")
                return redirect(url_for("admin_dashboard"))
        if assignment:
            conn.execute(
                "UPDATE interventor_assignments SET student_id = ?, active = ?, end_date = CASE WHEN ? = 0 THEN CURRENT_TIMESTAMP ELSE NULL END WHERE id = ?",
                (student_id, active_now, active_now, assignment["id"]),
            )
        else:
            conn.execute(
                "INSERT INTO interventor_assignments (user_id, student_id, active) VALUES (?, ?, ?)",
                (user_id, student_id, active_now),
            )
        log_action(conn, user["id"], "update_interventor_assignment", "user", user_id, f"student={student_id or 'sin vínculo'} active={active_now}")
        conn.commit()
    flash("Asignación de interventor actualizada.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/cycles")
@role_required("admin")
def admin_cycle_center():
    with get_connection() as conn:
        settings = conn.execute("SELECT * FROM economic_settings ORDER BY effective_from DESC, id DESC LIMIT 1").fetchone()
        treasury = conn.execute("SELECT * FROM wallets WHERE owner_type = 'treasury' LIMIT 1").fetchone()
        open_cycle = get_open_cycle(conn)
        draft_cycle = open_cycle if open_cycle and not open_cycle["started"] else None
        active_cycle = open_cycle if open_cycle and open_cycle["started"] else None
        current_cycle = open_cycle

        participant_groups = {"robotica": [], "desarrollo": []}
        development_rows = []
        pending_charges = []
        applied_charges = []
        cycle_run = None
        if current_cycle:
            participant_groups["robotica"] = cycle_team_rows(conn, current_cycle["id"], "robotica")
            participant_groups["desarrollo"] = cycle_team_rows(conn, current_cycle["id"], "desarrollo")

        if active_cycle:
            development_rows = [dict(row) for row in development_team_cycle_rows(conn, active_cycle["id"])]
            pending_map = {
                row["team_id"]: row["total"]
                for row in conn.execute(
                    "SELECT team_id, SUM(amount) AS total FROM cycle_custom_charges WHERE cycle_id = ? AND status = 'pending' GROUP BY team_id",
                    (active_cycle["id"],),
                ).fetchall()
            }
            for row in development_rows:
                row["maintenance_amount"] = calculate_maintenance(row["member_count"], row["active_projects"], settings) if row["active"] else 0
                row["pending_extra"] = pending_map.get(row["id"], 0)
                row["projected_total"] = row["maintenance_amount"] + row["pending_extra"]
            pending_charges = conn.execute(
                """
                SELECT cc.*, t.name AS team_name
                FROM cycle_custom_charges cc
                JOIN teams t ON t.id = cc.team_id
                WHERE cc.cycle_id = ? AND cc.status = 'pending'
                ORDER BY cc.id DESC
                """,
                (active_cycle["id"],),
            ).fetchall()
            applied_charges = conn.execute(
                """
                SELECT cc.*, t.name AS team_name
                FROM cycle_custom_charges cc
                JOIN teams t ON t.id = cc.team_id
                WHERE cc.cycle_id = ? AND cc.status = 'applied'
                ORDER BY cc.id DESC
                """,
                (active_cycle["id"],),
            ).fetchall()
            cycle_run = conn.execute(
                """
                SELECT cr.*, u.username AS executed_by_username
                FROM cycle_runs cr
                LEFT JOIN users u ON u.id = cr.executed_by_user_id
                WHERE cr.cycle_id = ?
                """,
                (active_cycle["id"],),
            ).fetchone()

        if draft_cycle:
            draft_team_ids = [row["id"] for row in participant_groups["robotica"]] + [row["id"] for row in participant_groups["desarrollo"]]
        else:
            draft_team_ids = []
        available_robotics = conn.execute(
            f"SELECT id, name, active FROM teams WHERE team_type = 'robotica' AND active = 1 {'AND id NOT IN (' + ','.join(['?']*len(draft_team_ids)) + ')' if draft_team_ids else ''} ORDER BY name",
            tuple(draft_team_ids),
        ).fetchall()
        available_development = conn.execute(
            f"SELECT id, name, active FROM teams WHERE team_type = 'desarrollo' AND active = 1 {'AND id NOT IN (' + ','.join(['?']*len(draft_team_ids)) + ')' if draft_team_ids else ''} ORDER BY name",
            tuple(draft_team_ids),
        ).fetchall()
        creatable_robotics = conn.execute("SELECT id, name, active FROM teams WHERE team_type = 'robotica' AND active = 1 ORDER BY name").fetchall()
        creatable_development = conn.execute("SELECT id, name, active FROM teams WHERE team_type = 'desarrollo' AND active = 1 ORDER BY name").fetchall()

        previous_runs = conn.execute(
            """
            SELECT cr.*, c.name AS cycle_name, u.username AS executed_by_username
            FROM cycle_runs cr
            JOIN cycles c ON c.id = cr.cycle_id
            LEFT JOIN users u ON u.id = cr.executed_by_user_id
            ORDER BY cr.id DESC
            LIMIT 12
            """
        ).fetchall()
        closed_cycles = conn.execute(
            """
            SELECT c.*, 
                   COALESCE((SELECT COUNT(*) FROM cycle_teams ct WHERE ct.cycle_id = c.id), 0) AS linked_team_count,
                   COALESCE((SELECT COUNT(*) FROM cycle_teams ct JOIN teams t ON t.id = ct.team_id WHERE ct.cycle_id = c.id AND t.active = 1), 0) AS active_linked_team_count,
                   COALESCE((SELECT COUNT(*) FROM cycle_runs cr WHERE cr.cycle_id = c.id), 0) AS run_count,
                   COALESCE((SELECT COUNT(*) FROM rewards r WHERE r.cycle_id = c.id), 0) AS reward_count,
                   COALESCE((SELECT COUNT(*) FROM contracts k WHERE k.cycle_id = c.id), 0) AS contract_count
            FROM cycles c
            WHERE c.status = 'closed'
            ORDER BY c.id DESC
            LIMIT 12
            """
        ).fetchall()

    return render_template(
        "admin_cycle_center.html",
        settings=settings,
        treasury=treasury,
        current_cycle=current_cycle,
        cycle_state=cycle_state_label(current_cycle),
        draft_cycle=draft_cycle,
        active_cycle=active_cycle,
        participant_groups=participant_groups,
        development_rows=development_rows,
        pending_charges=pending_charges,
        applied_charges=applied_charges,
        cycle_run=cycle_run,
        previous_runs=previous_runs,
        available_robotics=available_robotics,
        available_development=available_development,
        creatable_robotics=creatable_robotics,
        creatable_development=creatable_development,
        closed_cycles=closed_cycles,
    )


@app.route("/admin/cycles/<int:cycle_id>")
@role_required("admin", "interventor")
def admin_cycle_detail(cycle_id: int):
    with get_connection() as conn:
        detail = cycle_overview_rows(conn, cycle_id)
        if not detail:
            flash("Ciclo no encontrado.", "danger")
            return redirect(url_for("admin_cycle_center"))
        settings = conn.execute("SELECT * FROM economic_settings ORDER BY effective_from DESC, id DESC LIMIT 1").fetchone()
        deliveries = conn.execute(
            """
            SELECT d.id, d.contract_id, d.status, d.submitted_at, d.original_filename,
                   CASE WHEN COALESCE(d.code_text, '') != '' THEN 1 ELSE 0 END AS has_code,
                   CASE WHEN COALESCE(d.repository_link, '') != '' THEN 1 ELSE 0 END AS has_link,
                   CASE WHEN COALESCE(d.stored_filename, '') != '' THEN 1 ELSE 0 END AS has_file,
                   rt.name AS robotics_name, dt.name AS development_name,
                   rt.name AS client_name, dt.name AS provider_name,
                   dt.service_track AS provider_service_track
            FROM deliveries d
            JOIN contracts c ON c.id = d.contract_id
            JOIN teams rt ON rt.id = COALESCE(c.client_team_id, c.robotics_team_id)
            JOIN teams dt ON dt.id = COALESCE(c.provider_team_id, c.development_team_id)
            WHERE c.cycle_id = ?
            ORDER BY d.id DESC
            LIMIT 20
            """,
            (cycle_id,),
        ).fetchall()
        reviews = conn.execute(
            """
            SELECT cr.contract_id, cr.review_stage, cr.decision, cr.comment, cr.signed_at,
                   u.username AS interventor_username,
                   rt.name AS robotics_name, dt.name AS development_name,
                   rt.name AS client_name, dt.name AS provider_name,
                   dt.service_track AS provider_service_track
            FROM contract_reviews cr
            JOIN contracts c ON c.id = cr.contract_id
            JOIN teams rt ON rt.id = COALESCE(c.client_team_id, c.robotics_team_id)
            JOIN teams dt ON dt.id = COALESCE(c.provider_team_id, c.development_team_id)
            JOIN users u ON u.id = cr.interventor_user_id
            WHERE c.cycle_id = ?
            ORDER BY cr.id DESC
            LIMIT 20
            """,
            (cycle_id,),
        ).fetchall()
        cycle_contracts = conn.execute(
            """
            SELECT c.*, rt.name AS robotics_name, dt.name AS development_name,
                   rt.name AS client_name, dt.name AS provider_name,
                   dt.service_track AS provider_service_track_resolved,
                   p.title AS portfolio_title,
                   iu.username AS last_interventor_username,
                   COALESCE((SELECT COUNT(*) FROM deliveries d WHERE d.contract_id = c.id), 0) AS delivery_count,
                   COALESCE((SELECT COUNT(*) FROM contract_reviews cr WHERE cr.contract_id = c.id), 0) AS review_count,
                   (SELECT MAX(d.submitted_at) FROM deliveries d WHERE d.contract_id = c.id) AS last_delivery_at
            FROM contracts c
            JOIN teams rt ON rt.id = COALESCE(c.client_team_id, c.robotics_team_id)
            JOIN teams dt ON dt.id = COALESCE(c.provider_team_id, c.development_team_id)
            LEFT JOIN portfolios p ON p.id = c.portfolio_id
            LEFT JOIN users iu ON iu.id = c.last_interventor_user_id
            WHERE c.cycle_id = ?
            ORDER BY c.id DESC
            """,
            (cycle_id,),
        ).fetchall()
        cycle_transactions = conn.execute(
            """
            SELECT tr.*, u.username AS actor_username,
                   fw.owner_type AS from_owner_type, fw.owner_id AS from_owner_id,
                   tw.owner_type AS to_owner_type, tw.owner_id AS to_owner_id,
                   ft.name AS from_team_name, tt.name AS to_team_name
            FROM transactions tr
            LEFT JOIN users u ON u.id = tr.created_by_user_id
            LEFT JOIN wallets fw ON fw.id = tr.from_wallet_id
            LEFT JOIN wallets tw ON tw.id = tr.to_wallet_id
            LEFT JOIN teams ft ON fw.owner_type = 'team' AND fw.owner_id = ft.id
            LEFT JOIN teams tt ON tw.owner_type = 'team' AND tw.owner_id = tt.id
            WHERE tr.cycle_id = ?
            ORDER BY tr.id DESC
            LIMIT 80
            """,
            (cycle_id,),
        ).fetchall()
        transaction_summary = conn.execute(
            """
            SELECT transaction_type,
                   COUNT(*) AS tx_count,
                   COALESCE(SUM(amount), 0) AS total_amount
            FROM transactions
            WHERE cycle_id = ?
            GROUP BY transaction_type
            ORDER BY total_amount DESC, transaction_type
            """,
            (cycle_id,),
        ).fetchall()
        transaction_totals = conn.execute(
            """
            SELECT COALESCE(SUM(amount), 0) AS total_amount, COUNT(*) AS tx_count
            FROM transactions
            WHERE cycle_id = ?
            """,
            (cycle_id,),
        ).fetchone()
    detail_payload = dict(detail)
    detail_payload.pop("contracts", None)
    return render_template(
        "admin_cycle_detail.html",
        settings=settings,
        deliveries=deliveries,
        reviews=reviews,
        contracts=cycle_contracts,
        cycle_transactions=cycle_transactions,
        transaction_summary=transaction_summary,
        transaction_totals=transaction_totals,
        **detail_payload,
    )


@app.route("/admin/contracts/<int:contract_id>")
@role_required("admin", "interventor")
def admin_contract_detail(contract_id: int):
    detail = fetch_contract_detail_bundle(contract_id)
    if not detail:
        flash("Contrato no encontrado.", "danger")
        return redirect(url_for("admin_cycle_center"))
    return render_template("admin_contract_detail.html", **detail)


@app.route("/desarrollo/contracts/<int:contract_id>")
@role_required("desarrollo_team")
def development_contract_detail(contract_id: int):
    user = current_user()
    detail = fetch_contract_detail_bundle(contract_id)
    if not detail or contract_provider_team_id(detail["contract"]) != user["team_id"]:
        flash("Contrato no encontrado.", "danger")
        return redirect_back("development_dashboard")
    contract = detail["contract"]
    can_submit_delivery = contract["status"] in OPEN_CONTRACT_STATUSES and not contract["paused_by_deadline"]
    return render_template(
        "contract_workspace.html",
        viewer_role="desarrollo_team",
        back_endpoint="development_dashboard",
        back_label="Volver al panel de desarrollo",
        can_submit_delivery=can_submit_delivery,
        current_contract=contract,
        **detail,
    )


@app.route("/desarrollo/client-contracts/<int:contract_id>")
@role_required("desarrollo_team")
def development_client_contract_detail(contract_id: int):
    user = current_user()
    detail = fetch_contract_detail_bundle(contract_id)
    if not detail or contract_client_team_id(detail["contract"]) != user["team_id"]:
        flash("Contrato no encontrado.", "danger")
        return redirect_back("development_dashboard")
    contract = detail["contract"]
    return render_template(
        "contract_workspace.html",
        viewer_role="desarrollo_team",
        back_endpoint="development_dashboard",
        back_label="Volver al panel de desarrollo",
        can_submit_delivery=False,
        current_contract=contract,
        **detail,
    )


@app.route("/robotica/contracts/<int:contract_id>")
@role_required("robotica_team")
def robotics_contract_detail(contract_id: int):
    user = current_user()
    detail = fetch_contract_detail_bundle(contract_id)
    if not detail or contract_client_team_id(detail["contract"]) != user["team_id"]:
        flash("Contrato no encontrado.", "danger")
        return redirect(url_for("robotics_dashboard"))
    contract = detail["contract"]
    return render_template(
        "contract_workspace.html",
        viewer_role="robotica_team",
        back_endpoint="robotics_dashboard",
        back_label="Volver al panel de robótica",
        can_submit_delivery=False,
        current_contract=contract,
        **detail,
    )


@app.route("/admin/cycles/create", methods=["POST"])
@role_required("admin")
def admin_create_cycle():
    user = current_user()
    name = request.form.get("name", "").strip()
    start_date = request.form.get("start_date", "").strip() or None
    end_date = request.form.get("end_date", "").strip() or None
    selected_team_ids = [int(team_id) for team_id in request.form.getlist("team_ids") if team_id]
    if not name:
        flash("El nuevo ciclo necesita un nombre.", "danger")
        return redirect(url_for("admin_cycle_center"))
    with get_connection() as conn:
        open_cycle = get_open_cycle(conn)
        if open_cycle:
            flash("Ya hay un ciclo en borrador o activo. Terminá ese proceso antes de abrir otro.", "warning")
            return redirect(url_for("admin_cycle_center"))
        selected_teams = []
        if selected_team_ids:
            placeholders = ",".join("?" for _ in selected_team_ids)
            selected_teams = conn.execute(
                f"SELECT id, name, team_type FROM teams WHERE id IN ({placeholders}) AND active = 1",
                tuple(selected_team_ids),
            ).fetchall()
        robotics_count = sum(1 for row in selected_teams if row["team_type"] == "robotica")
        development_count = sum(1 for row in selected_teams if row["team_type"] == "desarrollo")
        if robotics_count == 0 or development_count == 0:
            flash("Para crear el ciclo necesitás seleccionar al menos un equipo de robótica y uno de desarrollo.", "danger")
            return redirect(url_for("admin_cycle_center"))
        cycle_id = conn.execute(
            "INSERT INTO cycles (name, start_date, end_date, status, started) VALUES (?, ?, ?, 'open', 0)",
            (name, start_date, end_date),
        ).lastrowid
        for row in selected_teams:
            conn.execute(
                "INSERT INTO cycle_teams (cycle_id, team_id, team_type_snapshot) VALUES (?, ?, ?)",
                (cycle_id, row["id"], row["team_type"]),
            )
        log_action(conn, user["id"], "create_cycle", "cycle", cycle_id, f"{name} · borrador")
        conn.commit()
    flash("Ciclo creado en modo borrador. Revisá los equipos y después activalo.", "success")
    return redirect(url_for("admin_cycle_center"))


@app.route("/admin/cycles/<int:cycle_id>/teams/add", methods=["POST"])
@role_required("admin")
def admin_add_cycle_team(cycle_id: int):
    user = current_user()
    team_id = int(request.form.get("team_id") or 0)
    with get_connection() as conn:
        cycle = conn.execute("SELECT * FROM cycles WHERE id = ?", (cycle_id,)).fetchone()
        if not cycle or cycle["status"] != "open" or cycle["started"]:
            flash("Solo podés agregar equipos mientras el ciclo está en borrador.", "warning")
            return redirect(url_for("admin_cycle_center"))
        team = conn.execute("SELECT * FROM teams WHERE id = ? AND active = 1", (team_id,)).fetchone()
        if not team:
            flash("Elegí un equipo activo válido.", "danger")
            return redirect(url_for("admin_cycle_center"))
        if cycle_has_team(conn, cycle_id, team_id):
            flash("Ese equipo ya forma parte del ciclo.", "warning")
            return redirect(url_for("admin_cycle_center"))
        conn.execute(
            "INSERT INTO cycle_teams (cycle_id, team_id, team_type_snapshot) VALUES (?, ?, ?)",
            (cycle_id, team_id, team["team_type"]),
        )
        log_action(conn, user["id"], "add_cycle_team", "cycle", cycle_id, f"team={team_id}")
        conn.commit()
    flash("Equipo agregado al borrador del ciclo.", "success")
    return redirect(url_for("admin_cycle_center"))


@app.route("/admin/cycles/<int:cycle_id>/teams/<int:team_id>/remove", methods=["POST"])
@role_required("admin")
def admin_remove_cycle_team(cycle_id: int, team_id: int):
    user = current_user()
    with get_connection() as conn:
        cycle = conn.execute("SELECT * FROM cycles WHERE id = ?", (cycle_id,)).fetchone()
        if not cycle or cycle["status"] != "open" or cycle["started"]:
            flash("Solo podés quitar equipos mientras el ciclo está en borrador.", "warning")
            return redirect(url_for("admin_cycle_center"))
        conn.execute("DELETE FROM cycle_teams WHERE cycle_id = ? AND team_id = ?", (cycle_id, team_id))
        log_action(conn, user["id"], "remove_cycle_team", "cycle", cycle_id, f"team={team_id}")
        conn.commit()
    flash("Equipo quitado del borrador.", "success")
    return redirect(url_for("admin_cycle_center"))


@app.route("/admin/cycles/<int:cycle_id>/start", methods=["POST"])
@role_required("admin")
def admin_start_cycle(cycle_id: int):
    user = current_user()
    with get_connection() as conn:
        cycle = conn.execute("SELECT * FROM cycles WHERE id = ?", (cycle_id,)).fetchone()
        if not cycle or cycle["status"] != "open" or cycle["started"]:
            flash("Ese ciclo no está disponible para iniciarse.", "warning")
            return redirect(url_for("admin_cycle_center"))
        robotics_count = conn.execute(
            "SELECT COUNT(*) AS c FROM cycle_teams ct JOIN teams t ON t.id = ct.team_id WHERE ct.cycle_id = ? AND t.team_type = 'robotica'",
            (cycle_id,),
        ).fetchone()["c"]
        development_count = conn.execute(
            "SELECT COUNT(*) AS c FROM cycle_teams ct JOIN teams t ON t.id = ct.team_id WHERE ct.cycle_id = ? AND t.team_type = 'desarrollo'",
            (cycle_id,),
        ).fetchone()["c"]
        if robotics_count == 0 or development_count == 0:
            flash("Para iniciar el ciclo necesitás al menos un equipo de robótica y uno de desarrollo implicados.", "danger")
            return redirect(url_for("admin_cycle_center"))
        conn.execute("UPDATE cycles SET started = 1 WHERE id = ?", (cycle_id,))
        log_action(conn, user["id"], "start_cycle", "cycle", cycle_id, cycle["name"])
        conn.commit()
    flash("Ciclo iniciado. A partir de ahora ya se pueden operar contratos, recompensas y cobros sobre este grupo de equipos.", "success")
    return redirect(url_for("admin_cycle_center"))


@app.route("/admin/cycles/<int:cycle_id>/delete", methods=["POST"])
@role_required("admin")
def admin_delete_cycle(cycle_id: int):
    user = current_user()
    with get_connection() as conn:
        cycle = conn.execute("SELECT * FROM cycles WHERE id = ?", (cycle_id,)).fetchone()
        if not cycle:
            flash("Ciclo no encontrado.", "danger")
            return redirect(url_for("admin_cycle_center"))
        if cycle["status"] == "open" and cycle["started"]:
            flash("No podés borrar un ciclo activo. Primero hay que finalizarlo.", "warning")
            return redirect(url_for("admin_cycle_center"))
        linked_team_count = conn.execute(
            "SELECT COUNT(*) AS c FROM cycle_teams ct JOIN teams t ON t.id = ct.team_id WHERE ct.cycle_id = ? AND t.active = 1",
            (cycle_id,),
        ).fetchone()["c"]
        if cycle["status"] == "closed" and linked_team_count > 0:
            flash("Este ciclo todavía conserva equipos activos vinculados. Solo se puede borrar cuando esos equipos ya no estén activos en el sistema.", "warning")
            return redirect(url_for("admin_cycle_center"))
        conn.execute("DELETE FROM cycles WHERE id = ?", (cycle_id,))
        log_action(conn, user["id"], "delete_cycle", "cycle", cycle_id, cycle["name"])
        conn.commit()
    flash("Ciclo borrado correctamente.", "success")
    return redirect(url_for("admin_cycle_center"))


@app.route("/admin/cycles/charges/add", methods=["POST"])
@role_required("admin")
def admin_add_cycle_charge():
    user = current_user()
    team_id = int(request.form.get("team_id") or 0)
    amount = int(request.form.get("amount") or 0)
    reason = request.form.get("reason", "").strip()
    if amount <= 0 or not reason:
        flash("El cargo específico necesita monto positivo y motivo.", "danger")
        return redirect(url_for("admin_cycle_center"))
    with get_connection() as conn:
        active_cycle = get_active_cycle(conn)
        if not active_cycle:
            flash("No hay un ciclo activo para cargar ese cobro.", "danger")
            return redirect(url_for("admin_cycle_center"))
        already_run = conn.execute("SELECT id FROM cycle_runs WHERE cycle_id = ?", (active_cycle['id'],)).fetchone()
        if already_run:
            flash("Ese ciclo ya fue ejecutado. Abrí uno nuevo para agregar más cobros.", "warning")
            return redirect(url_for("admin_cycle_center"))
        team = conn.execute(
            """
            SELECT t.*
            FROM teams t
            JOIN cycle_teams ct ON ct.team_id = t.id AND ct.cycle_id = ?
            WHERE t.id = ? AND t.team_type = 'desarrollo'
            """,
            (active_cycle['id'], team_id),
        ).fetchone()
        if not team:
            flash("Elegí un equipo de desarrollo que participe en el ciclo activo.", "danger")
            return redirect(url_for("admin_cycle_center"))
        charge_id = conn.execute(
            "INSERT INTO cycle_custom_charges (cycle_id, team_id, amount, reason, status, created_by_user_id) VALUES (?, ?, ?, ?, 'pending', ?)",
            (active_cycle['id'], team_id, amount, reason, user['id']),
        ).lastrowid
        log_action(conn, user['id'], 'add_cycle_charge', 'cycle_custom_charge', charge_id, f"cycle={active_cycle['id']} team={team_id} amount={amount}")
        conn.commit()
    flash("Cargo específico agregado al ciclo.", "success")
    return redirect(url_for("admin_cycle_center"))


@app.route("/admin/cycles/charges/<int:charge_id>/cancel", methods=["POST"])
@role_required("admin")
def admin_cancel_cycle_charge(charge_id: int):
    user = current_user()
    with get_connection() as conn:
        charge = conn.execute("SELECT * FROM cycle_custom_charges WHERE id = ?", (charge_id,)).fetchone()
        if not charge:
            flash("Cargo no encontrado.", "danger")
            return redirect(url_for("admin_cycle_center"))
        if charge['status'] != 'pending':
            flash("Ese cargo ya no está pendiente y no se puede cancelar.", "warning")
            return redirect(url_for("admin_cycle_center"))
        conn.execute("UPDATE cycle_custom_charges SET status = 'cancelled' WHERE id = ?", (charge_id,))
        log_action(conn, user['id'], 'cancel_cycle_charge', 'cycle_custom_charge', charge_id, f"cycle={charge['cycle_id']}")
        conn.commit()
    flash("Cargo cancelado.", "success")
    return redirect(url_for("admin_cycle_center"))


@app.route("/admin/cycles/execute", methods=["POST"])
@role_required("admin")
def admin_execute_cycle():
    user = current_user()
    notes = request.form.get("notes", "").strip()
    create_next_draft = request.form.get("create_next_draft") == "on"
    next_cycle_name = request.form.get("next_cycle_name", "").strip()
    next_cycle_start = request.form.get("next_cycle_start", "").strip() or None
    next_cycle_end = request.form.get("next_cycle_end", "").strip() or None
    if create_next_draft and not next_cycle_name:
        flash("Si querés dejar preparado el siguiente ciclo, indicá su nombre.", "danger")
        return redirect(url_for("admin_cycle_center"))
    with get_connection() as conn:
        active_cycle = get_active_cycle(conn)
        if not active_cycle:
            flash("No hay un ciclo activo para ejecutar.", "danger")
            return redirect(url_for("admin_cycle_center"))
        existing_run = conn.execute("SELECT * FROM cycle_runs WHERE cycle_id = ?", (active_cycle['id'],)).fetchone()
        if existing_run:
            flash("Ese ciclo ya fue ejecutado.", "warning")
            return redirect(url_for("admin_cycle_center"))
        settings = conn.execute("SELECT * FROM economic_settings ORDER BY effective_from DESC, id DESC LIMIT 1").fetchone()
        treasury = conn.execute("SELECT * FROM wallets WHERE owner_type = 'treasury' LIMIT 1").fetchone()
        if not settings or not treasury:
            flash("Falta configuración económica o tesorería.", "danger")
            return redirect(url_for("admin_cycle_center"))

        maintenance_total = 0
        for row in development_team_cycle_rows(conn, active_cycle['id']):
            if not row['active']:
                continue
            amount = calculate_maintenance(row['member_count'], row['active_projects'], settings)
            if amount <= 0:
                continue
            team_wallet = conn.execute("SELECT * FROM wallets WHERE owner_type = 'team' AND owner_id = ?", (row['id'],)).fetchone()
            if not team_wallet:
                continue
            conn.execute("UPDATE wallets SET balance = balance - ? WHERE id = ?", (amount, team_wallet['id']))
            conn.execute("UPDATE wallets SET balance = balance + ? WHERE id = ?", (amount, treasury['id']))
            conn.execute(
                "INSERT INTO transactions (from_wallet_id, to_wallet_id, amount, transaction_type, description, created_by_user_id, cycle_id) VALUES (?, ?, ?, 'maintenance', ?, ?, ?)",
                (team_wallet['id'], treasury['id'], amount, f"{active_cycle['name']}: mantenimiento automático", user['id'], active_cycle['id']),
            )
            maintenance_total += amount

        pending_charges = conn.execute(
            """
            SELECT cc.*, w.id AS wallet_id
            FROM cycle_custom_charges cc
            JOIN wallets w ON w.owner_type = 'team' AND w.owner_id = cc.team_id
            WHERE cc.cycle_id = ? AND cc.status = 'pending'
            ORDER BY cc.id
            """,
            (active_cycle['id'],),
        ).fetchall()
        custom_total = 0
        for charge in pending_charges:
            conn.execute("UPDATE wallets SET balance = balance - ? WHERE id = ?", (charge['amount'], charge['wallet_id']))
            conn.execute("UPDATE wallets SET balance = balance + ? WHERE id = ?", (charge['amount'], treasury['id']))
            transaction_id = conn.execute(
                "INSERT INTO transactions (from_wallet_id, to_wallet_id, amount, transaction_type, description, created_by_user_id, cycle_id) VALUES (?, ?, ?, 'penalty', ?, ?, ?)",
                (charge['wallet_id'], treasury['id'], charge['amount'], f"{active_cycle['name']}: cargo específico · {charge['reason']}", user['id'], active_cycle['id']),
            ).lastrowid
            conn.execute("UPDATE cycle_custom_charges SET status = 'applied', applied_transaction_id = ? WHERE id = ?", (transaction_id, charge['id']))
            custom_total += charge['amount']

        run_id = conn.execute(
            "INSERT INTO cycle_runs (cycle_id, executed_by_user_id, maintenance_total, custom_total, notes) VALUES (?, ?, ?, ?, ?)",
            (active_cycle['id'], user['id'], maintenance_total, custom_total, notes),
        ).lastrowid
        conn.execute("UPDATE cycles SET status = 'closed' WHERE id = ?", (active_cycle['id'],))
        log_action(conn, user['id'], 'execute_cycle', 'cycle_run', run_id, f"cycle={active_cycle['id']} maintenance={maintenance_total} custom={custom_total}")

        next_cycle_id = None
        if create_next_draft:
            next_cycle_id = conn.execute(
                "INSERT INTO cycles (name, start_date, end_date, status, started) VALUES (?, ?, ?, 'open', 0)",
                (next_cycle_name, next_cycle_start, next_cycle_end),
            ).lastrowid
            log_action(conn, user['id'], 'create_cycle', 'cycle', next_cycle_id, f"{next_cycle_name} · borrador")

        conn.commit()

    extra = " y se dejó preparado un nuevo borrador" if create_next_draft else ""
    flash(f"Ciclo ejecutado y finalizado: mantenimiento {maintenance_total} + cargos específicos {custom_total}{extra}.", "success")
    return redirect(url_for("admin_cycle_center"))


def history_purge_blockers(conn) -> dict[str, int]:
    return {
        "teams": conn.execute("SELECT COUNT(*) AS c FROM teams").fetchone()["c"],
        "students": conn.execute("SELECT COUNT(*) AS c FROM students").fetchone()["c"],
        "interventors": conn.execute("SELECT COUNT(*) AS c FROM users WHERE role = 'interventor'").fetchone()["c"],
        "team_users": conn.execute("SELECT COUNT(*) AS c FROM users WHERE role IN ('robotica_team', 'desarrollo_team')").fetchone()["c"],
    }


@app.route("/admin/deliveries")
@role_required("admin")
def admin_deliveries():
    deliveries = query_all(
        """
        SELECT d.*, u.username AS submitted_by,
               c.status AS contract_status, c.requested_amount,
               rt.name AS robotics_name, dt.name AS development_name,
               rt.name AS client_name, dt.name AS provider_name,
               dt.service_track AS provider_service_track,
               CASE WHEN COALESCE(d.code_text, '') != '' THEN 1 ELSE 0 END AS has_code,
               CASE WHEN COALESCE(d.repository_link, '') != '' THEN 1 ELSE 0 END AS has_link,
               CASE WHEN COALESCE(d.stored_filename, '') != '' THEN 1 ELSE 0 END AS has_file
        FROM deliveries d
        JOIN users u ON d.submitted_by_user_id = u.id
        JOIN contracts c ON d.contract_id = c.id
        JOIN teams rt ON rt.id = COALESCE(c.client_team_id, c.robotics_team_id)
        JOIN teams dt ON dt.id = COALESCE(c.provider_team_id, c.development_team_id)
        ORDER BY d.id DESC
        """
    )
    with get_connection() as conn:
        purge_blockers = history_purge_blockers(conn)
    can_purge_history = all(value == 0 for value in purge_blockers.values())
    return render_template(
        "admin_deliveries.html",
        deliveries=deliveries,
        can_purge_history=can_purge_history,
        purge_blockers=purge_blockers,
    )


@app.route("/admin/deliveries/purge-history", methods=["POST"])
@role_required("admin")
def admin_purge_history():
    with get_connection() as conn:
        blockers = history_purge_blockers(conn)
        if any(value != 0 for value in blockers.values()):
            flash("No se puede borrar el historial todavía. Primero el sistema debe quedar sin equipos, estudiantes, cuentas de equipos ni interventores.", "warning")
            return redirect(url_for("admin_deliveries"))

        for table in [
            "ai_assistant_messages",
            "contract_reviews",
            "deliveries",
            "cycle_custom_charges",
            "cycle_runs",
            "rewards",
            "transactions",
            "contracts",
            "cycle_teams",
            "cycles",
            "audit_log",
            "portfolios",
            "team_gallery",
        ]:
            conn.execute(f"DELETE FROM {table}")

        try:
            conn.execute(
                "DELETE FROM sqlite_sequence WHERE name IN ('ai_assistant_messages','contract_reviews','deliveries','cycle_custom_charges','cycle_runs','rewards','transactions','contracts','cycle_teams','cycles','audit_log','portfolios','team_gallery')"
            )
        except Exception:
            pass
        conn.commit()

    for directory in (UPLOAD_DIR, REQUEST_UPLOAD_DIR, TEAM_LOGO_DIR, TEAM_GALLERY_DIR):
        try:
            for child in directory.iterdir():
                if child.is_file():
                    child.unlink(missing_ok=True)
        except Exception:
            pass

    flash("Historial completo borrado. Se eliminaron contratos, entregas, revisiones, movimientos, ciclos y archivos asociados.", "success")
    return redirect(url_for("admin_deliveries"))


@app.route("/admin/deliveries/<int:delivery_id>")
@role_required("admin")
def admin_delivery_detail(delivery_id: int):
    delivery = query_one(
        """
        SELECT d.*, u.username AS submitted_by, c.status AS contract_status, c.requested_amount,
               rt.id AS robotics_team_id, dt.id AS development_team_id,
               rt.id AS client_team_id, dt.id AS provider_team_id,
               rt.name AS robotics_name, dt.name AS development_name,
               rt.name AS client_name, dt.name AS provider_name,
               dt.service_track AS provider_service_track
        FROM deliveries d
        JOIN users u ON d.submitted_by_user_id = u.id
        JOIN contracts c ON d.contract_id = c.id
        JOIN teams rt ON rt.id = COALESCE(c.client_team_id, c.robotics_team_id)
        JOIN teams dt ON dt.id = COALESCE(c.provider_team_id, c.development_team_id)
        WHERE d.id = ?
        """,
        (delivery_id,),
    )
    if not delivery:
        flash("Entrega no encontrada.", "danger")
        return redirect(url_for("admin_deliveries"))
    reviews = query_all(
        """
        SELECT cr.*, u.username AS interventor_username
        FROM contract_reviews cr
        JOIN users u ON cr.interventor_user_id = u.id
        WHERE cr.contract_id = ?
        ORDER BY cr.id DESC
        """,
        (delivery["contract_id"],),
    )
    return render_template("admin_delivery_detail.html", delivery=delivery, reviews=reviews)


@app.route("/deliveries/files/<path:stored_filename>")
@login_required
def download_delivery_file(stored_filename: str):
    delivery = query_one(
        """
        SELECT d.*, rt.id AS robotics_team_id, dt.id AS development_team_id,
               rt.id AS client_team_id, dt.id AS provider_team_id
        FROM deliveries d
        JOIN contracts c ON d.contract_id = c.id
        JOIN teams rt ON rt.id = COALESCE(c.client_team_id, c.robotics_team_id)
        JOIN teams dt ON dt.id = COALESCE(c.provider_team_id, c.development_team_id)
        WHERE d.stored_filename = ?
        """,
        (stored_filename,),
    )
    if not delivery or not can_access_delivery(current_user(), delivery):
        abort(404)
    return send_from_directory(UPLOAD_DIR, stored_filename, as_attachment=True, download_name=delivery["original_filename"] or stored_filename)


@app.route("/deliveries/<int:delivery_id>/focus")
@login_required
def delivery_focus(delivery_id: int):
    delivery = query_one(
        """
        SELECT d.*, u.username AS submitted_by,
               c.id AS contract_id, c.robotics_team_id, c.development_team_id,
               c.client_team_id, c.provider_team_id,
               rt.name AS robotics_name, dt.name AS development_name,
               rt.name AS client_name, dt.name AS provider_name,
               p.title AS portfolio_title,
               CASE WHEN COALESCE(d.code_text, '') != '' THEN 1 ELSE 0 END AS has_code,
               CASE WHEN COALESCE(d.repository_link, '') != '' THEN 1 ELSE 0 END AS has_link,
               CASE WHEN COALESCE(d.stored_filename, '') != '' THEN 1 ELSE 0 END AS has_file
        FROM deliveries d
        JOIN contracts c ON d.contract_id = c.id
        JOIN teams rt ON rt.id = COALESCE(c.client_team_id, c.robotics_team_id)
        JOIN teams dt ON dt.id = COALESCE(c.provider_team_id, c.development_team_id)
        LEFT JOIN portfolios p ON p.id = c.portfolio_id
        LEFT JOIN users u ON u.id = d.submitted_by_user_id
        WHERE d.id = ?
        """,
        (delivery_id,),
    )
    if not delivery or not can_access_delivery(current_user(), delivery):
        abort(404)
    next_url = request.args.get("next")
    if not next_url:
        viewer = current_user()
        if viewer["role"] == "robotica_team":
            next_url = url_for("robotics_contract_detail", contract_id=delivery["contract_id"])
        elif viewer["role"] == "desarrollo_team":
            if contract_provider_team_id(delivery) == viewer.get("team_id"):
                next_url = url_for("development_contract_detail", contract_id=delivery["contract_id"])
            elif contract_client_team_id(delivery) == viewer.get("team_id"):
                next_url = url_for("development_client_contract_detail", contract_id=delivery["contract_id"])
            else:
                next_url = url_for("development_dashboard")
        elif viewer["role"] == "interventor":
            next_url = url_for("interventor_dashboard")
        else:
            next_url = url_for("admin_contract_detail", contract_id=delivery["contract_id"])
    return render_template("delivery_focus.html", delivery=delivery, next_url=next_url)


@app.route("/requests/files/<path:stored_filename>")
@login_required
def download_request_file(stored_filename: str):
    contract = query_one(
        """
        SELECT c.*, rt.id AS robotics_team_id, dt.id AS development_team_id,
               rt.id AS client_team_id, dt.id AS provider_team_id
        FROM contracts c
        JOIN teams rt ON rt.id = COALESCE(c.client_team_id, c.robotics_team_id)
        JOIN teams dt ON dt.id = COALESCE(c.provider_team_id, c.development_team_id)
        WHERE c.request_stored_filename = ?
        """,
        (stored_filename,),
    )
    if not contract or not can_access_contract(current_user(), contract):
        abort(404)
    return send_from_directory(REQUEST_UPLOAD_DIR, stored_filename, as_attachment=True, download_name=contract["request_original_filename"] or stored_filename)


@app.route("/public/team-assets/logo/<path:stored_filename>")
def public_team_logo_file(stored_filename: str):
    row = query_one("SELECT id FROM teams WHERE logo_stored_filename = ?", (stored_filename,))
    if not row:
        abort(404)
    return send_from_directory(TEAM_LOGO_DIR, stored_filename)


@app.route("/public/team-assets/gallery/<path:stored_filename>")
def public_team_gallery_file(stored_filename: str):
    row = query_one("SELECT id FROM team_gallery WHERE stored_filename = ?", (stored_filename,))
    if not row:
        abort(404)
    return send_from_directory(TEAM_GALLERY_DIR, stored_filename)


@app.route("/team-assets/logo/<path:stored_filename>")
@login_required
def team_logo_file(stored_filename: str):
    row = query_one("SELECT * FROM teams WHERE logo_stored_filename = ?", (stored_filename,))
    if not row:
        abort(404)
    user = current_user()
    if user["role"] in {"admin", "interventor"} or user.get("team_id") == row["id"]:
        return send_from_directory(TEAM_LOGO_DIR, stored_filename)
    # También permitir la visualización entre equipos sin exponer integrantes
    return send_from_directory(TEAM_LOGO_DIR, stored_filename)


@app.route("/team-assets/gallery/<path:stored_filename>")
@login_required
def team_gallery_file(stored_filename: str):
    row = query_one(
        "SELECT tg.*, t.id AS team_id FROM team_gallery tg JOIN teams t ON t.id = tg.team_id WHERE tg.stored_filename = ?",
        (stored_filename,),
    )
    if not row:
        abort(404)
    return send_from_directory(TEAM_GALLERY_DIR, stored_filename)


@app.route("/equipo/reglas")
@role_required("robotica_team", "desarrollo_team")
def team_rules_page():
    user = current_user()
    team = query_one("SELECT * FROM teams WHERE id = ?", (user["team_id"],))
    role_mode = "robotica" if user["role"] == "robotica_team" else "desarrollo"
    settings = query_one("SELECT * FROM economic_settings ORDER BY effective_from DESC, id DESC LIMIT 1")
    return render_template(
        "team_rules.html",
        team=team,
        role_mode=role_mode,
        contract_price=settings["contract_price"] if settings else 30,
        points_rule=build_points_rule_text(),
        points_per_panchicoin=POINTS_PER_PANCHICOIN,
        success_project_bonus=SUCCESS_PROJECT_BONUS,
        return_penalty=RETURN_PENALTY,
        cancellation_penalty=CANCELLATION_PENALTY,
    )


@app.route("/mi-equipo")
@role_required("robotica_team", "desarrollo_team")
def team_profile_manage():
    user = current_user()
    team = query_one("SELECT * FROM teams WHERE id = ?", (user["team_id"],))
    members = query_all(
        """
        SELECT s.full_name, s.course, tm.internal_role
        FROM team_members tm
        JOIN students s ON tm.student_id = s.id
        WHERE tm.team_id = ? AND tm.active = 1
        ORDER BY tm.id
        """,
        (team["id"],),
    )
    with get_connection() as conn:
        gallery = fetch_team_gallery(conn, team["id"])
    return render_template("team_profile_manage.html", team=team, members=members, gallery=gallery, service_track_options=SERVICE_TRACK_OPTIONS)


@app.route("/mi-equipo/actualizar", methods=["POST"])
@role_required("robotica_team", "desarrollo_team")
def update_team_profile():
    user = current_user()
    profile_blurb = (request.form.get("profile_blurb") or "").strip()
    logo_file = request.files.get("team_logo")
    try:
        logo_original, logo_stored = save_team_logo_file(logo_file, user["team_id"])
    except ValueError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("team_profile_manage"))
    with get_connection() as conn:
        team = conn.execute("SELECT * FROM teams WHERE id = ?", (user["team_id"],)).fetchone()
        if not team:
            flash("Equipo no encontrado.", "danger")
            return redirect(url_for("dashboard"))
        if logo_stored:
            conn.execute(
                "UPDATE teams SET profile_blurb = ?, logo_original_filename = ?, logo_stored_filename = ? WHERE id = ?",
                (profile_blurb, logo_original or team["logo_original_filename"], logo_stored, team["id"]),
            )
        else:
            conn.execute(
                "UPDATE teams SET profile_blurb = ? WHERE id = ?",
                (profile_blurb, team["id"]),
            )
        log_action(conn, user["id"], "update_team_profile", "team", team["id"], "visual_profile")
        conn.commit()
    flash("Perfil del equipo actualizado.", "success")
    return redirect(url_for("team_profile_manage"))


@app.route("/mi-equipo/galeria", methods=["POST"])
@role_required("robotica_team", "desarrollo_team")
def add_team_gallery_item():
    user = current_user()
    caption = (request.form.get("caption") or "").strip()
    image_file = request.files.get("gallery_image")
    try:
        original_name, stored_name = save_team_gallery_file(image_file, user["team_id"])
    except ValueError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("team_profile_manage"))
    if not stored_name:
        flash("Tenés que subir una imagen para la galería.", "warning")
        return redirect(url_for("team_profile_manage"))
    with get_connection() as conn:
        item_id = conn.execute(
            "INSERT INTO team_gallery (team_id, caption, original_filename, stored_filename) VALUES (?, ?, ?, ?)",
            (user["team_id"], caption, original_name, stored_name),
        ).lastrowid
        log_action(conn, user["id"], "add_team_gallery_item", "team_gallery", item_id, caption or original_name or "imagen")
        conn.commit()
    flash("Imagen agregada a la galería.", "success")
    return redirect(url_for("team_profile_manage"))


@app.route("/mi-equipo/galeria/<int:item_id>/delete", methods=["POST"])
@role_required("robotica_team", "desarrollo_team")
def delete_team_gallery_item(item_id: int):
    user = current_user()
    with get_connection() as conn:
        item = conn.execute("SELECT * FROM team_gallery WHERE id = ? AND team_id = ?", (item_id, user["team_id"])).fetchone()
        if not item:
            flash("Imagen no encontrada.", "danger")
            return redirect(url_for("team_profile_manage"))
        conn.execute("DELETE FROM team_gallery WHERE id = ?", (item_id,))
        log_action(conn, user["id"], "delete_team_gallery_item", "team_gallery", item_id, item["original_filename"] or "imagen")
        conn.commit()
    try:
        path = TEAM_GALLERY_DIR / item["stored_filename"]
        if path.exists():
            path.unlink()
    except Exception:
        pass
    flash("Imagen quitada de la galería.", "info")
    return redirect(url_for("team_profile_manage"))


@app.route("/admin/teams")
@role_required("admin", "interventor")
def admin_teams():
    teams = query_all(
        """
        SELECT t.*, COALESCE(w.balance, 0) AS wallet_balance,
               COUNT(CASE WHEN tm.active = 1 THEN 1 END) AS member_count,
               u.username AS login_username,
               COUNT(DISTINCT CASE WHEN c.status IN ('pending_interventor_activation','active','in_development','submitted_for_review','correction_required') THEN c.id END) AS active_contracts,
               oc.id AS open_cycle_id,
               oc.name AS open_cycle_name,
               oc.started AS open_cycle_started
        FROM teams t
        LEFT JOIN wallets w ON w.owner_type = 'team' AND w.owner_id = t.id
        LEFT JOIN team_members tm ON tm.team_id = t.id
        LEFT JOIN users u ON u.team_id = t.id AND u.role IN ('robotica_team', 'desarrollo_team')
        LEFT JOIN contracts c ON (COALESCE(c.client_team_id, c.robotics_team_id) = t.id OR COALESCE(c.provider_team_id, c.development_team_id) = t.id)
        LEFT JOIN cycle_teams ct ON ct.team_id = t.id
        LEFT JOIN cycles oc ON oc.id = ct.cycle_id AND oc.status = 'open'
        GROUP BY t.id, w.balance, u.username, oc.id, oc.name, oc.started
        ORDER BY t.team_type, t.service_track, t.name
        """
    )
    students = query_all(
        """
        SELECT s.*, t.name AS current_team, tm.internal_role
        FROM students s
        LEFT JOIN team_members tm ON tm.student_id = s.id AND tm.active = 1
        LEFT JOIN teams t ON t.id = tm.team_id
        WHERE s.active = 1
        ORDER BY s.full_name
        """
    )
    with get_connection() as conn:
        global_open_cycle = any_open_cycle(conn)
    return render_template(
        "admin_teams.html",
        teams=teams,
        students=students,
        internal_roles=INTERNAL_ROLES,
        team_role_options=ROLE_OPTIONS_BY_TEAM_TYPE,
        service_track_options=SERVICE_TRACK_OPTIONS,
        market_role_options=MARKET_ROLE_OPTIONS,
        global_open_cycle=global_open_cycle,
    )


@app.route("/admin/teams/<int:team_id>")
@role_required("admin", "interventor")
def admin_team_detail(team_id: int):
    team = query_one(
        """
        SELECT t.*, COALESCE(w.balance, 0) AS wallet_balance, w.id AS wallet_id,
               u.username AS login_username,
               u.id AS login_user_id,
               oc.id AS open_cycle_id,
               oc.name AS open_cycle_name,
               oc.started AS open_cycle_started
        FROM teams t
        LEFT JOIN wallets w ON w.owner_type = 'team' AND w.owner_id = t.id
        LEFT JOIN users u ON u.team_id = t.id AND u.role IN ('robotica_team', 'desarrollo_team')
        LEFT JOIN cycle_teams ct ON ct.team_id = t.id
        LEFT JOIN cycles oc ON oc.id = ct.cycle_id AND oc.status = 'open'
        WHERE t.id = ?
        """,
        (team_id,),
    )
    if not team:
        flash("Equipo no encontrado.", "danger")
        return redirect(url_for("admin_teams"))

    members = query_all(
        """
        SELECT tm.id AS member_id, tm.team_id, tm.student_id, tm.internal_role, tm.active AS membership_active,
               s.full_name, s.course, s.active AS student_active
        FROM team_members tm
        JOIN students s ON s.id = tm.student_id
        WHERE tm.team_id = ?
        ORDER BY tm.active DESC, s.full_name
        """,
        (team_id,),
    )
    available_students = query_all(
        """
        SELECT s.*
        FROM students s
        WHERE s.active = 1 AND NOT EXISTS (
            SELECT 1 FROM team_members tm WHERE tm.student_id = s.id AND tm.active = 1
        )
        ORDER BY s.full_name
        """
    )
    portfolios = query_all(
        "SELECT * FROM portfolios WHERE team_id = ? ORDER BY id DESC",
        (team_id,),
    )
    contracts = query_all(
        """
        SELECT c.*, rt.name AS robotics_name, dt.name AS development_name,
               rt.name AS client_name, dt.name AS provider_name,
               dt.service_track AS provider_service_track,
               iu.username AS last_interventor_username
        FROM contracts c
        JOIN teams rt ON rt.id = COALESCE(c.client_team_id, c.robotics_team_id)
        JOIN teams dt ON dt.id = COALESCE(c.provider_team_id, c.development_team_id)
        LEFT JOIN users iu ON iu.id = c.last_interventor_user_id
        WHERE COALESCE(c.client_team_id, c.robotics_team_id) = ? OR COALESCE(c.provider_team_id, c.development_team_id) = ?
        ORDER BY c.id DESC
        LIMIT 20
        """,
        (team_id, team_id),
    )
    wallet_transactions = query_all(
        """
        SELECT tr.*, fu.username AS actor_username,
               fw.owner_type AS from_owner_type, tw.owner_type AS to_owner_type,
               ft.name AS from_team_name, tt.name AS to_team_name
        FROM transactions tr
        LEFT JOIN users fu ON tr.created_by_user_id = fu.id
        LEFT JOIN wallets fw ON tr.from_wallet_id = fw.id
        LEFT JOIN wallets tw ON tr.to_wallet_id = tw.id
        LEFT JOIN teams ft ON fw.owner_type = 'team' AND fw.owner_id = ft.id
        LEFT JOIN teams tt ON tw.owner_type = 'team' AND tw.owner_id = tt.id
        WHERE tr.from_wallet_id = ? OR tr.to_wallet_id = ?
        ORDER BY tr.id DESC
        LIMIT 20
        """,
        (team["wallet_id"], team["wallet_id"]),
    )
    with get_connection() as conn:
        reviews_by_contract = fetch_contract_reviews_map(conn, [row["id"] for row in contracts])
        open_cycle_info = team_in_open_cycle(conn, team_id)
        global_open_cycle = any_open_cycle(conn)
    member_count = query_one(
        "SELECT COUNT(*) AS c FROM team_members WHERE team_id = ? AND active = 1", (team_id,)
    )["c"]
    contract_count = query_one(
        "SELECT COUNT(*) AS c FROM contracts WHERE COALESCE(client_team_id, robotics_team_id) = ? OR COALESCE(provider_team_id, development_team_id) = ?",
        (team_id, team_id),
    )["c"]
    portfolio_count = query_one(
        "SELECT COUNT(*) AS c FROM portfolios WHERE team_id = ?", (team_id,)
    )["c"]
    can_hard_delete = contract_count == 0 and portfolio_count == 0
    can_delete_team_now = member_count == 0 and not global_open_cycle
    active_coo = None
    if team['team_type'] == 'desarrollo':
        coo_row = query_one(
            """
            SELECT s.full_name
            FROM team_members tm
            JOIN students s ON s.id = tm.student_id
            WHERE tm.team_id = ? AND tm.internal_role = 'COO' AND tm.active = 1
            LIMIT 1
            """,
            (team_id,),
        )
        active_coo = coo_row['full_name'] if coo_row else None
    return render_template(
        "admin_team_detail.html",
        team=team,
        members=members,
        available_students=available_students,
        portfolios=portfolios,
        contracts=contracts,
        wallet_transactions=wallet_transactions,
        internal_roles=INTERNAL_ROLES,
        allowed_roles=allowed_roles_for_team_type(team['team_type']),
        service_track_options=SERVICE_TRACK_OPTIONS,
        market_role_options=MARKET_ROLE_OPTIONS,
        portfolio_service_category_options=PORTFOLIO_SERVICE_CATEGORY_OPTIONS,
        member_count=member_count,
        contract_count=contract_count,
        portfolio_count=portfolio_count,
        can_hard_delete=can_hard_delete,
        can_delete_team_now=can_delete_team_now,
        active_coo=active_coo,
        open_cycle_info=open_cycle_info,
        global_open_cycle=global_open_cycle,
        reviews_by_contract=reviews_by_contract,
    )


@app.route("/admin/team/create", methods=["POST"])
@role_required("admin")
def admin_create_team():
    user = current_user()
    name = request.form.get("name", "").strip()
    requested_service_track = request.form.get("service_track", "").strip()
    course_label = request.form.get("course_label", "").strip()
    max_contracts = int(request.form.get("max_contracts", 2) or 2)
    team_type = team_type_from_service_track(requested_service_track)
    service_track = normalize_service_track(team_type, requested_service_track)
    market_role = normalize_market_role(team_type, service_track, request.form.get("market_role"))
    notes = request.form.get("notes", "").strip()
    initial_balance = int(request.form.get("initial_balance", 0) or 0)
    create_login = request.form.get("create_login") == "on"
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")

    if not name:
        flash("El equipo necesita un nombre.", "danger")
        return redirect_back("admin_teams")
    if requested_service_track not in SERVICE_TRACK_OPTIONS:
        flash("Servicio principal inválido.", "danger")
        return redirect_back("admin_teams")
    if max_contracts < 1:
        flash("El máximo de contratos debe ser al menos 1.", "danger")
        return redirect_back("admin_teams")

    with get_connection() as conn:
        try:
            team_id = conn.execute(
                "INSERT INTO teams (name, team_type, service_track, course_label, market_role, max_contracts, notes) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (name, team_type, service_track, course_label, market_role, max_contracts, notes),
            ).lastrowid
            wallet_id = conn.execute(
                "INSERT INTO wallets (owner_type, owner_id, balance) VALUES ('team', ?, ?)",
                (team_id, initial_balance),
            ).lastrowid
            if create_login:
                if not username or not password:
                    raise ValueError("Para crear login del equipo necesitás usuario y contraseña.")
                conn.execute(
                    "INSERT INTO users (username, password_hash, role, team_id, active) VALUES (?, ?, ?, ?, 1)",
                    (username, generate_password_hash(password), ROLE_BY_TEAM_TYPE[team_type], team_id),
                )
            descriptor = f"{name} ({course_label or 'Sin curso'} · {market_role} · {service_track})"
            log_action(conn, user["id"], "create_team", "team", team_id, descriptor)
            log_action(conn, user["id"], "create_wallet", "wallet", wallet_id, f"Saldo inicial: {initial_balance}")
            conn.commit()
            flash("Equipo creado correctamente.", "success")
        except Exception as exc:
            conn.rollback()
            flash(f"No se pudo crear el equipo: {exc}", "danger")
    return redirect_back("admin_teams")


@app.route("/admin/team/<int:team_id>/update", methods=["POST"])
@role_required("admin")
def admin_update_team(team_id: int):
    user = current_user()
    name = request.form.get("name", "").strip()
    course_label = request.form.get("course_label", "").strip()
    max_contracts = int(request.form.get("max_contracts", 2) or 2)
    notes = request.form.get("notes", "").strip()
    requested_service_track = request.form.get("service_track")
    active = 1 if request.form.get("active") == "on" else 0

    if not name:
        flash("El nombre del equipo no puede quedar vacío.", "danger")
        return redirect_back("admin_team_detail", team_id=team_id)

    with get_connection() as conn:
        team = conn.execute("SELECT * FROM teams WHERE id = ?", (team_id,)).fetchone()
        if not team:
            flash("Equipo no encontrado.", "danger")
            return redirect_back("admin_teams")
        open_cycle = team_in_open_cycle(conn, team_id)
        if open_cycle and active == 0:
            flash("Ese equipo forma parte de un ciclo en curso o en borrador. No se puede desactivar hasta cerrar o borrar ese ciclo.", "warning")
            return redirect_back("admin_team_detail", team_id=team_id)
        service_track = normalize_service_track(team["team_type"], requested_service_track or team["service_track"])
        market_role = normalize_market_role(team["team_type"], service_track, request.form.get("market_role"))
        conn.execute(
            "UPDATE teams SET name = ?, course_label = ?, service_track = ?, market_role = ?, max_contracts = ?, notes = ?, active = ? WHERE id = ?",
            (name, course_label, service_track, market_role, max_contracts, notes, active, team_id),
        )
        if active == 0:
            conn.execute("UPDATE users SET active = 0 WHERE team_id = ?", (team_id,))
        log_action(conn, user["id"], "update_team", "team", team_id, f"{name} · course={course_label or 'Sin curso'} · role={market_role} · track={service_track} · active={active} · max={max_contracts}")
        conn.commit()
    flash("Equipo actualizado.", "success")
    return redirect_back("admin_team_detail", team_id=team_id)


@app.route("/admin/team/<int:team_id>/delete", methods=["POST"])
@role_required("admin")
def admin_delete_team(team_id: int):
    user = current_user()
    with get_connection() as conn:
        team = conn.execute("SELECT * FROM teams WHERE id = ?", (team_id,)).fetchone()
        if not team:
            flash("Equipo no encontrado.", "danger")
            return redirect(url_for("admin_teams"))

        global_open_cycle = any_open_cycle(conn)
        if global_open_cycle:
            flash("No se puede borrar equipos mientras exista un ciclo o borrador abierto. Cerralo o borrarlo primero.", "warning")
            return redirect(url_for("admin_team_detail", team_id=team_id))

        active_members = conn.execute(
            "SELECT COUNT(*) AS c FROM team_members WHERE team_id = ? AND active = 1",
            (team_id,),
        ).fetchone()["c"]
        if active_members > 0:
            flash("Primero quitá o desactivá a todos los integrantes activos del equipo antes de borrarlo.", "warning")
            return redirect(url_for("admin_team_detail", team_id=team_id))

        conn.execute("DELETE FROM team_members WHERE team_id = ?", (team_id,))
        conn.execute("DELETE FROM users WHERE team_id = ?", (team_id,))
        conn.execute("DELETE FROM wallets WHERE owner_type = 'team' AND owner_id = ?", (team_id,))
        conn.execute("DELETE FROM teams WHERE id = ?", (team_id,))
        log_action(conn, user["id"], "delete_team", "team", team_id, team["name"])
        conn.commit()
    flash("Equipo borrado definitivamente.", "success")
    return redirect(url_for("admin_teams"))


@app.route("/admin/student/create", methods=["POST"])
@role_required("admin", "interventor")
def admin_create_student():
    user = current_user()
    full_name = request.form.get("full_name", "").strip()
    course = request.form.get("course", "").strip()
    team_id = request.form.get("team_id")
    internal_role = request.form.get("internal_role", "").strip()
    if not full_name:
        flash("El estudiante necesita nombre y apellido.", "danger")
        return redirect_back("admin_teams")
    with get_connection() as conn:
        student_id = conn.execute(
            "INSERT INTO students (full_name, course, active) VALUES (?, ?, 1)",
            (full_name, course),
        ).lastrowid
        log_action(conn, user["id"], "create_student", "student", student_id, full_name)
        if team_id:
            if any_open_cycle(conn):
                conn.rollback()
                flash("No se pueden asignar integrantes mientras exista un ciclo o borrador abierto.", "warning")
                return redirect_back("admin_teams")
            try:
                validate_member_role(conn, int(team_id), internal_role, active=1)
            except ValueError as exc:
                conn.rollback()
                flash(str(exc), "danger")
                return redirect_back("admin_teams")
            member_id = conn.execute(
                "INSERT INTO team_members (team_id, student_id, internal_role, active) VALUES (?, ?, ?, 1)",
                (int(team_id), student_id, internal_role),
            ).lastrowid
            log_action(conn, user["id"], "add_member", "team_member", member_id, f"student={student_id} team={team_id} role={internal_role}")
        conn.commit()
    flash("Estudiante agregado.", "success")
    return redirect_back("admin_teams")


@app.route("/admin/student/<int:student_id>/update", methods=["POST"])
@role_required("admin")
def admin_update_student(student_id: int):
    user = current_user()
    full_name = request.form.get("full_name", "").strip()
    course = request.form.get("course", "").strip()
    active = 1 if request.form.get("active") == "on" else 0
    if not full_name:
        flash("El estudiante no puede quedar sin nombre.", "danger")
        return redirect_back("admin_teams")
    with get_connection() as conn:
        conn.execute(
            "UPDATE students SET full_name = ?, course = ?, active = ? WHERE id = ?",
            (full_name, course, active, student_id),
        )
        if active == 0:
            conn.execute("UPDATE team_members SET active = 0 WHERE student_id = ?", (student_id,))
        log_action(conn, user["id"], "update_student", "student", student_id, f"{full_name} · active={active}")
        conn.commit()
    flash("Estudiante actualizado.", "success")
    return redirect_back("admin_teams")


@app.route("/admin/student/<int:student_id>/delete", methods=["POST"])
@role_required("admin")
def admin_delete_student(student_id: int):
    user = current_user()
    with get_connection() as conn:
        student = conn.execute("SELECT * FROM students WHERE id = ?", (student_id,)).fetchone()
        if not student:
            flash("Estudiante no encontrado.", "danger")
            return redirect(url_for("admin_teams"))
        active_membership = conn.execute(
            "SELECT 1 FROM team_members WHERE student_id = ? AND active = 1 LIMIT 1",
            (student_id,),
        ).fetchone()
        if active_membership:
            flash("Ese estudiante sigue formando parte de un equipo activo. Quitalo del equipo antes de borrarlo del sistema.", "warning")
            return redirect_back("admin_teams")
        conn.execute("DELETE FROM team_members WHERE student_id = ?", (student_id,))
        conn.execute("DELETE FROM students WHERE id = ?", (student_id,))
        log_action(conn, user["id"], "delete_student", "student", student_id, student["full_name"])
        conn.commit()
    flash("Estudiante borrado.", "success")
    return redirect_back("admin_teams")


@app.route("/admin/member/add", methods=["POST"])
@role_required("admin", "interventor")
def admin_add_member():
    user = current_user()
    team_id = int(request.form.get("team_id"))
    student_id = int(request.form.get("student_id"))
    internal_role = request.form.get("internal_role", "").strip()
    with get_connection() as conn:
        if any_open_cycle(conn):
            flash("No se pueden modificar integrantes de equipos mientras exista un ciclo o borrador abierto.", "warning")
            return redirect_back("admin_teams")
        existing = conn.execute(
            "SELECT * FROM team_members WHERE student_id = ? AND active = 1",
            (student_id,),
        ).fetchone()
        if existing:
            flash("Ese estudiante ya tiene una asignación activa. Editala o desactívala antes.", "warning")
            return redirect_back("admin_teams")
        try:
            validate_member_role(conn, team_id, internal_role, active=1)
        except ValueError as exc:
            flash(str(exc), "danger")
            return redirect_back("admin_teams")
        member_id = conn.execute(
            "INSERT INTO team_members (team_id, student_id, internal_role, active) VALUES (?, ?, ?, 1)",
            (team_id, student_id, internal_role),
        ).lastrowid
        log_action(conn, user["id"], "add_member", "team_member", member_id, f"student={student_id} team={team_id} role={internal_role}")
        conn.commit()
    flash("Integrante asignado al equipo.", "success")
    return redirect_back("admin_teams")


@app.route("/admin/member/<int:member_id>/update", methods=["POST"])
@role_required("admin")
def admin_update_member(member_id: int):
    user = current_user()
    team_id = int(request.form.get("team_id"))
    internal_role = request.form.get("internal_role", "").strip()
    active = 1 if request.form.get("active") == "on" else 0
    with get_connection() as conn:
        if any_open_cycle(conn):
            flash("No se pueden modificar integrantes de equipos mientras exista un ciclo o borrador abierto.", "warning")
            return redirect_back("admin_teams")
        try:
            validate_member_role(conn, team_id, internal_role, active=active, exclude_member_id=member_id)
        except ValueError as exc:
            flash(str(exc), "danger")
            return redirect_back("admin_teams")
        conn.execute(
            "UPDATE team_members SET team_id = ?, internal_role = ?, active = ? WHERE id = ?",
            (team_id, internal_role, active, member_id),
        )
        log_action(conn, user["id"], "update_member", "team_member", member_id, f"team={team_id} role={internal_role} active={active}")
        conn.commit()
    flash("Integrante actualizado.", "success")
    return redirect_back("admin_teams")


@app.route("/admin/member/<int:member_id>/delete", methods=["POST"])
@role_required("admin")
def admin_delete_member(member_id: int):
    user = current_user()
    with get_connection() as conn:
        if any_open_cycle(conn):
            flash("No se pueden quitar integrantes de equipos mientras exista un ciclo o borrador abierto.", "warning")
            return redirect_back("admin_teams")
        member = conn.execute("SELECT * FROM team_members WHERE id = ?", (member_id,)).fetchone()
        if not member:
            flash("Asignación no encontrada.", "danger")
            return redirect(url_for("admin_teams"))
        conn.execute("DELETE FROM team_members WHERE id = ?", (member_id,))
        log_action(conn, user["id"], "delete_member", "team_member", member_id, f"student={member['student_id']} team={member['team_id']}")
        conn.commit()
    flash("Integrante quitado del equipo.", "success")
    return redirect_back("admin_teams")


@app.route("/admin/wallet/<int:wallet_id>/adjust", methods=["POST"])
@role_required("admin")
def admin_adjust_wallet(wallet_id: int):
    user = current_user()
    amount = int(request.form.get("amount", 0) or 0)
    description = request.form.get("description", "Ajuste manual").strip()
    if amount == 0:
        flash("El ajuste no puede ser 0.", "danger")
        return redirect_back("admin_dashboard")
    with get_connection() as conn:
        wallet = conn.execute("SELECT * FROM wallets WHERE id = ?", (wallet_id,)).fetchone()
        if not wallet:
            flash("Billetera no encontrada.", "danger")
            return redirect_back("admin_dashboard")
        if wallet["balance"] + amount < 0:
            flash("Ese ajuste dejaría saldo negativo.", "danger")
            return redirect_back("admin_dashboard")
        conn.execute("UPDATE wallets SET balance = balance + ? WHERE id = ?", (amount, wallet_id))
        conn.execute(
            "INSERT INTO transactions (from_wallet_id, to_wallet_id, amount, transaction_type, description, created_by_user_id) VALUES (?, ?, ?, 'adjustment', ?, ?)",
            (None if amount > 0 else wallet_id, wallet_id if amount > 0 else None, abs(amount), description, user["id"]),
        )
        log_action(conn, user["id"], "adjust_wallet", "wallet", wallet_id, f"amount={amount} · {description}")
        conn.commit()
    flash("Ajuste aplicado.", "success")
    return redirect_back("admin_dashboard")


@app.route("/admin/offers/create", methods=["POST"])
@role_required("admin")
def admin_create_offer():
    user = current_user()
    title = (request.form.get("title") or "").strip()
    description = (request.form.get("description") or "").strip()
    service_category = normalize_service_category(request.form.get("service_category"), None)
    deadline = (request.form.get("deadline") or "").strip() or None
    try:
        reward_amount = int(request.form.get("reward_amount", 0) or 0)
    except ValueError:
        reward_amount = 0
    if not title:
        flash("La oferta necesita un título.", "danger")
        return redirect(url_for("admin_dashboard"))
    if reward_amount < 0:
        flash("La recompensa no puede ser negativa.", "danger")
        return redirect(url_for("admin_dashboard"))
    with get_connection() as conn:
        active_cycle = get_active_cycle(conn)
        offer_id = conn.execute(
            "INSERT INTO admin_offers (title, description, service_category, reward_amount, created_by_user_id, cycle_id, deadline, status) VALUES (?, ?, ?, ?, ?, ?, ?, 'open')",
            (title, description, service_category, reward_amount, user["id"], active_cycle["id"] if active_cycle else None, deadline),
        ).lastrowid
        log_action(conn, user["id"], "create_admin_offer", "admin_offer", offer_id, f"{title} · {service_category} · {reward_amount}")
        conn.commit()
    flash("Oferta pública creada y publicada en el mercado.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/offers/<int:offer_id>/close", methods=["POST"])
@role_required("admin")
def admin_close_offer(offer_id: int):
    user = current_user()
    with get_connection() as conn:
        offer = conn.execute("SELECT * FROM admin_offers WHERE id = ?", (offer_id,)).fetchone()
        if not offer:
            flash("Oferta no encontrada.", "danger")
            return redirect(url_for("admin_dashboard"))
        if offer["status"] == "closed":
            flash("Esa oferta ya está cerrada.", "warning")
            return redirect(url_for("admin_dashboard"))
        if offer["status"] == "cancelled":
            flash("Esa oferta ya fue cancelada.", "warning")
            return redirect(url_for("admin_dashboard"))
        if offer["taken_by_team_id"]:
            treasury_wallet = conn.execute("SELECT * FROM wallets WHERE owner_type = 'treasury' LIMIT 1").fetchone()
            team_wallet = conn.execute("SELECT * FROM wallets WHERE owner_type = 'team' AND owner_id = ?", (offer["taken_by_team_id"],)).fetchone()
            if not treasury_wallet or not team_wallet:
                flash("No se pudo localizar la tesorería o la billetera del equipo adjudicado.", "danger")
                return redirect(url_for("admin_dashboard"))
            if treasury_wallet["balance"] < offer["reward_amount"]:
                flash("Tesorería insuficiente para cerrar y pagar esta oferta.", "danger")
                return redirect(url_for("admin_dashboard"))
            if offer["reward_amount"] > 0:
                conn.execute("UPDATE wallets SET balance = balance - ? WHERE id = ?", (offer["reward_amount"], treasury_wallet["id"]))
                conn.execute("UPDATE wallets SET balance = balance + ? WHERE id = ?", (offer["reward_amount"], team_wallet["id"]))
                conn.execute(
                    "INSERT INTO transactions (from_wallet_id, to_wallet_id, amount, transaction_type, description, created_by_user_id, cycle_id) VALUES (?, ?, ?, 'reward', ?, ?, ?)",
                    (treasury_wallet["id"], team_wallet["id"], offer["reward_amount"], f"Pago de oferta admin #{offer_id}: {offer['title']}", user["id"], offer["cycle_id"]),
                )
        conn.execute("UPDATE admin_offers SET status = 'closed' WHERE id = ?", (offer_id,))
        log_action(conn, user["id"], "close_admin_offer", "admin_offer", offer_id, f"taken_by={offer['taken_by_team_id'] or 'nadie'}")
        conn.commit()
    flash("Oferta cerrada correctamente.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/offers/<int:offer_id>/cancel", methods=["POST"])
@role_required("admin")
def admin_cancel_offer(offer_id: int):
    user = current_user()
    with get_connection() as conn:
        offer = conn.execute("SELECT * FROM admin_offers WHERE id = ?", (offer_id,)).fetchone()
        if not offer:
            flash("Oferta no encontrada.", "danger")
            return redirect(url_for("admin_dashboard"))
        if offer["status"] in {"closed", "cancelled"}:
            flash("La oferta ya no se puede cancelar.", "warning")
            return redirect(url_for("admin_dashboard"))
        conn.execute("UPDATE admin_offers SET status = 'cancelled' WHERE id = ?", (offer_id,))
        log_action(conn, user["id"], "cancel_admin_offer", "admin_offer", offer_id, offer["title"])
        conn.commit()
    flash("Oferta cancelada.", "info")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/reward", methods=["POST"])
@role_required("admin")
def add_reward():
    team_id = int(request.form["team_id"])
    amount = int(request.form["amount"])
    reason = request.form.get("reason", "Recompensa manual")
    user = current_user()
    with get_connection() as conn:
        active_cycle = get_active_cycle(conn)
        if not active_cycle:
            flash("No hay un ciclo activo. Primero iniciá un ciclo con equipos implicados.", "warning")
            return redirect(url_for("admin_dashboard"))
        if not cycle_has_team(conn, active_cycle["id"], team_id):
            flash("Ese equipo no está implicado en el ciclo activo.", "danger")
            return redirect(url_for("admin_dashboard"))
        treasury = conn.execute(
            "SELECT * FROM wallets WHERE owner_type = 'treasury' LIMIT 1"
        ).fetchone()
        target = conn.execute(
            "SELECT * FROM wallets WHERE owner_type = 'team' AND owner_id = ?",
            (team_id,),
        ).fetchone()
        if not treasury or not target:
            flash("No se encontró tesorería o billetera destino.", "danger")
            return redirect(url_for("admin_dashboard"))
        if treasury["balance"] < amount:
            flash("La tesorería no tiene saldo suficiente.", "danger")
            return redirect(url_for("admin_dashboard"))
        conn.execute("UPDATE wallets SET balance = balance - ? WHERE id = ?", (amount, treasury["id"]))
        conn.execute("UPDATE wallets SET balance = balance + ? WHERE id = ?", (amount, target["id"]))
        reward_id = conn.execute(
            "INSERT INTO rewards (cycle_id, robotics_team_id, reason, amount, created_by_user_id) VALUES (?, ?, ?, ?, ?)",
            (active_cycle["id"], team_id, reason, amount, user["id"]),
        ).lastrowid
        conn.execute(
            "INSERT INTO transactions (from_wallet_id, to_wallet_id, amount, transaction_type, description, created_by_user_id, cycle_id) VALUES (?, ?, ?, 'reward', ?, ?, ?)",
            (treasury["id"], target["id"], amount, reason, user["id"], active_cycle['id']),
        )
        log_action(conn, user["id"], "reward", "reward", reward_id, f"team={team_id} amount={amount}")
        conn.commit()
    flash("Recompensa cargada correctamente.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/mercado")
@login_required
def marketplace():
    user = current_user()
    requested_category = (request.args.get("service_category") or "").strip()
    requested_track = (request.args.get("service_track") or "").strip()
    with get_connection() as conn:
        active_cycle = get_active_cycle(conn)
        portfolios = market_portfolio_rows(
            conn,
            active_cycle=active_cycle,
            service_category=requested_category or None,
            service_track=requested_track or None,
        )
        admin_offers = market_offer_rows(
            conn,
            active_cycle=active_cycle,
            service_category=requested_category or None,
        )
    return render_template(
        "marketplace.html",
        user=user,
        active_cycle=active_cycle,
        portfolios=portfolios,
        admin_offers=admin_offers,
        selected_service_category=requested_category,
        selected_service_track=requested_track,
        portfolio_service_category_options=PORTFOLIO_SERVICE_CATEGORY_OPTIONS,
        service_track_options=SERVICE_TRACK_OPTIONS,
    )


@app.route("/mercado/portfolio/<int:portfolio_id>")
@login_required
def market_portfolio_detail(portfolio_id: int):
    user = current_user()
    with get_connection() as conn:
        active_cycle = get_active_cycle(conn)
        settings = query_one("SELECT * FROM economic_settings ORDER BY effective_from DESC, id DESC LIMIT 1")
        params = []
        joins = []
        if active_cycle:
            joins.append("JOIN cycle_teams ct ON ct.team_id = t.id AND ct.cycle_id = ?")
            params.append(active_cycle['id'])
        params.append(portfolio_id)
        portfolio = conn.execute(
            f"""
            SELECT p.*, t.name AS team_name, t.id AS team_id, t.team_type, t.service_track, t.max_contracts,
                   t.logo_stored_filename, t.profile_blurb, t.active
            FROM portfolios p
            JOIN teams t ON p.team_id = t.id
            {' '.join(joins)}
            WHERE p.id = ? AND p.status = 'published' AND t.active = 1
            """,
            tuple(params),
        ).fetchone()
        if not portfolio:
            flash("Ese portfolio no está disponible en el mercado actual.", "warning")
            return redirect(url_for("marketplace"))
        gallery = fetch_team_gallery(conn, portfolio['team_id'], limit=10)
        contract_request_allowed = False
        contract_request_note = None
        contract_request_team = None
        contract_request_wallet = None
        contract_request_open_contracts = 0
        contract_request_price = settings["contract_price"] if settings else 30
        contract_request_dashboard_endpoint = team_dashboard_endpoint(user) if user else "dashboard"
        if user and user['role'] in {'robotica_team', 'desarrollo_team'}:
            contract_request_team = conn.execute("SELECT * FROM teams WHERE id = ?", (user['team_id'],)).fetchone()
            if contract_request_team:
                contract_request_wallet = conn.execute(
                    "SELECT * FROM wallets WHERE owner_type = 'team' AND owner_id = ?",
                    (contract_request_team['id'],),
                ).fetchone()
                allowed, note = team_can_request_portfolio(contract_request_team, portfolio)
                contract_request_note = note
                if allowed:
                    if not active_cycle:
                        contract_request_note = "No hay un ciclo activo listo para operar este contrato."
                    elif not cycle_has_team(conn, active_cycle['id'], contract_request_team['id']):
                        contract_request_note = "Tu equipo no está incluido en el ciclo activo."
                    elif not cycle_has_team(conn, active_cycle['id'], portfolio['team_id']):
                        contract_request_note = "Ese equipo proveedor no participa en el ciclo activo."
                    else:
                        contract_request_open_contracts = count_open_client_contracts(conn, contract_request_team['id'], active_cycle['id'])
                        if contract_request_open_contracts >= CLIENT_OPEN_CONTRACT_LIMIT:
                            contract_request_note = f"Tu equipo ya tiene {CLIENT_OPEN_CONTRACT_LIMIT} contratos abiertos como cliente en este ciclo."
                        else:
                            provider_open_contracts = count_open_provider_contracts(conn, portfolio['team_id'], active_cycle['id'])
                            if provider_open_contracts >= portfolio['max_contracts']:
                                contract_request_note = "Ese equipo proveedor ya alcanzó su carga máxima de trabajos activos."
                            elif not contract_request_wallet or contract_request_wallet['balance'] < contract_request_price:
                                contract_request_note = "Tu equipo no tiene saldo suficiente para reservar este contrato."
                            else:
                                contract_request_allowed = True
    return render_template(
        "market_portfolio_detail.html",
        user=user,
        active_cycle=active_cycle,
        portfolio=portfolio,
        gallery=gallery,
        contract_request_allowed=contract_request_allowed,
        contract_request_note=contract_request_note,
        contract_request_team=contract_request_team,
        contract_request_wallet=contract_request_wallet,
        contract_request_open_contracts=contract_request_open_contracts,
        contract_request_limit=CLIENT_OPEN_CONTRACT_LIMIT,
        contract_request_price=contract_request_price,
        contract_request_dashboard_endpoint=contract_request_dashboard_endpoint,
        portfolio_service_category_options=PORTFOLIO_SERVICE_CATEGORY_OPTIONS,
        service_track_options=SERVICE_TRACK_OPTIONS,
    )


@app.route("/market/offers/<int:offer_id>")
@login_required
def market_offer_detail(offer_id: int):
    user = current_user()
    with get_connection() as conn:
        active_cycle = get_active_cycle(conn)
        offer = conn.execute(
            """
            SELECT ao.*, u.username AS created_by_username, t.name AS taken_by_team_name
            FROM admin_offers ao
            LEFT JOIN users u ON u.id = ao.created_by_user_id
            LEFT JOIN teams t ON t.id = ao.taken_by_team_id
            WHERE ao.id = ?
            """,
            (offer_id,),
        ).fetchone()
        if not offer:
            flash("Oferta no encontrada.", "danger")
            return redirect(url_for("marketplace"))
        team = None
        can_take = False
        team_capacity_note = None
        if user and user["role"] == "desarrollo_team":
            team = conn.execute("SELECT * FROM teams WHERE id = ?", (user["team_id"],)).fetchone()
            if team_can_take_offer(team, offer) and offer["status"] == "open":
                cycle_ok = True
                if offer["cycle_id"]:
                    cycle_ok = cycle_has_team(conn, offer["cycle_id"], team["id"])
                open_contracts = conn.execute(
                    f"SELECT COUNT(*) AS total FROM contracts WHERE COALESCE(provider_team_id, development_team_id) = ? AND status IN ({','.join(['?'] * len(OPEN_CONTRACT_STATUSES))})",
                    (team["id"], *OPEN_CONTRACT_STATUSES),
                ).fetchone()["total"]
                taken_offers = conn.execute(
                    "SELECT COUNT(*) AS total FROM admin_offers WHERE taken_by_team_id = ? AND status = 'taken'",
                    (team["id"],),
                ).fetchone()["total"]
                capacity_left = team["max_contracts"] - (open_contracts + taken_offers)
                if not cycle_ok:
                    team_capacity_note = "Tu equipo no está incluido en el ciclo de esta oferta."
                elif capacity_left <= 0:
                    team_capacity_note = "Tu equipo ya alcanzó su carga activa máxima entre contratos y ofertas tomadas."
                else:
                    can_take = True
            elif team and not team_can_take_offer(team, offer):
                team_capacity_note = "Esta oferta no coincide con la línea de servicio de tu equipo."
        return render_template(
            "market_offer_detail.html",
            offer=offer,
            active_cycle=active_cycle,
            current_team=team,
            can_take=can_take,
            team_capacity_note=team_capacity_note,
            portfolio_service_category_options=PORTFOLIO_SERVICE_CATEGORY_OPTIONS,
            service_track_options=SERVICE_TRACK_OPTIONS,
        )


@app.route("/market/offers/<int:offer_id>/take", methods=["POST"])
@role_required("desarrollo_team")
def take_admin_offer(offer_id: int):
    user = current_user()
    with get_connection() as conn:
        offer = conn.execute("SELECT * FROM admin_offers WHERE id = ?", (offer_id,)).fetchone()
        team = conn.execute("SELECT * FROM teams WHERE id = ?", (user["team_id"],)).fetchone()
        if not offer or not team:
            flash("Oferta o equipo no encontrado.", "danger")
            return redirect(url_for("marketplace"))
        if offer["status"] != "open":
            flash("Esa oferta ya no está disponible para tomarla.", "warning")
            return redirect(url_for("market_offer_detail", offer_id=offer_id))
        if not team_can_take_offer(team, offer):
            flash("Tu equipo no es compatible con esta oferta pública.", "danger")
            return redirect(url_for("market_offer_detail", offer_id=offer_id))
        if offer["cycle_id"] and not cycle_has_team(conn, offer["cycle_id"], team["id"]):
            flash("Tu equipo no está incluido en el ciclo de esta oferta.", "warning")
            return redirect(url_for("market_offer_detail", offer_id=offer_id))
        open_contracts = conn.execute(
            f"SELECT COUNT(*) AS total FROM contracts WHERE COALESCE(provider_team_id, development_team_id) = ? AND status IN ({','.join(['?'] * len(OPEN_CONTRACT_STATUSES))})",
            (team["id"], *OPEN_CONTRACT_STATUSES),
        ).fetchone()["total"]
        taken_offers = conn.execute(
            "SELECT COUNT(*) AS total FROM admin_offers WHERE taken_by_team_id = ? AND status = 'taken'",
            (team["id"],),
        ).fetchone()["total"]
        if open_contracts + taken_offers >= team["max_contracts"]:
            flash("Tu equipo ya alcanzó su carga activa máxima entre contratos y ofertas tomadas.", "warning")
            return redirect(url_for("market_offer_detail", offer_id=offer_id))
        active_cycle = get_active_cycle(conn)
        conn.execute(
            "UPDATE admin_offers SET status = 'taken', taken_by_team_id = ?, cycle_id = COALESCE(cycle_id, ?) WHERE id = ?",
            (team["id"], active_cycle["id"] if active_cycle else None, offer_id),
        )
        log_action(conn, user["id"], "take_admin_offer", "admin_offer", offer_id, f"team={team['name']}")
        conn.commit()
    flash("Oferta tomada por tu equipo. Ahora el admin puede cerrarla y pagar desde tesorería cuando corresponda.", "success")
    return redirect(url_for("development_dashboard"))


@app.route("/robotica")
@role_required("robotica_team")
def robotics_dashboard():
    user = current_user()
    team = query_one("SELECT * FROM teams WHERE id = ?", (user["team_id"],))
    wallet = query_one(
        "SELECT * FROM wallets WHERE owner_type = 'team' AND owner_id = ?", (team["id"],)
    )
    members = query_all(
        """
        SELECT s.full_name, s.course, tm.internal_role
        FROM team_members tm
        JOIN students s ON tm.student_id = s.id
        WHERE tm.team_id = ? AND tm.active = 1
        ORDER BY tm.id
        """,
        (team["id"],),
    )
    with get_connection() as conn:
        active_cycle = get_active_cycle(conn)
        team_gallery = fetch_team_gallery(conn, team["id"], limit=6)
        if active_cycle and cycle_has_team(conn, active_cycle['id'], team['id']):
            portfolios = conn.execute(
                """
                SELECT p.*, t.name AS dev_team_name, t.logo_stored_filename, t.profile_blurb,
                       t.service_track,
                       (SELECT stored_filename FROM team_gallery tg WHERE tg.team_id = t.id ORDER BY tg.id DESC LIMIT 1) AS preview_gallery_image
                FROM portfolios p
                JOIN teams t ON p.team_id = t.id
                JOIN cycle_teams ct ON ct.team_id = t.id AND ct.cycle_id = ?
                WHERE p.status = 'published' AND t.active = 1
                ORDER BY p.created_at DESC
                """,
                (active_cycle['id'],),
            ).fetchall()
        else:
            portfolios = []
    track_counter = Counter()
    category_counter = Counter()
    for row in portfolios:
        if row['service_track']:
            track_counter[row['service_track']] += 1
        if row['service_category']:
            category_counter[row['service_category']] += 1
    market_featured_categories = []
    for key, label in PORTFOLIO_SERVICE_CATEGORY_OPTIONS.items():
        total = category_counter.get(key, 0)
        if total:
            market_featured_categories.append({'key': key, 'label': label, 'total': total})
    market_track_summary = []
    for key, label in SERVICE_TRACK_OPTIONS.items():
        total = track_counter.get(key, 0)
        if total:
            market_track_summary.append({'key': key, 'label': label, 'total': total})
    contracts = query_all(
        """
        SELECT c.*, dt.name AS development_name, dt.name AS provider_name,
               dt.service_track AS provider_service_track_resolved,
               p.title AS portfolio_title,
               iu.username AS last_interventor_username,
               (SELECT d.id FROM deliveries d WHERE d.contract_id = c.id ORDER BY d.id DESC LIMIT 1) AS latest_delivery_id
        FROM contracts c
        JOIN teams dt ON dt.id = COALESCE(c.provider_team_id, c.development_team_id)
        LEFT JOIN portfolios p ON p.id = c.portfolio_id
        LEFT JOIN users iu ON iu.id = c.last_interventor_user_id
        WHERE COALESCE(c.client_team_id, c.robotics_team_id) = ?
        ORDER BY c.id DESC
        """,
        (team["id"],),
    )
    with get_connection() as conn:
        contract_reviews = fetch_contract_reviews_map(conn, [row["id"] for row in contracts])
    latest_return_reviews = latest_review_lookup(contract_reviews, review_stage='final_delivery', decisions={'correction_required', 'rejected'})
    settings = query_one(
        "SELECT * FROM economic_settings ORDER BY effective_from DESC, id DESC LIMIT 1"
    )
    return render_template(
        "robotics_dashboard.html",
        team=team,
        wallet=wallet,
        members=members,
        team_gallery=team_gallery,
        portfolios=portfolios,
        contracts=contracts,
        contract_reviews=contract_reviews,
        latest_return_reviews=latest_return_reviews,
        settings=settings,
        active_cycle=active_cycle,
        market_featured_categories=market_featured_categories,
        market_track_summary=market_track_summary,
        portfolio_service_category_options=PORTFOLIO_SERVICE_CATEGORY_OPTIONS,
        service_track_options=SERVICE_TRACK_OPTIONS,
    )


@app.route("/robotica/portfolio/<int:portfolio_id>")
@role_required("robotica_team")
def robotics_portfolio_detail(portfolio_id: int):
    user = current_user()
    team = query_one("SELECT * FROM teams WHERE id = ?", (user["team_id"],))
    wallet = query_one(
        "SELECT * FROM wallets WHERE owner_type = 'team' AND owner_id = ?", (team["id"],)
    )
    with get_connection() as conn:
        active_cycle = get_active_cycle(conn)
        if not active_cycle or not cycle_has_team(conn, active_cycle['id'], team['id']):
            flash("No hay un ciclo activo para tu equipo o tu equipo todavía no fue incluido en ese ciclo.", "warning")
            return redirect_back("robotics_dashboard")
        portfolio = conn.execute(
            """
            SELECT p.*, t.name AS dev_team_name, t.id AS dev_team_id, t.max_contracts,
                   t.logo_stored_filename, t.profile_blurb, t.service_track
            FROM portfolios p
            JOIN teams t ON p.team_id = t.id
            JOIN cycle_teams ct ON ct.team_id = t.id AND ct.cycle_id = ?
            WHERE p.id = ? AND p.status = 'published' AND t.active = 1
            """,
            (active_cycle['id'], portfolio_id),
        ).fetchone()
        dev_gallery = fetch_team_gallery(conn, portfolio["dev_team_id"], limit=8) if portfolio else []
        my_gallery = fetch_team_gallery(conn, team["id"], limit=6)
    if not portfolio:
        flash("Ese portfolio ya no está disponible para el ciclo activo.", "danger")
        return redirect_back("robotics_dashboard")
    my_members = query_all(
        """
        SELECT s.full_name, s.course, tm.internal_role
        FROM team_members tm
        JOIN students s ON s.id = tm.student_id
        WHERE tm.team_id = ? AND tm.active = 1
        ORDER BY tm.id
        """,
        (team["id"],),
    )
    active_contracts = query_one(
        f"""
        SELECT COUNT(*) AS total
        FROM contracts
        WHERE COALESCE(provider_team_id, development_team_id) = ?
          AND status IN ({','.join(['?'] * len(OPEN_CONTRACT_STATUSES))})
        """,
        (portfolio["dev_team_id"], *OPEN_CONTRACT_STATUSES),
    )["total"]
    robotics_open_contracts = query_one(
        f"""
        SELECT COUNT(*) AS total
        FROM contracts
        WHERE COALESCE(client_team_id, robotics_team_id) = ? AND cycle_id = ?
          AND status IN ({','.join(['?'] * len(OPEN_CONTRACT_STATUSES))})
        """,
        (team['id'], active_cycle['id'], *OPEN_CONTRACT_STATUSES),
    )["total"]
    current_contract = query_one(
        f"""
        SELECT c.*, iu.username AS last_interventor_username
        FROM contracts c
        LEFT JOIN users iu ON iu.id = c.last_interventor_user_id
        WHERE c.cycle_id = ? AND COALESCE(c.client_team_id, c.robotics_team_id) = ? AND c.portfolio_id = ?
          AND c.status IN ({','.join(['?'] * len(OPEN_CONTRACT_STATUSES))})
        ORDER BY c.id DESC
        LIMIT 1
        """,
        (active_cycle["id"], team["id"], portfolio["id"], *OPEN_CONTRACT_STATUSES),
    )
    last_contract = query_one(
        """
        SELECT c.*, iu.username AS last_interventor_username
        FROM contracts c
        LEFT JOIN users iu ON iu.id = c.last_interventor_user_id
        WHERE c.cycle_id = ? AND COALESCE(c.client_team_id, c.robotics_team_id) = ? AND c.portfolio_id = ?
        ORDER BY c.id DESC
        LIMIT 1
        """,
        (active_cycle["id"], team["id"], portfolio["id"]),
    )
    current_contract_reviews = {}
    last_contract_reviews = {}
    latest_return_review = None
    target_contract = current_contract or last_contract
    if target_contract:
        with get_connection() as conn:
            review_map = fetch_contract_reviews_map(conn, [target_contract["id"]])
            if current_contract:
                current_contract_reviews = review_map
            else:
                last_contract_reviews = review_map
            latest_return_review = latest_review_lookup(review_map, review_stage='final_delivery', decisions={'correction_required', 'rejected'}).get(target_contract['id'])
    settings = query_one(
        "SELECT * FROM economic_settings ORDER BY effective_from DESC, id DESC LIMIT 1"
    )
    return render_template(
        "robotics_portfolio_detail.html",
        team=team,
        wallet=wallet,
        my_members=my_members,
        my_gallery=my_gallery,
        portfolio=portfolio,
        dev_gallery=dev_gallery,
        active_contracts=active_contracts,
        robotics_open_contracts=robotics_open_contracts,
        settings=settings,
        active_cycle=active_cycle,
        current_contract=current_contract,
        current_contract_reviews=current_contract_reviews,
        last_contract=last_contract,
        last_contract_reviews=last_contract_reviews,
        latest_return_review=latest_return_review,
        portfolio_service_category_options=PORTFOLIO_SERVICE_CATEGORY_OPTIONS,
        service_track_options=SERVICE_TRACK_OPTIONS,
    )


@app.route("/mercado/request-contract", methods=["POST"])
@role_required("robotica_team", "desarrollo_team")
def request_market_contract():
    user = current_user()
    portfolio_id = int(request.form["portfolio_id"])
    requested_delivery_date = (request.form.get("requested_delivery_date") or "").strip()
    request_message = (request.form.get("request_message") or "").strip()
    uploaded_file = request.files.get("request_file")
    next_target = (request.form.get("next") or "").strip()
    settings = query_one(
        "SELECT * FROM economic_settings ORDER BY effective_from DESC, id DESC LIMIT 1"
    )
    contract_price = settings["contract_price"] if settings else 30
    portfolio = query_one(
        """
        SELECT p.*, t.name AS team_name, t.id AS team_id, t.team_type, t.service_track, t.max_contracts, t.active
        FROM portfolios p
        JOIN teams t ON t.id = p.team_id
        WHERE p.id = ? AND p.status = 'published' AND t.active = 1
        """,
        (portfolio_id,),
    )
    if not portfolio:
        flash("Portfolio no encontrado o no publicado.", "danger")
        return safe_redirect_target(next_target, "marketplace")
    if not requested_delivery_date:
        flash("Tenés que indicar una fecha esperada de entrega.", "warning")
        return safe_redirect_target(next_target, "market_portfolio_detail", portfolio_id=portfolio_id)
    try:
        request_original_filename, request_stored_filename, request_file_size = save_request_file(uploaded_file, portfolio_id)
    except ValueError as exc:
        flash(str(exc), "danger")
        return safe_redirect_target(next_target, "market_portfolio_detail", portfolio_id=portfolio_id)

    with get_connection() as conn:
        active_cycle = get_active_cycle(conn)
        client_team = conn.execute("SELECT * FROM teams WHERE id = ?", (user["team_id"],)).fetchone()
        if not active_cycle:
            flash("No hay un ciclo activo. Primero iniciá uno desde el panel admin.", "warning")
            return safe_redirect_target(next_target, team_dashboard_endpoint(user))
        if not client_team:
            flash("Tu equipo no fue encontrado.", "danger")
            return safe_redirect_target(next_target, team_dashboard_endpoint(user))
        allowed, note = team_can_request_portfolio(client_team, portfolio)
        if not allowed:
            flash(note or "Tu equipo no puede contratar este portfolio en esta fase.", "warning")
            return safe_redirect_target(next_target, "market_portfolio_detail", portfolio_id=portfolio_id)
        if not cycle_has_team(conn, active_cycle['id'], client_team['id']):
            flash("Tu equipo no está implicado en el ciclo activo.", "warning")
            return safe_redirect_target(next_target, team_dashboard_endpoint(user))
        if not cycle_has_team(conn, active_cycle['id'], portfolio['team_id']):
            flash("Ese equipo proveedor no participa en el ciclo activo.", "warning")
            return safe_redirect_target(next_target, "market_portfolio_detail", portfolio_id=portfolio_id)
        team_wallet = conn.execute(
            "SELECT * FROM wallets WHERE owner_type = 'team' AND owner_id = ?",
            (user["team_id"],),
        ).fetchone()
        if not team_wallet or team_wallet["balance"] < contract_price:
            flash("Saldo insuficiente para pedir ese contrato.", "danger")
            return safe_redirect_target(next_target, "market_portfolio_detail", portfolio_id=portfolio_id)
        client_active_contracts = count_open_client_contracts(conn, user["team_id"], active_cycle['id'])
        if client_active_contracts >= CLIENT_OPEN_CONTRACT_LIMIT:
            flash(f"Tu equipo ya tiene {CLIENT_OPEN_CONTRACT_LIMIT} contratos abiertos en este ciclo. Esperen a cerrar uno para contratar de nuevo.", "warning")
            return safe_redirect_target(next_target, team_dashboard_endpoint(user))
        provider_active_contracts = count_open_provider_contracts(conn, portfolio["team_id"], active_cycle['id'])
        provider_team = conn.execute("SELECT * FROM teams WHERE id = ?", (portfolio["team_id"],)).fetchone()
        if provider_active_contracts >= provider_team["max_contracts"]:
            flash("Ese equipo proveedor ya alcanzó su máximo de proyectos activos.", "danger")
            return safe_redirect_target(next_target, "market_portfolio_detail", portfolio_id=portfolio_id)
        conn.execute(
            "UPDATE wallets SET balance = balance - ? WHERE id = ?",
            (contract_price, team_wallet["id"]),
        )
        contract_id = conn.execute(
            """
            INSERT INTO contracts (
                cycle_id, robotics_team_id, development_team_id,
                client_team_id, provider_team_id, client_team_type, provider_team_type, provider_service_track,
                portfolio_id, requested_amount, reserved_amount, status, requested_by_user_id,
                requested_delivery_date, request_message, request_file_path,
                request_original_filename, request_stored_filename, request_file_size,
                paused_by_deadline
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending_interventor_activation', ?, ?, ?, ?, ?, ?, ?, 0)
            """,
            (
                active_cycle['id'], client_team["id"], portfolio["team_id"],
                client_team["id"], portfolio["team_id"], client_team['team_type'], 'desarrollo', provider_team['service_track'] or 'programacion',
                portfolio_id, contract_price, contract_price, user["id"],
                requested_delivery_date, request_message,
                str((REQUEST_UPLOAD_DIR / request_stored_filename).relative_to(BASE_DIR)) if request_stored_filename else None,
                request_original_filename, request_stored_filename, request_file_size,
            ),
        ).lastrowid
        sync_contract_party_fields(conn, contract_id)
        conn.execute(
            "INSERT INTO transactions (from_wallet_id, to_wallet_id, amount, transaction_type, description, created_by_user_id, cycle_id) VALUES (?, NULL, ?, 'reserve', ?, ?, ?)",
            (team_wallet["id"], contract_price, f"Reserva del contrato #{contract_id}", user["id"], active_cycle['id']),
        )
        log_action(conn, user["id"], "request_contract", "contract", contract_id, f"portfolio={portfolio_id} deadline={requested_delivery_date}")
        conn.commit()
    flash("Contrato solicitado desde el mercado. Quedó pendiente de validación del interventor.", "success")
    return safe_redirect_target(next_target, team_dashboard_endpoint(user))


@app.route("/robotica/request-contract", methods=["POST"])
@role_required("robotica_team")
def request_contract():
    user = current_user()
    portfolio_id = int(request.form["portfolio_id"])
    requested_delivery_date = (request.form.get("requested_delivery_date") or "").strip()
    request_message = (request.form.get("request_message") or "").strip()
    uploaded_file = request.files.get("request_file")
    settings = query_one(
        "SELECT * FROM economic_settings ORDER BY effective_from DESC, id DESC LIMIT 1"
    )
    portfolio = query_one("SELECT * FROM portfolios WHERE id = ? AND status = 'published'", (portfolio_id,))
    if not portfolio:
        flash("Portfolio no encontrado o no publicado.", "danger")
        return redirect_back("robotics_dashboard")
    if not requested_delivery_date:
        flash("Tenés que indicar una fecha esperada de entrega.", "warning")
        return redirect_back("robotics_dashboard")
    try:
        request_original_filename, request_stored_filename, request_file_size = save_request_file(uploaded_file, portfolio_id)
    except ValueError as exc:
        flash(str(exc), "danger")
        return redirect_back("robotics_dashboard")

    with get_connection() as conn:
        active_cycle = get_active_cycle(conn)
        if not active_cycle:
            flash("No hay un ciclo activo. Primero iniciá uno desde el panel admin.", "warning")
            return redirect_back("robotics_dashboard")
        if not cycle_has_team(conn, active_cycle['id'], user['team_id']):
            flash("Tu equipo no está implicado en el ciclo activo.", "warning")
            return redirect_back("robotics_dashboard")
        if not cycle_has_team(conn, active_cycle['id'], portfolio['team_id']):
            flash("Ese equipo de desarrollo no participa en el ciclo activo.", "warning")
            return redirect_back("robotics_dashboard")
        team_wallet = conn.execute(
            "SELECT * FROM wallets WHERE owner_type = 'team' AND owner_id = ?",
            (user["team_id"],),
        ).fetchone()
        if not team_wallet or team_wallet["balance"] < settings["contract_price"]:
            flash("Saldo insuficiente para pedir ese contrato.", "danger")
            return redirect_back("robotics_dashboard")
        robotics_active_contracts = conn.execute(
            f"SELECT COUNT(*) AS total FROM contracts WHERE COALESCE(client_team_id, robotics_team_id) = ? AND cycle_id = ? AND status IN ({','.join(['?'] * len(OPEN_CONTRACT_STATUSES))})",
            (user["team_id"], active_cycle['id'], *OPEN_CONTRACT_STATUSES),
        ).fetchone()["total"]
        if robotics_active_contracts >= 2:
            flash("Tu equipo ya tiene 2 contratos abiertos en este ciclo. Esperen a cerrar uno para contratar de nuevo.", "warning")
            return redirect_back("robotics_dashboard")
        active_contracts = conn.execute(
            f"SELECT COUNT(*) AS total FROM contracts WHERE COALESCE(provider_team_id, development_team_id) = ? AND cycle_id = ? AND status IN ({','.join(['?'] * len(OPEN_CONTRACT_STATUSES))})",
            (portfolio["team_id"], active_cycle['id'], *OPEN_CONTRACT_STATUSES),
        ).fetchone()["total"]
        dev_team = conn.execute("SELECT * FROM teams WHERE id = ?", (portfolio["team_id"],)).fetchone()
        if active_contracts >= dev_team["max_contracts"]:
            flash("Ese equipo desarrollador ya alcanzó su máximo de proyectos activos.", "danger")
            return redirect_back("robotics_dashboard")
        conn.execute(
            "UPDATE wallets SET balance = balance - ? WHERE id = ?",
            (settings["contract_price"], team_wallet["id"]),
        )
        contract_id = conn.execute(
            """
            INSERT INTO contracts (
                cycle_id, robotics_team_id, development_team_id,
                client_team_id, provider_team_id, client_team_type, provider_team_type, provider_service_track,
                portfolio_id, requested_amount, reserved_amount, status, requested_by_user_id,
                requested_delivery_date, request_message, request_file_path,
                request_original_filename, request_stored_filename, request_file_size,
                paused_by_deadline
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending_interventor_activation', ?, ?, ?, ?, ?, ?, ?, 0)
            """,
            (
                active_cycle['id'], user["team_id"], portfolio["team_id"],
                user["team_id"], portfolio["team_id"], 'robotica', 'desarrollo', dev_team['service_track'] or 'programacion',
                portfolio_id, settings["contract_price"], settings["contract_price"], user["id"],
                requested_delivery_date, request_message,
                str((REQUEST_UPLOAD_DIR / request_stored_filename).relative_to(BASE_DIR)) if request_stored_filename else None,
                request_original_filename, request_stored_filename, request_file_size,
            ),
        ).lastrowid
        sync_contract_party_fields(conn, contract_id)
        conn.execute(
            "INSERT INTO transactions (from_wallet_id, to_wallet_id, amount, transaction_type, description, created_by_user_id, cycle_id) VALUES (?, NULL, ?, 'reserve', ?, ?, ?)",
            (team_wallet["id"], settings["contract_price"], f"Reserva del contrato #{contract_id}", user["id"], active_cycle['id']),
        )
        log_action(conn, user["id"], "request_contract", "contract", contract_id, f"portfolio={portfolio_id} deadline={requested_delivery_date}")
        conn.commit()
    flash("Contrato solicitado. Quedó pendiente de validación del interventor.", "success")
    return redirect_back("robotics_dashboard")


@app.route("/robotica/contracts/<int:contract_id>/cancel", methods=["POST"])

@role_required("robotica_team")
def cancel_robotics_contract(contract_id: int):
    user = current_user()
    reason = (request.form.get("cancel_reason") or "").strip()
    if not reason:
        flash("Tenés que escribir un motivo para cancelar el contrato.", "warning")
        return redirect_back("robotics_dashboard")
    with get_connection() as conn:
        contract = conn.execute(
            "SELECT * FROM contracts WHERE id = ? AND robotics_team_id = ?",
            (contract_id, user["team_id"]),
        ).fetchone()
        if not contract:
            flash("Contrato no encontrado.", "danger")
            return redirect_back("robotics_dashboard")
        if contract["status"] in ("closed", "cancelled", "validated"):
            flash("Ese contrato ya no se puede cancelar desde robótica.", "warning")
            return redirect_back("robotics_dashboard")
        if contract["payment_released"]:
            flash("Ese contrato ya liberó el pago y no se puede cancelar desde robótica.", "warning")
            return redirect_back("robotics_dashboard")
        robotics_wallet = conn.execute(
            "SELECT * FROM wallets WHERE owner_type = 'team' AND owner_id = ?",
            (user["team_id"],),
        ).fetchone()
        if contract["reserved_amount"] and robotics_wallet:
            conn.execute(
                "UPDATE wallets SET balance = balance + ? WHERE id = ?",
                (contract["reserved_amount"], robotics_wallet["id"]),
            )
            conn.execute(
                "INSERT INTO transactions (from_wallet_id, to_wallet_id, amount, transaction_type, description, created_by_user_id, cycle_id) VALUES (NULL, ?, ?, 'refund', ?, ?, ?)",
                (robotics_wallet["id"], contract["reserved_amount"], f"Cancelación del contrato #{contract_id}. Motivo: {reason}", user["id"], contract["cycle_id"]),
            )
        conn.execute(
            "UPDATE contracts SET status = 'cancelled', reserved_amount = 0, closed_at = CURRENT_TIMESTAMP WHERE id = ?",
            (contract_id,),
        )
        log_action(conn, user["id"], "cancel_contract_by_robotics", "contract", contract_id, reason)
        conn.commit()
    flash("Contrato cancelado y reserva devuelta al equipo de robótica.", "success")
    return redirect_back("robotics_dashboard")


@app.route("/desarrollo")
@role_required("desarrollo_team")
def development_dashboard():
    user = current_user()
    team = query_one("SELECT * FROM teams WHERE id = ?", (user["team_id"],))
    wallet = query_one(
        "SELECT * FROM wallets WHERE owner_type = 'team' AND owner_id = ?", (team["id"],)
    )
    members = query_all(
        """
        SELECT s.full_name, s.course, tm.internal_role
        FROM team_members tm
        JOIN students s ON tm.student_id = s.id
        WHERE tm.team_id = ? AND tm.active = 1
        ORDER BY tm.id
        """,
        (team["id"],),
    )
    with get_connection() as conn:
        team_gallery = fetch_team_gallery(conn, team["id"], limit=8)
    portfolios = query_all("SELECT * FROM portfolios WHERE team_id = ? ORDER BY id DESC", (team["id"],))
    contracts = query_all(
        """
        SELECT c.*, rt.name AS robotics_name, rt.name AS client_name,
               dt.name AS development_name, dt.name AS provider_name,
               iu.username AS last_interventor_username
        FROM contracts c
        JOIN teams rt ON rt.id = COALESCE(c.client_team_id, c.robotics_team_id)
        JOIN teams dt ON dt.id = COALESCE(c.provider_team_id, c.development_team_id)
        LEFT JOIN users iu ON iu.id = c.last_interventor_user_id
        WHERE COALESCE(c.provider_team_id, c.development_team_id) = ?
        ORDER BY c.id DESC
        """,
        (team["id"],),
    )
    client_contracts = query_all(
        """
        SELECT c.*, rt.name AS robotics_name, rt.name AS client_name,
               dt.name AS development_name, dt.name AS provider_name,
               iu.username AS last_interventor_username
        FROM contracts c
        JOIN teams rt ON rt.id = COALESCE(c.client_team_id, c.robotics_team_id)
        JOIN teams dt ON dt.id = COALESCE(c.provider_team_id, c.development_team_id)
        LEFT JOIN users iu ON iu.id = c.last_interventor_user_id
        WHERE COALESCE(c.client_team_id, c.robotics_team_id) = ?
          AND COALESCE(c.provider_team_id, c.development_team_id) != ?
        ORDER BY c.id DESC
        """,
        (team["id"], team["id"]),
    )
    deliveries = query_all(
        """
        SELECT d.*, c.status AS contract_status, c.id AS contract_id,
               rt.name AS robotics_name, rt.name AS client_name,
               CASE WHEN COALESCE(d.code_text, '') != '' THEN 1 ELSE 0 END AS has_code,
               CASE WHEN COALESCE(d.repository_link, '') != '' THEN 1 ELSE 0 END AS has_link,
               CASE WHEN COALESCE(d.stored_filename, '') != '' THEN 1 ELSE 0 END AS has_file
        FROM deliveries d
        JOIN contracts c ON d.contract_id = c.id
        JOIN teams rt ON rt.id = COALESCE(c.client_team_id, c.robotics_team_id)
        WHERE COALESCE(c.provider_team_id, c.development_team_id) = ?
        ORDER BY d.id DESC
        """,
        (team["id"],),
    )
    deliveries_by_contract: dict[int, list] = defaultdict(list)
    for delivery in deliveries:
        deliveries_by_contract[delivery["contract_id"]].append(delivery)
    with get_connection() as conn:
        contract_ids = sorted({row["id"] for row in contracts} | {row["id"] for row in client_contracts})
        reviews_by_contract = fetch_contract_reviews_map(conn, contract_ids)
        ai_messages_by_contract = fetch_ai_messages_map(conn, [row["id"] for row in contracts])
    latest_return_reviews = latest_review_lookup(reviews_by_contract, review_stage='final_delivery', decisions={'correction_required', 'rejected'})
    admin_offers = query_all(
        "SELECT * FROM admin_offers WHERE taken_by_team_id = ? ORDER BY id DESC",
        (team["id"],),
    )

    settings = query_one(
        "SELECT * FROM economic_settings ORDER BY effective_from DESC, id DESC LIMIT 1"
    )
    member_count = len(members)
    active_projects = len([c for c in contracts if c["status"] in {"active", "in_development", "submitted_for_review", "correction_required", "pending_interventor_activation"}])
    maintenance = calculate_maintenance(member_count, active_projects, settings)
    return render_template(
        "development_dashboard.html",
        team=team,
        wallet=wallet,
        members=members,
        team_gallery=team_gallery,
        portfolios=portfolios,
        contracts=contracts,
        client_contracts=client_contracts,
        deliveries_by_contract=deliveries_by_contract,
        reviews_by_contract=reviews_by_contract,
        latest_return_reviews=latest_return_reviews,
        ai_messages_by_contract=ai_messages_by_contract,
        admin_offers=admin_offers,
        maintenance=maintenance,
        portfolio_service_category_options=PORTFOLIO_SERVICE_CATEGORY_OPTIONS,
        service_track_options=SERVICE_TRACK_OPTIONS,
        ai_enabled=ai_feature_enabled(),
    )


@app.route("/desarrollo/portfolio/nuevo")
@role_required("desarrollo_team")
def development_new_portfolio():
    user = current_user()
    team = query_one("SELECT * FROM teams WHERE id = ?", (user["team_id"],))
    with get_connection() as conn:
        gallery = fetch_team_gallery(conn, team["id"], limit=8)
    return render_template("development_portfolio_form.html", team=team, gallery=gallery, portfolio=None, portfolio_service_category_options=PORTFOLIO_SERVICE_CATEGORY_OPTIONS, service_track_options=SERVICE_TRACK_OPTIONS)


@app.route("/desarrollo/portfolio/<int:portfolio_id>/editar")
@role_required("desarrollo_team")
def development_edit_portfolio(portfolio_id: int):
    user = current_user()
    team = query_one("SELECT * FROM teams WHERE id = ?", (user["team_id"],))
    portfolio = query_one("SELECT * FROM portfolios WHERE id = ? AND team_id = ?", (portfolio_id, user["team_id"]))
    if not portfolio:
        flash("Portfolio no encontrado.", "danger")
        return redirect_back("development_dashboard")
    with get_connection() as conn:
        gallery = fetch_team_gallery(conn, team["id"], limit=8)
    return render_template("development_portfolio_form.html", team=team, gallery=gallery, portfolio=portfolio, portfolio_service_category_options=PORTFOLIO_SERVICE_CATEGORY_OPTIONS, service_track_options=SERVICE_TRACK_OPTIONS)


@app.route("/desarrollo/portfolio", methods=["POST"])
@role_required("desarrollo_team")
def create_portfolio():
    user = current_user()
    title = request.form.get("title", "").strip()
    description = request.form.get("description", "").strip()
    skills = request.form.get("skills", "").strip()
    tools = request.form.get("tools", "").strip()
    work_style = request.form.get("work_style", "").strip()
    team = query_one("SELECT service_track FROM teams WHERE id = ?", (user["team_id"],))
    service_category = normalize_service_category(request.form.get("service_category"), team["service_track"] if team else None)
    if not title:
        flash("El portfolio necesita un título.", "danger")
        return redirect(url_for("development_new_portfolio"))
    portfolio_id = execute(
        "INSERT INTO portfolios (team_id, title, description, skills, tools, work_style, service_category, status) VALUES (?, ?, ?, ?, ?, ?, ?, 'published')",
        (user["team_id"], title, description, skills, tools, work_style, service_category),
    )
    with get_connection() as conn:
        log_action(conn, user["id"], "create_portfolio", "portfolio", portfolio_id, f"{title} · {service_category}")
        conn.commit()
    flash("Portfolio creado y publicado.", "success")
    return redirect(url_for("development_edit_portfolio", portfolio_id=portfolio_id))


@app.route("/desarrollo/portfolio/<int:portfolio_id>/update", methods=["POST"])
@role_required("desarrollo_team")
def update_portfolio(portfolio_id: int):
    user = current_user()
    title = request.form.get("title", "").strip()
    description = request.form.get("description", "").strip()
    skills = request.form.get("skills", "").strip()
    tools = request.form.get("tools", "").strip()
    work_style = request.form.get("work_style", "").strip()
    status = request.form.get("status", "published").strip()
    team = query_one("SELECT service_track FROM teams WHERE id = ?", (user["team_id"],))
    service_category = normalize_service_category(request.form.get("service_category"), team["service_track"] if team else None)
    if not title:
        flash("El portfolio necesita un título.", "danger")
        return redirect(url_for("development_edit_portfolio", portfolio_id=portfolio_id))
    if status not in {"draft", "published", "archived"}:
        status = "published"
    with get_connection() as conn:
        portfolio = conn.execute("SELECT * FROM portfolios WHERE id = ? AND team_id = ?", (portfolio_id, user['team_id'])).fetchone()
        if not portfolio:
            flash("Portfolio no encontrado.", "danger")
            return redirect_back("development_dashboard")
        conn.execute(
            "UPDATE portfolios SET title = ?, description = ?, skills = ?, tools = ?, work_style = ?, service_category = ?, status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (title, description, skills, tools, work_style, service_category, status, portfolio_id),
        )
        log_action(conn, user['id'], 'update_portfolio', 'portfolio', portfolio_id, f"{title} · {service_category}")
        conn.commit()
    flash("Portfolio actualizado.", "success")
    return redirect(url_for("development_edit_portfolio", portfolio_id=portfolio_id))


@app.route("/desarrollo/portfolio/<int:portfolio_id>/delete", methods=["POST"])
@role_required("desarrollo_team")
def delete_portfolio(portfolio_id: int):
    user = current_user()
    with get_connection() as conn:
        portfolio = conn.execute("SELECT * FROM portfolios WHERE id = ? AND team_id = ?", (portfolio_id, user['team_id'])).fetchone()
        if not portfolio:
            flash("Portfolio no encontrado.", "danger")
            return redirect_back("development_dashboard")
        contract_count = conn.execute("SELECT COUNT(*) AS c FROM contracts WHERE portfolio_id = ?", (portfolio_id,)).fetchone()['c']
        if contract_count > 0:
            conn.execute("UPDATE portfolios SET status = 'archived', updated_at = CURRENT_TIMESTAMP WHERE id = ?", (portfolio_id,))
            log_action(conn, user['id'], 'archive_portfolio', 'portfolio', portfolio_id, portfolio['title'])
            conn.commit()
            flash("El portfolio tenía historial. Se archivó en vez de borrarse.", "warning")
            return redirect_back("development_dashboard")
        conn.execute("DELETE FROM portfolios WHERE id = ?", (portfolio_id,))
        log_action(conn, user['id'], 'delete_portfolio', 'portfolio', portfolio_id, portfolio['title'])
        conn.commit()
    flash("Portfolio borrado.", "success")
    return redirect_back("development_dashboard")


@app.route("/desarrollo/contracts/<int:contract_id>/submit", methods=["POST"])
@role_required("desarrollo_team")
def submit_delivery(contract_id: int):
    user = current_user()
    notes = request.form.get("delivery_notes", "").strip()
    repo = request.form.get("repository_link", "").strip()
    code_text = request.form.get("code_text", "").strip()
    uploaded_file = request.files.get("delivery_file")
    contract = query_one(
        "SELECT * FROM contracts WHERE id = ? AND development_team_id = ?",
        (contract_id, user["team_id"]),
    )
    if not contract:
        flash("Contrato no encontrado.", "danger")
        return redirect_back("development_dashboard")
    if contract["paused_by_deadline"]:
        flash("Este contrato está pausado por vencimiento de la fecha. Esperá la decisión del interventor.", "warning")
        return redirect_back("development_dashboard")
    if not any([repo, code_text, uploaded_file and uploaded_file.filename]):
        flash("La entrega debe incluir al menos código pegado, un link o un archivo.", "danger")
        return redirect_back("development_dashboard")

    try:
        original_filename, stored_filename, file_size = save_delivery_file(uploaded_file, contract_id)
    except ValueError as exc:
        flash(str(exc), "danger")
        return redirect_back("development_dashboard")

    with get_connection() as conn:
        delivery_id = conn.execute(
            """
            INSERT INTO deliveries (
                contract_id, submitted_by_user_id, delivery_notes, repository_link,
                code_text, file_path, original_filename, stored_filename, file_size, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'submitted')
            """,
            (
                contract_id,
                user["id"],
                notes,
                repo,
                code_text,
                str((UPLOAD_DIR / stored_filename).relative_to(BASE_DIR)) if stored_filename else None,
                original_filename,
                stored_filename,
                file_size,
            ),
        ).lastrowid
        conn.execute(
            "UPDATE contracts SET status = 'submitted_for_review' WHERE id = ?",
            (contract_id,),
        )
        log_action(conn, user["id"], "submit_delivery", "delivery", delivery_id, f"contract={contract_id}")
        conn.commit()
    flash("Entrega cargada. Quedó pendiente de revisión.", "success")
    return redirect_back("development_dashboard")


def process_contract_ai_help(user, contract_id: int, question: str, pasted_code: str):
    question = (question or "").strip()
    pasted_code = (pasted_code or "").strip()
    if not question:
        return {"ok": False, "message": "Escribí una pregunta puntual para el asistente.", "category": "warning"}
    if len(question) > AI_MAX_QUESTION_CHARS:
        return {"ok": False, "message": f"La pregunta no puede superar {AI_MAX_QUESTION_CHARS} caracteres.", "category": "warning"}

    with get_connection() as conn:
        contract = conn.execute(
            """
            SELECT c.*, dt.name AS development_name, rt.name AS robotics_name
            FROM contracts c
            JOIN teams dt ON c.development_team_id = dt.id
            JOIN teams rt ON c.robotics_team_id = rt.id
            WHERE c.id = ? AND c.development_team_id = ?
            """,
            (contract_id, user["team_id"]),
        ).fetchone()
        if not contract:
            return {"ok": False, "message": "Contrato no encontrado.", "category": "danger"}
        if pasted_code:
            source_kind = "pasted_code"
            source_text = pasted_code[:AI_MAX_CODE_CHARS]
        else:
            source_kind, source_text, _source_label = latest_contract_code_context(conn, contract_id)
        if not source_text:
            return {"ok": False, "message": "Pegá un fragmento de código o cargá antes una entrega con código/texto para que el asistente tenga contexto.", "category": "warning"}
        context = {
            "team_name": contract["development_name"],
            "contract_id": str(contract_id),
            "request_message": contract["request_message"],
        }

    try:
        model_name, answer, status = request_ai_code_explanation(question, source_text, context)
    except Exception as exc:
        model_name = None
        status = "error"
        answer = f"No se pudo consultar al asistente: {exc}"

    with get_connection() as conn:
        message_id = conn.execute(
            """
            INSERT INTO ai_assistant_messages (
                contract_id, asked_by_user_id, question, pasted_code,
                source_kind, source_excerpt, response_text, status, model_name
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                contract_id,
                user["id"],
                question,
                pasted_code[:AI_MAX_CODE_CHARS] if pasted_code else None,
                source_kind or "pasted_code",
                (source_text or "")[:AI_MAX_CODE_CHARS],
                answer,
                status,
                model_name,
            ),
        ).lastrowid
        row = conn.execute(
            """
            SELECT am.*, u.username AS asked_by_username
            FROM ai_assistant_messages am
            LEFT JOIN users u ON u.id = am.asked_by_user_id
            WHERE am.id = ?
            """,
            (message_id,),
        ).fetchone()
        log_action(conn, user["id"], "ai_contract_help", "ai_assistant_message", message_id, f"contract={contract_id} status={status}")
        conn.commit()

    return {
        "ok": True,
        "message": answer,
        "category": "success" if status == "answered" else "warning",
        "row": dict(row),
        "status": status,
    }


@app.route("/desarrollo/contracts/<int:contract_id>/ai-help", methods=["POST"])
@role_required("desarrollo_team")
def contract_ai_help(contract_id: int):
    user = current_user()
    result = process_contract_ai_help(
        user,
        contract_id,
        request.form.get("question", ""),
        request.form.get("assistant_code_text", ""),
    )
    if result["ok"]:
        if result["status"] == "answered":
            flash("Consulta enviada al asistente y guardada en el historial del contrato.", "success")
        elif result["status"] == "blocked":
            flash("La respuesta quedó bloqueada porque se parecía demasiado a código. Revisá el historial del contrato.", "warning")
        else:
            flash(result["message"], "warning")
    else:
        flash(result["message"], result["category"])
    return redirect_back("development_dashboard")


@app.route("/desarrollo/contracts/<int:contract_id>/ai-help.json", methods=["POST"])
@role_required("desarrollo_team")
def contract_ai_help_json(contract_id: int):
    user = current_user()
    payload = request.get_json(silent=True) or {}
    result = process_contract_ai_help(
        user,
        contract_id,
        payload.get("question", ""),
        payload.get("assistant_code_text", ""),
    )
    if not result["ok"]:
        return jsonify({"ok": False, "message": result["message"], "category": result["category"]}), 400
    row = result["row"]
    return jsonify({
        "ok": True,
        "message": {
            "id": row["id"],
            "question": row["question"],
            "response_text": row["response_text"],
            "status": row["status"],
            "model_name": row["model_name"],
            "created_at": row["created_at"],
            "source_kind": row["source_kind"],
            "pasted_code": row["pasted_code"],
            "asked_by_username": row.get("asked_by_username"),
        },
    })


@app.route("/interventor")
@role_required("interventor")
def interventor_dashboard():
    with get_connection() as conn:
        active_cycle = get_active_cycle(conn)
        pending_activation = conn.execute(
            """
            SELECT c.*, rt.name AS robotics_name, dt.name AS development_name,
                   rt.name AS client_name, dt.name AS provider_name,
                   dt.service_track AS provider_service_track
            FROM contracts c
            JOIN teams rt ON rt.id = COALESCE(c.client_team_id, c.robotics_team_id)
            JOIN teams dt ON dt.id = COALESCE(c.provider_team_id, c.development_team_id)
            WHERE c.status = 'pending_interventor_activation'
            ORDER BY c.id DESC
            """
        ).fetchall()
        pending_delivery = conn.execute(
            """
            SELECT c.*, rt.name AS robotics_name, dt.name AS development_name,
                   rt.name AS client_name, dt.name AS provider_name,
                   dt.service_track AS provider_service_track,
                   d.id AS delivery_id, d.delivery_notes, d.repository_link, d.code_text,
                   d.original_filename, d.stored_filename, d.file_size, d.submitted_at,
                   CASE WHEN COALESCE(d.code_text, '') != '' THEN 1 ELSE 0 END AS has_code,
                   CASE WHEN COALESCE(d.repository_link, '') != '' THEN 1 ELSE 0 END AS has_link,
                   CASE WHEN COALESCE(d.stored_filename, '') != '' THEN 1 ELSE 0 END AS has_file
            FROM contracts c
            JOIN teams rt ON rt.id = COALESCE(c.client_team_id, c.robotics_team_id)
            JOIN teams dt ON dt.id = COALESCE(c.provider_team_id, c.development_team_id)
            LEFT JOIN deliveries d ON d.id = (
                SELECT d2.id FROM deliveries d2 WHERE d2.contract_id = c.id ORDER BY d2.id DESC LIMIT 1
            )
            WHERE c.status = 'submitted_for_review'
            ORDER BY c.id DESC
            """
        ).fetchall()
        paused_contracts = conn.execute(
            """
            SELECT c.*, rt.name AS robotics_name, dt.name AS development_name,
                   rt.name AS client_name, dt.name AS provider_name,
                   dt.service_track AS provider_service_track
            FROM contracts c
            JOIN teams rt ON rt.id = COALESCE(c.client_team_id, c.robotics_team_id)
            JOIN teams dt ON dt.id = COALESCE(c.provider_team_id, c.development_team_id)
            WHERE c.paused_by_deadline = 1 AND c.status NOT IN ('closed', 'cancelled')
            ORDER BY c.id DESC
            """
        ).fetchall()
    return render_template(
        "interventor_dashboard.html",
        active_cycle=active_cycle,
        pending_activation=pending_activation,
        pending_delivery=pending_delivery,
        paused_contracts=paused_contracts,
    )


@app.route("/interventor/students")
@role_required("interventor")
def interventor_students():
    with get_connection() as conn:
        open_cycle = any_open_cycle(conn)
        teams = conn.execute(
            """
            SELECT t.*, COALESCE(w.balance, 0) AS wallet_balance,
                   COUNT(CASE WHEN tm.active = 1 THEN 1 END) AS member_count
            FROM teams t
            LEFT JOIN wallets w ON w.owner_type = 'team' AND w.owner_id = t.id
            LEFT JOIN team_members tm ON tm.team_id = t.id
            WHERE t.active = 1
            GROUP BY t.id, w.balance
            ORDER BY COALESCE(NULLIF(t.course_label, ''), 'Sin curso'), t.service_track, t.name
            """
        ).fetchall()
        available_students = conn.execute(
            """
            SELECT s.*
            FROM students s
            WHERE s.active = 1 AND NOT EXISTS (
                SELECT 1 FROM team_members tm WHERE tm.student_id = s.id AND tm.active = 1
            )
            ORDER BY s.full_name
            """
        ).fetchall()
        active_members = conn.execute(
            """
            SELECT tm.id AS member_id, tm.team_id, tm.student_id, tm.internal_role,
                   s.full_name, s.course,
                   t.name AS team_name, t.team_type, t.service_track, t.course_label, t.market_role
            FROM team_members tm
            JOIN students s ON s.id = tm.student_id
            JOIN teams t ON t.id = tm.team_id
            WHERE tm.active = 1 AND s.active = 1
            ORDER BY COALESCE(NULLIF(t.course_label, ''), 'Sin curso'), t.name, s.full_name
            """
        ).fetchall()
    return render_template(
        "interventor_students.html",
        open_cycle=open_cycle,
        teams=teams,
        available_students=available_students,
        active_members=active_members,
        team_role_options=ROLE_OPTIONS_BY_TEAM_TYPE,
        service_track_options=SERVICE_TRACK_OPTIONS,
        market_role_options=MARKET_ROLE_OPTIONS,
    )


@app.route("/interventor/member/<int:member_id>/move", methods=["POST"])
@role_required("interventor")
def interventor_move_member(member_id: int):
    user = current_user()
    team_id_raw = (request.form.get("team_id") or "").strip()
    internal_role = (request.form.get("internal_role") or "").strip()
    if not team_id_raw:
        flash("Tenés que elegir un equipo de destino.", "warning")
        return redirect_back("interventor_students")
    team_id = int(team_id_raw)
    with get_connection() as conn:
        if any_open_cycle(conn):
            flash("No se pueden mover integrantes mientras exista un ciclo o borrador abierto.", "warning")
            return redirect_back("interventor_students")
        member = conn.execute(
            """
            SELECT tm.*, s.full_name, t.name AS current_team_name
            FROM team_members tm
            JOIN students s ON s.id = tm.student_id
            JOIN teams t ON t.id = tm.team_id
            WHERE tm.id = ?
            """,
            (member_id,),
        ).fetchone()
        if not member:
            flash("Asignación no encontrada.", "danger")
            return redirect(url_for("interventor_students"))
        try:
            validate_member_role(conn, team_id, internal_role, active=1, exclude_member_id=member_id)
        except ValueError as exc:
            flash(str(exc), "danger")
            return redirect_back("interventor_students")
        target_team = conn.execute("SELECT name FROM teams WHERE id = ?", (team_id,)).fetchone()
        conn.execute(
            "UPDATE team_members SET team_id = ?, internal_role = ?, active = 1 WHERE id = ?",
            (team_id, internal_role, member_id),
        )
        log_action(
            conn,
            user["id"],
            "interventor_move_member",
            "team_member",
            member_id,
            f"student={member['student_id']} from={member['current_team_name']} to={(target_team['name'] if target_team else team_id)} role={internal_role}",
        )
        conn.commit()
    flash("Integrante reasignado correctamente.", "success")
    return redirect_back("interventor_students")


@app.route("/interventor/contracts/<int:contract_id>/activation-review", methods=["POST"])
@role_required("interventor")
def activation_review(contract_id: int):
    user = current_user()
    decision = request.form.get("decision")
    comment = request.form.get("comment", "").strip()
    if decision not in {"approved", "rejected", "correction_required"}:
        flash("Decisión inválida.", "danger")
        return redirect(url_for("interventor_dashboard"))
    with get_connection() as conn:
        contract = conn.execute("SELECT * FROM contracts WHERE id = ?", (contract_id,)).fetchone()
        if not contract:
            flash("Contrato no encontrado.", "danger")
            return redirect(url_for("interventor_dashboard"))
        review_id = conn.execute(
            "INSERT INTO contract_reviews (contract_id, review_stage, interventor_user_id, decision, comment) VALUES (?, 'contract_activation', ?, ?, ?)",
            (contract_id, user["id"], decision, comment or "Sin comentario"),
        ).lastrowid
        new_status = "active" if decision == "approved" else ("cancelled" if decision == "rejected" else "correction_required")
        conn.execute(
            "UPDATE contracts SET status = ?, activated_at = CASE WHEN ? = 'approved' THEN CURRENT_TIMESTAMP ELSE activated_at END, last_interventor_user_id = ?, last_interventor_comment = ?, last_interventor_action = ?, last_interventor_signed_at = CURRENT_TIMESTAMP WHERE id = ?",
            (new_status, decision, user['id'], comment or 'Sin comentario', f'activation_{decision}', contract_id),
        )
        if decision == "rejected":
            team_wallet = conn.execute(
                "SELECT * FROM wallets WHERE owner_type = 'team' AND owner_id = ?",
                (contract["robotics_team_id"],),
            ).fetchone()
            if team_wallet and contract["reserved_amount"]:
                conn.execute(
                    "UPDATE wallets SET balance = balance + ? WHERE id = ?",
                    (contract["reserved_amount"], team_wallet["id"]),
                )
                conn.execute(
                    "INSERT INTO transactions (from_wallet_id, to_wallet_id, amount, transaction_type, description, created_by_user_id, cycle_id) VALUES (NULL, ?, ?, 'refund', ?, ?, ?)",
                    (team_wallet["id"], contract["reserved_amount"], f"Devolución del contrato #{contract_id}", user["id"], contract['cycle_id']),
                )
                conn.execute(
                    "UPDATE contracts SET reserved_amount = 0 WHERE id = ?",
                    (contract_id,),
                )
        log_action(conn, user["id"], "activation_review", "contract_review", review_id, f"contract={contract_id} decision={decision}")
        conn.commit()
    flash("Revisión de activación guardada.", "success")
    return redirect(url_for("interventor_dashboard"))


@app.route("/interventor/contracts/<int:contract_id>/deadline-reactivate", methods=["POST"])
@role_required("interventor")
def deadline_reactivate(contract_id: int):
    user = current_user()
    new_date = (request.form.get("new_delivery_date") or "").strip()
    if not new_date:
        flash("Tenés que indicar una nueva fecha de entrega.", "warning")
        return redirect(url_for("interventor_dashboard"))
    with get_connection() as conn:
        contract = conn.execute("SELECT * FROM contracts WHERE id = ?", (contract_id,)).fetchone()
        if not contract:
            flash("Contrato no encontrado.", "danger")
            return redirect(url_for("interventor_dashboard"))
        conn.execute(
            "UPDATE contracts SET paused_by_deadline = 0, paused_at = NULL, pause_reason = NULL, requested_delivery_date = ?, last_interventor_user_id = ?, last_interventor_comment = ?, last_interventor_action = 'deadline_reactivate', last_interventor_signed_at = CURRENT_TIMESTAMP WHERE id = ?",
            (new_date, user['id'], f'Reactivado con nueva fecha: {new_date}', contract_id),
        )
        log_action(conn, user["id"], "reactivate_deadline_contract", "contract", contract_id, f"new_deadline={new_date}")
        conn.commit()
    flash("Contrato reactivado con nueva fecha.", "success")
    return redirect(url_for("interventor_dashboard"))


@app.route("/interventor/contracts/<int:contract_id>/deadline-cancel", methods=["POST"])
@role_required("interventor")
def deadline_cancel(contract_id: int):
    user = current_user()
    comment = (request.form.get("comment") or "").strip()
    if not comment:
        flash("Escribí un motivo para cancelar el contrato vencido.", "warning")
        return redirect(url_for("interventor_dashboard"))
    with get_connection() as conn:
        contract = conn.execute("SELECT * FROM contracts WHERE id = ?", (contract_id,)).fetchone()
        if not contract:
            flash("Contrato no encontrado.", "danger")
            return redirect(url_for("interventor_dashboard"))
        if contract["payment_released"]:
            flash("Ese contrato ya liberó el pago y no puede cancelarse por vencimiento.", "warning")
            return redirect(url_for("interventor_dashboard"))
        robotics_wallet = conn.execute(
            "SELECT * FROM wallets WHERE owner_type = 'team' AND owner_id = ?",
            (contract["robotics_team_id"],),
        ).fetchone()
        if robotics_wallet and contract["reserved_amount"]:
            conn.execute(
                "UPDATE wallets SET balance = balance + ? WHERE id = ?",
                (contract["reserved_amount"], robotics_wallet["id"]),
            )
            conn.execute(
                "INSERT INTO transactions (from_wallet_id, to_wallet_id, amount, transaction_type, description, created_by_user_id, cycle_id) VALUES (NULL, ?, ?, 'refund', ?, ?, ?)",
                (robotics_wallet["id"], contract["reserved_amount"], f"Cancelación por vencimiento del contrato #{contract_id}. Motivo: {comment}", user["id"], contract['cycle_id']),
            )
        conn.execute(
            "UPDATE contracts SET status = 'cancelled', reserved_amount = 0, paused_by_deadline = 0, pause_reason = ?, closed_at = CURRENT_TIMESTAMP, last_interventor_user_id = ?, last_interventor_comment = ?, last_interventor_action = 'deadline_cancel', last_interventor_signed_at = CURRENT_TIMESTAMP WHERE id = ?",
            (comment, user['id'], comment, contract_id),
        )
        log_action(conn, user["id"], "cancel_deadline_contract", "contract", contract_id, comment)
        conn.commit()
    flash("Contrato cancelado por vencimiento y fondos devueltos a robótica.", "success")
    return redirect(url_for("interventor_dashboard"))


@app.route("/interventor/contracts/<int:contract_id>/final-review", methods=["POST"])
@role_required("interventor")
def final_review(contract_id: int):
    user = current_user()
    decision = request.form.get("decision")
    comment = request.form.get("comment", "").strip()
    if decision not in {"approved", "rejected", "correction_required"}:
        flash("Decisión inválida.", "danger")
        return redirect(url_for("interventor_dashboard"))
    with get_connection() as conn:
        contract = conn.execute("SELECT * FROM contracts WHERE id = ?", (contract_id,)).fetchone()
        if not contract:
            flash("Contrato no encontrado.", "danger")
            return redirect(url_for("interventor_dashboard"))
        latest_delivery = conn.execute(
            "SELECT * FROM deliveries WHERE contract_id = ? ORDER BY id DESC LIMIT 1",
            (contract_id,),
        ).fetchone()
        review_id = conn.execute(
            "INSERT INTO contract_reviews (contract_id, review_stage, interventor_user_id, decision, comment) VALUES (?, 'final_delivery', ?, ?, ?)",
            (contract_id, user["id"], decision, comment or "Sin comentario"),
        ).lastrowid
        if decision == "approved":
            dev_wallet = conn.execute(
                "SELECT * FROM wallets WHERE owner_type = 'team' AND owner_id = ?",
                (contract["development_team_id"],),
            ).fetchone()
            if dev_wallet and contract["reserved_amount"]:
                conn.execute(
                    "UPDATE wallets SET balance = balance + ? WHERE id = ?",
                    (contract["reserved_amount"], dev_wallet["id"]),
                )
                conn.execute(
                    "INSERT INTO transactions (from_wallet_id, to_wallet_id, amount, transaction_type, description, created_by_user_id, cycle_id) VALUES (NULL, ?, ?, 'contract_payment', ?, ?, ?)",
                    (dev_wallet["id"], contract["reserved_amount"], f"Pago liberado del contrato #{contract_id}", user["id"], contract['cycle_id']),
                )
            conn.execute(
                "UPDATE contracts SET payment_released = 1, reserved_amount = 0, status = 'closed', paused_by_deadline = 0, paused_at = NULL, pause_reason = NULL, closed_at = CURRENT_TIMESTAMP, last_interventor_user_id = ?, last_interventor_comment = ?, last_interventor_action = ?, last_interventor_signed_at = CURRENT_TIMESTAMP WHERE id = ?",
                (user['id'], comment or 'Sin comentario', f'final_{decision}', contract_id),
            )
            if latest_delivery:
                conn.execute("UPDATE deliveries SET status = 'validated' WHERE id = ?", (latest_delivery["id"],))
        elif decision == "correction_required":
            conn.execute(
                "UPDATE contracts SET status = 'correction_required', paused_by_deadline = 0, paused_at = NULL, last_interventor_user_id = ?, last_interventor_comment = ?, last_interventor_action = ?, last_interventor_signed_at = CURRENT_TIMESTAMP WHERE id = ?",
                (user['id'], comment or 'Sin comentario', f'final_{decision}', contract_id),
            )
            if latest_delivery:
                conn.execute("UPDATE deliveries SET status = 'correction_required' WHERE id = ?", (latest_delivery["id"],))
        else:
            robotics_wallet = conn.execute(
                "SELECT * FROM wallets WHERE owner_type = 'team' AND owner_id = ?",
                (contract["robotics_team_id"],),
            ).fetchone()
            if robotics_wallet and contract["reserved_amount"]:
                conn.execute(
                    "UPDATE wallets SET balance = balance + ? WHERE id = ?",
                    (contract["reserved_amount"], robotics_wallet["id"]),
                )
                conn.execute(
                    "INSERT INTO transactions (from_wallet_id, to_wallet_id, amount, transaction_type, description, created_by_user_id, cycle_id) VALUES (NULL, ?, ?, 'refund', ?, ?, ?)",
                    (robotics_wallet["id"], contract["reserved_amount"], f"Devolución por rechazo final del contrato #{contract_id}", user["id"], contract['cycle_id']),
                )
            conn.execute(
                "UPDATE contracts SET status = 'cancelled', reserved_amount = 0, paused_by_deadline = 0, paused_at = NULL, last_interventor_user_id = ?, last_interventor_comment = ?, last_interventor_action = ?, last_interventor_signed_at = CURRENT_TIMESTAMP WHERE id = ?",
                (user['id'], comment or 'Sin comentario', f'final_{decision}', contract_id),
            )
            if latest_delivery:
                conn.execute("UPDATE deliveries SET status = 'correction_required' WHERE id = ?", (latest_delivery["id"],))
        log_action(conn, user["id"], "final_review", "contract_review", review_id, f"contract={contract_id} decision={decision}")
        conn.commit()
    flash("Revisión final guardada.", "success")
    return redirect(url_for("interventor_dashboard"))


if __name__ == "__main__":
    app.run(debug=True)
