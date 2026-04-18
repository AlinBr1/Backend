"""
Backend Flask para conversão de vídeos para formato TikTok (1080x1920)

CORREÇÕES APLICADAS:
- Rota /outputs/<filename> corrigida
- Validação de arquivos
- Tratamento de erro do FFmpeg
- Limpeza de arquivos
- UUID para nomes únicos
- Healthcheck
- CORS configurado
"""

from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from werkzeug.utils import secure_filename
import subprocess
import os
import time
import uuid

app = Flask(__name__)

# Configuração CORS - adicione seu domínio Vercel aqui
CORS(app, origins=[
    "http://localhost:3000",
    "https://*.vercel.app",
    "https://backend-alinbr1.up.railway.app"
])

# Configuração de upload
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024  # 500MB
UPLOAD_FOLDER = "uploads"
OUTPUT_FOLDER = "outputs"
ALLOWED_EXTENSIONS = {'mp4', 'mov', 'avi', 'mkv'}
ALLOWED_MIMETYPES = {'video/mp4', 'video/quicktime', 'video/x-msvideo', 'video/x-matroska'}

# Criar pastas se não existirem
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(OUTPUT_FOLDER, exist_ok=True)

def allowed_file(filename, mimetype):
    """Valida extensão e MIME type do arquivo"""
    has_extension = '.' in filename and \
                   filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS
    has_valid_mime = mimetype in ALLOWED_MIMETYPES
    return has_extension and has_valid_mime

def cleanup_old_files(folder, max_age_seconds=3600):
    """Remove arquivos mais antigos que max_age_seconds"""
    try:
        now = time.time()
        for filename in os.listdir(folder):
            filepath = os.path.join(folder, filename)
            if os.path.isfile(filepath):
                if now - os.path.getmtime(filepath) > max_age_seconds:
                    os.remove(filepath)
    except Exception as e:
        print(f"Erro ao limpar arquivos: {e}")

@app.route("/")
def home():
    """Health check básico"""
    return jsonify({"status": "ok", "message": "Backend rodando!"}), 200

@app.route("/health")
def health():
    """Health check detalhado"""
    return jsonify({
        "status": "healthy",
        "ffmpeg": os.system("which ffmpeg") == 0 or os.system("where ffmpeg") == 0,
        "folders": {
            "uploads": os.path.exists(UPLOAD_FOLDER),
            "outputs": os.path.exists(OUTPUT_FOLDER)
        }
    }), 200

@app.route("/upload", methods=["POST"])
def upload_video():
    """Upload e conversão de vídeo para formato TikTok"""
    
    # Validar presença do arquivo
    if "video" not in request.files:
        return jsonify({"error": "Nenhum arquivo enviado"}), 400
    
    file = request.files["video"]
    
    # Validar nome do arquivo
    if file.filename == "":
        return jsonify({"error": "Nome de arquivo vazio"}), 400
    
    # Validar extensão e MIME type
    if not allowed_file(file.filename, file.content_type):
        return jsonify({
            "error": f"Formato não permitido. Use: {', '.join(ALLOWED_EXTENSIONS)}"
        }), 400
    
    try:
        # Gerar nomes únicos
        filename = secure_filename(file.filename)
        unique_id = str(uuid.uuid4())[:8]
        input_filename = f"{unique_id}_{filename}"
        output_filename = f"output-{unique_id}.mp4"
        
        input_path = os.path.join(UPLOAD_FOLDER, input_filename)
        output_path = os.path.join(OUTPUT_FOLDER, output_filename)
        
        # Salvar arquivo original
        file.save(input_path)
        
        # Comando FFmpeg para conversão vertical 1080x1920
        command = [
            "ffmpeg",
            "-i", input_path,
            "-vf", "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2",
            "-c:v", "libx264",
            "-preset", "medium",
            "-crf", "23",
            "-c:a", "aac",
            "-b:a", "128k",
            "-y",  # Sobrescrever sem perguntar
            output_path
        ]
        
        # Executar FFmpeg com timeout e captura de erro
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=120  # 2 minutos de timeout
        )
        
        # Verificar se FFmpeg teve sucesso
        if result.returncode != 0:
            # Limpar arquivo de entrada
            if os.path.exists(input_path):
                os.remove(input_path)
            
            return jsonify({
                "error": "Falha na conversão do vídeo",
                "details": result.stderr[:500]  # Primeiros 500 chars do erro
            }), 500
        
        # Verificar se arquivo de saída foi criado
        if not os.path.exists(output_path):
            if os.path.exists(input_path):
                os.remove(input_path)
            return jsonify({"error": "Arquivo convertido não foi gerado"}), 500
        
        # Limpar arquivo de entrada
        if os.path.exists(input_path):
            os.remove(input_path)
        
        # Limpar arquivos antigos (mais de 1 hora)
        cleanup_old_files(UPLOAD_FOLDER)
        cleanup_old_files(OUTPUT_FOLDER)
        
        # Retornar URL do arquivo convertido
        base_url = request.url_root.rstrip('/')
        download_url = f"{base_url}/outputs/{output_filename}"
        
        return jsonify({
            "url": download_url,
            "filename": output_filename
        }), 200
        
    except subprocess.TimeoutExpired:
        # Limpar em caso de timeout
        if os.path.exists(input_path):
            os.remove(input_path)
        return jsonify({"error": "Timeout: vídeo muito grande ou complexo"}), 504
    
    except Exception as e:
        # Limpar em caso de erro
        if 'input_path' in locals() and os.path.exists(input_path):
            os.remove(input_path)
        
        return jsonify({
            "error": "Erro interno do servidor",
            "details": str(e)
        }), 500

@app.route("/outputs/<filename>")
def get_file(filename):
    """Servir arquivo convertido para download"""
    try:
        # Validar filename para prevenir path traversal
        secure_name = secure_filename(filename)
        if secure_name != filename:
            return jsonify({"error": "Nome de arquivo inválido"}), 400
        
        return send_from_directory(OUTPUT_FOLDER, filename, as_attachment=True)
    
    except FileNotFoundError:
        return jsonify({"error": "Arquivo não encontrado"}), 404
    
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    # Não usar em produção - usar Gunicorn
    app.run(host="0.0.0.0", port=5000, debug=False)
