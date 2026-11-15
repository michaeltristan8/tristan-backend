"""
Service Motor - Backend API (Python standard library + SQLite)

Jalankan:
    python3 server.py            # default http://localhost:8000
    python3 server.py --port 9000

Tidak butuh pip install apa pun. Server ini juga menyajikan frontend
(folder ../frontend) sehingga cukup buka http://localhost:8000 di browser.
"""

import argparse
import json
import mimetypes
import os
import re
import sqlite3
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FRONTEND_DIR = os.path.normpath(os.path.join(BASE_DIR, "..", "frontend"))
DB_PATH = os.environ.get("SERVICE_MOTOR_DB", os.path.join(BASE_DIR, "service_motor.db"))

# Jenis servis bawaan beserta interval default (km, hari).
SERVICE_TYPES = [
    {"jenis": "Ganti oli mesin", "interval_km": 2000, "interval_hari": 60},
    {"jenis": "Ganti oli gardan", "interval_km": 8000, "interval_hari": 240},
    {"jenis": "Servis berkala / tune up", "interval_km": 4000, "interval_hari": 120},
    {"jenis": "Servis CVT", "interval_km": 8000, "interval_hari": 240},
    {"jenis": "Ganti busi", "interval_km": 8000, "interval_hari": 365},
    {"jenis": "Ganti filter udara", "interval_km": 12000, "interval_hari": 365},
    {"jenis": "Cek / ganti kampas rem", "interval_km": 10000, "interval_hari": 365},
    {"jenis": "Ganti air radiator", "interval_km": 12000, "interval_hari": 365},
    {"jenis": "Lainnya", "interval_km": 0, "interval_hari": 0},
]

SOON_KM = 300      # dianggap "segera" jika sisa <= 300 km
SOON_DAYS = 7      # atau sisa <= 7 hari


# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    with get_db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS motors (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                nama        TEXT NOT NULL,
                plat        TEXT,
                merk        TEXT,
                tahun       INTEGER,
                km_sekarang INTEGER NOT NULL DEFAULT 0,
                created_at  TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS services (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                motor_id      INTEGER NOT NULL REFERENCES motors(id) ON DELETE CASCADE,
                tanggal       TEXT NOT NULL,
                km            INTEGER NOT NULL,
                jenis         TEXT NOT NULL,
                biaya         INTEGER NOT NULL DEFAULT 0,
                bengkel       TEXT,
                catatan       TEXT,
                interval_km   INTEGER NOT NULL DEFAULT 0,
                interval_hari INTEGER NOT NULL DEFAULT 0,
                created_at    TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_services_motor ON services(motor_id);
            """
        )


def row_to_dict(row):
    return dict(row) if row is not None else None


# --------------------------------------------------------------------------- #
# Validasi
# --------------------------------------------------------------------------- #
class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def _int(value, field, minimum=0, required=False, default=0):
    if value in (None, ""):
        if required:
            raise ApiError(400, f"'{field}' wajib diisi")
        return default
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise ApiError(400, f"'{field}' harus berupa angka")
    if n < minimum:
        raise ApiError(400, f"'{field}' minimal {minimum}")
    return n


def _str(value, field, required=False, max_len=200):
    value = (value or "").strip() if isinstance(value, str) else ("" if value is None else str(value))
    if required and not value:
        raise ApiError(400, f"'{field}' wajib diisi")
    return value[:max_len]


def _date(value, field):
    value = _str(value, field, required=True, max_len=10)
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise ApiError(400, f"'{field}' harus berformat YYYY-MM-DD")
    return value


def validate_motor(data, partial=False):
    out = {}
    if not partial or "nama" in data:
        out["nama"] = _str(data.get("nama"), "nama", required=True)
    if not partial or "plat" in data:
        out["plat"] = _str(data.get("plat"), "plat", max_len=20).upper()
    if not partial or "merk" in data:
        out["merk"] = _str(data.get("merk"), "merk")
    if not partial or "tahun" in data:
        out["tahun"] = _int(data.get("tahun"), "tahun", minimum=1950, default=None) if data.get("tahun") else None
    if not partial or "km_sekarang" in data:
        out["km_sekarang"] = _int(data.get("km_sekarang"), "km_sekarang")
    return out


def validate_service(data):
    defaults = next((t for t in SERVICE_TYPES if t["jenis"] == data.get("jenis")), None)
    return {
        "motor_id": _int(data.get("motor_id"), "motor_id", minimum=1, required=True),
        "tanggal": _date(data.get("tanggal"), "tanggal"),
        "km": _int(data.get("km"), "km", required=True),
        "jenis": _str(data.get("jenis"), "jenis", required=True),
        "biaya": _int(data.get("biaya"), "biaya"),
        "bengkel": _str(data.get("bengkel"), "bengkel"),
        "catatan": _str(data.get("catatan"), "catatan", max_len=1000),
        "interval_km": _int(data.get("interval_km"), "interval_km",
                            default=defaults["interval_km"] if defaults else 0),
        "interval_hari": _int(data.get("interval_hari"), "interval_hari",
                              default=defaults["interval_hari"] if defaults else 0),
    }


# --------------------------------------------------------------------------- #
# Logika pengingat
# --------------------------------------------------------------------------- #
def compute_reminders(conn, motor_id=None, today=None):
    """Untuk tiap (motor, jenis servis) ambil servis terakhir lalu hitung jatuh tempo."""
    today = today or date.today()
    params = []
    where = ""
    if motor_id is not None:
        where = "AND s.motor_id = ?"
        params.append(motor_id)

    rows = conn.execute(
        f"""
        SELECT s.*, m.nama AS motor_nama, m.plat AS motor_plat, m.km_sekarang
        FROM services s
        JOIN motors m ON m.id = s.motor_id
        WHERE (s.interval_km > 0 OR s.interval_hari > 0) {where}
          AND s.id = (
              SELECT s2.id FROM services s2
              WHERE s2.motor_id = s.motor_id AND s2.jenis = s.jenis
              ORDER BY s2.tanggal DESC, s2.km DESC, s2.id DESC LIMIT 1
          )
        """,
        params,
    ).fetchall()

    reminders = []
    for r in rows:
        due_km = r["km"] + r["interval_km"] if r["interval_km"] else None
        due_date = None
        if r["interval_hari"]:
            due_date = datetime.strptime(r["tanggal"], "%Y-%m-%d").date() + timedelta(days=r["interval_hari"])

        sisa_km = due_km - r["km_sekarang"] if due_km is not None else None
        sisa_hari = (due_date - today).days if due_date is not None else None

        status = "ok"
        if (sisa_km is not None and sisa_km <= 0) or (sisa_hari is not None and sisa_hari <= 0):
            status = "terlambat"
        elif (sisa_km is not None and sisa_km <= SOON_KM) or (sisa_hari is not None and sisa_hari <= SOON_DAYS):
            status = "segera"

        reminders.append({
            "motor_id": r["motor_id"],
            "motor_nama": r["motor_nama"],
            "motor_plat": r["motor_plat"],
            "jenis": r["jenis"],
            "servis_terakhir_tanggal": r["tanggal"],
            "servis_terakhir_km": r["km"],
            "km_sekarang": r["km_sekarang"],
            "jatuh_tempo_km": due_km,
            "jatuh_tempo_tanggal": due_date.isoformat() if due_date else None,
            "sisa_km": sisa_km,
            "sisa_hari": sisa_hari,
            "status": status,
        })

    order = {"terlambat": 0, "segera": 1, "ok": 2}
    reminders.sort(key=lambda x: (order[x["status"]],
                                  x["sisa_km"] if x["sisa_km"] is not None else 10**9,
                                  x["sisa_hari"] if x["sisa_hari"] is not None else 10**9))
    return reminders


# --------------------------------------------------------------------------- #
# Handler API
# --------------------------------------------------------------------------- #
def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def api_list_motors(conn, **_):
    rows = conn.execute(
        """
        SELECT m.*,
               (SELECT COUNT(*) FROM services s WHERE s.motor_id = m.id) AS jumlah_servis,
               (SELECT COALESCE(SUM(biaya),0) FROM services s WHERE s.motor_id = m.id) AS total_biaya,
               (SELECT MAX(tanggal) FROM services s WHERE s.motor_id = m.id) AS servis_terakhir
        FROM motors m ORDER BY m.id
        """
    ).fetchall()
    return 200, [row_to_dict(r) for r in rows]


def get_motor_or_404(conn, motor_id):
    row = conn.execute("SELECT * FROM motors WHERE id = ?", (motor_id,)).fetchone()
    if row is None:
        raise ApiError(404, "Motor tidak ditemukan")
    return row


def api_get_motor(conn, motor_id, **_):
    return 200, row_to_dict(get_motor_or_404(conn, motor_id))


def api_create_motor(conn, body, **_):
    m = validate_motor(body)
    cur = conn.execute(
        "INSERT INTO motors (nama, plat, merk, tahun, km_sekarang, created_at) VALUES (?,?,?,?,?,?)",
        (m["nama"], m["plat"], m["merk"], m["tahun"], m["km_sekarang"], now_iso()),
    )
    return 201, row_to_dict(get_motor_or_404(conn, cur.lastrowid))


def api_update_motor(conn, motor_id, body, **_):
    get_motor_or_404(conn, motor_id)
    m = validate_motor(body, partial=True)
    if not m:
        raise ApiError(400, "Tidak ada data yang diubah")
    sets = ", ".join(f"{k} = ?" for k in m)
    conn.execute(f"UPDATE motors SET {sets} WHERE id = ?", (*m.values(), motor_id))
    return 200, row_to_dict(get_motor_or_404(conn, motor_id))


def api_delete_motor(conn, motor_id, **_):
    get_motor_or_404(conn, motor_id)
    conn.execute("DELETE FROM motors WHERE id = ?", (motor_id,))
    return 200, {"deleted": motor_id}


def api_list_services(conn, motor_id, **_):
    get_motor_or_404(conn, motor_id)
    rows = conn.execute(
        "SELECT * FROM services WHERE motor_id = ? ORDER BY tanggal DESC, km DESC, id DESC",
        (motor_id,),
    ).fetchall()
    return 200, [row_to_dict(r) for r in rows]


def api_create_service(conn, body, **_):
    s = validate_service(body)
    motor = get_motor_or_404(conn, s["motor_id"])
    cur = conn.execute(
        """INSERT INTO services (motor_id, tanggal, km, jenis, biaya, bengkel, catatan,
                                 interval_km, interval_hari, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (s["motor_id"], s["tanggal"], s["km"], s["jenis"], s["biaya"], s["bengkel"],
         s["catatan"], s["interval_km"], s["interval_hari"], now_iso()),
    )
    # Odometer motor ikut naik bila km servis lebih besar.
    if s["km"] > motor["km_sekarang"]:
        conn.execute("UPDATE motors SET km_sekarang = ? WHERE id = ?", (s["km"], s["motor_id"]))
    row = conn.execute("SELECT * FROM services WHERE id = ?", (cur.lastrowid,)).fetchone()
    return 201, row_to_dict(row)


def api_delete_service(conn, service_id, **_):
    row = conn.execute("SELECT id FROM services WHERE id = ?", (service_id,)).fetchone()
    if row is None:
        raise ApiError(404, "Data servis tidak ditemukan")
    conn.execute("DELETE FROM services WHERE id = ?", (service_id,))
    return 200, {"deleted": service_id}


def api_reminders(conn, **_):
    return 200, compute_reminders(conn)


def api_service_types(conn, **_):
    return 200, SERVICE_TYPES


def api_health(conn, **_):
    return 200, {"status": "ok", "time": now_iso()}


ROUTES = [
    ("GET",    r"^/api/health$",                       api_health),
    ("GET",    r"^/api/service-types$",                api_service_types),
    ("GET",    r"^/api/reminders$",                    api_reminders),
    ("GET",    r"^/api/motors$",                       api_list_motors),
    ("POST",   r"^/api/motors$",                       api_create_motor),
    ("GET",    r"^/api/motors/(?P<motor_id>\d+)$",     api_get_motor),
    ("PUT",    r"^/api/motors/(?P<motor_id>\d+)$",     api_update_motor),
    ("DELETE", r"^/api/motors/(?P<motor_id>\d+)$",     api_delete_motor),
    ("GET",    r"^/api/motors/(?P<motor_id>\d+)/services$", api_list_services),
    ("POST",   r"^/api/services$",                     api_create_service),
    ("DELETE", r"^/api/services/(?P<service_id>\d+)$", api_delete_service),
]


class Handler(BaseHTTPRequestHandler):
    server_version = "ServiceMotor/1.0"

    # --- util ---
    def _send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _cors(self):
        # Mengizinkan frontend dibuka dari origin lain (mis. file:// atau port berbeda).
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ApiError(400, "Body harus JSON yang valid")
        if not isinstance(data, dict):
            raise ApiError(400, "Body harus berupa objek JSON")
        return data

    def _handle_api(self, method, path):
        for m, pattern, fn in ROUTES:
            match = re.match(pattern, path)
            if match and m == method:
                kwargs = {k: int(v) for k, v in match.groupdict().items()}
                if method in ("POST", "PUT"):
                    kwargs["body"] = self._read_body()
                with get_db() as conn:
                    return fn(conn, **kwargs)
        if any(re.match(p, path) for _, p, _ in ROUTES):
            raise ApiError(405, "Method tidak diizinkan")
        raise ApiError(404, "Endpoint tidak ditemukan")

    def _dispatch(self, method):
        path = urlparse(self.path).path
        if path.startswith("/api/"):
            try:
                status, payload = self._handle_api(method, path)
                self._send_json(status, payload)
            except ApiError as e:
                self._send_json(e.status, {"error": e.message})
            except Exception as e:  # pragma: no cover
                self.log_error("Unhandled error: %r", e)
                self._send_json(500, {"error": "Terjadi kesalahan di server"})
            return
        if method == "GET":
            self._serve_static(path)
        else:
            self._send_json(404, {"error": "Tidak ditemukan"})

    def _serve_static(self, path):
        rel = "index.html" if path in ("", "/") else path.lstrip("/")
        full = os.path.normpath(os.path.join(FRONTEND_DIR, rel))
        if not full.startswith(FRONTEND_DIR) or not os.path.isfile(full):
            self._send_json(404, {"error": "File tidak ditemukan"})
            return
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        with open(full, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # --- method HTTP ---
    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_DELETE(self):
        self._dispatch("DELETE")


def main():
    parser = argparse.ArgumentParser(description="Service Motor backend")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    init_db()
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Service Motor berjalan di http://{args.host}:{args.port}  (DB: {DB_PATH})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nServer dihentikan.")


if __name__ == "__main__":
    main()
