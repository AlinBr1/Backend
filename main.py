from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import os
import subprocess
import time

app = Flask(__name__)
CORS(app)

UPLOAD_FOLDER = "uploads"
OUTPUT_FOLDER = "outputs"

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(OUTPUT_FOLDER, exist_ok=True)

# 🔥 ROTA PRINCIPAL (teste)
@app.route("/")
def home():
    return "Backend rodando!"

# 🚀 UPLOAD E CONVERSÃO
@app.route("/upload", methods=["POST"])
def upload_video():
    if "video" not in request.files:
        return jsonify({"error": "Nenhum arquivo enviado"}), 400

    file = request.files["video"]

    input_path = os.path.join(UPLOAD_FOLDER, file.filename)
    file.save(input_path)

    output_filename = f"output-{int(time.time())}.mp4"
    output_path = os.path.join(OUTPUT_FOLDER, output_filename)

    # 🔥 AJUSTE PARA NÃO CORTAR LEGENDA (melhor crop + centralização)
    command = [
        "ffmpeg",
        "-i", input_path,
        "-vf", "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2",
        "-c:a", "copy",
        output_path
    ]

    subprocess.run(command)

    # 🔥 URL DINÂMICA (FUNCIONA ONLINE)
    base_url = request.host_url

    return jsonify({
        "url": f"{base_url}outputs/{output_filename}"
    })

# 📥 DOWNLOAD DO VÍDEO
@app.route("/outputs/<filename>")
def get_file(filename):
    return send_from_directory(OUTPUT_FOLDER, filename)

# ❌ NÃO USAR app.run() EM PRODUÇÃO COM GUNICORN