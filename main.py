"""
Backend Flask - Video Converter com autenticação, planos e Mercado Pago
"""

from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
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

app = Flask(__name__)
CORS(app, origins="*")

# ─── CONFIG ───────────────────────────────────────────────────────────────────
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024
SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-key-change-in-production")
DATABASE_URL = os.environ.get("DATABASE_URL")
MP_ACCESS_TOKEN = os.environ.get("MP_ACCESS_TOKEN")
FRONTEND_URL = os.environ.get("FRONTEND_URL", "https://video-converter-17tx.vercel.app")

UPLOAD_FOLDER = "uploads"
OUTPUT_FOLDER = "outputs"
ALLOWED_EXTENSIONS = {'mp4', 'mov', 'avi', 'mkv'}
ALLOWED_MIMETYPES = {'video/mp4', 'video/quicktime', 'video/x-msvideo', 'video/x-matroska'}

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(OUTPUT_FOLDER, exist_ok=True)

# ─── PLANOS ───────────────────────────────────────────────────────────────────
PLANS = {
    "free":    {"name": "Free",    "daily_limit": 3,  "max_size_mb": 50,  "price": 0,     "type": "subscription"},
    "pro":     {"name": "Pro",     "daily_limit": -1, "max_size_mb": 500, "price": 7.90,  "type": "subscription"},
    "premium": {"name": "Premium", "daily_limit": -1, "max_size_mb": 500, "price": 14.90, "type": "subscription"},
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
    """Cria tabelas se não existirem"""
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            email VARCHAR(255) UNIQUE NOT NULL,
            password_hash VARCHAR(255) NOT NULL,
            plan VARCHAR(50) DEFAULT 'free',
            pack_conversions INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS conversions (
            id SERIAL PRIMARY KEY,
            user_id INTEGER REFERENCES users(id),
            filename VARCHAR(255),
            output_url TEXT,
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
    """)
    conn.commit()
    cur.close()
    conn.close()

# Inicializa banco ao subir
try:
    init_db()
    print("✅ Banco de dados inicializado")
except Exception as e:
    print(f"⚠️ Erro ao inicializar banco: {e}")

# ─── AUTH ─────────────────────────────────────────────────────────────────────
def generate_token(user_id):
    payload = {
        "user_id": user_id,
        "exp": datetime.utcnow() + timedelta(days=7)
    }
    return jwt.encode(payload, SECRET_KEY, algorithm="HS256")

def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get("Authorization", "").replace("Bearer ", "")
        if not token:
            return jsonify({"error": "Token não fornecido"}), 401
        try:
            data = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
            user_id = data["user_id"]
        except jwt.ExpiredSignatureError:
            return jsonify({"error": "Token expirado"}), 401
        except jwt.InvalidTokenError:
            return jsonify({"error": "Token inválido"}), 401
        return f(user_id, *args, **kwargs)
    return decorated

# ─── HELPERS ──────────────────────────────────────────────────────────────────
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
    cur.execute("""
        SELECT COUNT(*) FROM conversions
        WHERE user_id = %s AND created_at >= CURRENT_DATE
    """, (user_id,))
    count = cur.fetchone()[0]
    cur.close()
    conn.close()
    return count

def allowed_file(filename, mimetype):
    has_ext = '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS
    has_mime = mimetype in ALLOWED_MIMETYPES
    return has_ext and has_mime

def cleanup_old_files(folder, max_age_seconds=3600):
    try:
        now = time.time()
        for filename in os.listdir(folder):
            filepath = os.path.join(folder, filename)
            if os.path.isfile(filepath):
                if now - os.path.getmtime(filepath) > max_age_seconds:
                    os.remove(filepath)
    except Exception as e:
        print(f"Erro ao limpar arquivos: {e}")

# ─── ROTAS AUTH ───────────────────────────────────────────────────────────────
@app.route("/register", methods=["POST"])
def register():
    data = request.get_json()
    email = data.get("email", "").strip().lower()
    password = data.get("password", "")

    if not email or not password:
        return jsonify({"error": "Email e senha são obrigatórios"}), 400
    if len(password) < 6:
        return jsonify({"error": "Senha deve ter pelo menos 6 caracteres"}), 400

    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute("SELECT id FROM users WHERE email = %s", (email,))
        if cur.fetchone():
            return jsonify({"error": "Email já cadastrado"}), 409

        password_hash = generate_password_hash(password)
        cur.execute(
            "INSERT INTO users (email, password_hash) VALUES (%s, %s) RETURNING id",
            (email, password_hash)
        )
        user_id = cur.fetchone()["id"]
        conn.commit()
        token = generate_token(user_id)
        return jsonify({"token": token, "email": email, "plan": "free"}), 201
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        cur.close()
        conn.close()

@app.route("/login", methods=["POST"])
def login():
    data = request.get_json()
    email = data.get("email", "").strip().lower()
    password = data.get("password", "")

    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute("SELECT * FROM users WHERE email = %s", (email,))
        user = cur.fetchone()
        if not user or not check_password_hash(user["password_hash"], password):
            return jsonify({"error": "Email ou senha incorretos"}), 401

        token = generate_token(user["id"])
        return jsonify({
            "token": token,
            "email": user["email"],
            "plan": user["plan"],
            "pack_conversions": user["pack_conversions"]
        }), 200
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

# ─── ROTA UPLOAD ──────────────────────────────────────────────────────────────
@app.route("/upload", methods=["POST"])
@token_required
def upload_video(user_id):
    user = get_user(user_id)
    plan = PLANS.get(user["plan"], PLANS["free"])

    # Verificar limite
    if plan["daily_limit"] != -1:
        today = get_today_conversions(user_id)
        if today >= plan["daily_limit"] and user["pack_conversions"] <= 0:
            return jsonify({
                "error": f"Limite de {plan['daily_limit']} conversões diárias atingido. Faça upgrade do plano!"
            }), 403

    if "video" not in request.files:
        return jsonify({"error": "Nenhum arquivo enviado"}), 400

    file = request.files["video"]
    if file.filename == "":
        return jsonify({"error": "Nome de arquivo vazio"}), 400

    # Verificar tamanho
    file.seek(0, 2)
    size_mb = file.tell() / (1024 * 1024)
    file.seek(0)
    if size_mb > plan["max_size_mb"]:
        return jsonify({"error": f"Arquivo muito grande. Seu plano permite até {plan['max_size_mb']}MB"}), 400

    if not allowed_file(file.filename, file.content_type):
        return jsonify({"error": "Formato não permitido. Use: mp4, mov, avi, mkv"}), 400

    try:
        filename = secure_filename(file.filename)
        unique_id = str(uuid.uuid4())[:8]
        input_filename = f"{unique_id}_{filename}"
        output_filename = f"output-{unique_id}.mp4"
        input_path = os.path.join(UPLOAD_FOLDER, input_filename)
        output_path = os.path.join(OUTPUT_FOLDER, output_filename)

        file.save(input_path)

        command = [
            "ffmpeg",
            "-i", input_path,
            "-vf", "split[a][b];[a]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,gblur=sigma=20[bg];[b]scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=black@0[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2",
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-crf", "28",
            "-c:a", "aac",
            "-b:a", "128k",
            "-y",
            output_path
        ]

        result = subprocess.run(command, capture_output=True, text=True, timeout=120)

        if result.returncode != 0:
            if os.path.exists(input_path):
                os.remove(input_path)
            return jsonify({"error": "Falha na conversão do vídeo", "details": result.stderr[-500:]}), 500

        if not os.path.exists(output_path):
            if os.path.exists(input_path):
                os.remove(input_path)
            return jsonify({"error": "Arquivo convertido não foi gerado"}), 500

        if os.path.exists(input_path):
            os.remove(input_path)

        # Registrar conversão
        conn = get_db()
        cur = conn.cursor()
        try:
            cur.execute(
                "INSERT INTO conversions (user_id, filename, output_url) VALUES (%s, %s, %s)",
                (user_id, filename, output_filename)
            )
            # Se usou pacote avulso, desconta
            if plan["daily_limit"] != -1:
                today = get_today_conversions(user_id)
                if today > plan["daily_limit"] and user["pack_conversions"] > 0:
                    cur.execute(
                        "UPDATE users SET pack_conversions = pack_conversions - 1 WHERE id = %s",
                        (user_id,)
                    )
            conn.commit()
        finally:
            cur.close()
            conn.close()

        cleanup_old_files(UPLOAD_FOLDER)
        cleanup_old_files(OUTPUT_FOLDER)

        base_url = request.url_root.rstrip('/')
        download_url = f"{base_url}/outputs/{output_filename}"
        return jsonify({"url": download_url, "filename": output_filename}), 200

    except subprocess.TimeoutExpired:
        if os.path.exists(input_path):
            os.remove(input_path)
        return jsonify({"error": "Timeout: vídeo muito grande ou complexo"}), 504
    except Exception as e:
        if 'input_path' in locals() and os.path.exists(input_path):
            os.remove(input_path)
        return jsonify({"error": "Erro interno", "details": str(e)}), 500

@app.route("/outputs/<filename>")
def get_file(filename):
    try:
        secure_name = secure_filename(filename)
        if secure_name != filename:
            return jsonify({"error": "Nome de arquivo inválido"}), 400
        return send_from_directory(OUTPUT_FOLDER, filename, as_attachment=True)
    except FileNotFoundError:
        return jsonify({"error": "Arquivo não encontrado"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ─── PLANOS INFO ──────────────────────────────────────────────────────────────
@app.route("/plans", methods=["GET"])
def get_plans():
    return jsonify({"plans": PLANS, "packages": PACKAGES}), 200

# ─── PAGAMENTO - CRIAR PREFERÊNCIA ────────────────────────────────────────────
@app.route("/payment/create", methods=["POST"])
@token_required
def create_payment(user_id):
    data = request.get_json()
    payment_type = data.get("type")  # "subscription" ou "package"
    item_id = data.get("item_id")    # ex: "pro", "pack_10"

    user = get_user(user_id)

    if payment_type == "subscription":
        plan = PLANS.get(item_id)
        if not plan or plan["price"] == 0:
            return jsonify({"error": "Plano inválido"}), 400
        title = f"Plano {plan['name']} - ClipFlip"
        price = plan["price"]
        package_id = None
    elif payment_type == "package":
        package = PACKAGES.get(item_id)
        if not package:
            return jsonify({"error": "Pacote inválido"}), 400
        title = f"{package['name']} - ClipFlip"
        price = package["price"]
        package_id = item_id
    else:
        return jsonify({"error": "Tipo inválido"}), 400

    try:
        sdk = mercadopago.SDK(MP_ACCESS_TOKEN)

        preference_data = {
            "items": [{
                "title": title,
                "quantity": 1,
                "unit_price": price,
                "currency_id": "BRL"
            }],
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

        # Registrar pagamento pendente
        conn = get_db()
        cur = conn.cursor()
        try:
            cur.execute("""
                INSERT INTO payments (user_id, type, plan, package, amount, status)
                VALUES (%s, %s, %s, %s, %s, 'pending')
            """, (user_id, payment_type, item_id if payment_type == "subscription" else None,
                  item_id if payment_type == "package" else None, price))
            conn.commit()
        finally:
            cur.close()
            conn.close()

        return jsonify({
            "init_point": pref_data.get("init_point"),
            "sandbox_init_point": pref_data.get("sandbox_init_point")
        }), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ─── WEBHOOK MERCADO PAGO ─────────────────────────────────────────────────────
@app.route("/payment/webhook", methods=["POST"])
def payment_webhook():
    data = request.get_json() or {}
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
        if len(parts) != 3:
            return jsonify({"status": "invalid reference"}), 200

        user_id, payment_type, item_id = parts
        user_id = int(user_id)

        conn = get_db()
        cur = conn.cursor()
        try:
            if payment_type == "subscription":
                cur.execute("UPDATE users SET plan = %s WHERE id = %s", (item_id, user_id))
            elif payment_type == "package":
                package = PACKAGES.get(item_id)
                if package:
                    cur.execute(
                        "UPDATE users SET pack_conversions = pack_conversions + %s WHERE id = %s",
                        (package["conversions"], user_id)
                    )
            cur.execute("""
                UPDATE payments SET status = 'approved', mp_payment_id = %s
                WHERE user_id = %s AND status = 'pending'
                ORDER BY created_at DESC LIMIT 1
            """, (str(payment_id), user_id))
            conn.commit()
        finally:
            cur.close()
            conn.close()

        return jsonify({"status": "ok"}), 200

    except Exception as e:
        print(f"Webhook error: {e}")
        return jsonify({"error": str(e)}), 500

# ─── HEALTH ───────────────────────────────────────────────────────────────────
@app.route("/")
def home():
    return jsonify({"status": "ok", "message": "ClipFlip Backend"}), 200

@app.route("/health")
def health():
    return jsonify({"status": "healthy"}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
