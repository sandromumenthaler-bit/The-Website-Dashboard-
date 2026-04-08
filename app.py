from gevent import monkey
monkey.patch_all()

import os
import json
import subprocess
import signal
from flask import Flask, render_template, request, redirect, url_for, jsonify, session, send_from_directory
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from flask_socketio import SocketIO, emit
from threading import Thread
import time
import sys
import shutil
import requests
import base64

# Setup absolute paths for Render and Local environments
base_dir = os.path.abspath(os.path.dirname(__file__))
DATA_DIR = os.path.join(base_dir, 'data')

if not os.path.exists(DATA_DIR):
    os.makedirs(DATA_DIR)

# Configuration for Persistence
USER_DATA_FILE = os.path.join(DATA_DIR, 'users.json')
BOT_SCRIPT_PATH = os.path.join(DATA_DIR, 'bot.py')
INDEX_JSON_PATH = os.path.join(DATA_DIR, 'index.json')
UPLOAD_FOLDER = os.path.join(DATA_DIR, 'images')

if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

app = Flask(__name__, 
            template_folder=os.path.join(base_dir, 'templates'),
            static_folder=os.path.join(base_dir, 'static'))
app.config['SECRET_KEY'] = 'secret-key-for-now'
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

# Initialize data files from defaults if they don't exist in data/
if not os.path.exists(USER_DATA_FILE):
    with open(USER_DATA_FILE, 'w') as f:
        json.dump({'test': 'test'}, f)

# Editable files whitelist
EDITABLE_FILES = ['bot.py', 'requirements.txt', 'index.json', 'Procfile', 'runtime.txt', 'static/style.css', 'templates/index.html']

# GitHub Configuration from Environment Variables
GITHUB_TOKEN = os.getenv('GITHUB_TOKEN')
GITHUB_REPO = os.getenv('GITHUB_REPO')
if GITHUB_REPO:
    # Sanitize: if it's a full URL, extract user/repo
    if 'github.com/' in GITHUB_REPO:
        GITHUB_REPO = GITHUB_REPO.split('github.com/')[-1].split('?')[0].split('#')[0].strip('/')
    # Remove .git suffix if present
    if GITHUB_REPO.endswith('.git'):
        GITHUB_REPO = GITHUB_REPO[:-4]

GITHUB_BRANCH = os.getenv('GITHUB_BRANCH', 'main')
RENDER_DEPLOY_HOOK = os.getenv('RENDER_DEPLOY_HOOK')

if not os.path.exists(INDEX_JSON_PATH):
    if os.path.exists(os.path.join(base_dir, 'index.json')):
        shutil.copy(os.path.join(base_dir, 'index.json'), INDEX_JSON_PATH)
    else:
        with open(INDEX_JSON_PATH, 'w') as f:
            json.dump({}, f)

if not os.path.exists(BOT_SCRIPT_PATH):
    if os.path.exists(os.path.join(base_dir, 'bot.py')):
        shutil.copy(os.path.join(base_dir, 'bot.py'), BOT_SCRIPT_PATH)

# Migration: copy existing images to data/images if they aren't there yet
if os.path.exists(os.path.join(base_dir, 'images')):
    for item in os.listdir(os.path.join(base_dir, 'images')):
        s = os.path.join(base_dir, 'images', item)
        d = os.path.join(UPLOAD_FOLDER, item)
        if os.path.isfile(s) and not os.path.exists(d):
            try:
                shutil.copy2(s, d)
            except:
                pass

socketio = SocketIO(app, cors_allowed_origins="*", async_mode='gevent')
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

# User management
def load_users():
    with open(USER_DATA_FILE, 'r') as f:
        return json.load(f)

def save_users(users):
    with open(USER_DATA_FILE, 'w') as f:
        json.dump(users, f)

class User(UserMixin):
    def __init__(self, id):
        self.id = id

@login_manager.user_loader
def load_user(user_id):
    users = load_users()
    if user_id in users:
        return User(user_id)
    return None

# Bot Process Management
bot_process = None
bot_thread = None

def bot_monitor():
    global bot_process
    while True:
        if bot_process:
            line = bot_process.stdout.readline()
            if line:
                socketio.emit('bot_log', {'data': line.decode('utf-8')})
            if bot_process.poll() is not None:
                socketio.emit('bot_log', {'data': '--- Bot process terminated ---\n'})
                bot_process = None
        time.sleep(0.1)

# Start monitor thread
monitor_thread = Thread(target=bot_monitor, daemon=True)
monitor_thread.start()

@app.route('/static/images/<path:filename>')
def custom_static(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/login', methods=['POST'])
def login():
    username = request.form.get('username')
    password = request.form.get('password')
    users = load_users()
    if username in users and users[username] == password:
        user = User(username)
        login_user(user)
        return redirect(url_for('index'))
    return "Invalid credentials", 401

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('index'))

@app.route('/update_user', methods=['POST'])
@login_required
def update_user():
    new_username = request.form.get('new_username')
    new_password = request.form.get('new_password')
    users = load_users()
    # Remove old user, add new one (simplified as there is only one user intended)
    old_username = current_user.id
    if old_username in users:
        del users[old_username]
    users[new_username] = new_password
    save_users(users)
    logout_user()
    return redirect(url_for('index'))

@app.route('/get_script')
@login_required
def get_script():
    filename = request.args.get('file', 'bot.py')
    if filename not in EDITABLE_FILES:
        return jsonify({'error': 'Unauthorized file'}), 403
    
    # Try data dir first, then root
    path = os.path.join(DATA_DIR, filename)
    if not os.path.exists(path):
        path = os.path.join(base_dir, filename)
        
    if not os.path.exists(path):
        return jsonify({'content': f'# File {filename} not found locally.'})

    with open(path, 'r', encoding='utf-8') as f:
        return jsonify({'content': f.read()})

@app.route('/list_files')
@login_required
def list_files():
    return jsonify({'files': EDITABLE_FILES})

@app.route('/save_script', methods=['POST'])
@login_required
def save_script():
    content = request.json.get('content')
    filename = request.json.get('file', 'bot.py')
    push = request.json.get('push', False)
    
    if filename not in EDITABLE_FILES:
        return jsonify({'status': 'Unauthorized file'}), 403
    
    # Save locally to data folder
    local_path = os.path.join(DATA_DIR, filename)
    os.makedirs(os.path.dirname(local_path), exist_ok=True)
    with open(local_path, 'w', encoding='utf-8') as f:
        f.write(content)
    
    # Also save to root if it exists there (for GitHub push consistency)
    root_path = os.path.join(base_dir, filename)
    if os.path.exists(root_path):
        os.makedirs(os.path.dirname(root_path), exist_ok=True)
        with open(root_path, 'w', encoding='utf-8') as f:
            f.write(content)

    status_msg = f'File {filename} saved locally.'

    if push:
        if not GITHUB_TOKEN or not GITHUB_REPO:
            return jsonify({'status': 'Saved locally, but GITHUB_TOKEN or GITHUB_REPO not set!'})
        
        try:
            # 1. Get current file info for SHA
            api_url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{filename}"
            headers = {
                "Authorization": f"token {GITHUB_TOKEN}",
                "Accept": "application/vnd.github.v3+json"
            }
            
            r = requests.get(api_url, headers=headers)
            sha = ""
            if r.status_code == 200:
                sha = r.json().get('sha')
            
            # 2. Update file on GitHub
            payload = {
                "message": f"Update {filename} from Dashboard",
                "content": base64.b64encode(content.encode('utf-8')).decode('utf-8'),
                "branch": GITHUB_BRANCH
            }
            if sha:
                payload["sha"] = sha
                
            r = requests.put(api_url, headers=headers, json=payload)
            if r.status_code in [200, 201]:
                status_msg = f'File {filename} saved and pushed to GitHub! Bot service should restart shortly.'
            else:
                try:
                    err_msg = r.json().get('message', r.text)
                except:
                    err_msg = r.text
                status_msg = f'Saved locally, but GitHub error: {err_msg}'
        except Exception as e:
            status_msg = f'Saved locally, but error pushing to GitHub: {str(e)}'

    # Local restart logic
    global bot_process
    # Always try to restart if bot.py was edited and it's currently running
    if bot_process is not None and filename == 'bot.py':
        try:
            # Stop the bot properly
            if os.name == 'nt':
                bot_process.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                bot_process.terminate()
            
            # Wait for it to exit
            try:
                bot_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                bot_process.kill()
            
            bot_process = None
            start_bot()
            status_msg += " Bot restarted locally."
        except Exception as e:
            status_msg += f" Error restarting locally: {str(e)}"
    
    return jsonify({'status': status_msg})

@app.route('/trigger_deploy', methods=['POST'])
@login_required
def trigger_deploy():
    if not RENDER_DEPLOY_HOOK:
        return jsonify({'status': 'RENDER_DEPLOY_HOOK not set in Environment Variables.'})
    try:
        r = requests.post(RENDER_DEPLOY_HOOK)
        return jsonify({'status': f'Deploy triggered! Response: {r.status_code}'})
    except Exception as e:
        return jsonify({'status': f'Error triggering deploy: {str(e)}'})

@app.route('/bot_status')
@login_required
def get_bot_status():
    return jsonify({'running': bot_process is not None})

@app.route('/start_bot', methods=['POST'])
@login_required
def start_bot():
    global bot_process
    if bot_process is None:
        try:
            bot_process = subprocess.Popen(
                [sys.executable, BOT_SCRIPT_PATH],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=1,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0,
                cwd=os.getcwd()
            )
            return jsonify({'status': 'Bot started'})
        except Exception as e:
            return jsonify({'status': f'Error starting bot: {str(e)}'})
    return jsonify({'status': 'Bot already running'})

@app.route('/stop_bot', methods=['POST'])
@login_required
def stop_bot():
    global bot_process
    if bot_process:
        try:
            if os.name == 'nt':
                bot_process.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                bot_process.terminate()
            
            try:
                bot_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                bot_process.kill()
            
            bot_process = None
            return jsonify({'status': 'Bot stopped'})
        except Exception as e:
            bot_process = None # Ensure it's cleared anyway
            return jsonify({'status': f'Error stopping bot: {str(e)}'})
    return jsonify({'status': 'Bot is not running'})

@app.route('/get_children')
@login_required
def get_children():
    if os.path.exists(INDEX_JSON_PATH):
        with open(INDEX_JSON_PATH, 'r', encoding='utf-8') as f:
            return jsonify(json.load(f))
    return jsonify({})

@app.route('/add_image', methods=['POST'])
@login_required
def add_image():
    name = request.form.get('name')
    rarity = request.form.get('rarity')
    file = request.files.get('file')
    
    with open(INDEX_JSON_PATH, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    pic_link = ""
    if file:
        filename = file.filename
        file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(file_path)
        pic_link = f"/static/images/{filename}" # Note: We need a way to serve this
    
    data[name] = {
        "pic_link": pic_link,
        "rarity": rarity
    }
    
    with open(INDEX_JSON_PATH, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=4)
    
    return jsonify({'status': 'Image/Vehicle added successfully'})

@app.route('/delete_image', methods=['POST'])
@login_required
def delete_image():
    name = request.json.get('name')
    with open(INDEX_JSON_PATH, 'r', encoding='utf-8') as f:
        data = json.load(f)
    if name in data:
        del data[name]
        with open(INDEX_JSON_PATH, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=4)
        return jsonify({'status': f'{name} deleted'})
    return jsonify({'status': 'Not found'}), 404

@app.route('/edit_image', methods=['POST'])
@login_required
def edit_image():
    old_name = request.json.get('old_name')
    new_name = request.json.get('new_name')
    rarity = request.json.get('rarity')
    
    with open(INDEX_JSON_PATH, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    if old_name in data:
        info = data.pop(old_name)
        info['rarity'] = rarity
        data[new_name] = info
        with open(INDEX_JSON_PATH, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=4)
        return jsonify({'status': 'Updated successfully'})
    return jsonify({'status': 'Not found'}), 404

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    socketio.run(app, host='0.0.0.0', port=port)
