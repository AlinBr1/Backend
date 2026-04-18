"""
Backend Flask - ClipFlip com segurança reforçada
"""

from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
import subprocess
import os
import time
import uuid
import jwt
import psycopg2
import psycopg2.extras
from datetime import datetime, timedelta
from functools import wraps
import mercadopago
import re
import logging

# ─── LOGGING ──────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ─── CORS ─────────────────────────────────────────────────────────────────────
FRONTEND_URL = os.environ.get("FRONTEND_URL", "https://video-converter-17tx.vercel.app")
CORS(app, origins=[FRONTEND_URL], supports_credentials=True)

# ─── RATE LIMITING ────────────────────────────────────────────────────────────
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["200 per hour", "30 per minute"],
    storage_uri="memory://",
)

# ─── CONFIG ───────────────────────────────────────────────────────────────────
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024
SECRET_KEY = os.environ.get("SECRET_KEY", "")
DATABASE_URL = os.environ.get("DATABASE_URL")
MP_ACCESS_TOKEN = os.environ.get("MP_ACCESS_TOKEN")

UPLOAD_FOLDER = "uploads"
OUTPUT_FOLDER = "outputs"
ALLOWED_EXTENSIONS = {'mp4', 'mov', 'avi', 'mkv'}
ALLOWED_MIMETYPES = {'video/mp4', 'video/quicktime', 'video/x-msvideo', 'video/x-matroska'}

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(OUTPUT_FOLDER, exist_ok=True)

# ─── SECURITY HEADERS ─────────────────────────────────────────────────────────
@app.after_request
def add_security_headers(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['X-XSS-Protection'] = '1; mode=block'
    response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    return response

# ─── PLANOS ───────────────────────────────────────────────────────────────────
PLANS = {
    "free":    {"name": "Free",    "daily_limit": 3,  "max_size_mb": 50,  "price": 0},
    "pro":     {"name": "Pro",     "daily_limit": -1, "max_size_mb": 500, "price": 7.90},
    "premium": {"name": "Premium", "daily_limit": -1, "max_size_mb": 500, "price": 14.90},
}

PACKAGES = {
    "pack_10":  {"name": "10 conversões",  "conversions": 10,  "price": 2.90},
    "pack_50":  {"name": "50 conversões",  "conversions": 50,  "price": 9.90},
    "pack_200": {"name": "200 conversões", "conversions": 200, "price": 24.90},
}

# ─── DATABASE ─────────────────────────────────────────────────────────────────
def get_db():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    return conn

def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            email VARCHAR(255) UNIQUE NOT NULL,
            password_hash VARCHAR(255) NOT NULL,
            plan VARCHAR(50) DEFAULT 'free',
            pack_conversions INTEGER DEFAULT 0,
            failed_login_attempts INTEGER DEFAULT 0,
            locked_until TIMESTAMP,
            created_at TIMESTAMP DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS conversions (
            id SERIAL PRIMARY KEY,
            user_id INTEGER REFERENCES users(id),
            filename VARCHAR(255),
            output_url TEXT,
            ip_address VARCHAR(45),
            created_at TIMESTAMP DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS payments (
            id SERIAL PRIMARY KEY,
            user_id INTEGER REFERENCES users(id),
            mp_payment_id VARCHAR(255),
            type VARCHAR(50),
            plan VARCHAR(50),
            package VARCHAR(50),
            amount DECIMAL(10,2),
            status VARCHAR(50) DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS login_attempts (
            id SERIAL PRIMARY KEY,
            ip_address VARCHAR(45),
            email VARCHAR(255),
            success BOOLEAN,
            created_at TIMESTAMP DEFAULT NOW()
        );
    """)
    conn.commit()
    cur.close()
    conn.close()

try:
    init_db()
    logger.info("✅ Banco de dados inicializado")
except Exception as e:
    logger.error(f"⚠️ Erro ao inicializar banco: {e}")

# ─── AUTH HELPERS ─────────────────────────────────────────────────────────────
def generate_token(user_id, expires_hours=24):
    payload = {
        "user_id": user_id,
        "exp": datetime.utcnow() + timedelta(hours=expires_hours),
        "iat": datetime.utcnow(),
        "jti": str(uuid.uuid4())
    }
    return jwt.encode(payload, SECRET_KEY, algorithm="HS256")

def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return jsonify({"error": "Token não fornecido"}), 401
        token = auth_header.replace("Bearer ", "").strip()
        try:
            data = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
            user_id = data["user_id"]
        except jwt.ExpiredSignatureError:
            return jsonify({"error": "Sessão expirada. Faça login novamente."}), 401
        except jwt.InvalidTokenError:
            return jsonify({"error": "Token inválido"}), 401
        return f(user_id, *args, **kwargs)
    return decorated

def validate_email(email):
    pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    return bool(re.match(pattern, email)) and len(email) <= 255

def validate_password(password):
    if len(password) < 8:
        return "Senha deve ter pelo menos 8 caracteres"
    if not re.search(r'[A-Za-z]', password):
        return "Senha deve conter pelo menos uma letra"
    if not re.search(r'[0-9]', password):
        return "Senha deve conter pelo menos um número"
    return None

def is_account_locked(user):
    if user.get("locked_until") and user["locked_until"] > datetime.utcnow():
        remaining = (user["locked_until"] - datetime.utcnow()).seconds // 60
        return True, f"Conta bloqueada. Tente novamente em {remaining} minutos."
    return False, None

def get_user(user_id):
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM users WHERE id = %s", (user_id,))
    user = cur.fetchone()
    cur.close()
    conn.close()
    return user

def get_today_conversions(user_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM conversions WHERE user_id = %s AND created_at >= CURRENT_DATE", (user_id,))
    count = cur.fetchone()[0]
    cur.close()
    conn.close()
    return count

def allowed_file(filename, mimetype):
    if len(filename) > 100 or '..' in filename or '/' in filename or '\\' in filename:
        return False
    has_ext = '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS
    has_mime = mimetype in ALLOWED_MIMETYPES
    return has_ext and has_mime

def cleanup_old_files(folder, max_age_seconds=3600):
    try:
        now = time.time()
        for filename in os.listdir(folder):
            filepath = os.path.join(folder, filename)
            if os.path.isfile(filepath) and now - os.path.getmtime(filepath) > max_age_seconds:
                os.remove(filepath)
    except Exception as e:
        logger.error(f"Erro ao limpar arquivos: {e}")

# ─── ROTAS AUTH ───────────────────────────────────────────────────────────────
@app.route("/register", methods=["POST"])
@limiter.limit("5 per minute; 20 per hour")
def register():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Dados inválidos"}), 400

    email = data.get("email", "").strip().lower()
    password = data.get("password", "")

    if not validate_email(email):
        return jsonify({"error": "Email inválido"}), 400

    pwd_error = validate_password(password)
    if pwd_error:
        return jsonify({"error": pwd_error}), 400

    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute("SELECT id FROM users WHERE email = %s", (email,))
        if cur.fetchone():
            return jsonify({"error": "Email já cadastrado"}), 409

        password_hash = generate_password_hash(password, method='pbkdf2:sha256:260000')
        cur.execute("INSERT INTO users (email, password_hash) VALUES (%s, %s) RETURNING id", (email, password_hash))
        user_id = cur.fetchone()["id"]
        conn.commit()

        logger.info(f"Novo usuário: {email}")
        token = generate_token(user_id)
        return jsonify({"token": token, "email": email, "plan": "free"}), 201
    except Exception as e:
        conn.rollback()
        logger.error(f"Erro no registro: {e}")
        return jsonify({"error": "Erro ao criar conta"}), 500
    finally:
        cur.close()
        conn.close()

@app.route("/login", methods=["POST"])
@limiter.limit("10 per minute; 50 per hour")
def login():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Dados inválidos"}), 400

    email = data.get("email", "").strip().lower()
    password = data.get("password", "")
    ip = get_remote_address()

    if not email or not password:
        return jsonify({"error": "Email e senha são obrigatórios"}), 400

    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute("SELECT * FROM users WHERE email = %s", (email,))
        user = cur.fetchone()

        if not user:
            check_password_hash("dummy_hash", password)  # timing attack prevention
            return jsonify({"error": "Email ou senha incorretos"}), 401

        locked, lock_msg = is_account_locked(user)
        if locked:
            return jsonify({"error": lock_msg}), 429

        if not check_password_hash(user["password_hash"], password):
            attempts = user["failed_login_attempts"] + 1
            lock_until = datetime.utcnow() + timedelta(minutes=15) if attempts >= 5 else None
            cur.execute("UPDATE users SET failed_login_attempts = %s, locked_until = %s WHERE id = %s", (attempts, lock_until, user["id"]))
            conn.commit()
            if attempts >= 5:
                logger.warning(f"Conta bloqueada: {email}")
            return jsonify({"error": "Email ou senha incorretos"}), 401

        cur.execute("UPDATE users SET failed_login_attempts = 0, locked_until = NULL WHERE id = %s", (user["id"],))
        conn.commit()

        token = generate_token(user["id"])
        logger.info(f"Login: {email} de {ip}")
        return jsonify({"token": token, "email": user["email"], "plan": user["plan"], "pack_conversions": user["pack_conversions"]}), 200
    finally:
        cur.close()
        conn.close()

@app.route("/me", methods=["GET"])
@token_required
def me(user_id):
    user = get_user(user_id)
    if not user:
        return jsonify({"error": "Usuário não encontrado"}), 404
    today = get_today_conversions(user_id)
    plan = PLANS.get(user["plan"], PLANS["free"])
    return jsonify({
        "email": user["email"],
        "plan": user["plan"],
        "pack_conversions": user["pack_conversions"],
        "today_conversions": today,
        "daily_limit": plan["daily_limit"],
        "max_size_mb": plan["max_size_mb"]
    }), 200

# ─── UPLOAD ───────────────────────────────────────────────────────────────────
@app.route("/upload", methods=["POST"])
@token_required
@limiter.limit("20 per hour")
def upload_video(user_id):
    user = get_user(user_id)
    plan = PLANS.get(user["plan"], PLANS["free"])
    ip = get_remote_address()

    if plan["daily_limit"] != -1:
        today = get_today_conversions(user_id)
        if today >= plan["daily_limit"] and user["pack_conversions"] <= 0:
            return jsonify({"error": f"Limite de {plan['daily_limit']} conversões diárias atingido."}), 403

    if "video" not in request.files:
        return jsonify({"error": "Nenhum arquivo enviado"}), 400

    file = request.files["video"]
    if not file or file.filename == "":
        return jsonify({"error": "Arquivo inválido"}), 400

    file.seek(0, 2)
    size_mb = file.tell() / (1024 * 1024)
    file.seek(0)
    if size_mb > plan["max_size_mb"]:
        return jsonify({"error": f"Arquivo muito grande. Seu plano permite até {plan['max_size_mb']}MB"}), 400

    if not allowed_file(file.filename, file.content_type):
        logger.warning(f"Arquivo inválido: {file.filename} de {ip}")
        return jsonify({"error": "Formato não permitido. Use: mp4, mov, avi, mkv"}), 400

    input_path = None
    output_path = None
    try:
        filename = secure_filename(file.filename)[:50]
        unique_id = str(uuid.uuid4())[:8]
        input_path = os.path.join(UPLOAD_FOLDER, f"{unique_id}_{filename}")
        output_filename = f"output-{unique_id}.mp4"
        output_path = os.path.join(OUTPUT_FOLDER, output_filename)

        file.save(input_path)

        command = [
            "ffmpeg", "-i", input_path,
            "-filter_complex", "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=20:5[bg];[0:v]scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=black@0[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
            "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", "-y",
            output_path
        ]

        result = subprocess.run(command, capture_output=True, text=True, timeout=180)

        if result.returncode != 0:
            logger.error(f"FFmpeg erro: {result.stderr[-500:]}")
            if input_path and os.path.exists(input_path):
                os.remove(input_path)
            return jsonify({"error": "Falha na conversão do vídeo"}), 500

        if not os.path.exists(output_path):
            if input_path and os.path.exists(input_path):
                os.remove(input_path)
            return jsonify({"error": "Arquivo convertido não foi gerado"}), 500

        if input_path and os.path.exists(input_path):
            os.remove(input_path)

        conn = get_db()
        cur = conn.cursor()
        try:
            cur.execute("INSERT INTO conversions (user_id, filename, output_url, ip_address) VALUES (%s, %s, %s, %s)", (user_id, filename, output_filename, ip))
            if plan["daily_limit"] != -1 and user["pack_conversions"] > 0:
                cur.execute("UPDATE users SET pack_conversions = pack_conversions - 1 WHERE id = %s", (user_id,))
            conn.commit()
        finally:
            cur.close()
            conn.close()

        cleanup_old_files(UPLOAD_FOLDER)
        cleanup_old_files(OUTPUT_FOLDER)

        download_url = f"{request.url_root.rstrip('/')}/outputs/{output_filename}"
        logger.info(f"Conversão OK: user {user_id} -> {output_filename}")
        return jsonify({"url": download_url, "filename": output_filename}), 200

    except subprocess.TimeoutExpired:
        if input_path and os.path.exists(input_path):
            os.remove(input_path)
        return jsonify({"error": "Timeout: vídeo muito grande ou complexo"}), 504
    except Exception as e:
        if input_path and os.path.exists(input_path):
            os.remove(input_path)
        logger.error(f"Erro upload: {e}")
        return jsonify({"error": "Erro interno"}), 500

@app.route("/outputs/<filename>")
@token_required
def get_file(user_id, filename):
    try:
        secure_name = secure_filename(filename)
        if secure_name != filename or '..' in filename or not filename.endswith('.mp4'):
            return jsonify({"error": "Arquivo inválido"}), 400
        return send_from_directory(OUTPUT_FOLDER, filename, as_attachment=True)
    except FileNotFoundError:
        return jsonify({"error": "Arquivo não encontrado"}), 404

# ─── PLANOS ───────────────────────────────────────────────────────────────────
@app.route("/plans", methods=["GET"])
def get_plans():
    return jsonify({"plans": PLANS, "packages": PACKAGES}), 200

# ─── PAGAMENTO ────────────────────────────────────────────────────────────────
@app.route("/payment/create", methods=["POST"])
@token_required
@limiter.limit("10 per hour")
def create_payment(user_id):
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Dados inválidos"}), 400

    payment_type = data.get("type")
    item_id = data.get("item_id", "")

    if not re.match(r'^[a-z0-9_]+$', item_id):
        return jsonify({"error": "Item inválido"}), 400

    if payment_type == "subscription":
        plan = PLANS.get(item_id)
        if not plan or plan["price"] == 0:
            return jsonify({"error": "Plano inválido"}), 400
        title = f"Plano {plan['name']} - ClipFlip"
        price = plan["price"]
    elif payment_type == "package":
        package = PACKAGES.get(item_id)
        if not package:
            return jsonify({"error": "Pacote inválido"}), 400
        title = f"{package['name']} - ClipFlip"
        price = package["price"]
    else:
        return jsonify({"error": "Tipo inválido"}), 400

    try:
        sdk = mercadopago.SDK(MP_ACCESS_TOKEN)
        preference_data = {
            "items": [{"title": title, "quantity": 1, "unit_price": price, "currency_id": "BRL"}],
            "back_urls": {
                "success": f"{FRONTEND_URL}?payment=success&type={payment_type}&item={item_id}",
                "failure": f"{FRONTEND_URL}?payment=failure",
                "pending": f"{FRONTEND_URL}?payment=pending"
            },
            "auto_return": "approved",
            "external_reference": f"{user_id}|{payment_type}|{item_id}",
            "notification_url": f"{request.url_root}payment/webhook"
        }
        preference = sdk.preference().create(preference_data)
        pref_data = preference["response"]

        conn = get_db()
        cur = conn.cursor()
        try:
            cur.execute("INSERT INTO payments (user_id, type, plan, package, amount, status) VALUES (%s, %s, %s, %s, %s, 'pending')",
                (user_id, payment_type, item_id if payment_type == "subscription" else None, item_id if payment_type == "package" else None, price))
            conn.commit()
        finally:
            cur.close()
            conn.close()

        return jsonify({"init_point": pref_data.get("init_point"), "sandbox_init_point": pref_data.get("sandbox_init_point")}), 200
    except Exception as e:
        logger.error(f"Erro pagamento: {e}")
        return jsonify({"error": "Erro ao processar pagamento"}), 500

# ─── WEBHOOK ──────────────────────────────────────────────────────────────────
@app.route("/payment/webhook", methods=["POST"])
@limiter.limit("60 per minute")
def payment_webhook():
    data = request.get_json(silent=True) or {}
    topic = data.get("type") or request.args.get("topic")
    payment_id = data.get("data", {}).get("id") or request.args.get("id")

    if topic != "payment" or not payment_id:
        return jsonify({"status": "ignored"}), 200

    try:
        sdk = mercadopago.SDK(MP_ACCESS_TOKEN)
        payment_info = sdk.payment().get(payment_id)
        payment = payment_info["response"]

        if payment.get("status") != "approved":
            return jsonify({"status": "not approved"}), 200

        external_ref = payment.get("external_reference", "")
        parts = external_ref.split("|")
        if len(parts) != 3 or not parts[0].isdigit():
            return jsonify({"status": "invalid reference"}), 200

        user_id, payment_type, item_id = int(parts[0]), parts[1], parts[2]

        conn = get_db()
        cur = conn.cursor()
        try:
            cur.execute("SELECT id FROM payments WHERE mp_payment_id = %s", (str(payment_id),))
            if cur.fetchone():
                return jsonify({"status": "already processed"}), 200

            if payment_type == "subscription" and item_id in PLANS:
                cur.execute("UPDATE users SET plan = %s WHERE id = %s", (item_id, user_id))
            elif payment_type == "package":
                package = PACKAGES.get(item_id)
                if package:
                    cur.execute("UPDATE users SET pack_conversions = pack_conversions + %s WHERE id = %s", (package["conversions"], user_id))

            cur.execute("UPDATE payments SET status = 'approved', mp_payment_id = %s WHERE user_id = %s AND status = 'pending' ORDER BY created_at DESC LIMIT 1", (str(payment_id), user_id))
            conn.commit()
            logger.info(f"Pagamento aprovado: {payment_id} user {user_id}")
        finally:
            cur.close()
            conn.close()

        return jsonify({"status": "ok"}), 200
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return jsonify({"error": "Erro interno"}), 500

# ─── HEALTH & ERRORS ──────────────────────────────────────────────────────────
@app.route("/")
def home():
    return jsonify({"status": "ok", "message": "ClipFlip Backend"}), 200

@app.route("/health")
def health():
    return jsonify({"status": "healthy"}), 200

@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": "Rota não encontrada"}), 404

@app.errorhandler(429)
def rate_limit_exceeded(e):
    return jsonify({"error": "Muitas requisições. Aguarde um momento."}), 429

@app.errorhandler(413)
def file_too_large(e):
    return jsonify({"error": "Arquivo muito grande. Máximo: 500MB"}), 413

@app.errorhandler(500)
def internal_error(e):
    logger.error(f"Erro 500: {e}")
    return jsonify({"error": "Erro interno do servidor"}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
