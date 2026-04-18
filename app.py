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

@app.route("/upload", methods=["POST"])
def upload_video():
    file = request.files["video"]
    
    input_path = os.path.join(UPLOAD_FOLDER, file.filename)
    file.save(input_path)

    output_filename = f"output-{int(time.time())}.mp4"
    output_path = os.path.join(OUTPUT_FOLDER, output_filename)

    command = [
        "ffmpeg",
        "-i", input_path,
        "-vf", "crop=ih*9/16:ih,scale=1080:1920",
        output_path
    ]

    subprocess.run(command)

    return jsonify({
        "url": f"http://localhost:5000/outputs/{output_filename}"
    })

@app.route("/outputs/<filename>")
def get_file(filename):
    return send_from_directory(OUTPUT_FOLDER, filename)

if __name__ == "__main__":
    app.run(port=5000)