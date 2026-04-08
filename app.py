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
EDITABLE_FILES = ['bot.py', 'index.json']

# GitHub Configuration from Environment Variables
GITHUB_TOKEN = os.getenv('GITHUB_TOKEN')
GITHUB_REPO = os.getenv('GITHUB_REPO')
GITHUB_BRANCH = os.getenv('GITHUB_BRANCH', 'main')
RENDER_DEPLOY_HOOK = os.getenv('RENDER_DEPLOY_HOOK')
RENDER_API_KEY = os.getenv('RENDER_API_KEY')
RENDER_BOT_SERVICE_ID = os.getenv('RENDER_BOT_SERVICE_ID')

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
# (Local process management removed as per user request to move to Render)

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
    with open(local_path, 'w', encoding='utf-8') as f:
        f.write(content)
    
    # Also save to root if it exists there (for GitHub push consistency)
    root_path = os.path.join(base_dir, filename)
    if os.path.exists(root_path):
        with open(root_path, 'w', encoding='utf-8') as f:
            f.write(content)

    status_msg = f'File {filename} saved locally.'

    if push:
        if not GITHUB_TOKEN or not GITHUB_REPO:
            return jsonify({'status': 'Saved locally, but GITHUB_TOKEN or GITHUB_REPO not set!'})
        
        try:
            # 1. Get current file info for SHA
            # Ensure GITHUB_REPO is in the format owner/repo
            repo = GITHUB_REPO.strip()
            
            # Clean up repo name if it's a full URL
            if "github.com/" in repo:
                repo = repo.split("github.com/")[-1].strip("/")
            # Also handle possible .git suffix
            if repo.endswith(".git"):
                repo = repo[:-4]
            
            if not "/" in repo:
                return jsonify({'status': f'Saved locally, but GITHUB_REPO "{repo}" is not in the format "owner/repo"!'})

            api_url = f"https://api.github.com/repos/{repo}/contents/{filename}"
            headers = {
                "Authorization": f"token {GITHUB_TOKEN}",
                "Accept": "application/vnd.github.v3+json"
            }
            
            # Use branch query param for GET to be sure we check the right branch
            get_params = {"ref": GITHUB_BRANCH}
            r = requests.get(api_url, headers=headers, params=get_params)
            sha = ""
            if r.status_code == 200:
                sha = r.json().get('sha')
            elif r.status_code == 404:
                # This could mean either the file doesn't exist OR the repo/branch doesn't exist.
                # If the repo/branch doesn't exist, the PUT will also fail with 404.
                pass
            elif r.status_code == 401:
                return jsonify({'status': f'Saved locally, but GitHub Unauthorized! Check your GITHUB_TOKEN.'})
            else:
                return jsonify({'status': f'Saved locally, but GitHub error (GET): {r.status_code} {r.text}'})
            
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
            elif r.status_code == 404:
                 status_msg = f'Saved locally, but GitHub error (PUT): 404 Not Found. This usually means your GITHUB_REPO ("{repo}") or GITHUB_BRANCH ("{GITHUB_BRANCH}") is incorrect.'
            else:
                status_msg = f'Saved locally, but GitHub error (PUT): {r.status_code} {r.text}'
        except Exception as e:
            status_msg = f'Saved locally, but error pushing to GitHub: {str(e)}'

    # Local restart logic removed as per user request
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

@app.route('/start_render', methods=['POST'])
@login_required
def start_render():
    if not RENDER_API_KEY or not RENDER_BOT_SERVICE_ID:
        return jsonify({'status': 'RENDER_API_KEY or RENDER_BOT_SERVICE_ID not set in Environment Variables.'})
    try:
        url = f"https://api.render.com/v1/services/{RENDER_BOT_SERVICE_ID}/resume"
        headers = {"Authorization": f"Bearer {RENDER_API_KEY}", "Accept": "application/json"}
        r = requests.post(url, headers=headers)
        if r.status_code in [200, 201, 204]:
            return jsonify({'status': 'Bot service resumed successfully!'})
        else:
            return jsonify({'status': f'Error resuming service: {r.status_code} {r.text}'})
    except Exception as e:
        return jsonify({'status': f'Error calling Render API: {str(e)}'})

@app.route('/stop_render', methods=['POST'])
@login_required
def stop_render():
    if not RENDER_API_KEY or not RENDER_BOT_SERVICE_ID:
        return jsonify({'status': 'RENDER_API_KEY or RENDER_BOT_SERVICE_ID not set in Environment Variables.'})
    try:
        url = f"https://api.render.com/v1/services/{RENDER_BOT_SERVICE_ID}/suspend"
        headers = {"Authorization": f"Bearer {RENDER_API_KEY}", "Accept": "application/json"}
        r = requests.post(url, headers=headers)
        if r.status_code in [200, 201, 204]:
            return jsonify({'status': 'Bot service suspended successfully!'})
        else:
            return jsonify({'status': f'Error suspending service: {r.status_code} {r.text}'})
    except Exception as e:
        return jsonify({'status': f'Error calling Render API: {str(e)}'})

@app.route('/bot_status')
@login_required
def get_bot_status():
    if not RENDER_API_KEY or not RENDER_BOT_SERVICE_ID:
        return jsonify({'status': 'Unknown (RENDER_API_KEY not set)', 'running': False})
    try:
        url = f"https://api.render.com/v1/services/{RENDER_BOT_SERVICE_ID}"
        headers = {"Authorization": f"Bearer {RENDER_API_KEY}", "Accept": "application/json"}
        r = requests.get(url, headers=headers)
        if r.status_code == 200:
            is_suspended = r.json().get('suspended')
            return jsonify({
                'running': not is_suspended,
                'status': 'Suspended' if is_suspended else 'Running'
            })
        return jsonify({'running': False, 'status': f'Error: {r.status_code}'})
    except:
        return jsonify({'running': False, 'status': 'Error connecting to Render API'})

# Local bot control routes removed

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
