from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from .domain import (ConflictError, DomainError, NotFoundError, PermissionDenied,
                     ValidationError)
from .ledger_service import LedgerService
from .service import Service


def make_handler(service: Service, ledger: LedgerService, static_dir: str):
    root = Path(static_dir)

    class Handler(BaseHTTPRequestHandler):
        server_version = "ModularHell/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def _json(self, status: int, payload: Any) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _html(self, path: Path) -> None:
            if not path.exists():
                self._json(404, {"error": "not_found"})
                return
            body = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _identity(self) -> Tuple[str, str]:
            return self.headers.get("X-Actor", ""), self.headers.get("X-Role", "")

        def _body(self) -> Dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0") or 0)
            if length <= 0:
                return {}
            if length > 2_000_000:
                raise ValidationError("请求体过大")
            try:
                value = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValidationError("请求体不是有效JSON") from exc
            if not isinstance(value, dict):
                raise ValidationError("请求体必须是JSON对象")
            return value

        def _send_error(self, exc: Exception) -> None:
            if isinstance(exc, ValidationError):
                status = 422
            elif isinstance(exc, NotFoundError):
                status = 404
            elif isinstance(exc, PermissionDenied):
                status = 403
            elif isinstance(exc, ConflictError):
                status = 409
            elif isinstance(exc, ValueError):
                status = 422
            elif isinstance(exc, DomainError):
                status = 400
            else:
                status = 500
            self._json(status, {"error": exc.__class__.__name__, "message": str(exc)})

        def do_GET(self) -> None:
            try:
                path = urlparse(self.path).path
                if path == "/health":
                    self._json(200, {"status": "ok"})
                elif path == "/":
                    self._html(root / "index.html")
                elif path == "/api/items":
                    actor, role = self._identity()
                    del actor
                    self._json(200, {"items": service.list_items(role)})
                elif path.startswith("/api/items/") and path.endswith("/records"):
                    item_id = int(path.split("/")[3])
                    actor, role = self._identity()
                    del actor
                    self._json(200, {"records": service.list_records(item_id, role)})
                elif path.startswith("/api/items/"):
                    item_id = int(path.rsplit("/", 1)[-1])
                    actor, role = self._identity()
                    del actor
                    self._json(200, service.get_item(item_id, role))
                elif path == "/api/audit":
                    actor, role = self._identity()
                    del actor
                    self._json(200, {"events": service.audit(role)})
                elif path == "/api/facilities":
                    actor, role = self._identity()
                    del actor
                    self._json(200, {"facilities": ledger.list_facilities(role)})
                elif path == "/api/ledger":
                    actor, role = self._identity()
                    query = parse_qs(urlparse(self.path).query)
                    def _one(name):
                        return query[name][0] if name in query else None
                    fid = _one("facility_id")
                    self._json(200, ledger.ledger(
                        role, int(fid) if fid else None,
                        _one("from"), _one("to")))
                elif path == "/api/directives":
                    actor, role = self._identity()
                    query = parse_qs(urlparse(self.path).query)
                    def _one(name):
                        return query[name][0] if name in query else None
                    fid = _one("facility_id")
                    self._json(200, {"directives": ledger.list_directives(
                        role, int(fid) if fid else None, _one("status"))})
                elif path == "/api/bases":
                    actor, role = self._identity()
                    query = parse_qs(urlparse(self.path).query)
                    fid = query["facility_id"][0] if "facility_id" in query else None
                    self._json(200, {"bases": ledger.list_bases(
                        role, int(fid) if fid else None)})
                else:
                    self._json(404, {"error": "not_found"})
            except Exception as exc:
                self._send_error(exc)

        def do_POST(self) -> None:
            try:
                path = urlparse(self.path).path
                actor, role = self._identity()
                body = self._body()
                if path == "/api/items":
                    self._json(201, service.create_item(body, actor, role))
                elif path.startswith("/api/items/") and path.endswith("/records"):
                    item_id = int(path.split("/")[3])
                    self._json(201, service.add_record(item_id, body, actor, role))
                elif path.startswith("/api/items/") and path.endswith("/transition"):
                    item_id = int(path.split("/")[3])
                    target = body.get("target")
                    expected = body.get("expected_version")
                    self._json(200, service.transition(
                        item_id, target, expected, actor, role))
                elif path == "/api/facilities":
                    self._json(201, ledger.create_facility(body, actor, role))
                elif path == "/api/plan/refresh":
                    self._json(200, ledger.refresh_plan(body, actor, role))
                elif path == "/api/dispatch":
                    self._json(200, ledger.dispatch(body, actor, role))
                elif path == "/api/legacy/import":
                    self._json(201, ledger.legacy_import(body, actor, role))
                elif (path.startswith("/api/facilities/")
                      and path.endswith("/bases")
                      and "/batches/" not in path
                      and "/directives/" not in path):
                    facility_id = int(path.split("/")[3])
                    self._json(201, ledger.register_basis(
                        facility_id, body, actor, role))
                elif path.startswith("/api/bases/"):
                    basis_id = int(path.rsplit("/", 1)[-1])
                    self._json(200, ledger.update_basis(
                        basis_id, body, actor, role))
                elif path.startswith("/api/batches/") and path.endswith("/recover"):
                    batch_id = int(path.split("/")[3])
                    self._json(200, ledger.recover_batch(
                        batch_id, body, actor, role))
                elif path.startswith("/api/directives/") and path.endswith("/execute"):
                    directive_id = int(path.split("/")[3])
                    self._json(200, ledger.execute_directive(
                        directive_id, actor, role))
                elif (path.startswith("/api/directives/")
                      and path.endswith("/supplement-basis")):
                    directive_id = int(path.split("/")[3])
                    self._json(200, ledger.supplement_basis(
                        directive_id, body, actor, role))
                else:
                    self._json(404, {"error": "not_found"})
            except Exception as exc:
                self._send_error(exc)

    return Handler
