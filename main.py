import asyncio
from datetime import datetime
from io import BytesIO
import json
import logging
import os
from pathlib import Path
import posixpath
import re
import time
from urllib.parse import parse_qs, urlencode
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape as xml_escape
from zipfile import ZIP_DEFLATED, ZipFile

from src.mic import AudioIn
from src.session_recorder import SessionRecorder
from src.voice_client import ActionOut, REALTIME_DEVICE_ID, TextOut, VoiceClient

XLSX_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
XLSX_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
XLSX_NS = {"a": XLSX_MAIN_NS}
RESULT_COLUMNS = [
    "case_id",
    "step_id",
    "Type",
    "Language",
    "prompt_text",
    "expected_response",
    "expected_keywords",
    "forbidden_keywords",
    "actual_response",
    "Results",
    "date",
    "note",
    "RunTest",
    "log_session_id",
    "test results",
]
REQUIRED_TESTCASE_COLUMNS = [
    "case_id",
    "step_id",
    "Type",
    "Language",
    "prompt_text",
    "expected_response",
    "expected_keywords",
    "forbidden_keywords",
]
LOG_COLUMNS = [
    "session_id",
    "time",
    "case_id",
    "step",
    "event",
    "message",
    "prompt",
    "actual_response",
    "test_result",
]
MANUAL_CHAT_COLUMNS = [
    "session_id",
    "turn_id",
    "question_time",
    "response_time",
    "question",
    "response",
]
BASE_DIR = Path(__file__).resolve().parent
WEB_DIR = BASE_DIR / "web"
LOG_SESSION_DIR = BASE_DIR / "logs" / "sessions"
WEB_HOST = os.environ.get("VINFAST_WEB_HOST", "0.0.0.0")
WEB_PORT = int(os.environ.get("VINFAST_WEB_PORT", "8080"))
ROBOT_HOST = os.environ.get("VINFAST_ROBOT_HOST", "0.0.0.0")
ROBOT_PORT = int(os.environ.get("VINFAST_ROBOT_PORT", "9000"))
REALTIME_WAKE_INTERVAL_SECONDS = int(os.environ.get("VINFAST_REALTIME_WAKE_INTERVAL", "30"))
REALTIME_STALE_SECONDS = int(os.environ.get("VINFAST_REALTIME_STALE_SECONDS", "90"))
REALTIME_WAKE_TIMEOUT_SECONDS = int(os.environ.get("VINFAST_REALTIME_WAKE_TIMEOUT", "60"))
REALTIME_REQUEST_TIMEOUT_SECONDS = float(os.environ.get("VINFAST_REALTIME_REQUEST_TIMEOUT", "120"))
REALTIME_REQUEST_RETRIES = int(os.environ.get("VINFAST_REALTIME_REQUEST_RETRIES", "3"))
REALTIME_MODALITIES = [
    item.strip()
    for item in os.environ.get("VINFAST_REALTIME_MODALITIES", "text").split(",")
    if item.strip()
] or ["text"]
MIC_MAX_DURATION_SECONDS = int(os.environ.get("VINFAST_MIC_MAX_DURATION_SECONDS", "60"))
SERVER_RESTART_EXIT_CODE = int(os.environ.get("VINFAST_RESTART_EXIT_CODE", "42"))
HMS_ROOM_STATUS_URL = os.environ.get(
    "VINFAST_HMS_ROOM_STATUS_URL",
    "https://groot-stg.vizone.ai/robot-agent-gateway/api/v2/hms/rooms/status",
)
HMS_API_KEY = os.environ.get("VINFAST_HMS_API_KEY", "")
HMS_ORG_ID = os.environ.get("VINFAST_HMS_ORG_ID", "1dc9c659-8c61-0370-20b3-1234f6664721")
HMS_TIMEOUT_SECONDS = float(os.environ.get("VINFAST_HMS_TIMEOUT_SECONDS", "25"))


def _normalized_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").strip().lower())


def _record_get_loose(record: dict, names: list[str]) -> str:
    lookup = {
        _normalized_key(key): str(value).strip()
        for key, value in record.items()
        if value is not None and str(value).strip()
    }
    for name in names:
        value = lookup.get(_normalized_key(name))
        if value:
            return value
    return ""


def _column_number(cell_ref: str) -> int:
    letters = "".join(ch for ch in cell_ref if ch.isalpha())
    number = 0
    for char in letters.upper():
        number = number * 26 + ord(char) - 64
    return number


def _column_name(number: int) -> str:
    name = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        name = chr(65 + remainder) + name
    return name


def _shared_strings(zip_file: ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in zip_file.namelist():
        return []
    root = ET.fromstring(zip_file.read("xl/sharedStrings.xml"))
    strings = []
    for item in root.findall("a:si", XLSX_NS):
        strings.append("".join(text.text or "" for text in item.findall(".//a:t", XLSX_NS)))
    return strings


def _cell_text(cell: ET.Element, shared_strings: list[str]) -> str:
    cell_type = cell.attrib.get("t")
    if cell_type == "s":
        value = cell.find("a:v", XLSX_NS)
        if value is None or value.text is None:
            return ""
        index = int(value.text)
        return shared_strings[index] if 0 <= index < len(shared_strings) else ""
    if cell_type == "inlineStr":
        return "".join(text.text or "" for text in cell.findall(".//a:t", XLSX_NS))
    value = cell.find("a:v", XLSX_NS)
    return value.text if value is not None and value.text is not None else ""


def _first_sheet_path(zip_file: ZipFile) -> tuple[str, str]:
    workbook = ET.fromstring(zip_file.read("xl/workbook.xml"))
    sheet = workbook.find("a:sheets/a:sheet", XLSX_NS)
    if sheet is None:
        raise ValueError("Workbook không có sheet")

    sheet_name = sheet.attrib.get("name", "Sheet1")
    rel_id = sheet.attrib.get(f"{{{XLSX_REL_NS}}}id")
    rels = ET.fromstring(zip_file.read("xl/_rels/workbook.xml.rels"))
    target = None
    for rel in rels:
        if rel.attrib.get("Id") == rel_id:
            target = rel.attrib.get("Target")
            break
    if not target:
        raise ValueError("Không tìm thấy worksheet trong workbook")

    path = target.lstrip("/")
    if not path.startswith("xl/"):
        path = posixpath.normpath(posixpath.join("xl", path))
    return path, sheet_name


def read_xlsx_testcases(data: bytes) -> tuple[list[dict], str, dict]:
    with ZipFile(BytesIO(data)) as zip_file:
        sheet_path, sheet_name = _first_sheet_path(zip_file)
        shared_strings = _shared_strings(zip_file)
        sheet = ET.fromstring(zip_file.read(sheet_path))

    rows = []
    for row in sheet.findall("a:sheetData/a:row", XLSX_NS):
        values = {}
        for cell in row.findall("a:c", XLSX_NS):
            index = _column_number(cell.attrib.get("r", ""))
            values[index] = _cell_text(cell, shared_strings).strip()
        if values:
            rows.append(values)

    if not rows:
        return [], sheet_name, {
            "sheet": sheet_name,
            "headers": [],
            "required_columns": REQUIRED_TESTCASE_COLUMNS,
            "missing_columns": REQUIRED_TESTCASE_COLUMNS,
            "data_rows": 0,
            "imported_rows": 0,
            "skipped_rows": 0,
            "skipped_prompt_rows": [],
            "message": "Sheet không có dữ liệu.",
        }

    header_row = rows[0]
    headers = [header_row.get(index, "").strip() for index in range(1, max(header_row) + 1)]
    normalized_headers = {_normalized_key(header) for header in headers if header}
    missing_columns = [
        column for column in REQUIRED_TESTCASE_COLUMNS if _normalized_key(column) not in normalized_headers
    ]
    records = []
    skipped_prompt_rows = []
    for row_index, row in enumerate(rows[1:], start=2):
        record = {}
        for index, header in enumerate(headers, start=1):
            if header:
                record[header] = row.get(index, "")

        prompt = (
            _record_get_loose(record, ["prompt_text", "prompt", "question", "text"])
        )
        if not prompt:
            if any(str(value).strip() for value in record.values()):
                skipped_prompt_rows.append(row_index)
            continue

        case_id = _record_get_loose(record, ["case_id", "case id"]) or f"CASE_{row_index - 1:03d}"
        step_id = _record_get_loose(record, ["step_id", "step id"])
        record.update(
            {
                "name": f"{case_id}{f' / Step {step_id}' if step_id else ''}",
                "prompt": prompt,
                "prompt_text": prompt,
                "case_id": case_id,
                "step_id": step_id,
                "Type": _record_get_loose(record, ["Type", "type"]) or record.get("Type", ""),
                "Language": _record_get_loose(record, ["Language", "language"]) or record.get("Language", ""),
                "expected_response": _record_get_loose(record, ["expected_response", "expected response"]),
                "expected_keywords": _record_get_loose(record, ["expected_keywords", "expected keywords"]),
                "forbidden_keywords": _record_get_loose(record, ["forbidden_keywords", "forbidden keywords"]),
                "_row": row_index,
            }
        )
        records.append(record)

    diagnostics = {
        "sheet": sheet_name,
        "headers": headers,
        "required_columns": REQUIRED_TESTCASE_COLUMNS,
        "missing_columns": missing_columns,
        "data_rows": max(len(rows) - 1, 0),
        "imported_rows": len(records),
        "skipped_rows": len(skipped_prompt_rows),
        "skipped_prompt_rows": skipped_prompt_rows[:30],
        "message": (
            f"Đã đọc {len(records)} testcase từ {max(len(rows) - 1, 0)} dòng dữ liệu."
        ),
    }
    return records, sheet_name, diagnostics


def read_xlsx_records(data: bytes) -> tuple[list[dict], str]:
    with ZipFile(BytesIO(data)) as zip_file:
        sheet_path, sheet_name = _first_sheet_path(zip_file)
        shared_strings = _shared_strings(zip_file)
        sheet = ET.fromstring(zip_file.read(sheet_path))

    rows = []
    for row in sheet.findall("a:sheetData/a:row", XLSX_NS):
        values = {}
        for cell in row.findall("a:c", XLSX_NS):
            index = _column_number(cell.attrib.get("r", ""))
            values[index] = _cell_text(cell, shared_strings).strip()
        if values:
            rows.append(values)

    if not rows:
        return [], sheet_name

    header_row = rows[0]
    headers = [header_row.get(index, "").strip() for index in range(1, max(header_row) + 1)]
    records = []
    for row_index, row in enumerate(rows[1:], start=2):
        record = {}
        for index, header in enumerate(headers, start=1):
            if header:
                record[header] = row.get(index, "")
        if any(str(value).strip() for value in record.values()):
            record.setdefault("_row", row_index)
            records.append(record)

    return records, sheet_name


def _safe_filename(value: str) -> str:
    stem = re.sub(r"\.[^.]+$", "", value or "testcases")
    stem = re.sub(r"[^0-9A-Za-zÀ-ỹ_-]+", "_", stem, flags=re.UNICODE).strip("_")
    return stem[:80] or "testcases"


def result_filename(source_name: str) -> str:
    return f"result_{datetime.now():%d%m%Y}_{_safe_filename(source_name)}.xlsx"


def split_filename(source_name: str) -> str:
    return f"split_{datetime.now():%d%m%Y}_{_safe_filename(source_name)}.xlsx"


def manual_chat_filename(source_name: str) -> str:
    return f"manual_chat_{datetime.now():%d%m%Y}_{_safe_filename(source_name or 'Manual_chat')}.xlsx"


def _safe_session_id(value: str) -> str:
    session_id = re.sub(r"[^0-9A-Za-z_-]+", "_", value or "").strip("_")
    return session_id[:120]


def _log_session_paths(session_id: str) -> tuple[Path, Path]:
    safe_id = _safe_session_id(session_id)
    if not safe_id:
        raise ValueError("Thiếu session log")
    LOG_SESSION_DIR.mkdir(parents=True, exist_ok=True)
    return LOG_SESSION_DIR / f"{safe_id}.json", LOG_SESSION_DIR / f"{safe_id}.txt"


def _format_log_entry(entry: dict) -> str:
    fields = [
        f"[{entry.get('time', '')}]",
        f"session={entry.get('session_id', '')}" if entry.get("session_id") else "",
        f"case={entry.get('case_id', '')}" if entry.get("case_id") else "",
        f"step={entry.get('step', '')}" if entry.get("step") else "",
        f"event={entry.get('event', '')}" if entry.get("event") else "",
        str(entry.get("message") or ""),
        f"prompt={entry.get('prompt', '')}" if entry.get("prompt") else "",
        f"response={entry.get('actual_response', '')}" if entry.get("actual_response") else "",
        f"result={entry.get('test_result', '')}" if entry.get("test_result") else "",
    ]
    return " | ".join(field for field in fields if field)


def _read_log_session(session_id: str) -> dict:
    json_path, _ = _log_session_paths(session_id)
    if not json_path.exists():
        raise FileNotFoundError("Không tìm thấy session log")
    with json_path.open("r", encoding="utf-8") as file:
        payload = json.load(file)
    payload.setdefault("id", _safe_session_id(session_id))
    payload.setdefault("source_name", "")
    payload.setdefault("created_at", "")
    payload.setdefault("updated_at", "")
    payload.setdefault("logs", [])
    payload.setdefault("run_state", {})
    return payload


def _write_log_session(payload: dict) -> dict:
    session_id = _safe_session_id(str(payload.get("id") or ""))
    if not session_id:
        raise ValueError("Thiếu session log")
    now = datetime.now().isoformat(timespec="seconds")
    logs = payload.get("logs") if isinstance(payload.get("logs"), list) else []
    record = {
        "id": session_id,
        "source_name": str(payload.get("source_name") or ""),
        "created_at": str(payload.get("created_at") or now),
        "updated_at": str(payload.get("updated_at") or now),
        "logs": logs,
        "run_state": payload.get("run_state") if isinstance(payload.get("run_state"), dict) else {},
    }
    for entry in record["logs"]:
        if isinstance(entry, dict):
            entry.setdefault("session_id", session_id)

    json_path, txt_path = _log_session_paths(session_id)
    json_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    txt_path.write_text(
        "\n".join(_format_log_entry(entry) for entry in record["logs"] if isinstance(entry, dict)),
        encoding="utf-8",
    )
    return record


def _append_log_session(session_id: str, source_name: str, entry: dict) -> dict:
    try:
        payload = _read_log_session(session_id)
    except FileNotFoundError:
        payload = {
            "id": session_id,
            "source_name": source_name,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "logs": [],
        }
    payload["source_name"] = payload.get("source_name") or source_name
    payload["updated_at"] = datetime.now().isoformat(timespec="seconds")
    entry = dict(entry)
    entry["session_id"] = _safe_session_id(session_id)
    payload.setdefault("logs", []).append(entry)
    return _write_log_session(payload)


def _find_first_key(payload, names: set[str]):
    if isinstance(payload, dict):
        for key, value in payload.items():
            normalized = re.sub(r"[^a-z0-9]+", "", str(key).lower())
            if normalized in names and value not in (None, ""):
                return value
        for value in payload.values():
            found = _find_first_key(value, names)
            if found not in (None, ""):
                return found
    elif isinstance(payload, list):
        for item in payload:
            found = _find_first_key(item, names)
            if found not in (None, ""):
                return found
    return None


def _hms_room_status_request(confirmation_number: str, last_name: str, org_id: str) -> dict:
    confirmation_number = str(confirmation_number or "").strip()
    last_name = str(last_name or "").strip()
    org_id = str(org_id or HMS_ORG_ID).strip()
    if not confirmation_number or not last_name:
        raise ValueError("Thiếu confirmationNumber hoặc lastName")
    if not org_id:
        raise ValueError("Thiếu orgId")

    query = urlencode(
        {
            "confirmationNumber": confirmation_number,
            "lastName": last_name,
            "orgId": org_id,
        }
    )
    request = Request(
        f"{HMS_ROOM_STATUS_URL}?{query}",
        headers={
            "Accept": "application/json",
            "apikey": HMS_API_KEY,
        },
        method="GET",
    )

    try:
        with urlopen(request, timeout=HMS_TIMEOUT_SECONDS) as response:
            raw_body = response.read().decode("utf-8", errors="replace")
            http_status = response.status
    except HTTPError as exc:
        raw_body = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw_body) if raw_body else {}
        except json.JSONDecodeError:
            payload = {"raw": raw_body}
        return {
            "ok": False,
            "httpStatus": exc.code,
            "confirmationNumber": confirmation_number,
            "lastName": last_name,
            "orgId": org_id,
            "roomStatus": "",
            "error": payload.get("message") if isinstance(payload, dict) else str(exc),
            "payload": payload,
        }
    except URLError as exc:
        return {
            "ok": False,
            "httpStatus": 502,
            "confirmationNumber": confirmation_number,
            "lastName": last_name,
            "orgId": org_id,
            "roomStatus": "",
            "error": str(exc.reason),
            "payload": {},
        }

    try:
        payload = json.loads(raw_body) if raw_body else {}
    except json.JSONDecodeError:
        payload = {"raw": raw_body}

    room_status = _find_first_key(payload, {"roomstatus", "roomstate", "vpstatus"})
    return {
        "ok": 200 <= http_status < 300,
        "httpStatus": http_status,
        "confirmationNumber": confirmation_number,
        "lastName": last_name,
        "orgId": org_id,
        "roomStatus": "" if room_status is None else str(room_status),
        "payload": payload,
    }


def _list_log_sessions() -> list[dict]:
    LOG_SESSION_DIR.mkdir(parents=True, exist_ok=True)
    sessions = []
    for json_path in LOG_SESSION_DIR.glob("*.json"):
        try:
            payload = json.loads(json_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        logs = payload.get("logs") if isinstance(payload.get("logs"), list) else []
        sessions.append(
            {
                "id": payload.get("id") or json_path.stem,
                "source_name": payload.get("source_name") or "",
                "created_at": payload.get("created_at") or "",
                "updated_at": payload.get("updated_at") or "",
                "count": len(logs),
                "kind": "session",
                "run_state": payload.get("run_state") if isinstance(payload.get("run_state"), dict) else {},
            }
        )

    for txt_path in BASE_DIR.glob("logs_*.txt"):
        session_id = f"legacy_{txt_path.stem}"
        sessions.append(
            {
                "id": session_id,
                "source_name": txt_path.name,
                "created_at": datetime.fromtimestamp(txt_path.stat().st_mtime).isoformat(timespec="seconds"),
                "updated_at": datetime.fromtimestamp(txt_path.stat().st_mtime).isoformat(timespec="seconds"),
                "count": sum(1 for _ in txt_path.open("r", encoding="utf-8", errors="ignore")),
                "kind": "legacy_txt",
            }
        )

    return sorted(sessions, key=lambda item: item.get("updated_at") or "", reverse=True)


def _read_legacy_log_session(session_id: str) -> dict:
    name = session_id.removeprefix("legacy_")
    if not re.fullmatch(r"logs_[0-9A-Za-z._-]+", name):
        raise FileNotFoundError("Không tìm thấy file log")
    txt_path = BASE_DIR / f"{name}.txt"
    if not txt_path.exists():
        raise FileNotFoundError("Không tìm thấy file log")
    logs = []
    for index, line in enumerate(txt_path.read_text(encoding="utf-8", errors="ignore").splitlines(), start=1):
        logs.append(
            {
                "time": "",
                "case_id": "",
                "step": "",
                "event": "legacy",
                "message": line,
                "prompt": "",
                "actual_response": "",
                "test_result": "",
                "session_id": session_id,
                "line": index,
            }
        )
    updated = datetime.fromtimestamp(txt_path.stat().st_mtime).isoformat(timespec="seconds")
    return {
        "id": session_id,
        "source_name": txt_path.name,
        "created_at": updated,
        "updated_at": updated,
        "logs": logs,
        "kind": "legacy_txt",
    }


def _delete_log_session(session_id: str) -> None:
    if session_id.startswith("legacy_"):
        name = session_id.removeprefix("legacy_")
        if not re.fullmatch(r"logs_[0-9A-Za-z._-]+", name):
            raise FileNotFoundError("Không tìm thấy file log")
        txt_path = BASE_DIR / f"{name}.txt"
        if txt_path.exists():
            txt_path.unlink()
        return

    json_path, txt_path = _log_session_paths(session_id)
    for path in (json_path, txt_path):
        if path.exists():
            path.unlink()


def _xlsx_cell(ref: str, value) -> str:
    text = "" if value is None else str(value)
    return (
        f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">'
        f"{xml_escape(text)}"
        "</t></is></c>"
    )


def _result_status_and_note(record: dict) -> tuple[str, str]:
    raw_result = str(
        record.get("Results")
        or record.get("results")
        or record.get("test_results")
        or record.get("test results")
        or ""
    ).strip()
    note = str(record.get("note") or record.get("Note") or "").strip()

    if ":" in raw_result:
        status, detail = raw_result.split(":", 1)
        status = status.strip().upper()
        if status in {"PASS", "FAIL", "ERROR", "RUNNING", "NOT RUN", "SKIPPED"}:
            return status, note or detail.strip()

    normalized = raw_result.upper()
    for status in ("PASS", "FAIL", "ERROR", "RUNNING", "NOT RUN", "SKIPPED"):
        if normalized == status or normalized.startswith(f"{status} "):
            return status, note

    return raw_result, note


def build_result_xlsx(records: list[dict], logs: list[dict] | None = None) -> bytes:
    extra_columns = []
    excluded = {
        "name",
        "prompt",
        "_row",
        "status",
        "Results",
        "results",
        "test_results",
        "test results",
        "note",
        "Note",
        "log_session_id",
        "session_id",
        "run_index",
        "runIndex",
        "run_status",
        "exported_at",
        "export_snapshot",
        "source",
        "sourceName",
        "source_name",
        "Source",
        "Nguồn",
        "nguồn",
        "Nguon",
        "nguon",
        "original_index",
    }
    for record in records:
        for key in record:
            if key not in RESULT_COLUMNS and key not in excluded and not key.startswith("_"):
                extra_columns.append(key)
    columns = RESULT_COLUMNS + sorted(set(extra_columns))

    def worksheet_xml(sheet_columns: list[str], sheet_records: list[dict]) -> str:
        rows_xml = []
        header_cells = [
            _xlsx_cell(f"{_column_name(index)}1", column)
            for index, column in enumerate(sheet_columns, start=1)
        ]
        rows_xml.append(f'<row r="1">{"".join(header_cells)}</row>')

        for row_index, record in enumerate(sheet_records, start=2):
            cells = []
            for col_index, column in enumerate(sheet_columns, start=1):
                value = record.get(column, "")
                if column in {"Results", "note"}:
                    result_status, result_note = _result_status_and_note(record)
                    value = result_status if column == "Results" else result_note
                if column == "prompt_text" and not value:
                    value = record.get("prompt", "")
                cells.append(_xlsx_cell(f"{_column_name(col_index)}{row_index}", value))
            rows_xml.append(f'<row r="{row_index}">{"".join(cells)}</row>')

        last_ref = f"{_column_name(len(sheet_columns))}{max(len(sheet_records) + 1, 1)}"
        return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="{XLSX_MAIN_NS}" xmlns:r="{XLSX_REL_NS}">
  <dimension ref="A1:{last_ref}"/>
  <sheetViews><sheetView workbookViewId="0"/></sheetViews>
  <sheetFormatPr defaultRowHeight="15"/>
  <sheetData>{"".join(rows_xml)}</sheetData>
</worksheet>"""

    sheet_xml = worksheet_xml(columns, records)
    log_xml = worksheet_xml(LOG_COLUMNS, logs or [])
    has_logs = bool(logs)
    now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    sheet2_content_type = (
        '\n  <Override PartName="/xl/worksheets/sheet2.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        if has_logs
        else ""
    )
    content_types = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>{sheet2_content_type}
  <Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
  <Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
  <Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
</Types>"""
    rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>"""
    log_sheet = '<sheet name="Execution Logs" sheetId="2" r:id="rId2"/>' if has_logs else ""
    workbook = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="{XLSX_MAIN_NS}" xmlns:r="{XLSX_REL_NS}">
  <sheets><sheet name="Test Results" sheetId="1" r:id="rId1"/>{log_sheet}</sheets>
</workbook>"""
    log_rel = (
        '\n  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet2.xml"/>'
        if has_logs
        else ""
    )
    style_rel_id = "rId3" if has_logs else "rId2"
    workbook_rels = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>{log_rel}
  <Relationship Id="{style_rel_id}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>"""
    styles = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="{XLSX_MAIN_NS}">
  <fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>
  <fills count="1"><fill><patternFill patternType="none"/></fill></fills>
  <borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>
  <cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
  <cellXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/></cellXfs>
</styleSheet>"""
    core = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <dc:creator>VinFast Voice Test Console</dc:creator>
  <cp:lastModifiedBy>VinFast Voice Test Console</cp:lastModifiedBy>
  <dcterms:created xsi:type="dcterms:W3CDTF">{now}</dcterms:created>
  <dcterms:modified xsi:type="dcterms:W3CDTF">{now}</dcterms:modified>
</cp:coreProperties>"""
    app = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">
  <Application>VinFast Voice Test Console</Application>
</Properties>"""

    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as zip_file:
        zip_file.writestr("[Content_Types].xml", content_types)
        zip_file.writestr("_rels/.rels", rels)
        zip_file.writestr("xl/workbook.xml", workbook)
        zip_file.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        zip_file.writestr("xl/worksheets/sheet1.xml", sheet_xml)
        if has_logs:
            zip_file.writestr("xl/worksheets/sheet2.xml", log_xml)
        zip_file.writestr("xl/styles.xml", styles)
        zip_file.writestr("docProps/core.xml", core)
        zip_file.writestr("docProps/app.xml", app)
    return output.getvalue()


def build_plain_xlsx(
    records: list[dict],
    sheet_name: str = "Testcases",
    columns: list[str] | None = None,
) -> bytes:
    excluded = {
        "name",
        "prompt",
        "status",
        "_row",
        "date",
        "actual_response",
        "test_results",
        "test results",
        "RunTest",
        "log_session_id",
        "session_id",
        "run_index",
        "run_status",
        "exported_at",
        "export_snapshot",
        "worker_id",
        "source",
        "sourceName",
        "source_name",
        "Source",
        "Nguồn",
        "nguồn",
        "Nguon",
        "nguon",
    }
    if columns is None:
        columns = []
        for record in records:
            for key in record:
                if key not in excluded and not str(key).startswith("_") and key not in columns:
                    columns.append(key)
    if not columns:
        columns = ["prompt_text"]

    rows_xml = []
    header_cells = [
        _xlsx_cell(f"{_column_name(index)}1", column)
        for index, column in enumerate(columns, start=1)
    ]
    rows_xml.append(f'<row r="1">{"".join(header_cells)}</row>')
    for row_index, record in enumerate(records, start=2):
        cells = []
        for col_index, column in enumerate(columns, start=1):
            value = record.get(column, "")
            if column == "prompt_text" and not value:
                value = record.get("prompt", "")
            cells.append(_xlsx_cell(f"{_column_name(col_index)}{row_index}", value))
        rows_xml.append(f'<row r="{row_index}">{"".join(cells)}</row>')

    safe_sheet_name = xml_escape((sheet_name or "Testcases")[:31])
    last_ref = f"{_column_name(len(columns))}{max(len(records) + 1, 1)}"
    sheet_xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="{XLSX_MAIN_NS}" xmlns:r="{XLSX_REL_NS}">
  <dimension ref="A1:{last_ref}"/>
  <sheetViews><sheetView workbookViewId="0"/></sheetViews>
  <sheetFormatPr defaultRowHeight="15"/>
  <sheetData>{"".join(rows_xml)}</sheetData>
</worksheet>"""
    now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    content_types = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
  <Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
  <Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
</Types>"""
    rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>"""
    workbook = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="{XLSX_MAIN_NS}" xmlns:r="{XLSX_REL_NS}">
  <sheets><sheet name="{safe_sheet_name}" sheetId="1" r:id="rId1"/></sheets>
</workbook>"""
    workbook_rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>"""
    styles = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="{XLSX_MAIN_NS}">
  <fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>
  <fills count="1"><fill><patternFill patternType="none"/></fill></fills>
  <borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>
  <cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
  <cellXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/></cellXfs>
</styleSheet>"""
    core = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <dc:creator>VinFast Voice Test Console</dc:creator>
  <cp:lastModifiedBy>VinFast Voice Test Console</cp:lastModifiedBy>
  <dcterms:created xsi:type="dcterms:W3CDTF">{now}</dcterms:created>
  <dcterms:modified xsi:type="dcterms:W3CDTF">{now}</dcterms:modified>
</cp:coreProperties>"""
    app = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">
  <Application>VinFast Voice Test Console</Application>
</Properties>"""
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as zip_file:
        zip_file.writestr("[Content_Types].xml", content_types)
        zip_file.writestr("_rels/.rels", rels)
        zip_file.writestr("xl/workbook.xml", workbook)
        zip_file.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        zip_file.writestr("xl/worksheets/sheet1.xml", sheet_xml)
        zip_file.writestr("xl/styles.xml", styles)
        zip_file.writestr("docProps/core.xml", core)
        zip_file.writestr("docProps/app.xml", app)
    return output.getvalue()


def manual_chat_rows_from_logs(logs: list[dict], session_id: str = "") -> list[dict]:
    rows = []
    pending: dict | None = None
    turn_number = 1
    for entry in logs:
        if not isinstance(entry, dict):
            continue
        event = str(entry.get("event") or "")
        prompt = str(entry.get("prompt") or "").strip()
        actual_response = str(entry.get("actual_response") or "").strip()
        entry_session_id = str(entry.get("session_id") or session_id or "").strip()
        if event == "chat_send":
            pending = {
                "session_id": entry_session_id,
                "turn_id": f"CHAT_{turn_number:03d}",
                "question_time": str(entry.get("time") or ""),
                "response_time": "",
                "question": prompt or str(entry.get("message") or "").removeprefix("CHAT SEND:").strip(),
                "response": "",
            }
            continue
        if event not in {"chat_response", "chat_error"}:
            continue
        row = pending or {
            "session_id": entry_session_id,
            "turn_id": f"CHAT_{turn_number:03d}",
            "question_time": "",
            "response_time": "",
            "question": prompt,
            "response": "",
        }
        if prompt and not row.get("question"):
            row["question"] = prompt
        row["session_id"] = row.get("session_id") or entry_session_id
        row["response_time"] = str(entry.get("time") or "")
        row["response"] = actual_response or str(entry.get("message") or "").strip()
        rows.append({column: row.get(column, "") for column in MANUAL_CHAT_COLUMNS})
        turn_number += 1
        pending = None
    return rows


def build_manual_chat_xlsx(logs: list[dict], session_id: str = "") -> bytes:
    rows = manual_chat_rows_from_logs(logs, session_id)
    return build_plain_xlsx(rows, "Manual Chat", MANUAL_CHAT_COLUMNS)


WEB_UI_HTML = r"""<!doctype html>
<html lang="vi">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>VinFast Voice Test</title>
  <style>
    :root {
      color-scheme: light;
      --vf-navy: #071d35;
      --vf-navy-2: #0b2b4c;
      --vf-blue: #1f6feb;
      --vf-red: #c41230;
      --ink: #132033;
      --muted: #66758b;
      --line: #d6dfeb;
      --panel: #ffffff;
      --page: #eef3f8;
      --ok: #007a5a;
      --warn: #b56a00;
      --shadow: 0 18px 46px rgba(7, 29, 53, 0.14);
    }

    * { box-sizing: border-box; }

    body {
      margin: 0;
      min-height: 100vh;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: linear-gradient(180deg, #eaf1f8 0%, var(--page) 220px);
      color: var(--ink);
      overflow-x: hidden;
    }

	    .topbar {
	      position: sticky;
	      top: 0;
	      z-index: 20;
	      height: 68px;
	      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 0 28px;
      background: var(--vf-navy);
      border-bottom: 1px solid rgba(255, 255, 255, 0.1);
      color: #fff;
      box-shadow: 0 10px 28px rgba(7, 29, 53, 0.18);
    }

    .brand {
      display: flex;
      align-items: center;
      gap: 14px;
      min-width: 0;
    }

    .brand-home {
      display: flex;
      align-items: center;
      gap: 14px;
      min-width: 0;
      color: inherit;
      text-decoration: none;
    }

	    .mark {
	      width: 66px;
	      height: 52px;
	      display: block;
	      flex: 0 0 auto;
	      padding: 8px 12px;
	      border-radius: 8px;
	      background: #fff;
	      box-shadow: inset 0 0 0 1px rgba(7, 29, 53, 0.05);
	    }

    .mark img {
      width: 100%;
      height: 100%;
      display: block;
      object-fit: contain;
    }

    .brand-title {
      display: block;
      font-size: 20px;
      font-weight: 750;
      line-height: 1.1;
    }

    .brand-subtitle {
      display: block;
      color: rgba(255, 255, 255, 0.72);
      font-size: 13px;
      margin-top: 3px;
    }

    .status {
      display: flex;
      align-items: center;
      gap: 8px;
      min-height: 32px;
      padding: 0 12px;
      border: 1px solid rgba(255, 255, 255, 0.16);
      border-radius: 999px;
      background: rgba(255, 255, 255, 0.08);
      font-size: 13px;
      color: rgba(255, 255, 255, 0.76);
    }

    .top-actions {
      display: flex;
      align-items: center;
      gap: 14px;
      flex-wrap: wrap;
      justify-content: flex-end;
    }

    .nav {
      display: flex;
      align-items: center;
      gap: 6px;
      padding: 4px;
      border: 1px solid rgba(255, 255, 255, 0.16);
      border-radius: 8px;
      background: rgba(255, 255, 255, 0.08);
    }

    .nav-button {
      min-height: 30px;
      padding: 0 12px;
      border-radius: 6px;
      background: transparent;
      color: rgba(255, 255, 255, 0.74);
      font-size: 13px;
      font-weight: 750;
    }

    .nav-button.active {
      color: var(--vf-navy);
      background: #fff;
    }

    .stop-button {
      min-height: 38px;
      padding: 0 12px;
      border: 1px solid #ffccd4;
      background: #fff7f8;
      color: var(--vf-red);
      font-size: 14px;
    }

    .stop-button:disabled {
      color: #97a0ad;
      border-color: var(--line);
      background: #f5f7fa;
      cursor: not-allowed;
    }

    .dot {
      width: 9px;
      height: 9px;
      border-radius: 999px;
      background: var(--warn);
    }

    .dot.ready { background: var(--ok); }

    main {
      width: min(1760px, calc(100vw - 40px));
      margin: 20px auto;
      display: grid;
      grid-template-columns: minmax(720px, 1fr) minmax(380px, 420px);
      gap: 20px;
      align-items: start;
    }

    .workspace,
    .side {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: var(--shadow);
    }

    .workspace {
      min-height: calc(100vh - 104px);
      overflow: hidden;
    }

    .view {
      display: none;
      min-height: calc(100vh - 104px);
    }

    .view.active {
      display: block;
    }

	    .chat-view.active {
	      display: grid;
	      grid-template-rows: 1fr auto;
	      height: calc(100vh - 104px);
	      min-height: 560px;
	      position: relative;
	    }

	    .conversation {
	      min-height: 0;
	      padding: 22px 22px 32px;
	      overflow: auto;
	      overscroll-behavior: contain;
	      scroll-behavior: smooth;
	      scrollbar-gutter: stable;
	      background:
	        linear-gradient(#fff 30%, rgba(255, 255, 255, 0)),
	        linear-gradient(rgba(255, 255, 255, 0), #fff 70%) 0 100%,
	        radial-gradient(farthest-side at 50% 0, rgba(7, 29, 53, 0.12), transparent),
	        radial-gradient(farthest-side at 50% 100%, rgba(7, 29, 53, 0.1), transparent) 0 100%;
	      background-repeat: no-repeat;
	      background-size: 100% 28px, 100% 28px, 100% 10px, 100% 10px;
	      background-attachment: local, local, scroll, scroll;
	    }

    .empty {
      min-height: 190px;
      display: grid;
      place-items: center;
      text-align: center;
      color: var(--muted);
      border: 1px dashed var(--line);
      border-radius: 8px;
      background: #fbfcfe;
    }

    .empty strong {
      display: block;
      color: var(--ink);
      font-size: 22px;
      margin-bottom: 8px;
    }

	    .turn {
	      display: grid;
	      gap: 10px;
	      margin-bottom: 16px;
	    }

	    .bubble {
	      max-width: min(920px, 88%);
	      padding: 14px 16px;
	      border: 1px solid var(--line);
	      border-radius: 8px;
	      line-height: 1.55;
	      white-space: pre-wrap;
	      overflow-wrap: anywhere;
	      box-shadow: 0 1px 2px rgba(7, 29, 53, 0.04);
	    }

	    .bubble.user {
	      justify-self: end;
	      max-width: min(820px, 82%);
	      background: #fff7f8;
	      border-color: #efbec8;
	    }

    .bubble.robot {
      justify-self: start;
      background: #f8fafc;
    }

    .label {
      display: block;
      margin-bottom: 6px;
      font-size: 12px;
      font-weight: 700;
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: 0;
    }

		    .composer {
		      padding: 14px 16px 16px;
		      border-top: 1px solid var(--line);
		      background: #ffffff;
		      display: grid;
		      grid-template-columns: minmax(0, 1fr) 72px 108px;
		      gap: 12px;
		      align-items: end;
		      box-shadow: 0 -12px 28px rgba(7, 29, 53, 0.06);
		    }

	    textarea {
	      width: 100%;
	      min-height: 54px;
      max-height: 140px;
      resize: vertical;
      border: 1px solid var(--line);
	      border-radius: 8px;
	      padding: 14px;
	      font: inherit;
	      line-height: 1.4;
	      outline: none;
	    }

	    #question {
	      min-height: 52px;
	      max-height: 168px;
	      resize: none;
	      overflow-y: hidden;
	      padding-right: 18px;
	    }

		    .send-button {
		      align-self: stretch;
		      min-height: 52px;
		      padding: 0 18px;
		    }

		    .voice-button {
		      align-self: stretch;
		      min-height: 52px;
		      padding: 0 12px;
		      border-color: #b9c7d8;
		      background: #fff;
		      color: var(--vf-navy);
		      box-shadow: 0 5px 14px rgba(7, 29, 53, 0.08);
		    }

		    .voice-button.listening {
		      border-color: #efbec8;
		      background: #fff7f8;
		      color: var(--vf-red);
		      box-shadow: 0 0 0 3px rgba(196, 18, 48, 0.1);
		    }

        .sleep-button,
        .wake-button {
          align-self: stretch;
          min-height: 52px;
          padding: 0 12px;
          white-space: nowrap;
        }

        .sleep-button {
          border-color: #efbec8;
          background: #fff7f8;
          color: var(--vf-red);
        }

        .wake-button {
          border-color: #9ed8c8;
          background: #eefbf7;
          color: var(--ok);
        }

	    .scroll-latest {
	      position: absolute;
	      right: 24px;
	      bottom: 96px;
	      z-index: 2;
	      min-height: 36px;
	      padding: 0 14px;
	      border-color: rgba(7, 29, 53, 0.14);
	      background: #ffffff;
	      color: var(--vf-navy);
	      font-size: 13px;
	      box-shadow: 0 10px 26px rgba(7, 29, 53, 0.16);
	    }

	    .scroll-latest[hidden] {
	      display: none;
	    }

	    #testcaseImport {
	      min-height: 112px;
	      max-height: 220px;
	    }

    textarea:focus {
      border-color: var(--vf-red);
      box-shadow: 0 0 0 3px rgba(196, 18, 48, 0.12);
    }

    button {
      border: 1px solid rgba(7, 29, 53, 0.12);
      border-radius: 8px;
      background: linear-gradient(180deg, #10395f, var(--vf-navy));
      color: #fff;
      font: inherit;
      font-weight: 750;
      cursor: pointer;
      min-height: 54px;
      box-shadow: 0 6px 14px rgba(7, 29, 53, 0.14);
      transition: transform 150ms ease, box-shadow 150ms ease, border-color 150ms ease, background 150ms ease, color 150ms ease;
      transform: translateZ(0);
      will-change: transform;
    }

    button:hover:not(:disabled) {
      transform: translateY(-1px);
      box-shadow: 0 10px 22px rgba(7, 29, 53, 0.18);
      border-color: rgba(31, 111, 235, 0.34);
    }

    button:active:not(:disabled) {
      transform: translateY(0);
      box-shadow: 0 4px 10px rgba(7, 29, 53, 0.14);
    }

    button:focus-visible,
    .file-button:focus-visible,
    input:focus-visible,
    textarea:focus-visible {
      outline: 3px solid rgba(31, 111, 235, 0.16);
      outline-offset: 2px;
    }

    button:disabled {
      cursor: not-allowed;
      background: #eff4f9;
      color: #8a98aa;
      box-shadow: none;
      border-color: #d4deea;
      transform: none;
    }

    .side {
      padding: 20px;
      align-self: start;
      position: sticky;
      top: 88px;
      max-height: calc(100vh - 104px);
      min-width: 0;
      overflow: auto;
      display: flex;
      flex-direction: column;
      scrollbar-width: thin;
    }

    .side h2 {
      margin: 0 0 14px;
      font-size: 18px;
    }

    .quick-list {
      display: grid;
      gap: 10px;
    }

    .quick {
      width: 100%;
      min-height: 42px;
      padding: 10px 12px;
      text-align: left;
      border: 1px solid var(--line);
      color: var(--ink);
      background: #fbfcfe;
      font-weight: 600;
    }

    .quick:hover {
      border-color: #aeb7c6;
      background: #f2f5f8;
    }

    .meta {
      margin-top: 18px;
      padding-top: 16px;
      border-top: 1px solid var(--line);
      color: var(--muted);
      font-size: 13px;
      line-height: 1.5;
    }

	    .tool-view {
	      padding: 20px;
	      overflow: auto;
	      contain: layout paint;
	    }

	    .view-header {
	      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      align-items: flex-start;
      justify-content: space-between;
      gap: 16px;
	      margin-bottom: 14px;
	    }

    .view-header h1 {
      margin: 0;
      font-size: 22px;
      line-height: 1.2;
    }

    .view-header p {
      margin: 6px 0 0;
      color: var(--muted);
      line-height: 1.45;
    }

    .toolbar {
      display: flex;
      align-items: center;
      gap: 10px;
      flex-wrap: wrap;
      justify-content: flex-end;
    }

    .toolbar button {
      min-height: 40px;
      min-width: 92px;
      padding-inline: 14px;
    }

    .search-input,
    .limit-input {
      min-height: 38px;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 0 12px;
      font: inherit;
      font-size: 14px;
      color: var(--ink);
      background: #fff;
    }

    .search-input {
      width: min(280px, 100%);
    }

    .limit-input {
      width: 82px;
    }

    .secondary,
    .danger {
      min-height: 40px;
      padding: 0 14px;
      border: 1px solid #b9c7d8;
      background: #fff;
      color: var(--vf-navy);
      font-size: 14px;
      box-shadow: 0 5px 14px rgba(7, 29, 53, 0.08);
    }

    .secondary:disabled,
    .danger:disabled,
    .stop-button:disabled {
      border-color: #d4deea;
      background: #f3f7fb;
      color: #8b98aa;
      box-shadow: none;
    }

    .danger {
      border-color: #f0b9c2;
      color: var(--vf-red);
      background: #fff9fa;
    }

	    .import-grid {
	      display: grid;
	      grid-template-columns: minmax(0, 1fr) 260px;
	      gap: 16px;
	      align-items: start;
	    }

    .import-help,
    .result-summary {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fbfcfe;
      padding: 14px;
      color: var(--muted);
      font-size: 13px;
      line-height: 1.55;
    }

    .import-help strong,
    .result-summary strong {
      display: block;
      color: var(--ink);
      margin-bottom: 8px;
    }

    .file-row {
      display: flex;
      gap: 10px;
      align-items: center;
      flex-wrap: wrap;
      margin-top: 10px;
    }

    .file-row > button,
    .file-row .file-button,
    .file-row .search-input,
    .file-row .file-name {
      min-height: 42px;
    }

    #importTestcases {
      min-height: 42px;
      padding: 0 18px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
    }

    input[type="file"] {
      max-width: 100%;
      font: inherit;
      font-size: 13px;
      color: var(--muted);
    }

    .file-input {
      position: absolute;
      width: 1px;
      height: 1px;
      padding: 0;
      overflow: hidden;
      clip: rect(0, 0, 0, 0);
      white-space: nowrap;
      border: 0;
    }

    .file-button {
      min-height: 38px;
      padding: 0 12px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      border: 1px solid #b9c7d8;
      border-radius: 8px;
      background: #fff;
      color: var(--vf-navy);
      font-size: 14px;
      font-weight: 750;
      cursor: pointer;
    }

    .file-name {
      min-height: 38px;
      display: inline-flex;
      align-items: center;
      color: var(--muted);
      font-size: 13px;
      max-width: min(280px, 100%);
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }

	    .testcase-list,
	    .result-list {
	      display: grid;
	      gap: 10px;
	      margin-top: 18px;
	      contain: content;
	    }

    .testcase-item,
    .result-item {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
      padding: 14px;
      display: grid;
      gap: 10px;
    }

    .testcase-title,
    .result-title {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: flex-start;
      font-weight: 750;
    }

    .testcase-prompt,
    .result-output {
      color: var(--ink);
      line-height: 1.5;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
    }

    .item-actions {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
    }

    .item-actions button {
      min-height: 34px;
      padding: 0 10px;
      font-size: 13px;
    }

    .table-wrap {
      overflow: auto;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
      contain: content;
      transform: translateZ(0);
    }

	    .testcase-list .table-wrap {
	      max-height: min(60vh, 640px);
	    }

    .data-table {
      width: 100%;
      min-width: 1040px;
      border-collapse: collapse;
      table-layout: fixed;
      font-size: 13px;
    }

    .data-table th,
    .data-table td {
      padding: 11px 12px;
      border-bottom: 1px solid var(--line);
      text-align: left;
      vertical-align: top;
      line-height: 1.45;
    }

    .data-table th {
      position: sticky;
      top: 0;
      z-index: 1;
      background: #f8fafc;
      color: var(--vf-navy);
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0;
    }

    .data-table tr:last-child td {
      border-bottom: 0;
    }

    .col-case { width: 132px; }
    .col-type { width: 160px; }
    .col-status { width: 144px; }
    .col-date { width: 152px; }
    .col-actions { width: 176px; }

    .result-table .col-case { width: 140px; }
    .result-table .col-prompt { width: 24%; }
    .result-table .col-response { width: 32%; }
    .result-table .col-result { width: 220px; }
    .result-table .col-date { width: 150px; }
    .result-table .col-actions { width: 120px; }

    .cell-main {
      font-weight: 750;
      color: var(--ink);
      min-width: 0;
      overflow-wrap: anywhere;
    }

    .cell-sub {
      margin-top: 3px;
      color: var(--muted);
      font-size: 12px;
    }

    .prompt-cell,
    .response-cell {
      display: -webkit-box;
      -webkit-line-clamp: 3;
      -webkit-box-orient: vertical;
      overflow: hidden;
      overflow-wrap: anywhere;
    }

    .result-table .prompt-cell,
    .result-table .response-cell,
    .result-table .result-detail {
      display: block;
      max-height: 120px;
      overflow: auto;
      padding-right: 4px;
      scrollbar-width: thin;
      white-space: pre-wrap;
    }

    .testcase-table .prompt-cell,
    .keyword-cell {
      display: block;
      max-height: 84px;
      overflow: auto;
      padding-right: 4px;
      scrollbar-width: thin;
    }

    .status-pill {
      display: inline-flex;
      align-items: center;
      min-height: 26px;
      padding: 0 9px;
      border-radius: 999px;
      border: 1px solid var(--line);
      background: #f8fafc;
      color: var(--muted);
      font-weight: 750;
      font-size: 12px;
    }

    .status-pill.pass {
      color: var(--ok);
      border-color: #9ed8c8;
      background: #eefbf7;
    }

    .status-pill.fail,
    .status-pill.error {
      color: var(--vf-red);
      border-color: #efbec8;
      background: #fff7f8;
    }

    .status-pill.running {
      color: #925b00;
      border-color: #f0cf92;
      background: #fff8e8;
    }

    .status-pill.skipped {
      color: #4b5563;
      border-color: #cbd5e1;
      background: #f8fafc;
    }

    .status-pill.chat {
      color: var(--vf-blue);
      border-color: #b8cdf1;
      background: #f3f7ff;
    }

    .result-cell {
      display: grid;
      gap: 6px;
      min-width: 0;
    }

    .result-detail {
      color: var(--muted);
      font-size: 12px;
      line-height: 1.45;
      overflow-wrap: anywhere;
    }

    .result-detail.fail {
      color: #a5122c;
    }

    .summary-grid,
    .metric-grid {
      display: grid;
      gap: 10px;
    }

    .summary-grid {
      grid-template-columns: repeat(4, minmax(0, 1fr));
      margin-bottom: 12px;
    }

    .metric {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fbfcfe;
      padding: 12px;
      contain: content;
      min-width: 0;
      overflow: hidden;
    }

    .metric-label {
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
      text-transform: uppercase;
    }

    .metric-value {
      margin-top: 6px;
      color: var(--ink);
      font-size: 24px;
      font-weight: 800;
      line-height: 1;
      overflow-wrap: anywhere;
    }

	    .run-progress {
	      display: grid;
	      grid-template-columns: 180px minmax(0, 1fr) 170px;
      gap: 14px;
      align-items: center;
      margin-bottom: 12px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
      padding: 14px;
	      transition: border-color 180ms ease, box-shadow 180ms ease;
	    }

    .run-progress.complete {
      border-color: #9ed8c8;
      background: linear-gradient(180deg, #ffffff 0%, #f4fbf8 100%);
      box-shadow: 0 0 0 3px rgba(0, 122, 90, 0.07);
    }

    .run-progress.paused {
      border-color: #f0cf92;
      background: linear-gradient(180deg, #ffffff 0%, #fffaf0 100%);
      box-shadow: 0 0 0 3px rgba(181, 106, 0, 0.08);
    }

    .run-setup {
      display: grid;
      grid-template-columns: minmax(280px, 1fr) minmax(230px, 0.75fr) minmax(300px, 1fr);
      gap: 14px;
      align-items: stretch;
      margin-bottom: 12px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fbfcfe;
      padding: 12px;
    }

    .advanced-panel {
      margin-bottom: 12px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fbfcfe;
      overflow: hidden;
    }

    .advanced-panel summary {
      min-height: 44px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 0 14px;
      cursor: pointer;
      color: var(--vf-navy);
      font-size: 13px;
      font-weight: 850;
      list-style: none;
      border-bottom: 1px solid transparent;
    }

    .advanced-panel summary::-webkit-details-marker {
      display: none;
    }

    .advanced-panel[open] summary {
      border-bottom-color: var(--line);
      background: #fff;
    }

    .advanced-panel .run-setup {
      margin: 0;
      border: 0;
      border-radius: 0;
      background: transparent;
      padding: 14px;
    }

    .summary-action {
      min-height: 32px;
      padding: 0 10px;
      border: 1px solid #b9c7d8;
      background: #fff;
      color: var(--vf-navy);
      box-shadow: none;
      font-size: 13px;
    }

    .batch-grid {
      display: grid;
      grid-template-columns: minmax(0, 1fr) 300px;
      gap: 14px;
      align-items: start;
    }

	    .hms-grid {
	      display: grid;
	      grid-template-columns: minmax(0, 1fr) 340px;
	      gap: 14px;
	      align-items: start;
	    }

    .tool-panel {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fbfcfe;
      padding: 14px;
      display: grid;
      gap: 12px;
      min-width: 0;
    }

    .tool-panel h2 {
      margin: 0;
      color: var(--ink);
      font-size: 16px;
      line-height: 1.25;
    }

    .tool-panel p {
      margin: 0;
      color: var(--muted);
      font-size: 13px;
      line-height: 1.5;
    }

    .tool-panel .setup-row {
      align-items: end;
    }

    .range-input {
      min-height: 38px;
      min-width: min(320px, 100%);
      flex: 1 1 260px;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 0 10px;
      font: inherit;
      color: var(--ink);
      background: #fff;
    }

    .split-preview {
      min-height: 42px;
      color: var(--muted);
      font-size: 13px;
      line-height: 1.45;
      overflow-wrap: anywhere;
    }

    .split-stat-grid {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 10px;
    }

    .setup-group {
      display: grid;
      gap: 7px;
      min-width: 0;
      align-content: stretch;
      padding: 12px;
      border: 1px solid #dbe4ef;
      border-radius: 8px;
      background: #fff;
    }

    .setup-label {
      color: var(--muted);
      font-size: 12px;
      font-weight: 800;
      text-transform: uppercase;
    }

    .setup-row {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      align-items: center;
      min-width: 0;
    }

    .setup-input {
      min-height: 40px;
      width: 112px;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 0 10px;
      font: inherit;
      color: var(--ink);
      background: #fff;
    }

    .hms-input {
      min-height: 40px;
      width: min(260px, 100%);
      flex: 1 1 180px;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 0 10px;
      font: inherit;
      color: var(--ink);
      background: #fff;
    }

    .hms-org-input {
      flex-basis: 300px;
    }

	    .hms-data-input {
	      min-height: 138px;
	      max-height: 260px;
	    }

    .hms-result-list {
      display: grid;
      gap: 12px;
      margin-top: 14px;
    }

    .hms-table {
      min-width: 980px;
    }

    .hms-table .col-case { width: 132px; }
    .hms-table .col-confirmation { width: 190px; }
    .hms-table .col-last-name { width: 160px; }
    .hms-table .col-room-status { width: 160px; }
    .hms-table .col-http { width: 92px; }
    .hms-table .col-actions { width: 170px; }

    .json-preview {
      max-height: 124px;
      overflow: auto;
      margin: 0;
      padding: 8px 10px;
      border: 1px solid #dce4ee;
      border-radius: 8px;
      background: #f8fafc;
      color: var(--muted);
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 12px;
      line-height: 1.45;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
    }

    .setup-row button {
      min-height: 40px;
      white-space: nowrap;
    }

    .thread-input {
      width: 132px;
    }

    .thread-lock-state {
      min-height: 40px;
      display: flex;
      align-items: center;
      gap: 8px;
      color: var(--ink);
      font-weight: 750;
    }

    .thread-chip {
      display: inline-flex;
      align-items: center;
      min-height: 28px;
      padding: 0 9px;
      border: 1px solid #9ed8c8;
      border-radius: 999px;
      color: var(--ok);
      background: #eefbf7;
      font-size: 12px;
      font-weight: 850;
    }

    .setup-actions {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      justify-content: flex-end;
    }

    .setup-actions button {
      min-height: 38px;
      padding: 0 12px;
      font-size: 14px;
    }

    .setup-hint {
      color: var(--muted);
      font-size: 12px;
      line-height: 1.35;
      overflow-wrap: anywhere;
    }

    .run-progress.running {
      border-color: #efbec8;
      box-shadow: 0 0 0 3px rgba(196, 18, 48, 0.08);
    }

    .progress-percent {
      font-size: 30px;
      font-weight: 850;
      line-height: 1;
      color: var(--vf-red);
    }

    .progress-status {
      margin-top: 5px;
      color: var(--muted);
      font-size: 13px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }

    .progress-track {
      height: 12px;
      overflow: hidden;
      border-radius: 999px;
      background: #edf1f6;
      border: 1px solid #dce2ea;
    }

    .progress-fill {
      height: 100%;
      width: 0%;
      background: var(--vf-blue);
      transition: width 180ms ease;
      transform: translateZ(0);
      will-change: width;
    }

    .run-progress.running .progress-fill {
      background: linear-gradient(90deg, var(--vf-navy), var(--vf-blue), var(--vf-navy));
      background-size: 220% 100%;
      animation: progressFlow 1.1s linear infinite;
    }

    .run-progress.complete .progress-fill {
      background: linear-gradient(90deg, #008766, var(--ok));
    }

    .run-progress.complete .progress-percent {
      color: var(--ok);
    }

    .run-progress.paused .progress-fill {
      background: var(--warn);
    }

    .run-progress.paused .progress-percent {
      color: var(--warn);
    }

    .progress-detail {
      text-align: right;
      color: var(--muted);
      font-size: 13px;
      line-height: 1.5;
    }

    .log-panel {
      margin-top: 18px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
      overflow: hidden;
    }

    .log-layout {
      display: grid;
      grid-template-columns: 280px minmax(0, 1fr);
      gap: 14px;
      margin-top: 18px;
    }

    .log-session-list {
      max-height: 380px;
      overflow: auto;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
    }

    .log-session {
      width: 100%;
      display: grid;
      gap: 5px;
      padding: 11px 12px;
      border: 0;
      border-bottom: 1px solid var(--line);
      border-radius: 0;
      background: #fff;
      color: var(--ink);
      text-align: left;
      font-size: 13px;
      cursor: pointer;
    }

    .log-session:last-child {
      border-bottom: 0;
    }

    .log-session.active {
      background: #fff7f8;
      box-shadow: inset 3px 0 0 var(--vf-red);
    }

    .log-session-name {
      font-weight: 800;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }

    .log-session-meta {
      color: var(--muted);
      font-size: 12px;
      line-height: 1.35;
    }

    .log-header {
      min-height: 44px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 0 14px;
      border-bottom: 1px solid var(--line);
      background: #f8fafc;
      font-weight: 800;
    }

    .log-list {
      max-height: 320px;
      overflow: auto;
      display: grid;
    }

    .log-row {
      display: grid;
      grid-template-columns: 150px 110px 92px minmax(0, 1fr);
      gap: 12px;
      padding: 10px 14px;
      border-bottom: 1px solid var(--line);
      font-size: 13px;
      line-height: 1.45;
    }

    .log-row:last-child {
      border-bottom: 0;
    }

    .log-time,
    .log-case,
    .log-step {
      color: var(--muted);
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }

    .log-message {
      white-space: pre-wrap;
      overflow-wrap: anywhere;
    }

    .pager {
      min-height: 46px;
      margin-top: 10px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      flex-wrap: wrap;
      color: var(--muted);
      font-size: 13px;
    }

    .pager-actions {
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
    }

    .pager button {
      min-height: 34px;
      padding: 0 11px;
      font-size: 13px;
    }

    .busy-overlay {
      position: fixed;
      inset: 0;
      z-index: 50;
      display: none;
      place-items: center;
      background: rgba(7, 29, 53, 0.62);
      backdrop-filter: blur(5px);
    }

    .busy-overlay.active {
      display: grid;
    }

    .busy-card {
      width: min(420px, calc(100vw - 40px));
      border: 1px solid rgba(255, 255, 255, 0.16);
      border-radius: 8px;
      background: #ffffff;
      padding: 24px;
      box-shadow: 0 30px 70px rgba(7, 29, 53, 0.34);
      transform: translateZ(0);
      will-change: transform, opacity;
    }

    .busy-visual {
      height: 72px;
      display: grid;
      place-items: center;
      margin-bottom: 12px;
    }

    .busy-ring {
      width: 56px;
      height: 56px;
      border-radius: 50%;
      border: 5px solid #dbe6f2;
      border-top-color: var(--vf-blue);
      border-right-color: var(--vf-navy);
      animation: spinGpu 880ms linear infinite;
      transform: translateZ(0);
      will-change: transform;
    }

    .busy-title {
      color: var(--vf-navy);
      font-size: 18px;
      font-weight: 850;
      text-align: center;
    }

    .busy-detail {
      margin-top: 8px;
      color: var(--muted);
      text-align: center;
      font-size: 13px;
      line-height: 1.5;
    }

    .busy-progress {
      height: 8px;
      margin-top: 16px;
      border: 1px solid #dce4ee;
      border-radius: 999px;
      overflow: hidden;
      background: #eef3f8;
    }

    .busy-progress-fill {
      height: 100%;
      width: 0%;
      background: linear-gradient(90deg, var(--vf-blue), var(--ok));
      transition: width 180ms ease;
    }

    .fallback-note {
      margin-top: 10px;
      padding: 10px 12px;
      border: 1px solid #d7e2ef;
      border-radius: 8px;
      background: #f6f9fc;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.45;
    }

    .fade-in {
      animation: fadeIn 180ms ease-out both;
    }

    @keyframes spinGpu {
      to { transform: rotate(360deg) translateZ(0); }
    }

    @keyframes progressFlow {
      from { background-position: 0% 50%; }
      to { background-position: 220% 50%; }
    }

    @keyframes fadeIn {
      from { opacity: 0; transform: translateY(4px); }
      to { opacity: 1; transform: translateY(0); }
    }

    .side-actions {
      display: grid;
      gap: 10px;
      margin-top: 16px;
    }

    .side-actions button {
      min-height: 42px;
      width: 100%;
    }

    .side .meta {
      margin-top: auto;
    }

    .settings-modal {
      position: fixed;
      inset: 0;
      z-index: 60;
      display: none;
      place-items: center;
      background: rgba(7, 29, 53, 0.56);
      backdrop-filter: blur(4px);
      padding: 24px;
    }

    .settings-modal.active {
      display: grid;
    }

    .settings-dialog {
      width: min(620px, calc(100vw - 32px));
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
      box-shadow: 0 30px 70px rgba(7, 29, 53, 0.34);
      overflow: hidden;
    }

    .settings-header {
      min-height: 56px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 0 18px;
      border-bottom: 1px solid var(--line);
      background: #f8fafc;
    }

    .settings-header h2 {
      margin: 0;
      font-size: 17px;
    }

    .settings-body {
      display: grid;
      gap: 14px;
      padding: 18px;
    }

    .settings-footer {
      display: flex;
      justify-content: flex-end;
      gap: 10px;
      padding: 14px 18px;
      border-top: 1px solid var(--line);
      background: #fbfcfe;
    }

    .diagnostic-text {
      white-space: pre-wrap;
      word-break: break-word;
      max-height: min(50vh, 420px);
      overflow: auto;
      margin: 0;
      padding: 12px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #0f172a;
      color: #e5eefc;
      font: 12px/1.55 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    }

    .diagnostic-summary {
      margin: 0;
      color: var(--text);
      line-height: 1.5;
    }

    .settings-close {
      min-width: 70px;
      min-height: 36px;
      padding: 0 12px;
    }

    .stop-button:disabled {
      color: #97a0ad;
      border-color: var(--line);
      background: #f5f7fa;
      cursor: not-allowed;
    }

    .muted {
      color: var(--muted);
      font-weight: 500;
    }

    .side .metric-grid {
      min-width: 0;
    }

    .side .metric {
      padding: 10px 12px;
    }

    .side .cell-main {
      display: block;
      max-width: 100%;
      line-height: 1.35;
      white-space: normal;
    }

    @media (max-width: 1100px) {
      main {
        grid-template-columns: 1fr;
      }

      .side {
        position: static;
      }
    }

    @media (max-width: 860px) {
      .topbar {
        height: auto;
        min-height: 64px;
        align-items: flex-start;
        flex-direction: column;
        gap: 12px;
        padding: 16px;
      }

      main {
        grid-template-columns: 1fr;
        margin-top: 16px;
        width: min(100vw - 24px, 100%);
      }

	      .workspace {
	        min-height: calc(100vh - 300px);
	      }

	      .chat-view.active {
	        height: calc(100vh - 248px);
	        min-height: 520px;
	      }

	      .composer {
		        grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
		        padding: 12px;
		      }

	      .composer textarea {
	        grid-column: 1 / -1;
	      }

		      .send-button {
		        min-height: 46px;
		      }

	      .voice-button {
	        min-height: 46px;
	      }

	      .scroll-latest {
	        right: 16px;
	        bottom: 128px;
	      }

	      .bubble {
	        max-width: 100%;
	      }

	      .import-grid {
	        grid-template-columns: 1fr;
	      }

	      .view-header {
	        grid-template-columns: 1fr;
	      }

      .toolbar {
        justify-content: flex-start;
      }

      .summary-grid {
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }

      .run-progress,
      .run-setup,
      .batch-grid,
      .hms-grid,
      .log-layout,
      .pager,
      .log-row {
        grid-template-columns: 1fr;
      }

      .progress-detail {
        text-align: left;
      }
    }
  </style>
</head>
<body>
  <header class="topbar">
    <div class="brand">
      <a class="brand-home" href="#chat" aria-label="Trang chủ VinFast Voice Test">
        <span class="mark" aria-label="VinFast">
	          <img src="/image/vinfast-logo.svg" alt="VinFast">
        </span>
        <span>
          <span class="brand-title">VF-Tool Communication VSF&amp;VMO</span>
          <span class="brand-subtitle">Robotics Teams</span>
        </span>
      </a>
    </div>
    <div class="top-actions">
      <nav class="nav" aria-label="Điều hướng">
        <button class="nav-button active" type="button" data-view="chat">Chat</button>
        <button class="nav-button" type="button" data-view="testcases">Testcases</button>
        <button class="nav-button" type="button" data-view="batch">Split TCs</button>
        <button class="nav-button" type="button" data-view="hms">HMS</button>
        <button class="nav-button" type="button" data-view="results">Kết quả</button>
        <button class="nav-button" type="button" data-view="logs">Logs</button>
      </nav>
      <div class="status"><span id="dot" class="dot"></span><span id="statusText">Đang kiểm tra kết nối</span></div>
    </div>
  </header>

  <main>
    <section class="workspace">
      <section id="chatView" class="view chat-view active">
	        <div id="conversation" class="conversation">
	          <div class="empty">
	            <div>
	              <strong>VinFast test console</strong>
	              <span>Nhập câu hỏi và xem phản hồi realtime tại đây.</span>
	            </div>
	          </div>
	        </div>
	        <button id="jumpToLatest" class="scroll-latest" type="button" hidden>Mới nhất</button>

		        <form id="form" class="composer">
		          <textarea id="question" autocomplete="off" placeholder="Nhập câu hỏi test..." required></textarea>
		          <button id="voiceInput" class="voice-button" type="button" aria-pressed="false">Mic</button>
              <button id="endRobotSection" class="sleep-button" type="button">End section</button>
              <button id="wakeRobot" class="wake-button" type="button" hidden>Wake robot</button>
		          <button id="send" class="send-button" type="submit">Gửi</button>
		        </form>
	      </section>

		      <section id="testcasesView" class="view tool-view">
        <div class="view-header">
          <div>
            <h1>Quản lý testcases</h1>
            <p>Import, kiểm thử và xuất kết quả theo mẫu Excel testcase.</p>
          </div>
          <div class="toolbar">
            <button id="runAll" class="secondary" type="button">Chạy tất cả</button>
            <button id="resumeRun" class="secondary" type="button">Chạy tiếp</button>
            <button id="reviewFailed" class="secondary" type="button">Rà soát fail</button>
            <button id="stopRun" class="stop-button" type="button" disabled>Dừng</button>
            <button id="exportResults" class="secondary" type="button">Xuất Excel</button>
            <button id="clearTestcases" class="danger" type="button">Xóa testcases</button>
          </div>
        </div>

        <div class="summary-grid" aria-label="Tổng quan testcase">
          <div class="metric"><div class="metric-label">Tổng testcase</div><div id="metricTotal" class="metric-value">0</div></div>
          <div class="metric"><div class="metric-label">Pass</div><div id="metricPass" class="metric-value">0</div></div>
          <div class="metric"><div class="metric-label">Fail</div><div id="metricFail" class="metric-value">0</div></div>
          <div class="metric"><div class="metric-label">Chưa chạy</div><div id="metricPending" class="metric-value">0</div></div>
        </div>

        <div class="run-progress" aria-label="Tiến trình chạy testcase">
          <div>
            <div id="progressPercent" class="progress-percent">0%</div>
            <div id="progressStatus" class="progress-status">Chưa chạy</div>
          </div>
          <div class="progress-track"><div id="progressFill" class="progress-fill"></div></div>
          <div id="progressDetail" class="progress-detail">0 / 0 testcase</div>
        </div>

        <details class="advanced-panel" id="advancedRunPanel">
          <summary>
            <span>Advanced setup</span>
            <button id="openRunSettings" class="summary-action" type="button">Cài đặt</button>
          </summary>
          <div class="run-setup" aria-label="Cấu hình chạy testcase">
            <div class="setup-group">
              <div class="setup-label">Khoảng case</div>
              <div class="setup-row">
                <input id="runFromCase" class="setup-input" type="number" min="1" placeholder="From">
                <input id="runToCase" class="setup-input" type="number" min="1" placeholder="To">
                <button id="runRange" class="secondary" type="button">Chạy</button>
              </div>
            </div>
            <div class="setup-group">
              <div class="setup-label">Luồng đã khóa</div>
              <div class="thread-lock-state">
                <span id="threadLockState">1 luồng</span>
                <span class="thread-chip">LOCKED</span>
              </div>
              <div class="setup-hint">Đổi số luồng trong Cài đặt rồi bấm khóa để áp dụng.</div>
            </div>
            <div class="setup-group">
              <div class="setup-label">Estimate</div>
              <div id="runEstimate" class="setup-hint">Chưa có dữ liệu tốc độ chạy.</div>
            </div>
          </div>
        </details>

        <div class="import-grid">
          <div>
            <textarea id="testcaseImport" placeholder="Nhập mỗi prompt một dòng, CSV hoặc JSON array." rows="6"></textarea>
            <div class="file-row">
              <button id="importTestcases" type="button">Import</button>
              <label class="file-button" for="testcaseFile">Chọn file</label>
              <input id="testcaseFile" class="file-input" type="file" accept=".xlsx,.txt,.json,.csv,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,application/json,text/plain">
              <span id="selectedFileName" class="file-name">Chưa chọn file</span>
              <input id="testcaseSearch" class="search-input" type="search" placeholder="Tìm case_id, prompt, keyword">
            </div>
          </div>

          <div class="import-help">
            <strong>Mẫu Excel hỗ trợ</strong>
            case_id, step_id, Type, Language, prompt_text, expected_response, expected_keywords, forbidden_keywords. Kết quả ghi vào date, actual_response, test results.
          </div>
        </div>

        <div id="testcaseList" class="testcase-list"></div>
        <div id="testcasePager" class="pager"></div>
      </section>

      <section id="batchView" class="view tool-view">
        <div class="view-header">
          <div>
            <h1>Split testcases</h1>
            <p>Tách testcase từ file đã import thành workbook sạch, không ghi thêm cột result/log/runtime.</p>
          </div>
        </div>

        <div class="batch-grid">
          <div class="tool-panel">
            <h2>Tạo file split</h2>
            <p>Nhập range theo số thứ tự trong file: 1-500 hoặc 1-100,250-300. File xuất chỉ giữ dữ liệu testcase gốc.</p>
            <div class="setup-row">
              <input id="splitRangeInput" class="range-input" type="text" placeholder="Ví dụ: 1-500, 800-1000">
              <button id="previewSplit" class="secondary" type="button">Xem</button>
              <button id="exportSplit" type="button">Export split</button>
            </div>
            <div id="splitPreview" class="split-preview">Chưa chọn range split.</div>
          </div>

          <div class="tool-panel">
            <h2>Tổng quan split</h2>
            <div class="split-stat-grid">
              <div class="metric"><div class="metric-label">Import</div><div id="splitMetricTotal" class="metric-value">0</div></div>
              <div class="metric"><div class="metric-label">Chọn</div><div id="splitMetricSelected" class="metric-value">0</div></div>
              <div class="metric"><div class="metric-label">Lỗi range</div><div id="splitMetricInvalid" class="metric-value">0</div></div>
            </div>
            <div class="setup-hint">Split không thay đổi testcase đang import và không ghi trạng thái chạy vào file mới.</div>
          </div>
        </div>
      </section>

	      <section id="hmsView" class="view tool-view">
        <div class="view-header">
          <div>
            <h1>HMS room status</h1>
            <p>Check roomStatus VP qua HMS bằng confirmationNumber và lastName từ data import.</p>
          </div>
          <div class="toolbar">
            <button id="loadHmsFromTestcases" class="secondary" type="button">Lấy từ testcases</button>
            <button id="checkAllHms" type="button">Check tất cả</button>
            <button id="clearHmsRows" class="danger" type="button">Xóa HMS</button>
          </div>
        </div>

        <div class="summary-grid" aria-label="Tổng quan HMS">
          <div class="metric"><div class="metric-label">Tổng HMS</div><div id="hmsMetricTotal" class="metric-value">0</div></div>
          <div class="metric"><div class="metric-label">Đã check</div><div id="hmsMetricChecked" class="metric-value">0</div></div>
          <div class="metric"><div class="metric-label">Có roomStatus</div><div id="hmsMetricOk" class="metric-value">0</div></div>
          <div class="metric"><div class="metric-label">Lỗi</div><div id="hmsMetricError" class="metric-value">0</div></div>
        </div>

        <div class="hms-grid">
          <div class="tool-panel">
            <h2>Check nhanh</h2>
            <div class="setup-row">
              <input id="hmsConfirmationNumber" class="hms-input" type="text" autocomplete="off" placeholder="confirmationNumber">
              <input id="hmsLastName" class="hms-input" type="text" autocomplete="off" placeholder="lastName">
              <button id="checkSingleHms" type="button">Check HMS</button>
            </div>
            <div class="setup-row">
              <input id="hmsOrgId" class="hms-input hms-org-input" type="text" autocomplete="off" value="1dc9c659-8c61-0370-20b3-1234f6664721" placeholder="orgId">
            </div>
            <div id="hmsSummary" class="result-summary"><strong>Tổng quan</strong>Chưa có data HMS.</div>
          </div>

          <div class="tool-panel">
            <h2>Nhập data HMS</h2>
            <p>Dán mỗi dòng dạng confirmationNumber,lastName hoặc JSON array có 2 field này.</p>
            <textarea id="hmsDataInput" class="hms-data-input" placeholder="VPLNTR7097432,Taktarova" rows="6"></textarea>
            <div class="setup-actions">
              <label class="file-button" for="hmsFile">Chọn file</label>
              <input id="hmsFile" class="file-input" type="file" accept=".xlsx,.json,.csv,.txt,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,application/json,text/plain">
              <button id="importHmsRows" class="secondary" type="button">Import HMS</button>
            </div>
            <div id="selectedHmsFileName" class="setup-hint">Chưa chọn file HMS.</div>
          </div>
        </div>

        <div id="hmsResultList" class="hms-result-list"></div>
      </section>

      <section id="resultsView" class="view tool-view">
        <div class="view-header">
          <div>
            <h1>Kết quả chạy testcase</h1>
            <p>Theo dõi phản hồi thực tế và trạng thái pass/fail.</p>
          </div>
          <div class="toolbar">
            <button id="reviewFailedFromResults" class="secondary" type="button">Rà soát fail</button>
            <button id="exportResultsFromResults" class="secondary" type="button">Xuất Excel</button>
            <button id="clearResults" class="danger" type="button">Xóa kết quả</button>
          </div>
        </div>
        <div id="resultSummary" class="result-summary"><strong>Tổng quan</strong>Chưa có kết quả.</div>
        <div id="resultList" class="result-list"></div>
      </section>

      <section id="logsView" class="view tool-view">
        <div class="view-header">
          <div>
            <h1>Execution logs</h1>
            <p>Log gửi prompt, nhận response, kết quả chấm và thao tác export.</p>
          </div>
          <div class="toolbar">
            <input id="logSearch" class="search-input" type="search" placeholder="Tìm case_id, event, message">
            <button id="showTestcaseLogs" class="secondary" type="button">Log TCs</button>
            <button id="showChatLogs" class="secondary" type="button">Log Chat</button>
            <button id="refreshLogSessions" class="secondary" type="button">Làm mới</button>
            <button id="exportChatXlsx" class="secondary" type="button">Xuất chat .xlsx</button>
            <button id="exportLogsTxt" class="secondary" type="button">Xuất log .txt</button>
          </div>
        </div>
        <div id="logSummary" class="result-summary"><strong>Tổng quan</strong>Chưa có log.</div>
        <div class="log-layout">
          <div>
            <div class="log-header">
              <span>Log sessions</span>
            </div>
            <div id="logSessionList" class="log-session-list"></div>
          </div>
          <div class="log-panel">
            <div class="log-header">
              <span id="logScopeTitle">Tất cả logs</span>
              <div class="toolbar">
                <button id="resetLogFilter" class="secondary" type="button">Tất cả</button>
                <button id="clearLogs" class="danger" type="button">Xóa log</button>
              </div>
            </div>
            <div id="logList" class="log-list"></div>
          </div>
        </div>
      </section>
    </section>

    <aside class="side">
      <h2>Bảng điều khiển</h2>
      <div class="metric-grid">
        <div class="metric"><div class="metric-label">File import</div><div id="metricFile" class="cell-main">Chưa có</div></div>
        <div class="metric"><div class="metric-label">Lần chạy gần nhất</div><div id="metricLastRun" class="cell-main">Chưa chạy</div></div>
      </div>
      <div class="side-actions">
        <button id="runAllSide" type="button">Chạy tất cả</button>
        <button id="resumeRunSide" class="secondary" type="button">Chạy tiếp</button>
        <button id="stopRunSide" class="stop-button" type="button" disabled>Dừng</button>
        <button id="exportResultsSide" class="secondary" type="button">Xuất result .xlsx</button>
        <button id="exportChatSide" class="secondary" type="button">Xuất chat .xlsx</button>
        <button id="exportLogsSide" class="secondary" type="button">Xuất log .txt</button>
      </div>
      <div class="meta">
        Endpoint: <strong>/ask</strong><br>
        Runtime: Python asyncio<br>
        TCP robot: port <strong>9000</strong>
      </div>
    </aside>
  </main>

  <div id="busyOverlay" class="busy-overlay" aria-live="polite" aria-hidden="true">
    <div class="busy-card">
      <div class="busy-visual"><div class="busy-ring"></div></div>
      <div id="busyTitle" class="busy-title">Đang xử lý</div>
      <div id="busyDetail" class="busy-detail">Vui lòng chờ trong giây lát.</div>
      <div class="busy-progress"><div id="busyProgressFill" class="busy-progress-fill"></div></div>
      <div class="fallback-note">Nếu file rất lớn, hệ thống sẽ chia nhỏ việc render để giao diện vẫn phản hồi. Có thể đổi port hoặc restart server nếu kết nối realtime đang bận.</div>
    </div>
  </div>

  <div id="runSettingsModal" class="settings-modal" aria-hidden="true">
    <div class="settings-dialog" role="dialog" aria-modal="true" aria-labelledby="runSettingsTitle">
      <div class="settings-header">
        <h2 id="runSettingsTitle">Cài đặt chạy testcase</h2>
        <button id="closeRunSettings" class="secondary settings-close" type="button">Đóng</button>
      </div>
      <div class="settings-body">
        <div class="setup-group">
          <div class="setup-label">Số luồng chạy</div>
          <div class="setup-row">
            <input id="runThreadCount" class="setup-input thread-input" type="number" min="1" max="16" value="1">
            <button id="lockThreadCount" type="button">Khóa luồng</button>
          </div>
          <div id="threadSettingsHint" class="setup-hint">Số luồng đang khóa: 1. Chỉ case có response hợp lệ mới được chấm PASS/FAIL.</div>
        </div>
        <div class="setup-group">
          <div class="setup-label">Fallback</div>
          <div class="setup-hint">Runner retry case không có response tối đa 3 lần; vẫn không có thì SKIPPED.</div>
        </div>
      </div>
      <div class="settings-footer">
        <button id="cancelRunSettings" class="secondary" type="button">Đóng</button>
      </div>
    </div>
  </div>

  <div id="diagnosticModal" class="settings-modal" aria-hidden="true">
    <div class="settings-dialog" role="dialog" aria-modal="true" aria-labelledby="diagnosticTitle">
      <div class="settings-header">
        <h2 id="diagnosticTitle">Diagnostic</h2>
        <button id="closeDiagnostic" class="secondary settings-close" type="button">Đóng</button>
      </div>
      <div class="settings-body">
        <p id="diagnosticSummary" class="diagnostic-summary">Chưa có diagnostic.</p>
        <pre id="diagnosticText" class="diagnostic-text"></pre>
      </div>
      <div class="settings-footer">
        <button id="openDiagnosticLogs" class="secondary" type="button">Mở Logs</button>
        <button id="cancelDiagnostic" type="button">Đóng</button>
      </div>
    </div>
  </div>

  <script src="/web/testcase_evaluator.js"></script>
  <script>
	    const form = document.getElementById('form');
		    const input = document.getElementById('question');
		    const send = document.getElementById('send');
		    const voiceInput = document.getElementById('voiceInput');
        const endRobotSection = document.getElementById('endRobotSection');
        const wakeRobot = document.getElementById('wakeRobot');
		    const conversation = document.getElementById('conversation');
	    const jumpToLatest = document.getElementById('jumpToLatest');
	    const dot = document.getElementById('dot');
    const statusText = document.getElementById('statusText');
    const testcaseImport = document.getElementById('testcaseImport');
    const testcaseFile = document.getElementById('testcaseFile');
    const selectedFileName = document.getElementById('selectedFileName');
    const testcaseList = document.getElementById('testcaseList');
    const testcasePager = document.getElementById('testcasePager');
    const resultList = document.getElementById('resultList');
    const resultSummary = document.getElementById('resultSummary');
    const testcaseSearch = document.getElementById('testcaseSearch');
    const stopRun = document.getElementById('stopRun');
    const stopRunSide = document.getElementById('stopRunSide');
    const metricTotal = document.getElementById('metricTotal');
    const metricPass = document.getElementById('metricPass');
    const metricFail = document.getElementById('metricFail');
    const metricPending = document.getElementById('metricPending');
    const metricFile = document.getElementById('metricFile');
    const metricLastRun = document.getElementById('metricLastRun');
    const progressPercent = document.getElementById('progressPercent');
    const progressStatus = document.getElementById('progressStatus');
    const progressFill = document.getElementById('progressFill');
    const progressDetail = document.getElementById('progressDetail');
    const runFromCase = document.getElementById('runFromCase');
    const runToCase = document.getElementById('runToCase');
    const runThreadCount = document.getElementById('runThreadCount');
    const threadLockState = document.getElementById('threadLockState');
    const threadSettingsHint = document.getElementById('threadSettingsHint');
    const runSettingsModal = document.getElementById('runSettingsModal');
    const runEstimate = document.getElementById('runEstimate');
    const splitRangeInput = document.getElementById('splitRangeInput');
    const splitPreview = document.getElementById('splitPreview');
    const splitMetricTotal = document.getElementById('splitMetricTotal');
    const splitMetricSelected = document.getElementById('splitMetricSelected');
    const splitMetricInvalid = document.getElementById('splitMetricInvalid');
    const hmsConfirmationNumber = document.getElementById('hmsConfirmationNumber');
    const hmsLastName = document.getElementById('hmsLastName');
    const hmsOrgId = document.getElementById('hmsOrgId');
    const hmsDataInput = document.getElementById('hmsDataInput');
    const hmsFile = document.getElementById('hmsFile');
    const selectedHmsFileName = document.getElementById('selectedHmsFileName');
    const hmsResultList = document.getElementById('hmsResultList');
    const hmsSummary = document.getElementById('hmsSummary');
    const hmsMetricTotal = document.getElementById('hmsMetricTotal');
    const hmsMetricChecked = document.getElementById('hmsMetricChecked');
    const hmsMetricOk = document.getElementById('hmsMetricOk');
    const hmsMetricError = document.getElementById('hmsMetricError');
    const logList = document.getElementById('logList');
    const logSearch = document.getElementById('logSearch');
    const logSummary = document.getElementById('logSummary');
    const logScopeTitle = document.getElementById('logScopeTitle');
    const logSessionList = document.getElementById('logSessionList');
    const busyOverlay = document.getElementById('busyOverlay');
    const busyTitle = document.getElementById('busyTitle');
    const busyDetail = document.getElementById('busyDetail');
    const busyProgressFill = document.getElementById('busyProgressFill');
    const diagnosticModal = document.getElementById('diagnosticModal');
    const diagnosticTitle = document.getElementById('diagnosticTitle');
    const diagnosticSummary = document.getElementById('diagnosticSummary');
    const diagnosticText = document.getElementById('diagnosticText');
    const navButtons = Array.from(document.querySelectorAll('.nav-button'));
    const TESTCASE_PAGE_SIZE = 100;
    const RESULT_RENDER_LIMIT = 300;
    const MAX_THREAD_COUNT = 16;
    const views = {
      chat: document.getElementById('chatView'),
      testcases: document.getElementById('testcasesView'),
      batch: document.getElementById('batchView'),
      hms: document.getElementById('hmsView'),
      results: document.getElementById('resultsView'),
      logs: document.getElementById('logsView')
    };
    let hasTurns = false;
    let testcases = JSON.parse(localStorage.getItem('vf-testcases') || '[]');
    let results = JSON.parse(localStorage.getItem('vf-results') || '[]');
    let testLogs = JSON.parse(localStorage.getItem('vf-test-logs') || '[]');
    let hmsRows = JSON.parse(localStorage.getItem('vf-hms-rows') || '[]');
    let logSessions = [];
    let activeLogSessionId = localStorage.getItem('vf-log-session-id') || '';
    let activeLogSessionName = localStorage.getItem('vf-log-session-name') || '';
    let chatLogSessionId = localStorage.getItem('vf-chat-log-session-id') || '';
    let testcaseLogSessionId = localStorage.getItem('vf-testcase-log-session-id') || '';
    let lastImportName = localStorage.getItem('vf-source-file') || '';
    let lastRunTime = localStorage.getItem('vf-last-run') || '';
    let lockedThreadCount = Number(localStorage.getItem('vf-locked-thread-count') || 1);
    let runState = {running: false, stopRequested: false, stopReason: '', total: 0, completed: 0, current: '', startedAt: 0};
    const activeRunAbortControllers = new Set();
    let runSessionState = {};
	    let activeLogCase = '';
	    let testcasePage = 1;
		    let speechRecognition = null;
		    let voiceListening = false;
		    let voiceBaseText = '';
		    let voiceFinalText = '';
		    let voiceHadResult = false;
		    let voiceMode = '';
		    let micPollTimer = null;
		    let micTranscriptCount = 0;
        let robotSleeping = false;
        let chatRequestInFlight = false;

	    function setStatus(text, ready) {
	      statusText.textContent = text;
	      dot.classList.toggle('ready', ready);
	    }

      function setRobotSleepingState(sleeping) {
        robotSleeping = Boolean(sleeping);
        endRobotSection.hidden = robotSleeping;
        wakeRobot.hidden = !robotSleeping;
        send.disabled = robotSleeping || chatRequestInFlight;
        voiceInput.disabled = robotSleeping || chatRequestInFlight;
        input.placeholder = robotSleeping ? 'Robot đang ngủ. Nhấn Wake robot để tiếp tục...' : 'Nhập câu hỏi test...';
        if (robotSleeping && voiceListening) {
          stopVoiceInput().catch((error) => setStatus(`Không dừng được mic: ${error.message}`, false));
        }
      }

      async function setRobotPowerState(action) {
        const sleeping = action === 'sleep';
        const button = sleeping ? endRobotSection : wakeRobot;
        button.disabled = true;
        setStatus(sleeping ? 'Đang đưa robot vào trạng thái ngủ' : 'Đang wake robot', false);
        try {
          const response = await fetch(`/robot/${action}`, {method: 'POST', cache: 'no-store'});
          const payload = await response.json().catch(() => ({}));
          if (!response.ok || payload.ok === false) {
            throw new Error(payload.error || payload.message || `Robot ${action} failed`);
          }
          setRobotSleepingState(Boolean(payload.sleeping));
          appendLog(sleeping ? 'chat_section_end' : 'chat_wake_robot', {
            message: payload.message || (sleeping ? 'Robot entered sleep mode' : 'Robot woke up')
          });
          setStatus(payload.message || (sleeping ? 'Robot đang ngủ' : 'Robot đã sẵn sàng'), !sleeping);
        } catch (error) {
          setStatus(`${sleeping ? 'End section' : 'Wake robot'} lỗi: ${error.message}`, false);
        } finally {
          button.disabled = false;
        }
      }

    function formatDiagnosticDetails(details = {}) {
      if (!details || typeof details !== 'object') return String(details || '');
      const lines = [];
      Object.entries(details).forEach(([key, value]) => {
        if (value === undefined || value === null || value === '') return;
        if (Array.isArray(value)) {
          lines.push(`${key}: ${value.length ? value.join(', ') : '(empty)'}`);
        } else if (typeof value === 'object') {
          lines.push(`${key}: ${JSON.stringify(value, null, 2)}`);
        } else {
          lines.push(`${key}: ${value}`);
        }
      });
      return lines.join('\n');
    }

    function showDiagnostic(title, summary, details = {}, options = {}) {
      const detailText = typeof details === 'string' ? details : formatDiagnosticDetails(details);
      diagnosticTitle.textContent = title || 'Diagnostic';
      diagnosticSummary.textContent = summary || 'Có lỗi cần kiểm tra.';
      diagnosticText.textContent = detailText || '(Không có log chi tiết)';
      diagnosticModal.classList.add('active');
      diagnosticModal.setAttribute('aria-hidden', 'false');
      setStatus(summary || title || 'Diagnostic', false);
      if (options.log !== false) {
        appendLog('diagnostic', {
          case_id: options.case_id || '',
          step: options.step || '',
          message: `${title || 'Diagnostic'}: ${summary || ''}${detailText ? `\n${detailText}` : ''}`
        });
      }
    }

    function closeDiagnosticModal() {
      diagnosticModal.classList.remove('active');
      diagnosticModal.setAttribute('aria-hidden', 'true');
    }

	    function isConversationNearBottom(threshold = 140) {
	      return conversation.scrollHeight - conversation.scrollTop - conversation.clientHeight <= threshold;
	    }

	    function scrollConversationToLatest({force = false} = {}) {
	      if (force || isConversationNearBottom()) {
	        conversation.scrollTop = conversation.scrollHeight;
	      }
	      updateJumpToLatest();
	    }

	    function updateJumpToLatest() {
	      jumpToLatest.hidden = !hasTurns || isConversationNearBottom(180);
	    }

	    function resizeQuestionInput() {
	      input.style.height = 'auto';
	      const nextHeight = Math.min(input.scrollHeight, 168);
	      input.style.height = `${Math.max(nextHeight, 52)}px`;
	      input.style.overflowY = input.scrollHeight > 168 ? 'auto' : 'hidden';
	    }

	    function setVoiceListening(active) {
	      voiceListening = active;
	      voiceInput.classList.toggle('listening', active);
	      voiceInput.textContent = active ? 'Dừng' : 'Mic';
	      voiceInput.setAttribute('aria-pressed', active ? 'true' : 'false');
	    }

		    function appendVoiceText(text) {
		      const nextText = String(text || '').trim();
		      if (!nextText) return;
		      const currentText = input.value.trim();
		      input.value = currentText ? `${currentText} ${nextText}` : nextText;
		      resizeQuestionInput();
		    }

		    async function pollBackendMic() {
		      try {
		        const response = await fetch('/mic/status', {cache: 'no-store'});
		        const payload = await response.json();
		        const transcripts = Array.isArray(payload.transcripts) ? payload.transcripts : [];
		        transcripts.slice(micTranscriptCount).forEach((item) => appendVoiceText(item.text));
		        micTranscriptCount = transcripts.length;
		        if (!payload.listening && voiceMode === 'backend') {
		          stopBackendMicPolling();
		          setVoiceListening(false);
		          if (payload.last_error) setStatus(`Lỗi mic: ${payload.last_error}`, false);
		        }
		      } catch (error) {
		        stopBackendMicPolling();
		        setVoiceListening(false);
		        setStatus(`Không đọc được trạng thái mic: ${error.message}`, false);
		      }
		    }

		    function startBackendMicPolling() {
		      stopBackendMicPolling();
		      micPollTimer = window.setInterval(() => {
		        pollBackendMic();
		      }, 800);
		    }

		    function stopBackendMicPolling() {
		      if (micPollTimer) {
		        window.clearInterval(micPollTimer);
		        micPollTimer = null;
		      }
		    }

		    async function stopVoiceInput() {
		      if (voiceMode === 'backend') {
		        stopBackendMicPolling();
		        try {
		          await fetch('/mic/stop', {method: 'POST', cache: 'no-store'});
		        } catch (error) {
		          setStatus(`Không dừng được mic: ${error.message}`, false);
		        }
		        setVoiceListening(false);
		        voiceMode = '';
		        return;
		      }
		      if (speechRecognition && voiceListening) {
		        speechRecognition.stop();
		      }
		    }

		    function startBrowserVoiceInput() {
		      const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
		      if (!Recognition) {
		        setStatus('Mic backend chưa bật được và browser không hỗ trợ SpeechRecognition', false);
		        return;
		      }
		      if (!speechRecognition) {
		        speechRecognition = new Recognition();
		        speechRecognition.lang = 'vi-VN';
		        speechRecognition.continuous = false;
		        speechRecognition.interimResults = true;
		        speechRecognition.maxAlternatives = 1;

		        speechRecognition.onstart = () => {
		          voiceMode = 'browser';
		          setVoiceListening(true);
		          setStatus('Đang nghe mic browser', false);
		        };
		        speechRecognition.onresult = (event) => {
		          let interimText = '';
		          for (let index = event.resultIndex; index < event.results.length; index += 1) {
		            const text = event.results[index][0].transcript;
		            if (text.trim()) voiceHadResult = true;
		            if (event.results[index].isFinal) {
		              voiceFinalText += `${text.trim()} `;
		            } else {
		              interimText += text;
		            }
		          }
		          input.value = `${voiceBaseText}${voiceFinalText}${interimText}`.trimStart();
		          resizeQuestionInput();
		        };
		        speechRecognition.onerror = (event) => {
		          const messages = {
		            'not-allowed': 'Browser chưa được cấp quyền micro',
		            'no-speech': 'Không nghe thấy giọng nói',
		            'audio-capture': 'Không tìm thấy micro',
		            network: 'Dịch vụ nhận diện giọng nói chưa sẵn sàng'
		          };
		          setStatus(messages[event.error] || `Lỗi mic: ${event.error}`, false);
		        };
		        speechRecognition.onend = () => {
		          setVoiceListening(false);
		          resizeQuestionInput();
		          voiceMode = '';
		          if (voiceHadResult) {
		            setStatus('Đã nhận giọng nói, kiểm tra rồi bấm Gửi', true);
		          }
		        };
		      }
		      voiceBaseText = input.value.trim();
		      if (voiceBaseText) voiceBaseText += ' ';
		      voiceFinalText = '';
		      voiceHadResult = false;
		      try {
		        speechRecognition.start();
		      } catch (error) {
		        setStatus(`Không bật được mic browser: ${error.message}`, false);
		      }
		    }

		    async function startVoiceInput() {
		      try {
		        voiceInput.disabled = true;
		        const statusResponse = await fetch('/mic/status', {cache: 'no-store'});
		        const statusPayload = await statusResponse.json();
		        micTranscriptCount = Array.isArray(statusPayload.transcripts) ? statusPayload.transcripts.length : 0;
		        const response = await fetch('/mic/start', {method: 'POST', cache: 'no-store'});
		        const payload = await response.json();
		        if (!response.ok || !payload.ok) {
		          throw new Error(payload.last_error || payload.error || 'Không bật được backend mic');
		        }
		        voiceMode = 'backend';
		        setVoiceListening(true);
		        setStatus('Đang nghe mic Python', false);
		        startBackendMicPolling();
		      } catch (error) {
		        setStatus(`Mic Python lỗi, thử mic browser: ${error.message}`, false);
		        startBrowserVoiceInput();
		      } finally {
		        voiceInput.disabled = false;
		      }
		    }

		    function setupVoiceInput() {
		      voiceInput.disabled = false;
		      voiceInput.title = 'Dùng mic Python backend; fallback sang browser nếu backend không bật được.';
		    }

    function setBusy(active, title = 'Đang xử lý', detail = 'Vui lòng chờ trong giây lát.') {
      busyTitle.textContent = title;
      busyDetail.textContent = detail;
      busyOverlay.classList.toggle('active', active);
      busyOverlay.setAttribute('aria-hidden', active ? 'false' : 'true');
      busyProgressFill.style.width = active ? '8%' : '0%';
      document.body.style.cursor = active ? 'wait' : '';
    }

    function setBusyProgress(percent, detail = '') {
      const value = Math.min(Math.max(Number(percent || 0), 0), 100);
      busyProgressFill.style.width = `${value}%`;
      if (detail) busyDetail.textContent = detail;
    }

    function yieldFrame() {
      return new Promise((resolve) => requestAnimationFrame(() => resolve()));
    }

    function isResumableRunStatus(status) {
      return new Set(['running', 'restarting', 'paused', 'stopped', 'running_parallel', 'stopping_parallel', 'stopped_parallel'])
        .has(String(status || ''));
    }

    async function normalizeRecordsAsync(records, title = 'Đang chuẩn hóa testcase') {
      const output = [];
      const total = records.length;
      for (let index = 0; index < total; index += 1) {
        const item = normalizeTestcase(records[index], index);
        if (item && item.prompt) output.push(item);
        if (index % 250 === 0) {
          const done = Math.min(index + 1, total);
          setBusy(true, title, `Đã xử lý ${done} / ${total} dòng.`);
          setBusyProgress(total ? (done / total) * 100 : 0);
          await yieldFrame();
        }
      }
      setBusyProgress(100, `Đã xử lý ${total} / ${total} dòng.`);
      return output;
    }

    function setView(name) {
      Object.entries(views).forEach(([viewName, view]) => {
        view.classList.toggle('active', viewName === name);
      });
      navButtons.forEach((button) => {
        button.classList.toggle('active', button.dataset.view === name);
      });
      if (location.hash !== `#${name}`) {
        history.replaceState(null, '', `#${name}`);
      }
    }

    function saveTestcases() {
      try {
        localStorage.setItem('vf-testcases', JSON.stringify(testcases));
      } catch (_) {
        localStorage.removeItem('vf-testcases');
      }
    }

    function saveResults() {
      try {
        localStorage.setItem('vf-results', JSON.stringify(results));
      } catch (_) {
        localStorage.removeItem('vf-results');
      }
    }

    function saveLogs() {
      try {
        localStorage.setItem('vf-test-logs', JSON.stringify(testLogs));
      } catch (_) {
        localStorage.removeItem('vf-test-logs');
      }
    }

    function saveHmsRows() {
      try {
        localStorage.setItem('vf-hms-rows', JSON.stringify(hmsRows));
      } catch (_) {
        localStorage.removeItem('vf-hms-rows');
      }
    }

    function saveLogSessionInfo() {
      try {
        localStorage.setItem('vf-log-session-id', activeLogSessionId);
        localStorage.setItem('vf-log-session-name', activeLogSessionName);
      } catch (_) {}
    }

    function saveContextSessionInfo() {
      try {
        localStorage.setItem('vf-chat-log-session-id', chatLogSessionId);
        localStorage.setItem('vf-testcase-log-session-id', testcaseLogSessionId);
      } catch (_) {}
    }

    function saveSourceInfo() {
      try {
        localStorage.setItem('vf-source-file', lastImportName);
        localStorage.setItem('vf-last-run', lastRunTime);
      } catch (_) {}
    }

    function runSetup() {
      return {
        from: Number(runFromCase.value || 1),
        to: Number(runToCase.value || testcases.length || 1),
        threads: lockedThreadValue()
      };
    }

    function normalizeThreadValue(value) {
      value = Math.round(Number(value || 1));
      return Math.min(Math.max(Number.isFinite(value) ? value : 1, 1), MAX_THREAD_COUNT);
    }

    function lockedThreadValue() {
      lockedThreadCount = normalizeThreadValue(lockedThreadCount);
      return lockedThreadCount;
    }

    function updateThreadLockUi() {
      const locked = lockedThreadValue();
      runThreadCount.value = locked;
      threadLockState.textContent = `${locked} luồng`;
      threadSettingsHint.textContent = `Số luồng đang khóa: ${locked}. Chỉ case có response hợp lệ mới được chấm PASS/FAIL.`;
      try {
        localStorage.setItem('vf-locked-thread-count', String(locked));
      } catch (_) {}
    }

    function openRunSettingsModal() {
      updateThreadLockUi();
      runSettingsModal.classList.add('active');
      runSettingsModal.setAttribute('aria-hidden', 'false');
      runThreadCount.focus();
    }

    function closeRunSettingsModal() {
      runSettingsModal.classList.remove('active');
      runSettingsModal.setAttribute('aria-hidden', 'true');
    }

    function lockThreadCount() {
      lockedThreadCount = normalizeThreadValue(runThreadCount.value);
      updateThreadLockUi();
      saveRunSetup();
      updateRunEstimate();
      setStatus(`Đã khóa ${lockedThreadCount} luồng chạy`, true);
      closeRunSettingsModal();
    }

    function syncBatchInputsFromRunRange() {
      updateSplitPreview();
    }

    function runSessionMap() {
      try {
        const saved = JSON.parse(localStorage.getItem('vf-run-sessions') || '{}');
        return saved && typeof saved === 'object' ? saved : {};
      } catch (_) {
        return {};
      }
    }

    function saveRunSessionMap(map) {
      try {
        localStorage.setItem('vf-run-sessions', JSON.stringify(map));
      } catch (_) {}
    }

    function normalizeRunSessionState(state = {}) {
      const bounds = rangeBounds();
      const from = Math.max(Number(state.from || bounds.from || 1), 1);
      const to = Math.min(Number(state.to || bounds.to || testcases.length || from), testcases.length || from);
      const nextIndex = Math.min(
        Math.max(Number(state.nextIndex || from - 1), from - 1),
        Math.max(to, from - 1)
      );
      return {
        sessionId: String(state.sessionId || activeLogSessionId || ''),
        sourceName: String(state.sourceName || activeLogSessionName || lastImportName || 'Manual session'),
        from,
        to,
        nextIndex,
        total: Math.max(to - from + 1, 0),
        status: String(state.status || 'idle'),
        stopAt: '',
        threads: Math.min(Math.max(Number(state.threads || lockedThreadValue()), 1), MAX_THREAD_COUNT),
        reason: String(state.reason || ''),
        updatedAt: new Date().toLocaleString('vi-VN')
      };
    }

    function currentRunSessionState() {
      return normalizeRunSessionState(runSessionState);
    }

    function saveRunSessionState(state = runSessionState, options = {}) {
      if (!activeLogSessionId && !state.sessionId) return;
      const normalized = normalizeRunSessionState({...state, sessionId: state.sessionId || activeLogSessionId});
      runSessionState = normalized;
      const map = runSessionMap();
      map[normalized.sessionId] = normalized;
      saveRunSessionMap(map);
      try {
        localStorage.setItem('vf-active-run-session', JSON.stringify(normalized));
      } catch (_) {}
      if (options.sync !== false) saveLogSessionToServer();
      updateDashboard();
      updateRunEstimate();
    }

    function loadRunSessionState(sessionId = activeLogSessionId) {
      const map = runSessionMap();
      const saved = sessionId && map[sessionId] ? map[sessionId] : {};
      runSessionState = normalizeRunSessionState(saved);
      if (runSessionState.from) runFromCase.value = runSessionState.from;
      if (runSessionState.to) runToCase.value = runSessionState.to;
      if (runSessionState.threads) lockedThreadCount = normalizeThreadValue(runSessionState.threads);
      updateThreadLockUi();
      refreshRunSetupState();
      updateDashboard();
      updateRunEstimate();
      return runSessionState;
    }

    function saveRunSetup() {
      try {
        localStorage.setItem('vf-run-setup', JSON.stringify(runSetup()));
      } catch (_) {}
      runSessionState = normalizeRunSessionState({
        ...runSessionState,
        ...runSetup(),
        status: runSessionState.status || 'idle'
      });
      saveRunSessionState(runSessionState, {sync: Boolean(activeLogSessionId)});
      updateRunEstimate();
    }

    function loadRunSetup() {
      try {
        const saved = JSON.parse(localStorage.getItem('vf-run-setup') || '{}');
        if (saved.from) runFromCase.value = saved.from;
        if (saved.to) runToCase.value = saved.to;
        if (saved.threads) lockedThreadCount = normalizeThreadValue(saved.threads);
      } catch (_) {}
      updateThreadLockUi();
    }

    function savedAverageMsPerCase() {
      const value = Number(localStorage.getItem('vf-average-ms-per-case') || 0);
      return Number.isFinite(value) && value > 0 ? value : 0;
    }

    function rememberAverageMsPerCase(value) {
      if (!Number.isFinite(value) || value <= 0) return;
      const oldValue = savedAverageMsPerCase();
      const next = oldValue ? Math.round((oldValue * 0.65) + (value * 0.35)) : Math.round(value);
      try {
        localStorage.setItem('vf-average-ms-per-case', String(next));
      } catch (_) {}
    }

    function formatClock(date) {
      return date.toLocaleString('vi-VN', {hour: '2-digit', minute: '2-digit', second: '2-digit', day: '2-digit', month: '2-digit', year: 'numeric'});
    }

    function rangeBounds() {
      const total = testcases.length;
      const from = Math.max(Number(runFromCase.value || 1), 1);
      const to = Math.min(Number(runToCase.value || total || from), total || from);
      return {
        from: Math.min(from, to),
        to: Math.max(from, to),
        total
      };
    }

    function parseSplitRangeSpec(spec) {
      const total = testcases.length;
      const selected = new Map();
      const errors = [];
      String(spec || '')
        .split(/[,\n;]+/)
        .map((part) => part.trim())
        .filter(Boolean)
        .forEach((part) => {
          const rangeMatch = part.match(/^#?(\d+)\s*(?:-|:|\.\.|đến|to)\s*#?(\d+)$/i);
          const singleMatch = part.match(/^#?(\d+)$/);
          if (!rangeMatch && !singleMatch) {
            errors.push(part);
            return;
          }
          const start = Number(rangeMatch ? rangeMatch[1] : singleMatch[1]);
          const end = Number(rangeMatch ? rangeMatch[2] : singleMatch[1]);
          const from = Math.min(start, end);
          const to = Math.max(start, end);
          if (!Number.isFinite(from) || !Number.isFinite(to) || from < 1 || to > total) {
            errors.push(part);
            return;
          }
          for (let index = from - 1; index <= to - 1; index += 1) {
            selected.set(index, {item: testcases[index], index});
          }
        });
      return {entries: Array.from(selected.values()).sort((a, b) => a.index - b.index), errors};
    }

    function updateRunEstimate() {
      const bounds = rangeBounds();
      const avg = savedAverageMsPerCase();
      const count = bounds.total ? Math.max(bounds.to - bounds.from + 1, 0) : 0;
      const checkpoint = activeLogSessionId ? currentRunSessionState() : null;
      const pendingFromCheckpoint = checkpoint && isResumableRunStatus(checkpoint.status)
        ? Math.max(Math.min(checkpoint.to, bounds.to) - Math.max(checkpoint.nextIndex, bounds.from - 1), 0)
        : 0;
      const effectiveCount = pendingFromCheckpoint || count;
      const threads = lockedThreadValue();
      if (!count) {
        runEstimate.textContent = 'Chưa chọn được khoảng case.';
        return;
      }
      if (!avg) {
        runEstimate.textContent = `Sẽ chạy ${effectiveCount} case (#${bounds.from} đến #${bounds.to}) với ${threads} luồng. Chưa có tốc độ mẫu để ước tính.`;
        return;
      }
      const estimatedMs = (effectiveCount * avg) / Math.max(threads, 1);
      const finishAt = new Date(Date.now() + estimatedMs);
      const minutes = Math.max(Math.round(estimatedMs / 60000), 1);
      const resumeText = pendingFromCheckpoint ? ` Chạy tiếp từ #${checkpoint.nextIndex + 1}.` : '';
      runEstimate.textContent = `Ước tính ${effectiveCount} case với ${threads} luồng mất khoảng ${minutes} phút, xong khoảng ${formatClock(finishAt)}.${resumeText}`;
    }

    function refreshRunSetupState() {
      updateRunEstimate();
    }

    function makeLogSessionId(sourceName = '') {
      const slug = String(sourceName || 'manual')
        .normalize('NFD')
        .replace(/[\u0300-\u036f]/g, '')
        .replace(/[^0-9A-Za-z_-]+/g, '_')
        .replace(/^_+|_+$/g, '')
        .slice(0, 48) || 'manual';
      const stamp = new Date().toISOString().replace(/[-:.TZ]/g, '').slice(0, 14);
      const rand = Math.random().toString(36).slice(2, 7);
      return `${stamp}_${slug}_${rand}`;
    }

    async function saveLogSessionToServer() {
      if (!activeLogSessionId) return;
      await fetch('/log-session', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          id: activeLogSessionId,
          source_name: activeLogSessionName || lastImportName || 'Manual session',
          logs: testLogs,
          run_state: currentRunSessionState()
        })
      }).catch(() => {});
    }

    function startLogSession(sourceName = 'Manual session', sessionId = '', options = {}) {
      activeLogSessionId = sessionId || makeLogSessionId(sourceName);
      activeLogSessionName = sourceName || 'Manual session';
      const bounds = rangeBounds();
      runSessionState = normalizeRunSessionState({
        sessionId: activeLogSessionId,
        sourceName: activeLogSessionName,
        from: bounds.from,
        to: bounds.to,
        nextIndex: bounds.from - 1,
        status: 'idle',
        threads: lockedThreadValue()
      });
      testLogs = [];
      saveLogs();
      saveLogSessionInfo();
      renderLogs();
      saveRunSessionState(runSessionState, {sync: false});
      saveLogSessionToServer().then(refreshLogSessions);
      if (options.kind === 'chat') {
        chatLogSessionId = activeLogSessionId;
        saveContextSessionInfo();
      }
      if (options.kind === 'testcase') {
        testcaseLogSessionId = activeLogSessionId;
        saveContextSessionInfo();
      }
      return activeLogSessionId;
    }

    function ensureActiveLogSession(sourceName = '') {
      if (!activeLogSessionId) {
        startLogSession(sourceName || lastImportName || 'Manual session');
      }
      return activeLogSessionId;
    }

    async function useChatLogSession() {
      if (chatLogSessionId) {
        if (activeLogSessionId !== chatLogSessionId) {
          await loadLogSession(chatLogSessionId);
        }
      } else {
        startLogSession('Manual chat', '', {kind: 'chat'});
      }
      return activeLogSessionId;
    }

    async function useTestcaseLogSession(sourceName = '') {
      if (testcaseLogSessionId) {
        if (activeLogSessionId !== testcaseLogSessionId) {
          await loadLogSession(testcaseLogSessionId);
        }
      } else {
        startLogSession(sourceName || lastImportName || 'Testcase run', '', {kind: 'testcase'});
      }
      return activeLogSessionId;
    }

    async function loadLogSession(sessionId) {
      if (!sessionId) return;
      const response = await fetch(`/log-session?id=${encodeURIComponent(sessionId)}`);
      const payload = await response.json();
      if (!response.ok) {
        setStatus(payload.error || 'Không đọc được log session', false);
        return;
      }
      activeLogSessionId = payload.id || sessionId;
      activeLogSessionName = payload.source_name || activeLogSessionName || lastImportName || '';
      testLogs = Array.isArray(payload.logs) ? payload.logs : [];
      if (payload.run_state && typeof payload.run_state === 'object') {
        runSessionState = normalizeRunSessionState({
          ...payload.run_state,
          sessionId: activeLogSessionId,
          sourceName: activeLogSessionName
        });
        runFromCase.value = runSessionState.from || '';
        runToCase.value = runSessionState.to || '';
        lockedThreadCount = normalizeThreadValue(runSessionState.threads || 1);
        updateThreadLockUi();
        saveRunSessionState(runSessionState, {sync: false});
        refreshRunSetupState();
      } else {
        loadRunSessionState(activeLogSessionId);
      }
      saveLogs();
      saveLogSessionInfo();
      if ((activeLogSessionName || '').toLocaleLowerCase('en-US').includes('chat')) {
        chatLogSessionId = activeLogSessionId;
        saveContextSessionInfo();
      }
      if ((activeLogSessionName || '').toLocaleLowerCase('en-US').includes('.xlsx') || (activeLogSessionName || '').toLocaleLowerCase('en-US').includes('testcase')) {
        testcaseLogSessionId = activeLogSessionId;
        saveContextSessionInfo();
      }
      activeLogCase = '';
      logSearch.value = '';
      renderLogs();
      renderLogSessions();
      updateProgress();
      setStatus(`Đã chọn session ${activeLogSessionName || activeLogSessionId}`, true);
    }

    async function refreshLogSessions() {
      const response = await fetch('/log-sessions').catch(() => null);
      if (!response || !response.ok) {
        renderLogSessions();
        return;
      }
      const payload = await response.json();
      logSessions = Array.isArray(payload.sessions) ? payload.sessions : [];
      renderLogSessions();
    }

    function isChatSession(session) {
      const name = String((session && (session.source_name || session.id)) || '').toLocaleLowerCase('en-US');
      return name.includes('chat') || name.includes('manual chat');
    }

    function isTestcaseSession(session) {
      if (!session || isChatSession(session)) return false;
      const name = String(session.source_name || session.id || '').toLocaleLowerCase('en-US');
      const state = session.run_state && typeof session.run_state === 'object' ? session.run_state : {};
      return name.includes('.xlsx')
        || name.includes('testcase')
        || name.includes('split')
        || ['running', 'restarting', 'paused', 'stopped', 'done', 'running_parallel', 'stopped_parallel'].includes(String(state.status || ''));
    }

    async function selectLogSessionByKind(kind, options = {}) {
      await refreshLogSessions();
      const knownId = kind === 'chat' ? chatLogSessionId : testcaseLogSessionId;
      const known = knownId ? logSessions.find((session) => session.id === knownId) : null;
      const match = known || logSessions.find((session) => kind === 'chat' ? isChatSession(session) : isTestcaseSession(session));
      if (!match) {
        setStatus(kind === 'chat' ? 'Chưa có log chat.' : 'Chưa có log testcase.', false);
        return false;
      }
      await loadLogSession(match.id);
      if (options.caseId) {
        activeLogCase = options.caseId;
        logSearch.value = '';
        renderLogs();
      }
      setView('logs');
      return true;
    }

    function getField(item, names) {
      for (const name of names) {
        if (item[name] !== undefined && item[name] !== null && String(item[name]).trim()) {
          return String(item[name]).trim();
        }
      }
      return '';
    }

    function normalizeDataKey(value) {
      return String(value || '')
        .normalize('NFD')
        .replace(/[\u0300-\u036f]/g, '')
        .toLocaleLowerCase('en-US')
        .replace(/[^a-z0-9]+/g, '');
    }

    function getFieldLoose(item, names) {
      if (!item || typeof item !== 'object') return '';
      const lookup = new Map();
      Object.entries(item).forEach(([key, value]) => {
        if (value !== undefined && value !== null && String(value).trim()) {
          lookup.set(normalizeDataKey(key), String(value).trim());
        }
      });
      for (const name of names) {
        const value = lookup.get(normalizeDataKey(name));
        if (value) return value;
      }
      return '';
    }

    function makeHmsRow(item, index = 0) {
      const source = item && typeof item === 'object' ? item : {};
      const confirmationNumber = getFieldLoose(source, [
        'confirmationNumber',
        'confirmation_number',
        'confirmation number',
        'confirmationNo',
        'confirmation no',
        'reservationNumber',
        'reservation number',
        'bookingCode',
        'booking code'
      ]);
      const lastName = getFieldLoose(source, [
        'lastName',
        'last_name',
        'last name',
        'surname',
        'familyName',
        'family name'
      ]);
      return {
        case_id: getFieldLoose(source, ['case_id', 'case id', 'Case ID']) || source.name || `HMS-${index + 1}`,
        confirmationNumber,
        lastName,
        roomStatus: source.roomStatus || source.room_status || '',
        status: source.status && source.confirmationNumber ? source.status : 'PENDING',
        httpStatus: source.httpStatus || '',
        message: source.message || '',
        checkedAt: source.checkedAt || '',
        payload: source.payload || null
      };
    }

    function validHmsRow(row) {
      return Boolean(row && row.confirmationNumber && row.lastName);
    }

    function parseDelimitedHmsLine(line, headers = []) {
      const separator = line.includes('\t') ? '\t' : (line.includes('|') ? '|' : (line.includes(';') ? ';' : ','));
      const parts = line.split(separator).map((part) => part.trim()).filter((part) => part !== '');
      if (!parts.length) return null;
      if (headers.length) {
        const record = {};
        headers.forEach((header, index) => {
          record[header] = parts[index] || '';
        });
        return makeHmsRow(record);
      }
      return makeHmsRow({confirmationNumber: parts[0] || '', lastName: parts[1] || '', case_id: parts[2] || ''});
    }

    function parseHmsRows(raw) {
      const text = String(raw || '').trim();
      if (!text) return [];
      try {
        const parsed = JSON.parse(text);
        const items = Array.isArray(parsed) ? parsed : (parsed.items || parsed.rows || parsed.data || []);
        return items.map((item, index) => makeHmsRow(item, index)).filter(validHmsRow);
      } catch (_) {}

      const lines = text.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
      if (!lines.length) return [];
      const firstParts = lines[0].split(lines[0].includes('\t') ? '\t' : (lines[0].includes('|') ? '|' : (lines[0].includes(';') ? ';' : ','))).map((part) => part.trim());
      const hasHeader = firstParts.some((part) => ['confirmationnumber', 'lastname'].includes(normalizeDataKey(part)));
      const headers = hasHeader ? firstParts : [];
      return lines.slice(hasHeader ? 1 : 0)
        .map((line, index) => {
          const row = parseDelimitedHmsLine(line, headers);
          if (row && !row.case_id) row.case_id = `HMS-${index + 1}`;
          return row;
        })
        .filter(validHmsRow);
    }

    function normalizeTestcase(item, index) {
      if (typeof item === 'string') {
        const prompt = item.trim();
        return {
          case_id: `TC-${index + 1}`,
          step_id: '',
          Type: '',
          Language: '',
          prompt_text: prompt,
          prompt,
          name: `TC-${index + 1}`,
          date: '',
          actual_response: '',
          test_results: '',
          status: 'NOT RUN'
        };
      }
      if (item && typeof item === 'object') {
        const record = {...item};
        const prompt = getFieldLoose(record, ['prompt_text', 'prompt', 'question', 'text']);
        const caseId = getFieldLoose(record, ['case_id', 'case id', 'Case ID']) || `TC-${index + 1}`;
        const stepId = getFieldLoose(record, ['step_id', 'step id', 'Step ID']);
        const resultText = getFieldLoose(record, ['test_results', 'test results']);
        const type = getFieldLoose(record, ['Type', 'type']);
        const language = getFieldLoose(record, ['Language', 'language']);
        const expectedResponse = getFieldLoose(record, ['expected_response', 'expected response']);
        const expectedKeywords = getFieldLoose(record, ['expected_keywords', 'expected keywords']);
        const forbiddenKeywords = getFieldLoose(record, ['forbidden_keywords', 'forbidden keywords']);
        const status = resultText.startsWith('PASS')
          ? 'PASS'
          : resultText.startsWith('FAIL')
            ? 'FAIL'
            : resultText.startsWith('ERROR')
              ? 'ERROR'
              : resultText.startsWith('SKIPPED')
                ? 'SKIPPED'
                : 'NOT RUN';
        return {
          ...record,
          case_id: caseId,
          step_id: stepId,
          Type: type,
          Language: language,
          prompt_text: record.prompt_text || prompt,
          prompt,
          expected_response: expectedResponse,
          expected_keywords: expectedKeywords,
          forbidden_keywords: forbiddenKeywords,
          name: String(record.name || record.title || `${caseId}${stepId ? ` / Step ${stepId}` : ''}`).trim(),
          date: record.date || '',
          actual_response: getFieldLoose(record, ['actual_response', 'actual response']) || '',
          test_results: resultText || '',
          status
        };
      }
      return null;
    }

    function parseTestcases(raw) {
      const text = raw.trim();
      if (!text) return [];

      try {
        const parsed = JSON.parse(text);
        const items = Array.isArray(parsed)
          ? parsed
          : (parsed.testcases || parsed.prompts || parsed.items || []);
        return items
          .map((item, index) => normalizeTestcase(item, index))
          .filter((item) => item && item.prompt);
      } catch (_) {
        return text
          .split(/\r?\n/)
          .map((line) => line.trim())
          .filter(Boolean)
          .map((line, index) => {
            const pipeIndex = line.indexOf('|');
            if (pipeIndex > 0) {
              return {
                name: line.slice(0, pipeIndex).trim() || `TC-${index + 1}`,
                prompt: line.slice(pipeIndex + 1).trim()
              };
            }
            return {name: `TC-${index + 1}`, prompt: line};
          })
          .filter((item) => item.prompt);
      }
    }

    function splitKeywords(value) {
      return window.TestcaseEvaluator.splitList(value);
    }

    function evaluateTestcase(item, output) {
      return window.TestcaseEvaluator.evaluateTestcase(item, output);
    }

    function resultStatusAndNote(resultText = '', fallbackStatus = '') {
      const raw = String(resultText || '').trim();
      const match = raw.match(/^(PASS|FAIL|ERROR|RUNNING|NOT RUN|SKIPPED)\s*:?\s*(.*)$/i);
      if (match) {
        return {
          status: match[1].toLocaleUpperCase('en-US'),
          note: match[2] || ''
        };
      }
      return {
        status: String(fallbackStatus || raw || 'NOT RUN').toLocaleUpperCase('en-US'),
        note: ''
      };
    }

    function testcaseEntries() {
      const query = (testcaseSearch.value || '').trim().toLocaleLowerCase('vi-VN');
      return testcases
        .map((item, index) => ({item, index}))
        .filter(({item}) => {
          if (!query) return true;
          return [
            item.case_id,
            item.step_id,
            item.Type,
            item.Language,
            item.prompt,
            item.expected_response,
            item.expected_keywords,
            item.forbidden_keywords,
            item.test_results
          ].join(' ').toLocaleLowerCase('vi-VN').includes(query);
        });
    }

    function statusClass(status) {
      return String(status || 'NOT RUN').toLocaleLowerCase('en-US').replace(/\s+/g, '-');
    }

    function isCheckpointStatus(status) {
      const normalized = String(status || 'NOT RUN').toLocaleUpperCase('en-US');
      return normalized === 'ERROR' || normalized === 'RUNNING';
    }

    function firstCheckpointIndex() {
      const errorIndex = testcases.findIndex((item) => isCheckpointStatus(item.status));
      if (errorIndex >= 0) return errorIndex;
      return testcases.findIndex((item) => String(item.status || 'NOT RUN').toLocaleUpperCase('en-US') === 'NOT RUN');
    }

    function entriesFromIndex(startIndex) {
      if (startIndex < 0 || startIndex >= testcases.length) return [];
      return testcases.slice(startIndex).map((item, offset) => ({item, index: startIndex + offset}));
    }

    function caseKey(item) {
      return String(item.case_id || item.name || item.prompt || '').trim();
    }

    function makeRunToken(item, index = 0, workerId = 0) {
      const key = caseKey(item) || `case-${index + 1}`;
      return `${Date.now()}-${workerId}-${index}-${Math.random().toString(36).slice(2)}-${key}`;
    }

    function isCurrentTestcaseRun(item, runToken) {
      return !runToken || item._active_run_token === runToken;
    }

    function clearResultsForEntries(entries) {
      const keys = new Set(entries.map(({item}) => caseKey(item)).filter(Boolean));
      if (!keys.size) return;
      results = results.filter((item) => !keys.has(caseKey(item)));
      saveResults();
      renderResults();
    }

    function prepareEntriesForRerun(entries) {
      clearResultsForEntries(entries);
      entries.forEach(({item}) => {
      item.date = '';
      item.actual_response = '';
      item.test_results = '';
      item['test results'] = '';
      item.Results = '';
      item.note = '';
      item.status = 'NOT RUN';
      item.RunTest = '';
      item._active_run_token = '';
      });
      saveTestcases();
      renderTestcases();
    }

    function markTestcaseRunning(item, runToken = '') {
      const date = new Date().toLocaleString('vi-VN');
      item.date = date;
      item.actual_response = '';
      item.test_results = 'RUNNING: Đang chờ phản hồi';
      item['test results'] = item.test_results;
      item.Results = 'RUNNING';
      item.note = 'Đang chờ phản hồi';
      item.status = 'RUNNING';
      item.RunTest = 'Y';
      item._active_run_token = runToken;
      saveTestcases();
    }

    function resetTestcaseForRetry(item) {
      item.actual_response = '';
      item.test_results = '';
      item['test results'] = '';
      item.Results = '';
      item.note = '';
      item.status = 'NOT RUN';
      item.RunTest = '';
      item._active_run_token = '';
      saveTestcases();
      renderTestcases();
    }

    function isRealtimePauseError(error) {
      const message = String(error && error.message ? error.message : error || '');
      return message.includes('Realtime chưa kết nối') || message.includes('Quá thời gian chờ phản hồi');
    }

    const MISSING_RESPONSE_MAX_ATTEMPTS = 3;
    const MISSING_RESPONSE_RETRY_LIMIT = Math.max(MISSING_RESPONSE_MAX_ATTEMPTS - 1, 0);

    function isMissingResponseText(value = '') {
      const text = String(value || '').trim();
      if (!text) return true;
      const normalized = text.toLocaleLowerCase('vi-VN');
      return [
        '(không có nội dung phản hồi)',
        'không có nội dung phản hồi',
        'quá thời gian chờ phản hồi',
        'realtime chưa kết nối',
        'realtime idle timeout',
        'realtime connection closed',
        'connection closed',
        'failed to fetch',
        'networkerror',
        'response không phải json',
        'request failed http',
        'reconnect',
        'timeout'
      ].some((pattern) => normalized.includes(pattern));
    }

    function isMissingResponseCase(item) {
      if (!item) return false;
      const status = String(item.status || 'NOT RUN').toLocaleUpperCase('en-US');
      if (status === 'PASS') return false;
      const actual = String(item.actual_response || '').trim();
      const note = String(item.note || item.test_results || item['test results'] || '').trim();
      if (isMissingResponseText(actual)) return true;
      return status === 'ERROR' && isMissingResponseText(note);
    }

    function markMissingResponseResult(item, actual, runToken = '') {
      if (!isCurrentTestcaseRun(item, runToken)) {
        appendLog('stale_response', {
          case_id: item.case_id || item.name,
          step: item.step_id || '',
          message: `Ignored stale missing response for ${item.case_id || item.name}`,
          prompt: item.prompt,
          actual_response: actual || '',
          test_result: `STALE token=${runToken}`
        });
        return false;
      }
      const date = new Date().toLocaleString('vi-VN');
      const detail = String(actual || '').trim() || 'Không có response hợp lệ';
      const result = `ERROR: ${detail}. Chưa đánh giá, sẽ retry tối đa ${MISSING_RESPONSE_MAX_ATTEMPTS} lần.`;
      item.date = date;
      item.actual_response = detail;
      item.test_results = result;
      item['test results'] = result;
      item.Results = 'ERROR';
      item.note = detail;
      item.status = 'ERROR';
      item.RunTest = 'Y';
      item._active_run_token = '';
      lastRunTime = date;
      saveSourceInfo();
      saveTestcases();
      addResult(
        item.prompt,
        item.actual_response,
        false,
        item.case_id || item.name,
        result,
        item.case_id || item.name,
        'testcase',
        {run_token: runToken, worker_id: item.worker_id || ''}
      );
      appendLog('missing_response', {
        case_id: item.case_id || item.name,
        step: item.step_id || '',
        message: result,
        prompt: item.prompt,
        actual_response: item.actual_response,
        test_result: result
      });
      renderTestcases();
      renderResults();
      return true;
    }

    function markSkippedMissingResponseEntries(entries, attempts) {
      entries.forEach(({item}) => {
        const date = new Date().toLocaleString('vi-VN');
        const actual = String(item.actual_response || '').trim();
        const note = `Không có response hợp lệ sau ${attempts} lần, bỏ qua.`;
        const result = `SKIPPED: ${note}`;
        item.date = date;
        item.actual_response = actual || '';
        item.test_results = result;
        item['test results'] = result;
        item.Results = 'SKIPPED';
        item.note = note;
        item.status = 'SKIPPED';
        item.RunTest = 'Y';
        item._active_run_token = '';
        lastRunTime = date;
        addResult(
          item.prompt,
          item.actual_response || note,
          true,
          item.case_id || item.name,
          result,
          item.case_id || item.name,
          'testcase'
        );
        appendLog('skipped_no_response', {
          case_id: item.case_id || item.name,
          step: item.step_id || '',
          message: result,
          prompt: item.prompt,
          actual_response: item.actual_response,
          test_result: result
        });
      });
      saveSourceInfo();
      saveTestcases();
    }

    async function rerunMissingResponseEntries(entries, modeLabel, options = {}) {
      const retryLimit = Number(options.retryLimit ?? MISSING_RESPONSE_RETRY_LIMIT);
      const totalAttempts = retryLimit + 1;
      let retryEntries = entries.filter(({item}) => isMissingResponseCase(item));
      if (!retryEntries.length) return 0;
      for (let attempt = 1; attempt <= retryLimit && retryEntries.length && !runState.stopRequested; attempt += 1) {
        appendLog('missing_response_retry_start', {
          message: `Retry missing response attempt ${attempt + 1}/${totalAttempts}: ${retryEntries.length} testcase(s)`,
          test_result: `retry_missing_response_${attempt}`
        });
        clearResultsForEntries(retryEntries);
        for (const {item, index: itemIndex} of retryEntries) {
          if (runState.stopRequested) break;
          const label = item.case_id || item.name || `#${itemIndex + 1}`;
          runState.current = `retry miss response ${attempt + 1}/${totalAttempts}: ${label}`;
          updateProgress();
          appendLog('missing_response_retry_case', {
            case_id: item.case_id || item.name,
            step: item.step_id || '',
            message: `Retry missing response ${attempt + 1}/${totalAttempts}: ${label}`,
            prompt: item.prompt,
            actual_response: item.actual_response || '',
            test_result: item.test_results || item['test results'] || ''
          });
          resetTestcaseForRetry(item);
          const runToken = makeRunToken(item, itemIndex, 0);
          markTestcaseRunning(item, runToken);
          renderTestcases();
          try {
            await ask(item.prompt, {testcase: item, showChat: false, runToken});
          } catch (_) {}
        }
        retryEntries = entries.filter(({item}) => isMissingResponseCase(item));
      }

      if (retryEntries.length) {
        clearResultsForEntries(retryEntries);
        markSkippedMissingResponseEntries(retryEntries, totalAttempts);
        appendLog('missing_response_retry_done', {
          message: `Skipped ${retryEntries.length} testcase(s) with no response after ${totalAttempts} attempt(s)`,
          test_result: 'retry_missing_response_skipped'
        });
      } else {
        appendLog('missing_response_retry_done', {
          message: `No missing response testcase remains after ${totalAttempts} attempt(s)`,
          test_result: 'retry_missing_response_done'
        });
      }
      saveLogSessionToServer();
      saveTestcases();
      renderTestcases();
      renderResults();
      return retryEntries.length;
    }

    function failedReviewEntries() {
      return testcases
        .map((item, index) => ({item, index}))
        .filter(({item}) => {
          const status = String(item.status || 'NOT RUN').toLocaleUpperCase('en-US');
          return status === 'FAIL' || status === 'ERROR' || status === 'SKIPPED' || isMissingResponseCase(item);
        });
    }

    function updateDashboard() {
      const pass = testcases.filter((item) => item.status === 'PASS').length;
      const fail = testcases.filter((item) => item.status === 'FAIL' || item.status === 'ERROR').length;
      const skipped = testcases.filter((item) => item.status === 'SKIPPED').length;
      const pending = Math.max(testcases.length - pass - fail - skipped, 0);
      const checkpointIndex = firstCheckpointIndex();
      const checkpoint = checkpointIndex >= 0 ? testcases[checkpointIndex] : null;
      const checkpointLabel = checkpoint ? (checkpoint.case_id || checkpoint.name || `#${checkpointIndex + 1}`) : '';
      const checkpointStatus = checkpoint ? String(checkpoint.status || 'NOT RUN').toLocaleUpperCase('en-US') : '';
      const checkpointAction = checkpointStatus === 'NOT RUN' ? 'Chạy từ case chưa chạy' : 'Chạy từ case lỗi';
      const sessionCheckpoint = activeLogSessionId ? currentRunSessionState() : null;
      const canResumeSession = sessionCheckpoint
        && isResumableRunStatus(sessionCheckpoint.status)
        && sessionCheckpoint.nextIndex >= sessionCheckpoint.from - 1
        && sessionCheckpoint.nextIndex < sessionCheckpoint.to
        && sessionCheckpoint.nextIndex < testcases.length;
      const sessionResumeLabel = canResumeSession ? `Chạy tiếp: #${sessionCheckpoint.nextIndex + 1}` : '';
      metricTotal.textContent = testcases.length;
      metricPass.textContent = pass;
      metricFail.textContent = fail;
      metricPending.textContent = pending;
      metricFile.textContent = lastImportName || 'Chưa có';
      metricLastRun.textContent = lastRunTime || 'Chưa chạy';
      document.getElementById('runAll').textContent = 'Chạy tất cả';
      document.getElementById('runAllSide').textContent = 'Chạy tất cả';
      document.getElementById('resumeRun').textContent = sessionResumeLabel || (checkpoint ? `${checkpointAction}: ${checkpointLabel}` : 'Chạy tiếp');
      document.getElementById('resumeRunSide').textContent = sessionResumeLabel || (checkpoint ? `${checkpointAction}: ${checkpointLabel}` : 'Chạy tiếp');
      document.getElementById('runAll').disabled = runState.running || !testcases.length;
      document.getElementById('runAllSide').disabled = runState.running || !testcases.length;
      document.getElementById('resumeRun').disabled = runState.running || (!canResumeSession && checkpointIndex < 0);
      document.getElementById('resumeRunSide').disabled = runState.running || (!canResumeSession && checkpointIndex < 0);
      document.getElementById('reviewFailed').disabled = runState.running || !failedReviewEntries().length;
      document.getElementById('reviewFailedFromResults').disabled = runState.running || !failedReviewEntries().length;
      stopRun.textContent = runState.stopRequested ? 'Đang dừng' : 'Dừng';
      stopRunSide.textContent = runState.stopRequested ? 'Đang dừng' : 'Dừng';
      stopRun.disabled = !runState.running || runState.stopRequested;
      stopRunSide.disabled = !runState.running || runState.stopRequested;
    }

    function updateProgress() {
      const total = runState.total || 0;
      const completed = runState.completed || 0;
      const percent = total ? Math.round((completed / total) * 100) : 0;
      progressPercent.textContent = `${percent}%`;
      progressFill.style.width = `${percent}%`;
      let etaText = '';
      if (runState.running && runState.startedAt && completed > 0 && completed < total) {
        const average = (Date.now() - runState.startedAt) / completed;
        const remainingMs = Math.max(total - completed, 0) * average;
        etaText = ` · ETA ${formatClock(new Date(Date.now() + remainingMs))}`;
      }
      progressStatus.textContent = runState.running
        ? (runState.stopRequested ? 'Đang dừng runner' : `Đang chạy ${runState.current || ''}${etaText}`.trim())
        : (completed && total && completed >= total ? 'Hoàn tất' : (completed && total ? 'Đã dừng' : 'Chưa chạy'));
      progressDetail.textContent = `${completed} / ${total} testcase`;
      const progressCard = document.querySelector('.run-progress');
      progressCard.classList.toggle('running', runState.running);
      const paused = !runState.running && total > 0 && completed > 0 && completed < total;
      progressCard.classList.toggle('complete', !runState.running && total > 0 && completed >= total);
      progressCard.classList.toggle('paused', paused);
      updateDashboard();
      updateRunEstimate();
    }

    function appendLog(event, details = {}) {
      ensureActiveLogSession(lastImportName || 'Manual session');
      const entry = {
        time: new Date().toLocaleString('vi-VN'),
        session_id: activeLogSessionId,
        case_id: details.case_id || '',
        step: details.step || '',
        event,
        message: details.message || '',
        prompt: details.prompt || '',
        actual_response: details.actual_response || '',
        test_result: details.test_result || ''
      };
      testLogs.push(entry);
      saveLogs();
      renderLogs();
      fetch('/log-session/append', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          sessionId: activeLogSessionId,
          sourceName: activeLogSessionName || lastImportName || 'Manual session',
          entry
        })
      }).then(refreshLogSessions).catch(() => {});
      return entry;
    }

    function renderLogSessions() {
      if (!logSessionList) return;
      if (!logSessions.length && activeLogSessionId) {
        logSessions = [{
          id: activeLogSessionId,
          source_name: activeLogSessionName || lastImportName || 'Session hiện tại',
          updated_at: '',
          count: testLogs.length,
          kind: 'session',
          run_state: currentRunSessionState()
        }];
      }
      if (!logSessions.length) {
        logSessionList.innerHTML = '<div class="log-row"><div class="log-message">Chưa có log session.</div></div>';
        return;
      }
      logSessionList.innerHTML = logSessions.map((session) => {
        const state = session.run_state && typeof session.run_state === 'object' ? session.run_state : {};
        const nextNumber = Number(state.nextIndex || 0) + 1;
        const toNumber = Number(state.to || 0);
        const resumeMeta = isResumableRunStatus(state.status) && nextNumber <= toNumber
          ? `<span class="log-session-meta">Chạy tiếp #${escapeHtml(nextNumber)} / #${escapeHtml(toNumber)} · ${escapeHtml(state.status)}</span>`
          : '';
        return `
        <button class="log-session ${session.id === activeLogSessionId ? 'active' : ''}" type="button" data-session="${escapeHtml(session.id)}">
          <span class="log-session-name">${escapeHtml(session.source_name || session.id)}</span>
          <span class="log-session-meta">${escapeHtml(session.updated_at || session.created_at || '-')}</span>
          <span class="log-session-meta">${escapeHtml(String(session.count || 0))} log${session.kind === 'legacy_txt' ? ' · file txt' : ''}</span>
          ${resumeMeta}
        </button>
      `;
      }).join('');
    }

    function renderLogs() {
      const query = (logSearch.value || '').trim().toLocaleLowerCase('vi-VN');
      const filtered = testLogs.filter((item) => {
        if (activeLogCase && item.case_id !== activeLogCase) return false;
        if (!query) return true;
        return [
          item.time,
          item.case_id,
          item.step,
          item.event,
          item.message,
          item.prompt,
          item.actual_response,
          item.test_result
        ].join(' ').toLocaleLowerCase('vi-VN').includes(query);
      });
      const scope = activeLogCase ? `case ${activeLogCase}` : (activeLogSessionName || activeLogSessionId || 'Tất cả logs');
      logScopeTitle.textContent = activeLogCase ? `Logs của ${activeLogCase}` : scope;
      logSummary.innerHTML = `<strong>Tổng quan</strong>${filtered.length ? `${filtered.length} log đang hiển thị trên tổng ${testLogs.length} log của session ${escapeHtml(activeLogSessionName || activeLogSessionId || '-')}.` : 'Chưa có log phù hợp.'}`;
      if (!filtered.length) {
        const emptyMessage = activeLogCase
          ? `Chưa có log cho case ${escapeHtml(activeLogCase)} trong session hiện tại.`
          : (activeLogSessionName || activeLogSessionId ? 'Chưa có log trong session hiện tại.' : 'Chưa chọn log session.');
        logList.innerHTML = `<div class="log-row"><div class="log-message">${emptyMessage}</div></div>`;
        renderLogSessions();
        return;
      }
      logList.innerHTML = filtered.slice(-500).reverse().map((item) => `
        <div class="log-row fade-in">
          <div class="log-time">${escapeHtml(item.time)}</div>
          <div class="log-case">${escapeHtml(item.case_id || '-')}</div>
          <div class="log-step">${escapeHtml(item.event)}</div>
          <div class="log-message">${escapeHtml(item.message || item.prompt || item.actual_response || item.test_result || '-')}</div>
        </div>
      `).join('');
      renderLogSessions();
    }

    function updateHmsSummary() {
      const total = hmsRows.length;
      const checked = hmsRows.filter((row) => row.status && row.status !== 'PENDING').length;
      const ok = hmsRows.filter((row) => row.roomStatus).length;
      const errors = hmsRows.filter((row) => row.status === 'ERROR').length;
      hmsMetricTotal.textContent = total;
      hmsMetricChecked.textContent = checked;
      hmsMetricOk.textContent = ok;
      hmsMetricError.textContent = errors;
      hmsSummary.innerHTML = `<strong>Tổng quan</strong>${total ? `${total} dòng HMS. Đã check ${checked}. Có roomStatus ${ok}. Lỗi ${errors}.` : 'Chưa có data HMS.'}`;
      document.getElementById('checkAllHms').disabled = !total || hmsRows.some((row) => row.status === 'CHECKING');
      document.getElementById('clearHmsRows').disabled = !total;
    }

    function compactPayload(payload) {
      if (!payload) return '';
      try {
        return JSON.stringify(payload, null, 2);
      } catch (_) {
        return String(payload);
      }
    }

    function renderHmsRows() {
      updateHmsSummary();
      if (!hmsRows.length) {
        hmsResultList.innerHTML = '<div class="empty"><div><strong>Chưa có data HMS</strong><span>Lấy từ testcases đã import hoặc nhập confirmationNumber,lastName.</span></div></div>';
        return;
      }
      hmsResultList.innerHTML = `
        <div class="table-wrap">
          <table class="data-table hms-table">
            <thead>
              <tr>
                <th class="col-case">Case</th>
                <th class="col-confirmation">confirmationNumber</th>
                <th class="col-last-name">lastName</th>
                <th class="col-room-status">roomStatus</th>
                <th class="col-http">HTTP</th>
                <th>Response</th>
                <th class="col-actions">Thao tác</th>
              </tr>
            </thead>
            <tbody>
              ${hmsRows.map((row, index) => `
                <tr>
                  <td>
                    <div class="cell-main">${escapeHtml(row.case_id || `HMS-${index + 1}`)}</div>
                    <div class="cell-sub">${escapeHtml(row.checkedAt || '-')}</div>
                  </td>
                  <td><div class="cell-main">${escapeHtml(row.confirmationNumber)}</div></td>
                  <td>${escapeHtml(row.lastName)}</td>
                  <td><span class="status-pill ${row.roomStatus ? 'pass' : statusClass(row.status)}">${escapeHtml(row.roomStatus || row.status || 'PENDING')}</span></td>
                  <td>${escapeHtml(row.httpStatus || '-')}</td>
                  <td>
                    <div class="result-detail ${row.status === 'ERROR' ? 'fail' : ''}">${escapeHtml(row.message || '-')}</div>
                    ${row.payload ? `<pre class="json-preview">${escapeHtml(compactPayload(row.payload))}</pre>` : ''}
                  </td>
                  <td>
                    <div class="item-actions">
                      <button type="button" data-hms-action="check" data-index="${index}" ${row.status === 'CHECKING' ? 'disabled' : ''}>Check</button>
                      <button class="secondary" type="button" data-hms-action="fill" data-index="${index}">Điền</button>
                      <button class="danger" type="button" data-hms-action="delete" data-index="${index}">Xóa</button>
                    </div>
                  </td>
                </tr>
              `).join('')}
            </tbody>
          </table>
        </div>
      `;
    }

    async function checkHmsRow(index) {
      const row = hmsRows[index];
      if (!validHmsRow(row)) {
        setStatus('Dòng HMS thiếu confirmationNumber hoặc lastName', false);
        return;
      }
      row.status = 'CHECKING';
      row.message = 'Đang gọi HMS...';
      saveHmsRows();
      renderHmsRows();
      setStatus(`Đang check HMS ${row.confirmationNumber}`, false);
      try {
        const response = await fetch('/hms/room-status', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({
            confirmationNumber: row.confirmationNumber,
            lastName: row.lastName,
            orgId: hmsOrgId.value.trim()
          })
        });
        const payload = await response.json().catch(() => ({}));
        row.httpStatus = payload.httpStatus || response.status;
        row.roomStatus = payload.roomStatus || '';
        row.payload = payload.payload || payload;
        row.checkedAt = new Date().toLocaleString('vi-VN');
        if (!response.ok || payload.ok === false) {
          row.status = 'ERROR';
          row.message = payload.error || 'HMS trả lỗi';
        } else {
          row.status = row.roomStatus ? 'OK' : 'NO_ROOM_STATUS';
          row.message = row.roomStatus ? `roomStatus=${row.roomStatus}` : 'HMS OK nhưng không thấy field roomStatus trong response';
        }
        saveHmsRows();
        renderHmsRows();
        setStatus(row.status === 'ERROR' ? `HMS lỗi ${row.confirmationNumber}` : `HMS OK ${row.confirmationNumber}`, row.status !== 'ERROR');
      } catch (error) {
        row.status = 'ERROR';
        row.message = error.message;
        row.checkedAt = new Date().toLocaleString('vi-VN');
        saveHmsRows();
        renderHmsRows();
        setStatus(`HMS lỗi: ${error.message}`, false);
      }
    }

    function loadHmsRowsFromTestcases() {
      const rows = testcases.map((item, index) => makeHmsRow(item, index)).filter(validHmsRow);
      hmsRows = rows;
      saveHmsRows();
      renderHmsRows();
      setStatus(rows.length ? `Đã lấy ${rows.length} dòng HMS từ testcases` : 'Không thấy confirmationNumber/lastName trong testcases', Boolean(rows.length));
      setView('hms');
    }

    async function checkAllHmsRows() {
      if (!hmsRows.length) {
        setStatus('Chưa có data HMS để check', false);
        return;
      }
      for (let index = 0; index < hmsRows.length; index += 1) {
        await checkHmsRow(index);
      }
      const errors = hmsRows.filter((row) => row.status === 'ERROR').length;
      setStatus(errors ? `Check HMS xong, có ${errors} lỗi` : 'Check HMS hoàn tất', !errors);
    }

    function renderTestcases() {
      updateDashboard();
      if (!testcases.length) {
        testcaseList.innerHTML = '<div class="empty"><div><strong>Chưa có testcase</strong><span>Import prompt testcase để bắt đầu chạy kiểm thử.</span></div></div>';
        testcasePager.innerHTML = '';
        return;
      }

      const entries = testcaseEntries();
      if (!entries.length) {
        testcaseList.innerHTML = '<div class="empty"><div><strong>Không có kết quả phù hợp</strong><span>Đổi từ khóa tìm kiếm để xem testcase.</span></div></div>';
        testcasePager.innerHTML = '';
        return;
      }
      const totalPages = Math.max(Math.ceil(entries.length / TESTCASE_PAGE_SIZE), 1);
      testcasePage = Math.min(Math.max(testcasePage, 1), totalPages);
      const start = (testcasePage - 1) * TESTCASE_PAGE_SIZE;
      const visibleEntries = entries.slice(start, start + TESTCASE_PAGE_SIZE);

      testcaseList.innerHTML = `
        <div class="table-wrap">
          <table class="data-table testcase-table">
            <thead>
              <tr>
                <th class="col-case">Case</th>
                <th class="col-type">Phân loại</th>
                <th>Prompt</th>
                <th>Keyword kiểm tra</th>
                <th class="col-status">Trạng thái</th>
                <th class="col-date">Date</th>
                <th class="col-actions">Thao tác</th>
              </tr>
            </thead>
            <tbody>
              ${visibleEntries.map(({item, index}) => `
                <tr>
                  <td>
                    <div class="cell-main">${escapeHtml(item.case_id || item.name)}</div>
                    <div class="cell-sub">${escapeHtml(item.step_id ? `Step ${item.step_id}` : `#${index + 1}`)}</div>
                  </td>
                  <td>
                    <div>${escapeHtml(item.Type || '-')}</div>
                    <div class="cell-sub">${escapeHtml(item.Language || '-')}</div>
                  </td>
                  <td><div class="prompt-cell">${escapeHtml(item.prompt)}</div></td>
                  <td>
                    <div class="keyword-cell">
                    <div class="cell-sub">Response: ${escapeHtml(item.expected_response || '-')}</div>
                    <div class="cell-sub">Expected: ${escapeHtml(item.expected_keywords || '-')}</div>
                    <div class="cell-sub">Forbidden: ${escapeHtml(item.forbidden_keywords || '-')}</div>
                    </div>
                  </td>
                  <td><span class="status-pill ${statusClass(item.status)}">${escapeHtml(item.status || 'NOT RUN')}</span></td>
                  <td>${escapeHtml(item.date || '-')}</td>
                  <td>
                    <div class="item-actions">
                      <button type="button" data-action="run" data-index="${index}">Run</button>
                      <button class="secondary" type="button" data-action="run-from" data-index="${index}">Từ đây</button>
                      <button class="secondary" type="button" data-action="use" data-index="${index}">Chat</button>
                      <button class="danger" type="button" data-action="delete" data-index="${index}">Xóa</button>
                    </div>
                  </td>
                </tr>
              `).join('')}
            </tbody>
          </table>
        </div>
      `;
      testcasePager.innerHTML = `
        <span>Hiển thị ${start + 1}-${Math.min(start + visibleEntries.length, entries.length)} / ${entries.length} testcase${entries.length !== testcases.length ? ` sau khi lọc từ ${testcases.length}` : ''}</span>
        <div class="pager-actions">
          <button class="secondary" type="button" data-page="prev" ${testcasePage <= 1 ? 'disabled' : ''}>Trước</button>
          <span>Trang ${testcasePage} / ${totalPages}</span>
          <button class="secondary" type="button" data-page="next" ${testcasePage >= totalPages ? 'disabled' : ''}>Sau</button>
        </div>
      `;
    }

    function renderResults() {
      const pass = results.filter((item) => resultParts(item).label === 'PASS').length;
      const fail = results.filter((item) => ['FAIL', 'ERROR'].includes(resultParts(item).label)).length;
      const skipped = results.filter((item) => resultParts(item).label === 'SKIPPED').length;
      const chat = results.filter((item) => resultParts(item).label === 'CHAT').length;
      const visibleResults = results.slice(0, RESULT_RENDER_LIMIT);
      resultSummary.innerHTML = `<strong>Tổng quan</strong>${results.length ? `${results.length} bản ghi đã lưu. Pass: ${pass}. Fail/Error: ${fail}. Skipped: ${skipped}. Chat: ${chat}.${results.length > visibleResults.length ? ` Đang hiển thị ${visibleResults.length} bản ghi mới nhất để giữ giao diện mượt.` : ''}` : 'Chưa có kết quả.'}`;
      if (!results.length) {
        resultList.innerHTML = '';
        return;
      }

      resultList.innerHTML = `
        <div class="table-wrap">
          <table class="data-table result-table">
            <thead>
              <tr>
                <th class="col-case">Case</th>
                <th class="col-prompt">Prompt</th>
                <th class="col-response">Actual response</th>
                <th class="col-result">Result</th>
                <th class="col-date">Time</th>
                <th class="col-actions">Logs</th>
              </tr>
            </thead>
            <tbody>
              ${visibleResults.map((item, index) => {
                const result = resultParts(item);
                return `
                <tr>
                  <td><div class="cell-main">${escapeHtml(item.case_id || item.name || `Run-${index + 1}`)}</div></td>
                  <td><div class="prompt-cell">${escapeHtml(item.prompt)}</div></td>
                  <td><div class="response-cell">${escapeHtml(item.output)}</div></td>
                  <td>
                    <div class="result-cell">
                      <span class="status-pill ${result.className}">${escapeHtml(result.label)}</span>
                      ${result.detail ? `<div class="result-detail ${result.className}">${escapeHtml(result.detail)}</div>` : ''}
                    </div>
                  </td>
                  <td>${escapeHtml(item.time)}</td>
                  <td><button class="secondary" type="button" data-action="logs" data-case="${escapeHtml(item.case_id || item.name || '')}">Xem log</button></td>
                </tr>
              `;
              }).join('')}
            </tbody>
          </table>
        </div>
      `;
    }

    function resultParts(item) {
      const raw = String(item.result || (item.kind === 'chat' ? 'CHAT' : (item.ok ? 'PASS' : 'FAIL')));
      const upper = raw.toLocaleUpperCase('en-US');
      const label = upper.startsWith('CHAT') ? 'CHAT' : (upper.startsWith('PASS') ? 'PASS' : (upper.startsWith('ERROR') ? 'ERROR' : (upper.startsWith('SKIPPED') ? 'SKIPPED' : 'FAIL')));
      const detail = raw.replace(/^(PASS|FAIL|ERROR|CHAT|SKIPPED)\s*:?\s*/i, '').trim();
      return {
        label,
        detail,
        className: label.toLocaleLowerCase('en-US')
      };
    }

    function addResult(prompt, output, ok = true, name = '', result = '', caseId = '', kind = 'testcase', meta = {}) {
      results.unshift({
        name: name || (ok ? 'Pass' : 'Error'),
        case_id: caseId || name || '',
        prompt,
        output,
        ok,
        kind,
        result: result || (ok ? 'PASS' : 'FAIL'),
        time: new Date().toLocaleString('vi-VN'),
        ...meta
      });
      saveResults();
      renderResults();
    }

    function escapeHtml(value) {
      return String(value)
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;')
        .replaceAll("'", '&#039;');
    }

	    function addBubble(kind, text, {forceScroll = false} = {}) {
	      const shouldStickToBottom = forceScroll || isConversationNearBottom();
	      if (!hasTurns) {
	        conversation.innerHTML = '';
	        hasTurns = true;
	      }
      const turn = document.createElement('div');
      turn.className = 'turn';
      const bubble = document.createElement('div');
      bubble.className = `bubble ${kind}`;
      const label = document.createElement('span');
      label.className = 'label';
      label.textContent = kind === 'user' ? 'Câu hỏi' : 'Phản hồi';
      bubble.appendChild(label);
	      bubble.appendChild(document.createTextNode(text));
	      turn.appendChild(bubble);
	      conversation.appendChild(turn);
	      if (shouldStickToBottom) {
	        conversation.scrollTop = conversation.scrollHeight;
	      }
	      updateJumpToLatest();
	      return bubble;
	    }

    function recordTestcaseResult(item, output, errorMessage = '', runToken = '') {
      if (!isCurrentTestcaseRun(item, runToken)) {
        appendLog('stale_response', {
          case_id: item.case_id || item.name,
          step: item.step_id || '',
          message: `Ignored stale response for ${item.case_id || item.name}`,
          prompt: item.prompt,
          actual_response: output || errorMessage || '',
          test_result: `STALE token=${runToken}`
        });
        return false;
      }
      const date = new Date().toLocaleString('vi-VN');
      const actual = output || errorMessage || '';
      if (errorMessage || isMissingResponseText(actual)) {
        return markMissingResponseResult(item, actual, runToken);
      }
      const evaluation = evaluateTestcase(item, actual);
      const resultExport = resultStatusAndNote(evaluation.result, evaluation.status);
      item.date = date;
      item.actual_response = actual;
      item.test_results = evaluation.result;
      item['test results'] = evaluation.result;
      item.Results = resultExport.status;
      item.note = resultExport.note;
      item.status = evaluation.status;
      item.RunTest = 'Y';
      item._active_run_token = '';
      lastRunTime = date;
      saveSourceInfo();
      saveTestcases();
      addResult(
        item.prompt,
        item.actual_response,
        evaluation.status === 'PASS',
        item.case_id || item.name,
        evaluation.result,
        item.case_id || item.name,
        'testcase',
        {run_token: runToken, worker_id: item.worker_id || ''}
      );
      appendLog('result', {
        case_id: item.case_id || item.name,
        step: item.step_id || '',
        message: evaluation.result,
        prompt: item.prompt,
        actual_response: item.actual_response,
        test_result: evaluation.result
      });
      renderTestcases();
      renderResults();
      return true;
    }

	    async function ask(question, options = {}) {
	      const showChat = options.showChat !== false;
	      if (showChat) addBubble('user', question, {forceScroll: true});
	      const waiting = showChat ? addBubble('robot', 'Đang chờ phản hồi...', {forceScroll: true}) : null;
	      const testcase = options.testcase || null;
	      const workerId = Number(options.workerId || 0);
      const runToken = String(options.runToken || '');
      const runAbortController = testcase ? new AbortController() : null;
      if (runAbortController) activeRunAbortControllers.add(runAbortController);
      if (!testcase) {
        await useChatLogSession();
      }
	      send.disabled = true;
	      send.textContent = 'Đang gửi';
	      voiceInput.disabled = true;
	      input.disabled = true;
      chatRequestInFlight = true;
      setStatus('Đang gửi câu hỏi', false);
      if (testcase) {
        appendLog('send', {
          case_id: testcase.case_id || testcase.name,
          step: testcase.step_id || '',
          message: `${workerId ? `WORKER ${workerId} ` : ''}SEND: ${question}`,
          prompt: question
        });
      } else {
        appendLog('chat_send', {
          message: `CHAT SEND: ${question}`,
          prompt: question
        });
      }
      try {
        const response = await fetch('/ask', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({question, workerId, requestId: runToken}),
          signal: runAbortController ? runAbortController.signal : undefined
        });
        const responseText = await response.text();
        let payload = {};
        try {
          payload = responseText ? JSON.parse(responseText) : {};
        } catch (_) {
          payload = {error: responseText || 'Response không phải JSON'};
        }
        if (!response.ok) {
          const requestError = new Error(payload.error || `Request failed HTTP ${response.status}`);
          requestError.status = response.status;
          requestError.payload = payload;
          requestError.body = responseText;
          throw requestError;
        }
	        const shouldFollowResponse = showChat && isConversationNearBottom(220);
	        const output = payload.output || '';
	        if (waiting) waiting.lastChild.textContent = output || '(Không có nội dung phản hồi)';
	        if (waiting) scrollConversationToLatest({force: shouldFollowResponse});
        if (testcase) {
          if (runToken && payload.request_id && payload.request_id !== runToken) {
            appendLog('stale_response', {
              case_id: testcase.case_id || testcase.name,
              step: testcase.step_id || '',
              message: `Ignored mismatched response token: ${payload.request_id}`,
              prompt: question,
              actual_response: output
            });
            return payload;
          }
          const deviceInfo = payload.device_id ? ` DEVICE ${payload.device_id}` : '';
          appendLog('response', {
            case_id: testcase.case_id || testcase.name,
            step: testcase.step_id || '',
            message: `${workerId ? `WORKER ${workerId} ` : ''}${deviceInfo} RESPONSE: ${output || '(Không có nội dung phản hồi)'}`,
            prompt: question,
            actual_response: output
          });
          recordTestcaseResult(testcase, output, '', runToken);
        } else {
          appendLog('chat_response', {
            message: `CHAT${payload.device_id ? ` DEVICE ${payload.device_id}` : ''} RESPONSE: ${output || '(Không có nội dung phản hồi)'}`,
            prompt: question,
            actual_response: output
          });
          addResult(question, output, true, 'Chat', 'CHAT: Manual conversation', 'Chat', 'chat');
        }
        setStatus('Sẵn sàng', true);
        return payload;
	      } catch (error) {
	        const shouldFollowError = showChat && isConversationNearBottom(220);
	        if (waiting) waiting.lastChild.textContent = `Lỗi: ${error.message}`;
	        if (waiting) scrollConversationToLatest({force: shouldFollowError});
        const stoppedByUser = testcase && runState.stopRequested && error.name === 'AbortError';
        if (testcase && stoppedByUser) {
          appendLog('stopped_current', {
            case_id: testcase.case_id || testcase.name,
            step: testcase.step_id || '',
            message: `${workerId ? `WORKER ${workerId} ` : ''}STOPPED: Người dùng bấm Dừng khi đang chờ phản hồi`,
            prompt: question
          });
          testcase.actual_response = '';
          testcase.test_results = '';
          testcase['test results'] = '';
          testcase.Results = '';
          testcase.note = '';
          testcase.status = 'NOT RUN';
          testcase.RunTest = '';
          testcase._active_run_token = '';
          saveTestcases();
          renderTestcases();
        } else if (testcase) {
          appendLog('error', {
            case_id: testcase.case_id || testcase.name,
            step: testcase.step_id || '',
            message: `${workerId ? `WORKER ${workerId} ` : ''}ERROR: ${error.message}`,
            prompt: question
          });
          recordTestcaseResult(testcase, '', error.message, runToken);
        } else {
          appendLog('chat_error', {
            message: `CHAT ERROR: ${error.message}`,
            prompt: question
          });
          addResult(question, error.message, true, 'Chat', `CHAT: Error - ${error.message}`, 'Chat', 'chat');
        }
        if (!stoppedByUser && (!testcase || options.showDiagnostic)) {
          showDiagnostic('Realtime request lỗi', error.message, {
            case_id: testcase ? (testcase.case_id || testcase.name || '') : '',
            step_id: testcase ? (testcase.step_id || '') : '',
            worker_id: workerId,
            http_status: error.status || '',
            payload: error.payload || '',
            response_body: error.body || '',
            prompt: question
          }, {
            case_id: testcase ? (testcase.case_id || testcase.name || '') : '',
            step: testcase ? (testcase.step_id || '') : ''
          });
        }
        setStatus(stoppedByUser ? 'Đã dừng runner' : 'Có lỗi khi gửi', stoppedByUser);
        throw error;
      } finally {
        if (runAbortController) activeRunAbortControllers.delete(runAbortController);
        chatRequestInFlight = false;
	        send.disabled = robotSleeping;
	        send.textContent = 'Gửi';
	        voiceInput.disabled = robotSleeping;
	        input.disabled = false;
	        input.focus();
	        resizeQuestionInput();
	      }
	    }

	    form.addEventListener('submit', (event) => {
	      event.preventDefault();
        if (robotSleeping) {
          setStatus('Robot đang ngủ, nhấn Wake robot để tiếp tục', false);
          return;
        }
	      const question = input.value.trim();
	      if (!question) return;
		      stopVoiceInput().catch((error) => setStatus(`Không dừng được mic: ${error.message}`, false));
	      input.value = '';
	      resizeQuestionInput();
	      ask(question).catch(() => {});
	    });

	    input.addEventListener('keydown', (event) => {
	      if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
	        event.preventDefault();
	        form.requestSubmit();
	      }
	    });

	    input.addEventListener('input', resizeQuestionInput);

		    voiceInput.addEventListener('click', () => {
          if (robotSleeping) {
            setStatus('Robot đang ngủ, nhấn Wake robot để dùng mic', false);
            return;
          }
		      if (voiceListening) {
		        stopVoiceInput().catch((error) => setStatus(`Không dừng được mic: ${error.message}`, false));
		      } else {
		        startVoiceInput().catch((error) => setStatus(`Không bật được mic: ${error.message}`, false));
		      }
		    });

      endRobotSection.addEventListener('click', () => setRobotPowerState('sleep'));
      wakeRobot.addEventListener('click', () => setRobotPowerState('wake'));

	    conversation.addEventListener('scroll', updateJumpToLatest, {passive: true});
	    window.addEventListener('resize', () => {
	      resizeQuestionInput();
	      updateJumpToLatest();
	    });

	    jumpToLatest.addEventListener('click', () => {
	      scrollConversationToLatest({force: true});
	      input.focus();
	    });

	    document.querySelectorAll('.quick').forEach((button) => {
	      button.addEventListener('click', () => {
	        input.value = button.textContent;
	        resizeQuestionInput();
	        form.requestSubmit();
	      });
	    });

    navButtons.forEach((button) => {
      button.addEventListener('click', () => setView(button.dataset.view));
    });

    document.querySelector('.brand-home').addEventListener('click', (event) => {
      event.preventDefault();
      setView('chat');
    });

    const initialView = location.hash.slice(1);
    if (views[initialView]) setView(initialView);

    document.getElementById('importTestcases').addEventListener('click', async () => {
      setBusy(true, 'Đang import testcase', 'Đang đọc nội dung nhập tay.');
      setBusyProgress(8);
      try {
        await yieldFrame();
        const imported = parseTestcases(testcaseImport.value);
        if (!imported.length) {
          showDiagnostic('Import testcase lỗi', 'Không tìm thấy testcase hợp lệ trong nội dung nhập tay.', {
            source: 'manual import',
            hint: 'Nhập JSON array, mỗi dòng một prompt, hoặc chọn file .xlsx theo mẫu testcase.',
            required_columns: 'case_id, step_id, Type, Language, prompt_text, expected_response, expected_keywords, forbidden_keywords'
          });
          return;
        }
        testcases = [...testcases, ...imported];
        testcasePage = 1;
        if (!runToCase.value) runToCase.value = testcases.length;
        syncBatchInputsFromRunRange();
        lastImportName = lastImportName || 'Manual import';
        testcaseLogSessionId = '';
        startLogSession(lastImportName, '', {kind: 'testcase'});
        saveTestcases();
        renderTestcases();
        updateSplitPreview();
        testcaseImport.value = '';
        saveSourceInfo();
        setStatus(`Đã import ${imported.length} testcase`, true);
        setBusyProgress(100, `Đã import ${imported.length} testcase.`);
      } catch (error) {
        showDiagnostic('Import testcase lỗi', error.message, {
          source: 'manual import',
          stack: error.stack || ''
        });
      } finally {
        setBusy(false);
      }
    });

    testcaseFile.addEventListener('change', async () => {
      const file = testcaseFile.files && testcaseFile.files[0];
      if (!file) return;
      selectedFileName.textContent = file.name;
      setBusy(true, 'Đang tải file', `${file.name} · ${(file.size / 1024 / 1024).toFixed(2)} MB`);
      setBusyProgress(5);
      try {
        if (file.name.toLocaleLowerCase('en-US').endsWith('.xlsx')) {
          setStatus('Đang đọc file Excel', false);
          setBusyProgress(12, 'Đang đọc cấu trúc Excel.');
          await yieldFrame();
          const response = await fetch('/import-xlsx', {
            method: 'POST',
            headers: {
              'Content-Type': 'application/octet-stream',
              'X-Filename': encodeURIComponent(file.name)
            },
            body: await file.arrayBuffer()
          });
          const payload = await response.json();
          if (!response.ok) {
            showDiagnostic('Import Excel lỗi', payload.error || 'Không import được Excel', {
              file: file.name,
              ...(payload.diagnostics || {})
            });
            return;
          }
          testcases = await normalizeRecordsAsync(payload.testcases, 'Đang chuẩn hóa Excel');
          if (!testcases.length) {
            showDiagnostic('Import Excel lỗi', 'File Excel đọc được nhưng không có testcase chạy được.', {
              file: file.name,
              ...(payload.diagnostics || {}),
              hint: 'Kiểm tra cột prompt_text có dữ liệu ở các dòng testcase.'
            });
            return;
          }
          if (payload.diagnostics && (payload.diagnostics.missing_columns?.length || payload.diagnostics.skipped_rows)) {
            showDiagnostic('Diagnostic import Excel', 'File đã import nhưng có điểm cần kiểm tra.', {
              file: file.name,
              ...payload.diagnostics
            });
          }
          testcasePage = 1;
          if (!runFromCase.value) runFromCase.value = 1;
          runToCase.value = testcases.length;
          syncBatchInputsFromRunRange();
          lastImportName = file.name;
          const importedSessionId = testcases
            .map((item) => item.log_session_id || item.session_id || '')
            .find((value) => String(value || '').trim());
          if (importedSessionId) {
            await loadLogSession(String(importedSessionId).trim());
            activeLogSessionName = file.name;
            testcaseLogSessionId = activeLogSessionId;
            saveContextSessionInfo();
            saveLogSessionInfo();
          } else {
            testcaseLogSessionId = '';
            startLogSession(file.name, '', {kind: 'testcase'});
          }
          saveSourceInfo();
          saveTestcases();
          renderTestcases();
          updateSplitPreview();
          setStatus(`Đã import ${testcases.length} testcase từ Excel`, true);
          setBusyProgress(100, `Đã import ${testcases.length} testcase từ Excel.`);
        } else {
          testcaseImport.value = await file.text();
          lastImportName = file.name;
          setBusyProgress(100, 'Đã nạp nội dung file vào ô import.');
          setStatus('Đã nạp nội dung file vào ô import', true);
        }
        saveSourceInfo();
        renderTestcases();
      } catch (error) {
        showDiagnostic('Import file lỗi', error.message, {
          file: file.name,
          stack: error.stack || ''
        });
      } finally {
        setBusy(false);
        testcaseFile.value = '';
      }
    });

    testcaseSearch.addEventListener('input', () => {
      testcasePage = 1;
      renderTestcases();
    });
    testcasePager.addEventListener('click', (event) => {
      const button = event.target.closest('button[data-page]');
      if (!button || button.disabled) return;
      testcasePage += button.dataset.page === 'next' ? 1 : -1;
      renderTestcases();
    });
    logSearch.addEventListener('input', renderLogs);

    testcaseList.addEventListener('click', async (event) => {
      const button = event.target.closest('button[data-action]');
      if (!button) return;
      const index = Number(button.dataset.index);
      const item = testcases[index];
      if (!item) return;
      if (runState.running && button.dataset.action !== 'use') return;

      if (button.dataset.action === 'delete') {
        testcases.splice(index, 1);
        saveTestcases();
        renderTestcases();
        return;
      }

	      if (button.dataset.action === 'use') {
	        input.value = item.prompt;
	        resizeQuestionInput();
	        setView('chat');
	        input.focus();
	        return;
	      }

      if (button.dataset.action === 'run-from') {
        await runFromIndex(index, item.case_id || item.name || `#${index + 1}`);
        return;
      }

      if (button.dataset.action === 'run') {
        await useTestcaseLogSession(lastImportName || 'Single testcase');
        prepareEntriesForRerun([{item, index}]);
        runState = {running: true, stopRequested: false, stopReason: '', total: 1, completed: 0, current: item.case_id || item.name, startedAt: Date.now()};
        updateProgress();
        appendLog('queued', {
          case_id: item.case_id || item.name,
          step: item.step_id || '',
          message: `Queued single testcase ${item.case_id || item.name}`,
          prompt: item.prompt
        });
        const runToken = makeRunToken(item, index, 0);
        markTestcaseRunning(item, runToken);
        renderTestcases();
        await ask(item.prompt, {testcase: item, showChat: false, showDiagnostic: true, runToken}).catch(() => {});
        if (!runState.stopRequested) {
          runState.completed = 1;
          rememberAverageMsPerCase(Date.now() - runState.startedAt);
        }
        runState.running = false;
        runState.stopRequested = false;
        runState.stopReason = '';
        updateProgress();
      }
    });

    async function runTestcaseEntries(entries, modeLabel, options = {}) {
      if (!entries.length) {
        showDiagnostic('Runner không chạy', 'Không có testcase cần chạy từ checkpoint.', {
          mode: modeLabel,
          total_imported: testcases.length,
          from: options.fromIndex !== undefined ? Number(options.fromIndex) + 1 : '',
          to: options.toIndex !== undefined ? Number(options.toIndex) + 1 : ''
        });
        updateProgress();
        return;
      }
      if (options.replaceResults) prepareEntriesForRerun(entries);
      await useTestcaseLogSession(lastImportName || 'Manual session');
      const firstEntryIndex = entries[0].index;
      const lastEntryIndex = entries[entries.length - 1].index;
      runSessionState = normalizeRunSessionState({
        sessionId: activeLogSessionId,
        sourceName: activeLogSessionName || lastImportName || 'Manual session',
        from: Number(options.fromIndex ?? firstEntryIndex) + 1,
        to: Number(options.toIndex ?? lastEntryIndex) + 1,
        nextIndex: firstEntryIndex,
        status: 'running',
        threads: lockedThreadValue()
      });
      saveRunSessionState(runSessionState, {sync: false});
      refreshRunSetupState();
      runState = {running: true, stopRequested: false, stopReason: '', total: entries.length, completed: 0, current: '', startedAt: Date.now()};
      appendLog('run_start', {message: `Start ${modeLabel}: ${entries.length} testcase(s)`});
      updateProgress();
      setView('testcases');
      let pausedByRealtime = false;
      let entryOffset = 0;
      while (entryOffset < entries.length) {
        const {item, index: itemIndex} = entries[entryOffset];
        if (runState.stopRequested) {
          const nextEntry = entries[entryOffset];
          runSessionState = normalizeRunSessionState({
            ...runSessionState,
            nextIndex: nextEntry ? nextEntry.index : lastEntryIndex + 1,
            status: 'stopped',
            reason: runState.stopReason || 'user'
          });
          saveRunSessionState(runSessionState, {sync: false});
          appendLog('run_stop', {message: `Stopped at ${runState.completed}/${runState.total} testcase(s)`, reason: runState.stopReason || 'user'});
          break;
        }
        runState.current = item.case_id || item.name || `#${entryOffset + 1}`;
        updateProgress();
        appendLog('queued', {
          case_id: item.case_id || item.name,
          step: item.step_id || '',
          message: `Queued ${entryOffset + 1}/${entries.length}: ${item.case_id || item.name}`,
          prompt: item.prompt
        });
        const runToken = makeRunToken(item, itemIndex, 0);
        markTestcaseRunning(item, runToken);
        renderTestcases();
        let runError = null;
        try {
          await ask(item.prompt, {testcase: item, showChat: false, runToken});
        } catch (error) {
          runError = error;
        }
        if (runState.stopRequested) {
          runSessionState = normalizeRunSessionState({
            ...runSessionState,
            nextIndex: itemIndex,
            status: 'stopped',
            reason: runState.stopReason || 'user'
          });
          saveRunSessionState(runSessionState, {sync: false});
          appendLog('run_stop', {message: `Stopped at ${runState.completed}/${runState.total} testcase(s)`, reason: runState.stopReason || 'user'});
          break;
        }

        runState.completed = entryOffset + 1;
        runSessionState = normalizeRunSessionState({
          ...runSessionState,
          nextIndex: itemIndex + 1,
          status: 'running',
          reason: ''
        });
        saveRunSessionState(runSessionState, {sync: false});
        updateProgress();
        entryOffset += 1;
      }
      let stopped = runState.stopRequested;
      if (!stopped && !pausedByRealtime) {
        await rerunMissingResponseEntries(entries, modeLabel);
        stopped = runState.stopRequested;
      }
      if (runState.completed > 0 && runState.startedAt) {
        rememberAverageMsPerCase((Date.now() - runState.startedAt) / runState.completed);
      }
      runState.running = false;
      runState.stopRequested = false;
      if (!pausedByRealtime && !stopped) {
        runSessionState = normalizeRunSessionState({
          ...runSessionState,
          nextIndex: lastEntryIndex + 1,
          status: 'done',
          reason: ''
        });
      } else if (stopped) {
        runSessionState = normalizeRunSessionState({
          ...runSessionState,
          status: 'stopped',
          reason: runState.stopReason || 'user'
        });
      }
      saveRunSessionState(runSessionState, {sync: false});
      appendLog(
        pausedByRealtime ? 'run_paused' : (stopped ? 'run_stopped' : 'run_done'),
        {message: `${pausedByRealtime ? 'Paused' : (stopped ? 'Stopped' : 'Completed')} ${runState.completed}/${runState.total} testcase(s)`, reason: stopped ? runState.stopReason || 'user' : ''}
      );
      saveLogSessionToServer();
      runState.stopReason = '';
      updateProgress();
      if (!pausedByRealtime) setView('results');
    }

    async function runThreadedTestcaseEntries(entries, modeLabel, options = {}) {
      if (!entries.length) {
        showDiagnostic('Runner không chạy', 'Không có testcase cần chạy.', {
          mode: modeLabel,
          total_imported: testcases.length,
          threads: lockedThreadValue()
        });
        updateProgress();
        return;
      }
      const threads = Math.min(lockedThreadValue(), entries.length);
      if (options.replaceResults) prepareEntriesForRerun(entries);
      await useTestcaseLogSession(lastImportName || 'Manual session');
      const firstEntryIndex = entries[0].index;
      const lastEntryIndex = entries[entries.length - 1].index;
      runSessionState = normalizeRunSessionState({
        sessionId: activeLogSessionId,
        sourceName: activeLogSessionName || lastImportName || 'Manual session',
        from: Number(options.fromIndex ?? firstEntryIndex) + 1,
        to: Number(options.toIndex ?? lastEntryIndex) + 1,
        nextIndex: firstEntryIndex,
        status: 'running_parallel',
        threads
      });
      saveRunSessionState(runSessionState, {sync: false});
      runState = {running: true, stopRequested: false, stopReason: '', total: entries.length, completed: 0, current: `${threads} luồng`, startedAt: Date.now()};
      appendLog('run_start', {message: `Start ${modeLabel}: ${entries.length} testcase(s), threads=${threads}`});
      updateProgress();
      setView('testcases');

      let cursor = 0;
      let completed = 0;
      let pauseIndex = null;
      let pauseMessage = '';
      const requeuedEntries = [];
      const inFlightWorkers = new Map();
      const MAX_WORKER_CRASH_REQUEUE = 3;
      const nextIncompleteIndex = () => {
        const pending = entries.find(({item}) => {
          const status = String(item.status || 'NOT RUN').toLocaleUpperCase('en-US');
          return !['PASS', 'FAIL', 'ERROR', 'SKIPPED'].includes(status);
        });
        return pending ? pending.index : lastEntryIndex + 1;
      };
      const updateThreadCurrent = () => {
        const active = Array.from(inFlightWorkers.entries())
          .sort(([left], [right]) => left - right)
          .map(([workerId, item]) => `luồng ${workerId}: ${item.case_id || item.name || '#'}`)
          .join(' | ');
        runState.current = active ? `${inFlightWorkers.size}/${threads} luồng · ${active}` : `${threads} luồng`;
        updateProgress();
      };
      const requeueWorkerEntry = (entry, workerId, error) => {
        if (!entry || runState.stopRequested) return false;
        const {item, index: itemIndex} = entry;
        const crashCount = Number(item._worker_crash_count || 0) + 1;
        item._worker_crash_count = crashCount;
        const message = error && error.message ? error.message : String(error || 'Unknown worker error');
        try {
          appendLog('worker_retry', {
            case_id: item.case_id || item.name,
            step: item.step_id || '',
            message: `Thread ${workerId} retry ${crashCount}/${MAX_WORKER_CRASH_REQUEUE}: ${message}`,
            prompt: item.prompt
          });
        } catch (logError) {
          console.warn('Không ghi được worker_retry log', logError);
        }
        if (crashCount <= MAX_WORKER_CRASH_REQUEUE) {
          item.actual_response = '';
          item.test_results = '';
          item['test results'] = '';
          item.Results = '';
          item.note = '';
          item.status = 'NOT RUN';
          item.RunTest = '';
          item._active_run_token = '';
          requeuedEntries.unshift({item, index: itemIndex});
          try {
            saveTestcases();
            renderTestcases();
          } catch (renderError) {
            console.warn('Không cập nhật được UI khi requeue worker', renderError);
          }
          return true;
        }

        const errorResult = `ERROR: Worker ${workerId} lỗi nội bộ sau ${MAX_WORKER_CRASH_REQUEUE} lần retry`;
        item.actual_response = message;
        item.test_results = errorResult;
        item['test results'] = errorResult;
        item.Results = 'ERROR';
        item.note = message;
        item.status = 'ERROR';
        item.RunTest = 'Y';
        item._active_run_token = '';
        try {
          saveTestcases();
          renderTestcases();
          appendLog('worker_error', {
            case_id: item.case_id || item.name,
            step: item.step_id || '',
            message: `${errorResult}: ${message}`,
            prompt: item.prompt,
            actual_response: message,
            test_result: errorResult
          });
        } catch (finalizeError) {
          console.warn('Không ghi được worker_error log', finalizeError);
        }
        return false;
      };
      const nextEntry = () => {
        if (runState.stopRequested || pauseIndex !== null) return null;
        if (requeuedEntries.length) return requeuedEntries.shift();
        if (cursor >= entries.length) return null;
        const entry = entries[cursor];
        cursor += 1;
        return entry;
      };

      async function worker(workerId) {
        while (true) {
          const entry = nextEntry();
          if (!entry) return;
          const {item, index: itemIndex} = entry;
          let shouldCountCompleted = false;
          try {
            if (!runState.stopRequested) {
              inFlightWorkers.set(workerId, item);
              updateThreadCurrent();
              item._worker_crash_count = Number(item._worker_crash_count || 0);
              appendLog('queued', {
                case_id: item.case_id || item.name,
                step: item.step_id || '',
                message: `Thread ${workerId} queued: ${item.case_id || item.name}`,
                prompt: item.prompt
              });
              const runToken = makeRunToken(item, itemIndex, workerId);
              markTestcaseRunning(item, runToken);
              item.worker_id = workerId;
              renderTestcases();
              updateProgress();
              try {
                await ask(item.prompt, {testcase: item, showChat: false, workerId, runToken});
              } catch (_) {}
              shouldCountCompleted = true;
            }
          } catch (error) {
            const requeued = requeueWorkerEntry(entry, workerId, error);
            inFlightWorkers.delete(workerId);
            updateThreadCurrent();
            if (requeued) continue;
            shouldCountCompleted = true;
          }
          inFlightWorkers.delete(workerId);
          updateThreadCurrent();
          if (runState.stopRequested) return;
          if (shouldCountCompleted) {
            completed += 1;
            runState.completed = completed;
            runSessionState = normalizeRunSessionState({
              ...runSessionState,
              nextIndex: pauseIndex === null ? nextIncompleteIndex() : pauseIndex,
              status: pauseIndex === null ? 'running_parallel' : 'paused',
              reason: pauseIndex === null ? '' : 'retry_limit'
            });
            saveRunSessionState(runSessionState, {sync: false});
            updateProgress();
          }
        }
      }

      await Promise.allSettled(Array.from({length: threads}, (_, index) => worker(index + 1)));
      let stopped = runState.stopRequested && pauseIndex === null;
      if (!stopped && pauseIndex === null) {
        await rerunMissingResponseEntries(entries, modeLabel);
        stopped = runState.stopRequested && pauseIndex === null;
      }
      if (runState.completed > 0 && runState.startedAt) {
        rememberAverageMsPerCase(((Date.now() - runState.startedAt) / runState.completed) * threads);
      }
      runState.running = false;
      runState.stopRequested = false;
      if (pauseIndex !== null) {
        runSessionState = normalizeRunSessionState({
          ...runSessionState,
          nextIndex: pauseIndex,
          status: 'paused',
          reason: 'retry_limit'
        });
        appendLog('run_paused', {message: pauseMessage || `Paused at #${pauseIndex + 1}`});
        const pausedItem = testcases[pauseIndex] || {};
        showDiagnostic('Runner tạm dừng', 'Runner đã tạm dừng.', {
          case_id: pausedItem.case_id || pausedItem.name || `#${pauseIndex + 1}`,
          step_id: pausedItem.step_id || '',
          threads,
          message: pauseMessage || `Paused at #${pauseIndex + 1}`,
          prompt: pausedItem.prompt || '',
          action: 'Admin kiểm tra backend/realtime rồi bấm Chạy tiếp.'
        }, {case_id: pausedItem.case_id || pausedItem.name || '', step: pausedItem.step_id || ''});
      } else if (stopped) {
        runSessionState = normalizeRunSessionState({
          ...runSessionState,
          nextIndex: nextIncompleteIndex(),
          status: 'stopped',
          reason: runState.stopReason || 'user'
        });
        appendLog('run_stopped', {message: `Stopped ${runState.completed}/${runState.total} testcase(s)`, reason: runState.stopReason || 'user'});
      } else {
        runSessionState = normalizeRunSessionState({
          ...runSessionState,
          nextIndex: lastEntryIndex + 1,
          status: 'done',
          reason: ''
        });
        appendLog('run_done', {message: `Completed ${runState.completed}/${runState.total} testcase(s) with ${threads} thread(s)`});
      }
      saveRunSessionState(runSessionState, {sync: false});
      saveLogSessionToServer();
      runState.stopReason = '';
      updateProgress();
      renderTestcases();
      renderResults();
      if (pauseIndex === null && !stopped) setView('results');
    }

    async function runConfiguredTestcaseEntries(entries, modeLabel, options = {}) {
      if (lockedThreadValue() <= 1) {
        await runTestcaseEntries(entries, modeLabel, options);
        return;
      }
      await runThreadedTestcaseEntries(entries, modeLabel, options);
    }

    async function runAllTestcases() {
      if (!testcases.length) {
        showDiagnostic('Runner không chạy', 'Chưa có testcase để chạy.', {
          action: 'Import file testcase trước khi bấm Chạy tất cả.',
          required_columns: 'case_id, step_id, Type, Language, prompt_text, expected_response, expected_keywords, forbidden_keywords'
        });
        return;
      }
      runFromCase.value = 1;
      runToCase.value = testcases.length;
      saveRunSetup();
      await runConfiguredTestcaseEntries(testcases.map((item, index) => ({item, index})), 'all', {replaceResults: true, fromIndex: 0, toIndex: testcases.length - 1});
    }

    async function runRangeTestcases() {
      if (!testcases.length) {
        showDiagnostic('Runner không chạy', 'Chưa có testcase để chạy.', {
          action: 'Import file testcase trước khi chạy range.'
        });
        return;
      }
      const bounds = rangeBounds();
      if (bounds.from < 1 || bounds.to > testcases.length || bounds.from > bounds.to) {
        showDiagnostic('Range testcase không hợp lệ', 'Khoảng case không hợp lệ.', {
          from: bounds.from,
          to: bounds.to,
          total_imported: testcases.length,
          hint: `Nhập From/To trong khoảng 1-${testcases.length}.`
        });
        return;
      }
      saveRunSetup();
      const entries = testcases
        .slice(bounds.from - 1, bounds.to)
        .map((item, offset) => ({item, index: bounds.from - 1 + offset}));
      await runConfiguredTestcaseEntries(entries, `range #${bounds.from}-#${bounds.to}`, {replaceResults: true, fromIndex: bounds.from - 1, toIndex: bounds.to - 1});
    }

    async function reviewFailedTestcases() {
      if (!testcases.length) {
        showDiagnostic('Rà soát fail', 'Chưa có testcase để rà soát.', {
          action: 'Import file testcase trước.'
        });
        return;
      }
      const entries = failedReviewEntries();
      if (!entries.length) {
        showDiagnostic('Rà soát fail', 'Không có case FAIL/ERROR/SKIPPED cần chạy lại.', {
          total_imported: testcases.length
        });
        updateProgress();
        return;
      }
      const firstIndex = Math.min(...entries.map(({index}) => index));
      const lastIndex = Math.max(...entries.map(({index}) => index));
      await runConfiguredTestcaseEntries(entries, `review failed (${entries.length})`, {
        replaceResults: true,
        fromIndex: firstIndex,
        toIndex: lastIndex
      });
    }

    async function resumeTestcases() {
      await useTestcaseLogSession(lastImportName || 'Testcase run');
      const checkpoint = activeLogSessionId ? currentRunSessionState() : null;
      if (checkpoint && isResumableRunStatus(checkpoint.status) && checkpoint.nextIndex < checkpoint.to && checkpoint.nextIndex < testcases.length) {
        const startIndex = Math.max(checkpoint.nextIndex, checkpoint.from - 1, 0);
        const endIndex = Math.min(checkpoint.to - 1, testcases.length - 1);
        if (startIndex <= endIndex) {
          runFromCase.value = checkpoint.from;
          runToCase.value = checkpoint.to;
          if (checkpoint.threads) {
            lockedThreadCount = normalizeThreadValue(checkpoint.threads);
            updateThreadLockUi();
          }
          const entries = testcases
            .slice(startIndex, endIndex + 1)
            .map((item, offset) => ({item, index: startIndex + offset}));
          await runConfiguredTestcaseEntries(entries, `resume session #${startIndex + 1}-#${endIndex + 1}`, {replaceResults: true, fromIndex: checkpoint.from - 1, toIndex: checkpoint.to - 1});
          return;
        }
      }
      const startIndex = firstCheckpointIndex();
      if (startIndex < 0) {
        showDiagnostic('Không có checkpoint', 'Không có case lỗi hoặc chưa chạy để tiếp tục.', {
          total_imported: testcases.length,
          action: 'Dùng Chạy tất cả hoặc chọn một case cụ thể để chạy lại.'
        });
        updateProgress();
        return;
      }
      const item = testcases[startIndex];
      await runFromIndex(startIndex, item.case_id || item.name || `#${startIndex + 1}`);
    }

    async function runFromIndex(startIndex, label = '') {
      const entries = entriesFromIndex(startIndex);
      runFromCase.value = startIndex + 1;
      runToCase.value = testcases.length;
      saveRunSetup();
      await runConfiguredTestcaseEntries(entries, `from ${label || `#${startIndex + 1}`}`, {replaceResults: true, fromIndex: startIndex, toIndex: testcases.length - 1});
    }

    function requestStopRun() {
      if (!runState.running) return;
      runState.stopRequested = true;
      runState.stopReason = 'user';
      activeRunAbortControllers.forEach((controller) => controller.abort());
      appendLog('stop_requested', {message: 'User requested stop. Runner is stopping current request(s).'});
      updateProgress();
    }

    document.getElementById('runAll').addEventListener('click', runAllTestcases);
    document.getElementById('runAllSide').addEventListener('click', runAllTestcases);
    document.getElementById('resumeRun').addEventListener('click', resumeTestcases);
    document.getElementById('resumeRunSide').addEventListener('click', resumeTestcases);
    document.getElementById('reviewFailed').addEventListener('click', () => reviewFailedTestcases().catch(() => {}));
    document.getElementById('reviewFailedFromResults').addEventListener('click', () => reviewFailedTestcases().catch(() => {}));
    document.getElementById('runRange').addEventListener('click', () => runRangeTestcases().catch(() => {}));
    [runFromCase, runToCase].forEach((input) => {
      input.addEventListener('change', () => {
        saveRunSetup();
        syncBatchInputsFromRunRange();
      });
      input.addEventListener('input', updateRunEstimate);
    });
    runThreadCount.addEventListener('input', () => {
      const value = normalizeThreadValue(runThreadCount.value);
      threadSettingsHint.textContent = `Sẵn sàng khóa ${value} luồng. Bấm Khóa luồng để áp dụng.`;
    });
    document.getElementById('lockThreadCount').addEventListener('click', lockThreadCount);
    document.getElementById('openRunSettings').addEventListener('click', (event) => {
      event.preventDefault();
      event.stopPropagation();
      openRunSettingsModal();
    });
    document.getElementById('closeRunSettings').addEventListener('click', closeRunSettingsModal);
    document.getElementById('cancelRunSettings').addEventListener('click', closeRunSettingsModal);
    document.getElementById('closeDiagnostic').addEventListener('click', closeDiagnosticModal);
    document.getElementById('cancelDiagnostic').addEventListener('click', closeDiagnosticModal);
    document.getElementById('openDiagnosticLogs').addEventListener('click', () => {
      closeDiagnosticModal();
      setView('logs');
    });
    runSettingsModal.addEventListener('click', (event) => {
      if (event.target === runSettingsModal) closeRunSettingsModal();
    });
    diagnosticModal.addEventListener('click', (event) => {
      if (event.target === diagnosticModal) closeDiagnosticModal();
    });
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape' && runSettingsModal.classList.contains('active')) {
        closeRunSettingsModal();
      }
      if (event.key === 'Escape' && diagnosticModal.classList.contains('active')) {
        closeDiagnosticModal();
      }
    });
    stopRun.addEventListener('click', requestStopRun);
    stopRunSide.addEventListener('click', requestStopRun);

    function exportRows(sourceItems = testcases) {
      return sourceItems.map((item, index) => {
        const row = {...item};
        const status = String(item.status || 'NOT RUN').toLocaleUpperCase('en-US');
        const resultExport = resultStatusAndNote(
          row.Results || row.test_results || row['test results'] || status,
          status
        );
        row.prompt_text = row.prompt_text || row.prompt || '';
        row.actual_response = row.actual_response || '';
        row.date = row.date || '';
        row.log_session_id = activeLogSessionId || '';
        row.Results = resultExport.status;
        row.note = row.note || resultExport.note;
        row['test results'] = row.test_results || row['test results'] || row.Results;
        if (status === 'RUNNING' && !row.actual_response) {
          row.actual_response = 'Đang chờ phản hồi tại thời điểm export';
          row.Results = 'RUNNING';
          row.note = row.note || 'Đang chờ phản hồi';
          row['test results'] = 'RUNNING: Đang chờ phản hồi';
        } else if (status === 'NOT RUN' && !row.Results) {
          row.Results = 'NOT RUN';
        }
        delete row.name;
        delete row.prompt;
        delete row.status;
        delete row._row;
        delete row.run_index;
        delete row.runIndex;
        delete row.run_status;
        delete row.exported_at;
        delete row.export_snapshot;
        delete row.source;
        delete row.sourceName;
        delete row.source_name;
        delete row.Source;
        delete row['Nguồn'];
        delete row['nguồn'];
        delete row.Nguon;
        delete row.nguon;
        delete row.original_index;
        return row;
      });
    }

    function splitExportRows(entries) {
      const runtimeFields = new Set([
        'date',
        'actual_response',
        'test_results',
        'test results',
        'RunTest',
        'log_session_id',
        'session_id',
        'Results',
        'note',
        'run_index',
        'run_status',
        'exported_at',
        'export_snapshot',
        'worker_id'
      ]);
      return entries.map(({item, index}) => {
        const row = {...item};
        row.original_index = index + 1;
        row.prompt_text = row.prompt_text || row.prompt || '';
        runtimeFields.forEach((key) => delete row[key]);
        delete row.name;
        delete row.prompt;
        delete row.status;
        delete row._row;
        return row;
      });
    }

    async function exportResultFile() {
      if (!testcases.length) {
        setStatus('Chưa có testcase để xuất Excel', false);
        return;
      }
      setStatus('Đang tạo file Excel kết quả', false);
      await useTestcaseLogSession(lastImportName || 'Export session');
      const response = await fetch('/export-xlsx', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          sourceName: lastImportName || 'testcases.xlsx',
          testcases: exportRows(),
          logs: testLogs
        })
      });
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        setStatus(payload.error || 'Không xuất được Excel', false);
        return;
      }
      const blob = await response.blob();
      const disposition = response.headers.get('Content-Disposition') || '';
      const match = disposition.match(/filename="([^"]+)"/);
      const filename = match ? match[1] : 'result_testcases.xlsx';
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = filename;
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
      appendLog('export', {message: `Exported result workbook: ${filename}`});
      setStatus(`Đã xuất ${filename}`, true);
    }

    document.getElementById('exportResults').addEventListener('click', exportResultFile);
    document.getElementById('exportResultsFromResults').addEventListener('click', exportResultFile);
    document.getElementById('exportResultsSide').addEventListener('click', exportResultFile);

    async function exportManualChatFile() {
      const activeHasChatLogs = testLogs.some((item) => String((item && item.event) || '').startsWith('chat_'));
      const activeIsChat = activeHasChatLogs || isChatSession({id: activeLogSessionId, source_name: activeLogSessionName});
      const sessionId = activeIsChat ? activeLogSessionId : chatLogSessionId;
      const sourceName = activeIsChat ? (activeLogSessionName || 'Manual chat') : 'Manual chat';
      const logs = activeIsChat ? testLogs : [];
      if (!sessionId && !logs.length) {
        setStatus('Chưa có manual chat session để xuất Excel', false);
        return;
      }
      setStatus('Đang tạo file Excel manual chat', false);
      const response = await fetch('/export-chat-xlsx', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          sessionId,
          sourceName,
          logs
        })
      });
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        setStatus(payload.error || 'Không xuất được Excel chat', false);
        return;
      }
      const blob = await response.blob();
      const disposition = response.headers.get('Content-Disposition') || '';
      const match = disposition.match(/filename="([^"]+)"/);
      const filename = match ? match[1] : 'manual_chat.xlsx';
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = filename;
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
      if (activeIsChat) {
        appendLog('chat_export', {message: `Exported manual chat workbook: ${filename}`});
      }
      setStatus(`Đã xuất ${filename}`, true);
    }

    document.getElementById('exportChatXlsx').addEventListener('click', () => exportManualChatFile().catch((error) => setStatus(`Export chat lỗi: ${error.message}`, false)));
    document.getElementById('exportChatSide').addEventListener('click', () => exportManualChatFile().catch((error) => setStatus(`Export chat lỗi: ${error.message}`, false)));

    function updateSplitPreview() {
      if (!testcases.length) {
        splitPreview.textContent = 'Chưa import testcase.';
        splitMetricTotal.textContent = '0';
        splitMetricSelected.textContent = '0';
        splitMetricInvalid.textContent = '0';
        return {entries: [], errors: []};
      }
      const parsed = parseSplitRangeSpec(splitRangeInput.value);
      const first = parsed.entries[0];
      const last = parsed.entries[parsed.entries.length - 1];
      const rangeText = first && last ? ` từ #${first.index + 1} đến #${last.index + 1}` : '';
      const errorText = parsed.errors.length ? ` Bỏ qua range không hợp lệ: ${parsed.errors.join(', ')}.` : '';
      splitPreview.textContent = parsed.entries.length
        ? `Sẽ export ${parsed.entries.length} testcase${rangeText}.${errorText}`
        : `Chưa chọn được testcase hợp lệ.${errorText}`;
      splitMetricTotal.textContent = testcases.length;
      splitMetricSelected.textContent = parsed.entries.length;
      splitMetricInvalid.textContent = parsed.errors.length;
      return parsed;
    }

    async function exportSplitFile() {
      const {entries, errors} = updateSplitPreview();
      if (!entries.length) {
        setStatus(errors.length ? 'Range split không hợp lệ' : 'Chưa có testcase để split', false);
        return;
      }
      setStatus('Đang tạo file split testcase', false);
      await useTestcaseLogSession(lastImportName || 'Split testcase');
      const rows = splitExportRows(entries);
      const sourceName = `split_${splitRangeInput.value || 'range'}_${lastImportName || 'testcases.xlsx'}`;
      const response = await fetch('/export-xlsx', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          sourceName,
          sheetName: 'Split TCs',
          plain: true,
          testcases: rows,
          logs: []
        })
      });
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        setStatus(payload.error || 'Không xuất được file split', false);
        return;
      }
      const blob = await response.blob();
      const disposition = response.headers.get('Content-Disposition') || '';
      const match = disposition.match(/filename="([^"]+)"/);
      const filename = match ? match[1] : 'split_testcases.xlsx';
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = filename;
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
      appendLog('split_export', {message: `Exported split workbook: ${filename} (${entries.length} testcase)`});
      setStatus(`Đã export split ${entries.length} testcase`, true);
    }

    document.getElementById('previewSplit').addEventListener('click', updateSplitPreview);
    document.getElementById('exportSplit').addEventListener('click', () => exportSplitFile().catch((error) => setStatus(`Split lỗi: ${error.message}`, false)));
    splitRangeInput.addEventListener('input', updateSplitPreview);

    document.getElementById('checkSingleHms').addEventListener('click', async () => {
      const row = makeHmsRow({
        confirmationNumber: hmsConfirmationNumber.value.trim(),
        lastName: hmsLastName.value.trim(),
        case_id: 'Manual HMS'
      });
      if (!validHmsRow(row)) {
        setStatus('Nhập confirmationNumber và lastName để check HMS', false);
        return;
      }
      hmsRows.unshift(row);
      saveHmsRows();
      renderHmsRows();
      await checkHmsRow(0);
    });

    document.getElementById('loadHmsFromTestcases').addEventListener('click', loadHmsRowsFromTestcases);
    document.getElementById('checkAllHms').addEventListener('click', () => checkAllHmsRows().catch((error) => setStatus(`Check HMS lỗi: ${error.message}`, false)));
    document.getElementById('clearHmsRows').addEventListener('click', () => {
      hmsRows = [];
      saveHmsRows();
      renderHmsRows();
      setStatus('Đã xóa data HMS', true);
    });
    document.getElementById('importHmsRows').addEventListener('click', () => {
      const rows = parseHmsRows(hmsDataInput.value);
      if (!rows.length) {
        setStatus('Không tìm thấy dòng HMS hợp lệ', false);
        return;
      }
      hmsRows = [...hmsRows, ...rows];
      hmsDataInput.value = '';
      saveHmsRows();
      renderHmsRows();
      setStatus(`Đã import ${rows.length} dòng HMS`, true);
    });

    hmsFile.addEventListener('change', async (event) => {
      const file = event.target.files && event.target.files[0];
      if (!file) {
        selectedHmsFileName.textContent = 'Chưa chọn file HMS.';
        return;
      }
      selectedHmsFileName.textContent = file.name;
      setStatus('Đang import file HMS', false);
      try {
        let rows = [];
        if (file.name.toLocaleLowerCase('en-US').endsWith('.xlsx')) {
          const response = await fetch('/import-hms-xlsx', {
            method: 'POST',
            headers: {'Content-Type': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'},
            body: await file.arrayBuffer()
          });
          const payload = await response.json().catch(() => ({}));
          if (!response.ok) {
            setStatus(payload.error || 'Không import được file HMS', false);
            return;
          }
          rows = (payload.rows || []).map((item, index) => makeHmsRow(item, index)).filter(validHmsRow);
        } else {
          rows = parseHmsRows(await file.text());
        }
        if (!rows.length) {
          setStatus('File HMS không có confirmationNumber/lastName hợp lệ', false);
          return;
        }
        hmsRows = [...hmsRows, ...rows];
        saveHmsRows();
        renderHmsRows();
        setStatus(`Đã import ${rows.length} dòng HMS từ ${file.name}`, true);
      } catch (error) {
        setStatus(`Import HMS lỗi: ${error.message}`, false);
      } finally {
        hmsFile.value = '';
      }
    });

    hmsResultList.addEventListener('click', (event) => {
      const button = event.target.closest('button[data-hms-action]');
      if (!button) return;
      const index = Number(button.dataset.index);
      const row = hmsRows[index];
      if (!row) return;
      if (button.dataset.hmsAction === 'check') {
        checkHmsRow(index).catch((error) => setStatus(`Check HMS lỗi: ${error.message}`, false));
      } else if (button.dataset.hmsAction === 'fill') {
        hmsConfirmationNumber.value = row.confirmationNumber || '';
        hmsLastName.value = row.lastName || '';
      } else if (button.dataset.hmsAction === 'delete') {
        hmsRows.splice(index, 1);
        saveHmsRows();
        renderHmsRows();
      }
    });

    function filteredLogs() {
      const query = (logSearch.value || '').trim().toLocaleLowerCase('vi-VN');
      return testLogs.filter((item) => {
        if (activeLogCase && item.case_id !== activeLogCase) return false;
        if (!query) return true;
        return Object.values(item).join(' ').toLocaleLowerCase('vi-VN').includes(query);
      });
    }

    function exportLogsText() {
      const rows = filteredLogs();
      if (!rows.length) {
        setStatus('Không có log để xuất', false);
        return;
      }
      const body = rows.map((item) => [
        `[${item.time}]`,
        item.case_id ? `case=${item.case_id}` : '',
        item.step ? `step=${item.step}` : '',
        item.event ? `event=${item.event}` : '',
        item.message || '',
        item.prompt ? `prompt=${item.prompt}` : '',
        item.actual_response ? `response=${item.actual_response}` : '',
        item.test_result ? `result=${item.test_result}` : ''
      ].filter(Boolean).join(' | ')).join('\n');
      const blob = new Blob([body], {type: 'text/plain;charset=utf-8'});
      const suffix = activeLogCase ? activeLogCase : 'all';
      const filename = `logs_${new Date().toISOString().slice(0, 10)}_${suffix}.txt`;
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = filename;
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
      appendLog('export_log', {case_id: activeLogCase, message: `Exported text log: ${filename}`});
      setStatus(`Đã xuất ${filename}`, true);
    }

    document.getElementById('exportLogsTxt').addEventListener('click', exportLogsText);
    document.getElementById('exportLogsSide').addEventListener('click', exportLogsText);
    document.getElementById('refreshLogSessions').addEventListener('click', refreshLogSessions);
    document.getElementById('showTestcaseLogs').addEventListener('click', () => {
      selectLogSessionByKind('testcase').catch((error) => setStatus(`Không mở được log testcase: ${error.message}`, false));
    });
    document.getElementById('showChatLogs').addEventListener('click', () => {
      selectLogSessionByKind('chat').catch((error) => setStatus(`Không mở được log chat: ${error.message}`, false));
    });

    logSessionList.addEventListener('click', async (event) => {
      const button = event.target.closest('button[data-session]');
      if (!button) return;
      await loadLogSession(button.dataset.session || '');
      setView('logs');
    });

    resultList.addEventListener('click', async (event) => {
      const button = event.target.closest('button[data-action="logs"]');
      if (!button) return;
      const caseId = button.dataset.case || '';
      if (caseId === 'Chat') {
        await selectLogSessionByKind('chat').catch((error) => setStatus(`Không mở được log chat: ${error.message}`, false));
        return;
      }
      if (caseId && caseId !== 'Chat') {
        const selected = await selectLogSessionByKind('testcase', {caseId}).catch((error) => {
          setStatus(`Không mở được log testcase: ${error.message}`, false);
          return false;
        });
        if (selected) return;
      }
      activeLogCase = caseId === 'Chat' ? '' : caseId;
      renderLogs();
      setView('logs');
    });

    document.getElementById('resetLogFilter').addEventListener('click', () => {
      activeLogCase = '';
      logSearch.value = '';
      renderLogs();
    });

    document.getElementById('clearTestcases').addEventListener('click', () => {
      testcases = [];
      saveTestcases();
      renderTestcases();
      updateSplitPreview();
    });

    document.getElementById('clearResults').addEventListener('click', () => {
      results = [];
      saveResults();
      renderResults();
    });

    document.getElementById('clearLogs').addEventListener('click', async () => {
      if (!activeLogSessionId) {
        testLogs = [];
        saveLogs();
        renderLogs();
        return;
      }
      const sessionId = activeLogSessionId;
      const response = await fetch(`/log-session?id=${encodeURIComponent(sessionId)}`, {method: 'DELETE'});
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        setStatus(payload.error || 'Không xóa được log session', false);
        return;
      }
      testLogs = [];
      if (sessionId === chatLogSessionId) chatLogSessionId = '';
      if (sessionId === testcaseLogSessionId) testcaseLogSessionId = '';
      activeLogSessionId = '';
      activeLogSessionName = '';
      activeLogCase = '';
      logSearch.value = '';
      saveLogs();
      saveLogSessionInfo();
      saveContextSessionInfo();
      await refreshLogSessions();
      renderLogs();
      setStatus('Đã xóa log session khỏi project', true);
    });

	    async function refreshHealthStatus() {
	      if (voiceListening) return;
	      try {
	        const response = await fetch(`/health?probe=${Date.now()}`, {cache: 'no-store'});
	        const payload = await response.json();
          setRobotSleepingState(Boolean(payload.sleeping));
	        if (payload.sleeping) {
	          setStatus('Robot đang ngủ', false);
	        } else if (payload.connected) {
	          setStatus('Sẵn sàng', true);
	        } else if (payload.last_error) {
	          setStatus(`Realtime lỗi: ${payload.last_error}`, false);
	        } else {
	          setStatus('Đang kết nối realtime', false);
	        }
	      } catch (_) {
	        if (!voiceListening) setStatus('Không kiểm tra được kết nối', false);
	      }
	    }

	    refreshHealthStatus();
	    window.setInterval(refreshHealthStatus, 3000);

    renderTestcases();
    renderResults();
    renderLogs();
    renderHmsRows();
    loadRunSetup();
    updateThreadLockUi();
    syncBatchInputsFromRunRange();
    updateSplitPreview();
    updateRunEstimate();
    refreshRunSetupState();
	    refreshLogSessions().then(() => {
	      if (activeLogSessionId) loadLogSession(activeLogSessionId).catch(() => {});
	    });
	    setupVoiceInput();
	    resizeQuestionInput();
	    updateJumpToLatest();
	    updateProgress();
	  </script>
</body>
</html>
"""

async def main():

    config = {
        "type": "session.update",
        "modalities": REALTIME_MODALITIES,
        "domain": "robot",
        "sample_rate": 16000,
        "session": {
            "modalities": REALTIME_MODALITIES,
            "system_persona": {
                "user_name": "",
                "robot_type": "ambassador",
            },
        },
    }
    if "audio" in REALTIME_MODALITIES:
        config["voice"] = "N_M02_TuanDuong"
        config["session"]["voice"] = "N_M02_TuanDuong"

    voice_cli = VoiceClient(config, timeout=120)
    # audio_in = AudioIn()
    recorder = SessionRecorder(base_dir="runs")

    text_out = TextOut()
    # audio_out = AudioOut()
    action_out = ActionOut()
    text_request_lock = asyncio.Lock()
    connect_lock = asyncio.Lock()
    batch_voice_clients = {}
    batch_text_outputs = {}
    batch_text_locks = {}
    batch_connect_locks = {}
    batch_voice_tasks = {}
    mic_state = {
        "audio_in": None,
        "task": None,
        "listening": False,
        "transcripts": [],
        "last_error": "",
        "auto_stop_task": None,
    }
    mic_lock = asyncio.Lock()

    async def send_audio_with_record(chunk: bytes):
        recorder.write_audio_in(chunk)
        await voice_cli.send_audio(chunk)

    def remember_input_transcript(transcript: str):
        text = str(transcript or "").strip()
        if not text:
            return
        mic_state["transcripts"].append(
            {
                "text": text,
                "time": datetime.now().strftime("%H:%M:%S %d/%m/%Y"),
            }
        )
        mic_state["transcripts"] = mic_state["transcripts"][-20:]

    def handle_mic_task_done(task: asyncio.Task):
        if task.cancelled():
            return
        error = task.exception()
        if error:
            logging.error(
                "Backend mic task failed",
                exc_info=(type(error), error, error.__traceback__),
            )
            mic_state["last_error"] = str(error)
        if mic_state.get("task") is task:
            mic_state["listening"] = False
            mic_state["audio_in"] = None
            mic_state["task"] = None

    async def auto_stop_backend_mic(task: asyncio.Task):
        await asyncio.sleep(MIC_MAX_DURATION_SECONDS)
        if mic_state.get("task") is task and mic_state.get("listening"):
            mic_state["last_error"] = f"Mic tự dừng sau {MIC_MAX_DURATION_SECONDS} giây để tiết kiệm mạng."
            await stop_backend_mic()

    async def start_backend_mic():
        async with mic_lock:
            task = mic_state.get("task")
            if task and not task.done():
                mic_state["listening"] = True
                return
            await ensure_realtime_ready()
            audio_in = AudioIn()
            audio_in.callbacks.append(send_audio_with_record)
            task = asyncio.create_task(audio_in.run())
            task.add_done_callback(handle_mic_task_done)
            auto_stop_task = asyncio.create_task(auto_stop_backend_mic(task))
            mic_state.update(
                {
                    "audio_in": audio_in,
                    "task": task,
                    "listening": True,
                    "last_error": "",
                    "auto_stop_task": auto_stop_task,
                }
            )

    async def stop_backend_mic():
        async with mic_lock:
            audio_in = mic_state.get("audio_in")
            task = mic_state.get("task")
            auto_stop_task = mic_state.get("auto_stop_task")
            mic_state["listening"] = False
            mic_state["audio_in"] = None
            mic_state["task"] = None
            mic_state["auto_stop_task"] = None
        if auto_stop_task and auto_stop_task is not asyncio.current_task() and not auto_stop_task.done():
            auto_stop_task.cancel()
        if audio_in and task and not task.done():
            await audio_in.stop(task)

    def mic_status_payload():
        task = mic_state.get("task")
        if task and task.done():
            handle_mic_task_done(task)
        return {
            "listening": bool(mic_state.get("listening")),
            "transcripts": list(mic_state.get("transcripts") or []),
            "last_error": mic_state.get("last_error") or "",
            "max_duration_seconds": MIC_MAX_DURATION_SECONDS,
        }

    async def handle_robot_tcp(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        peer = writer.get_extra_info("peername")
        print(f"[tcp] connected {peer}")
        try:
            while data := await reader.readline():
                text = data.decode("utf-8", errors="ignore").strip()
                if not text:
                    continue
                if text == "q":
                    writer.write(b"bye\n")
                    await writer.drain()
                    break

                print(f"[tcp] {peer}: {text}")
                try:
                    await send_text_and_wait(text)
                    writer.write(b"ok\n")
                except Exception as exc:
                    writer.write(f"error: {exc}\n".encode("utf-8"))
                await writer.drain()
        except Exception:
            logging.exception("Robot TCP client failed")
        finally:
            writer.close()
            await writer.wait_closed()
            print(f"[tcp] disconnected {peer}")

    def _configure_voice_client(client: VoiceClient, text_output: TextOut):
        client.text_out = text_output
        client.action_out = ActionOut()

    def _batch_worker_id(value) -> int:
        try:
            worker_id = int(value)
        except (TypeError, ValueError):
            return 0
        return min(max(worker_id, 0), 16)

    async def _batch_runtime(worker_id: int):
        worker_id = _batch_worker_id(worker_id)
        if worker_id <= 0:
            return voice_cli, text_out, text_request_lock, connect_lock
        if worker_id not in batch_voice_clients:
            client = VoiceClient(config, timeout=120)
            client.startup_done = True
            output = TextOut()
            _configure_voice_client(client, output)
            batch_voice_clients[worker_id] = client
            batch_text_outputs[worker_id] = output
            batch_text_locks[worker_id] = asyncio.Lock()
            batch_connect_locks[worker_id] = asyncio.Lock()
            batch_voice_tasks[worker_id] = asyncio.create_task(client.run())
            await client.connect()
        return (
            batch_voice_clients[worker_id],
            batch_text_outputs[worker_id],
            batch_text_locks[worker_id],
            batch_connect_locks[worker_id],
        )

    async def restart_realtime_client(
        client: VoiceClient,
        ready_lock: asyncio.Lock,
        timeout: float = REALTIME_WAKE_TIMEOUT_SECONDS,
    ) -> bool:
        async with ready_lock:
            await client.connect(force=True)
            return await client.wait_connected(timeout=timeout)

    async def send_text_and_wait(text: str, worker_id: int = 0, request_id: str = ""):
        client, output, request_lock, ready_lock = await _batch_runtime(worker_id)
        async with request_lock:
            if worker_id == 0 and not client.awake.is_set() and not client.conn:
                raise RuntimeError("Robot đang ngủ. Nhấn Wake robot để tiếp tục.")
            attempts = max(1, REALTIME_REQUEST_RETRIES + 1)
            last_error = None
            for attempt in range(1, attempts + 1):
                if attempt > 1:
                    connected = await restart_realtime_client(client, ready_lock)
                    if not connected:
                        detail = f" Last error: {client.last_error}" if client.last_error else ""
                        last_error = RuntimeError(
                            f"Realtime chưa kết nối sau {int(REALTIME_WAKE_TIMEOUT_SECONDS)} giây reconnect.{detail}"
                        )
                        continue

                await ensure_realtime_ready(client, ready_lock)
                waiter = output.prepare_response_waiter()
                try:
                    await client.send_text(text)
                    response = await asyncio.wait_for(
                        waiter,
                        timeout=REALTIME_REQUEST_TIMEOUT_SECONDS,
                    )
                    clean_output = str(response.get("output") or "").strip()
                    response["input"] = text
                    response["output"] = clean_output
                    response["request_id"] = request_id
                    response["worker_id"] = worker_id
                    response["device_id"] = REALTIME_DEVICE_ID
                    response["realtime_attempt"] = attempt
                    response["realtime_retried"] = attempt > 1
                    return response
                except Exception as exc:
                    last_error = exc
                    if hasattr(output, "cancel_response_waiter"):
                        output.cancel_response_waiter(waiter)
                    partial_output = getattr(output, "text", "").strip()
                    if partial_output:
                        return {
                            "input": text,
                            "output": partial_output,
                            "request_id": request_id,
                            "worker_id": worker_id,
                            "device_id": REALTIME_DEVICE_ID,
                            "realtime_attempt": attempt,
                            "realtime_retried": attempt > 1,
                            "partial": True,
                        }
                    if attempt >= attempts:
                        break
                    client.last_error = str(exc) or type(exc).__name__
                    logging.warning(
                        "Realtime request failed attempt=%s/%s worker=%s request_id=%s error=%s; reconnecting",
                        attempt,
                        attempts,
                        worker_id,
                        request_id,
                        client.last_error,
                    )

            if isinstance(last_error, asyncio.TimeoutError):
                raise asyncio.TimeoutError(
                    f"Quá thời gian chờ phản hồi sau {attempts} lần thử"
                ) from last_error
            if last_error:
                raise last_error
            raise RuntimeError("Realtime request failed without response")

    async def ensure_realtime_ready(client: VoiceClient = voice_cli, ready_lock: asyncio.Lock = connect_lock, timeout: float = REALTIME_WAKE_TIMEOUT_SECONDS):
        if client.conn:
            return

        async with ready_lock:
            if client.conn:
                return
            await client.connect()
            if await client.wait_connected(timeout=timeout):
                return

        detail = f" Last error: {client.last_error}" if client.last_error else ""
        raise RuntimeError(f"Realtime chưa kết nối sau {int(timeout)} giây wakeup.{detail}")

    async def keep_realtime_warm():
        while not voice_cli.stopping:
            runtimes = [(0, voice_cli, text_request_lock, connect_lock)]
            for worker_id, client in list(batch_voice_clients.items()):
                runtimes.append(
                    (
                        worker_id,
                        client,
                        batch_text_locks.get(worker_id),
                        batch_connect_locks.get(worker_id),
                    )
                )

            for worker_id, client, request_lock, ready_lock in runtimes:
                if not ready_lock:
                    continue
                if not client.awake.is_set() and not client.conn:
                    continue
                try:
                    await client.connect()
                    if not client.conn:
                        await client.wait_connected(timeout=REALTIME_WAKE_TIMEOUT_SECONDS)
                    elif (
                        request_lock is not None
                        and not request_lock.locked()
                        and client.last_event_at
                        and time.monotonic() - client.last_event_at > REALTIME_STALE_SECONDS
                    ):
                        logging.info(
                            "Realtime worker=%s stale for %.0fs, forcing reconnect",
                            worker_id,
                            time.monotonic() - client.last_event_at,
                        )
                        await restart_realtime_client(client, ready_lock)
                except Exception:
                    logging.exception("Realtime keep-warm failed worker=%s", worker_id)
            await asyncio.sleep(REALTIME_WAKE_INTERVAL_SECONDS)

    async def write_http_response(writer, status, content_type, body, extra_headers=None):
        reason = {
            200: "OK",
            202: "Accepted",
            204: "No Content",
            400: "Bad Request",
            401: "Unauthorized",
            403: "Forbidden",
            404: "Not Found",
            405: "Method Not Allowed",
            500: "Internal Server Error",
            502: "Bad Gateway",
            504: "Gateway Timeout",
        }.get(status, "OK")
        body_bytes = body if isinstance(body, bytes) else body.encode("utf-8")
        header_lines = "".join(
            f"{key}: {value}\r\n" for key, value in (extra_headers or {}).items()
        )
        writer.write(
            (
                f"HTTP/1.1 {status} {reason}\r\n"
                f"Content-Type: {content_type}\r\n"
                f"Content-Length: {len(body_bytes)}\r\n"
                f"{header_lines}"
                "Connection: close\r\n"
                "\r\n"
            ).encode("utf-8")
            + body_bytes
        )
        await writer.drain()

    async def write_json(writer, status, payload):
        await write_http_response(
            writer,
            status,
            "application/json; charset=utf-8",
            json.dumps(payload, ensure_ascii=False),
        )

    restart_scheduled = False

    async def restart_process_soon():
        await asyncio.sleep(0.5)
        logging.warning("Server restart requested; exiting with code %s", SERVER_RESTART_EXIT_CODE)
        os._exit(SERVER_RESTART_EXIT_CODE)

    def schedule_server_restart():
        nonlocal restart_scheduled
        if restart_scheduled:
            return False
        restart_scheduled = True
        asyncio.create_task(restart_process_soon())
        return True

    async def handle_web_ui(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            request_line = await reader.readline()
            if not request_line:
                return

            parts = request_line.decode("iso-8859-1").strip().split()
            if len(parts) < 2:
                await write_json(writer, 400, {"error": "Invalid request"})
                return

            method, raw_path = parts[0], parts[1]
            path, _, query_string = raw_path.partition("?")
            query = parse_qs(query_string)
            headers = {}
            while True:
                line = await reader.readline()
                if line in (b"\r\n", b"\n", b""):
                    break
                key, _, value = line.decode("iso-8859-1").partition(":")
                headers[key.lower()] = value.strip()

            content_length = int(headers.get("content-length", "0") or 0)
            body = await reader.readexactly(content_length) if content_length else b""

            if method == "GET" and path == "/":
                await write_http_response(writer, 200, "text/html; charset=utf-8", WEB_UI_HTML)
            elif method == "GET" and path == "/health":
                await write_json(
                    writer,
                    200,
                    {
                        "connected": bool(voice_cli.conn),
                        "warming": voice_cli.awake.is_set(),
                        "sleeping": not voice_cli.awake.is_set() and not voice_cli.conn,
                        "reconnect_count": voice_cli.reconnect_count,
                        "last_error": voice_cli.last_error,
                        "last_event_age_seconds": (
                            round(time.monotonic() - voice_cli.last_event_at, 1)
                            if voice_cli.last_event_at
                            else None
                        ),
                        "batch_workers": len(batch_voice_clients),
                        "device_id": REALTIME_DEVICE_ID,
                        "wake_interval_seconds": REALTIME_WAKE_INTERVAL_SECONDS,
                        "stale_seconds": REALTIME_STALE_SECONDS,
                        "wake_timeout_seconds": REALTIME_WAKE_TIMEOUT_SECONDS,
                        "request_timeout_seconds": REALTIME_REQUEST_TIMEOUT_SECONDS,
                        "request_retries": REALTIME_REQUEST_RETRIES,
                        "modalities": REALTIME_MODALITIES,
                        "mic": mic_status_payload(),
                        "network_stats": voice_cli.network_stats,
                    },
                )
            elif method == "POST" and path == "/server/restart":
                scheduled = schedule_server_restart()
                await write_json(
                    writer,
                    202,
                    {
                        "ok": True,
                        "scheduled": scheduled,
                        "message": "Server restart scheduled",
                        "exit_code": SERVER_RESTART_EXIT_CODE,
                    },
                )
            elif method == "POST" and path == "/realtime/restart":
                try:
                    connected = await restart_realtime_client(voice_cli, connect_lock)
                    await write_json(
                        writer,
                        200 if connected else 504,
                        {
                            "ok": connected,
                            "connected": connected,
                            "message": "Realtime restart completed" if connected else "Realtime restart timeout",
                            "last_error": voice_cli.last_error,
                        },
                    )
                except Exception as exc:
                    logging.exception("Realtime restart failed")
                    await write_json(writer, 500, {"ok": False, "error": str(exc)})
            elif method == "POST" and path == "/robot/sleep":
                try:
                    await stop_backend_mic()
                    async with connect_lock:
                        await voice_cli.sleep()
                        voice_cli.last_error = "Robot đang ngủ"
                    await write_json(
                        writer,
                        200,
                        {
                            "ok": True,
                            "connected": bool(voice_cli.conn),
                            "sleeping": True,
                            "message": "Robot đang ngủ",
                        },
                    )
                except Exception as exc:
                    logging.exception("Robot sleep failed")
                    await write_json(writer, 500, {"ok": False, "error": str(exc), "sleeping": False})
            elif method == "POST" and path == "/robot/wake":
                try:
                    async with connect_lock:
                        voice_cli.last_error = None
                        await voice_cli.connect()
                        connected = await voice_cli.wait_connected(timeout=REALTIME_WAKE_TIMEOUT_SECONDS)
                    await write_json(
                        writer,
                        200 if connected else 504,
                        {
                            "ok": connected,
                            "connected": connected,
                            "sleeping": not connected,
                            "message": "Robot đã sẵn sàng" if connected else "Wake robot timeout",
                            "last_error": voice_cli.last_error,
                        },
                    )
                except Exception as exc:
                    logging.exception("Robot wake failed")
                    await write_json(writer, 500, {"ok": False, "error": str(exc), "sleeping": True})
            elif method == "GET" and path == "/mic/status":
                await write_json(writer, 200, mic_status_payload())
            elif method == "POST" and path == "/mic/start":
                try:
                    await start_backend_mic()
                    await write_json(writer, 200, {"ok": True, **mic_status_payload()})
                except Exception as exc:
                    logging.exception("Backend mic start failed")
                    mic_state["listening"] = False
                    mic_state["last_error"] = str(exc)
                    await write_json(writer, 500, {"ok": False, **mic_status_payload()})
            elif method == "POST" and path == "/mic/stop":
                try:
                    await stop_backend_mic()
                    await write_json(writer, 200, {"ok": True, **mic_status_payload()})
                except Exception as exc:
                    logging.exception("Backend mic stop failed")
                    mic_state["last_error"] = str(exc)
                    await write_json(writer, 500, {"ok": False, **mic_status_payload()})
            elif method == "GET" and path == "/web/testcase_evaluator.js":
                evaluator_path = WEB_DIR / "testcase_evaluator.js"
                if evaluator_path.exists():
                    await write_http_response(
                        writer,
                        200,
                        "application/javascript; charset=utf-8",
                        evaluator_path.read_bytes(),
                        {"Cache-Control": "no-store"},
                    )
                else:
                    await write_json(writer, 404, {"error": "Evaluator module not found"})
            elif method == "GET" and path == "/log-sessions":
                await write_json(writer, 200, {"sessions": _list_log_sessions()})
            elif method == "GET" and path == "/log-session":
                session_id = str((query.get("id") or [""])[0])
                try:
                    payload = (
                        _read_legacy_log_session(session_id)
                        if session_id.startswith("legacy_")
                        else _read_log_session(session_id)
                    )
                    await write_json(writer, 200, payload)
                except Exception as exc:
                    await write_json(writer, 404, {"error": str(exc)})
            elif method == "DELETE" and path == "/log-session":
                session_id = str((query.get("id") or [""])[0])
                try:
                    _delete_log_session(session_id)
                    await write_json(writer, 200, {"ok": True})
                except Exception as exc:
                    await write_json(writer, 404, {"error": str(exc)})
            elif method == "POST" and path == "/log-session":
                try:
                    payload = json.loads(body.decode("utf-8") or "{}")
                    record = _write_log_session(payload)
                    await write_json(
                        writer,
                        200,
                        {
                            "ok": True,
                            "session": {
                                "id": record["id"],
                                "source_name": record["source_name"],
                                "created_at": record["created_at"],
                                "updated_at": record["updated_at"],
                                "count": len(record["logs"]),
                            },
                        },
                    )
                except Exception as exc:
                    await write_json(writer, 400, {"error": str(exc)})
            elif method == "POST" and path == "/log-session/append":
                try:
                    payload = json.loads(body.decode("utf-8") or "{}")
                    session_id = str(payload.get("sessionId") or payload.get("id") or "")
                    source_name = str(payload.get("sourceName") or payload.get("source_name") or "")
                    entry = payload.get("entry") if isinstance(payload.get("entry"), dict) else {}
                    record = _append_log_session(session_id, source_name, entry)
                    await write_json(
                        writer,
                        200,
                        {
                            "ok": True,
                            "session": {
                                "id": record["id"],
                                "source_name": record["source_name"],
                                "created_at": record["created_at"],
                                "updated_at": record["updated_at"],
                                "count": len(record["logs"]),
                            },
                        },
                    )
                except Exception as exc:
                    await write_json(writer, 400, {"error": str(exc)})
            elif method == "GET" and path.startswith("/image/"):
                asset_name = posixpath.basename(path)
                asset_path = BASE_DIR / "image" / asset_name
                content_types = {
                    ".png": "image/png",
                    ".svg": "image/svg+xml; charset=utf-8",
                }
                content_type = content_types.get(asset_path.suffix.lower())
                if content_type and asset_name == path.removeprefix("/image/") and asset_path.exists():
                    await write_http_response(
                        writer,
                        200,
                        content_type,
                        asset_path.read_bytes(),
                        {"Cache-Control": "public, max-age=3600"},
                    )
                else:
                    await write_json(writer, 404, {"error": "Image asset not found"})
            elif method == "GET" and path == "/favicon.ico":
                await write_http_response(writer, 204, "text/plain; charset=utf-8", b"")
            elif method == "POST" and path == "/import-xlsx":
                try:
                    testcases, sheet_name, diagnostics = read_xlsx_testcases(body)
                    await write_json(
                        writer,
                        200,
                        {
                            "sheet": sheet_name,
                            "total": len(testcases),
                            "testcases": testcases,
                            "diagnostics": diagnostics,
                        },
                    )
                except Exception as exc:
                    logging.exception("Import xlsx failed")
                    await write_json(
                        writer,
                        400,
                        {
                            "error": str(exc),
                            "diagnostics": {
                                "message": "Không đọc được file Excel.",
                                "error": str(exc),
                            },
                        },
                    )
            elif method == "POST" and path == "/import-hms-xlsx":
                try:
                    records, sheet_name = read_xlsx_records(body)
                    await write_json(
                        writer,
                        200,
                        {
                            "sheet": sheet_name,
                            "total": len(records),
                            "rows": records,
                        },
                    )
                except Exception as exc:
                    logging.exception("Import HMS xlsx failed")
                    await write_json(writer, 400, {"error": str(exc)})
            elif method == "POST" and path == "/export-xlsx":
                try:
                    payload = json.loads(body.decode("utf-8") or "{}")
                except json.JSONDecodeError:
                    await write_json(writer, 400, {"error": "Body JSON không hợp lệ"})
                    return

                records = payload.get("testcases") or payload.get("rows") or []
                if not isinstance(records, list) or not records:
                    await write_json(writer, 400, {"error": "Không có testcase để xuất"})
                    return

                logs = payload.get("logs") or []
                if not isinstance(logs, list):
                    logs = []
                filename = (
                    split_filename(str(payload.get("sourceName") or "testcases.xlsx"))
                    if payload.get("plain")
                    else result_filename(str(payload.get("sourceName") or "testcases.xlsx"))
                )
                workbook = (
                    build_plain_xlsx(records, str(payload.get("sheetName") or "Testcases"))
                    if payload.get("plain")
                    else build_result_xlsx(records, logs)
                )
                await write_http_response(
                    writer,
                    200,
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    workbook,
                    {
                        "Content-Disposition": f'attachment; filename="{filename}"',
                        "Cache-Control": "no-store",
                    },
                )
            elif method == "POST" and path == "/export-chat-xlsx":
                try:
                    payload = json.loads(body.decode("utf-8") or "{}")
                except json.JSONDecodeError:
                    await write_json(writer, 400, {"error": "Body JSON không hợp lệ"})
                    return

                session_id = str(payload.get("sessionId") or payload.get("id") or "").strip()
                source_name = str(payload.get("sourceName") or payload.get("source_name") or "Manual chat")
                logs = payload.get("logs") or []
                if not isinstance(logs, list):
                    logs = []
                if not logs and session_id:
                    try:
                        session_payload = _read_log_session(session_id)
                        logs = session_payload.get("logs") if isinstance(session_payload.get("logs"), list) else []
                        source_name = session_payload.get("source_name") or source_name
                    except Exception as exc:
                        await write_json(writer, 404, {"error": str(exc)})
                        return

                rows = manual_chat_rows_from_logs(logs, session_id)
                if not rows:
                    await write_json(writer, 400, {"error": "Không có manual chat question/response để xuất"})
                    return

                workbook = build_plain_xlsx(rows, "Manual Chat", MANUAL_CHAT_COLUMNS)
                filename = manual_chat_filename(source_name or session_id or "Manual_chat")
                await write_http_response(
                    writer,
                    200,
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    workbook,
                    {
                        "Content-Disposition": f'attachment; filename="{filename}"',
                        "Cache-Control": "no-store",
                    },
                )
            elif method == "POST" and path == "/hms/room-status":
                try:
                    payload = json.loads(body.decode("utf-8") or "{}")
                except json.JSONDecodeError:
                    await write_json(writer, 400, {"error": "Body JSON không hợp lệ"})
                    return

                confirmation_number = str(
                    payload.get("confirmationNumber")
                    or payload.get("confirmation_number")
                    or payload.get("confirmation number")
                    or ""
                ).strip()
                last_name = str(
                    payload.get("lastName")
                    or payload.get("last_name")
                    or payload.get("last name")
                    or ""
                ).strip()
                org_id = str(payload.get("orgId") or payload.get("org_id") or HMS_ORG_ID).strip()
                try:
                    result = await asyncio.to_thread(
                        _hms_room_status_request,
                        confirmation_number,
                        last_name,
                        org_id,
                    )
                    await write_json(
                        writer,
                        200 if result.get("ok") else int(result.get("httpStatus") or 502),
                        result,
                    )
                except ValueError as exc:
                    await write_json(writer, 400, {"ok": False, "error": str(exc)})
                except Exception as exc:
                    logging.exception("HMS room status failed")
                    await write_json(writer, 502, {"ok": False, "error": str(exc)})
            elif method == "POST" and path == "/ask":
                try:
                    payload = json.loads(body.decode("utf-8") or "{}")
                except json.JSONDecodeError:
                    await write_json(writer, 400, {"error": "Body JSON không hợp lệ"})
                    return

                question = str(payload.get("question", "")).strip()
                if not question:
                    await write_json(writer, 400, {"error": "Thiếu câu hỏi"})
                    return
                worker_id = _batch_worker_id(payload.get("workerId") or payload.get("worker_id") or 0)
                request_id = str(payload.get("requestId") or payload.get("request_id") or "").strip()

                try:
                    response = await send_text_and_wait(question[:2000], worker_id=worker_id, request_id=request_id)
                    await write_json(writer, 200, response)
                except asyncio.TimeoutError:
                    await write_json(writer, 504, {"error": "Quá thời gian chờ phản hồi"})
                except Exception as exc:
                    logging.exception("Web UI ask failed")
                    await write_json(writer, 500, {"error": str(exc)})
            else:
                await write_json(writer, 404, {"error": "Not found"})
        finally:
            writer.close()
            await writer.wait_closed()

    # audio_in.callbacks.append(send_audio_with_record)
    # audio_out.send_callbacks.append(recorder.write_audio_out)
    voice_cli.text_out = text_out
    # voice_cli.audio_out = audio_out
    voice_cli.action_out = action_out
    voice_cli.transcript_callbacks.append(remember_input_transcript)

    # t_audio_out = asyncio.create_task(audio_out.run())
    t_voice = asyncio.create_task(voice_cli.run())
    t_keep_warm = asyncio.create_task(keep_realtime_warm())
    # atask = asyncio.create_task(audio_in.run())
    # await audio_out.wait_ready()

    await voice_cli.connect()
    server = await asyncio.start_server(handle_robot_tcp, ROBOT_HOST, ROBOT_PORT)
    web_server = await asyncio.start_server(handle_web_ui, WEB_HOST, WEB_PORT)
    print(f">>> Project terminal: {BASE_DIR}")
    print(f">>> Robot TCP server running {ROBOT_HOST}:{ROBOT_PORT}")
    print(f">>> Realtime device_id: {REALTIME_DEVICE_ID}")
    print(f">>> VinFast test UI running http://127.0.0.1:{WEB_PORT}")

    try:
        while True:
            try:
                text = await asyncio.to_thread(input, "message > ")
            except EOFError:
                await asyncio.Event().wait()
            if text == "q":
                break
            elif text == "w":
                print("[manual] wake/reconnect realtime")
                await voice_cli.connect(force=True)
                await voice_cli.wait_connected(timeout=REALTIME_WAKE_TIMEOUT_SECONDS)
            else:
                await voice_cli.send_text(text)
        logging.info("closing..")
        # await audio_in.stop(atask)
    finally:
        await stop_backend_mic()
        server.close()
        web_server.close()
        await server.wait_closed()
        await web_server.wait_closed()
        voice_cli.stopping = True
        if voice_cli.conn:
            await voice_cli.disconnect()
        for client in batch_voice_clients.values():
            client.stopping = True
            if client.conn:
                await client.disconnect()
        if not t_voice.done():
            t_voice.cancel()
        if not t_keep_warm.done():
            t_keep_warm.cancel()
        for task in batch_voice_tasks.values():
            if not task.done():
                task.cancel()
        await asyncio.gather(t_voice, t_keep_warm, *batch_voice_tasks.values(), return_exceptions=True)
        recorder.close()

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
