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
from dotenv import load_dotenv

load_dotenv()

# Setup absolute paths for Render and Local environments
base_dir = os.path.abspath(os.path.dirname(__file__))
DATA_DIR = os.path.join(base_dir, 'data')

if not os.path.exists(DATA_DIR):
    os.makedirs(DATA_DIR)

# Configuration for Persistence
USER_DATA_FILE = os.path.join(DATA_DIR, 'users.json')
BOT_SCRIPT_PATH = os.path.join(DATA_DIR, 'bot.py')
INDEX_JSON_PATH = os.path.join(DATA_DIR, 'index.json')
ROOT_INDEX_JSON = os.path.join(base_dir, 'index.json')
UPLOAD_FOLDER = os.path.join(DATA_DIR, 'images')

if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

# Ensure users.json exists so the app doesn't crash
if not os.path.exists(USER_DATA_FILE):
    with open(USER_DATA_FILE, 'w') as f:
        json.dump({'test': 'test'}, f)

# Ensure data/index.json exists and is in sync with root if available
if not os.path.exists(INDEX_JSON_PATH):
    if os.path.exists(ROOT_INDEX_JSON):
        shutil.copy(ROOT_INDEX_JSON, INDEX_JSON_PATH)
    else:
        with open(INDEX_JSON_PATH, 'w') as f:
            json.dump({}, f)

app = Flask(__name__,
            template_folder=os.path.join(base_dir, 'templates'),
            static_folder=os.path.join(base_dir, 'static'))
app.config['SECRET_KEY'] = 'secret-key-for-now'
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

EXCLUDED_DIRS = {'.venv', '.git', '.idea', '__pycache__'}
EXCLUDED_FILES = {'.env', 'users.json'}
EDITOR_FILES_FILE = os.path.join(DATA_DIR, 'editor_files.json')


# Helper to get all editable files dynamically
def get_editable_files():
    files = []
    for rel_path in load_editor_files():
        _, abs_path = resolve_workspace_path(rel_path)
        if abs_path and os.path.exists(abs_path):
            files.append(rel_path)
    return sorted(set(files), key=str.lower)


def load_editor_files():
    if not os.path.exists(EDITOR_FILES_FILE):
        with open(EDITOR_FILES_FILE, 'w', encoding='utf-8') as f:
            json.dump([], f)
        return []

    try:
        with open(EDITOR_FILES_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if not isinstance(data, list):
            return []
        clean = []
        for item in data:
            normalized = normalize_relative_path(item)
            if normalized:
                clean.append(normalized)
        return clean
    except Exception:
        return []


def save_editor_files(files):
    clean = []
    for item in files:
        normalized = normalize_relative_path(item)
        if normalized:
            clean.append(normalized)
    with open(EDITOR_FILES_FILE, 'w', encoding='utf-8') as f:
        json.dump(sorted(set(clean), key=str.lower), f, indent=2)


def normalize_relative_path(filename):
    if not filename:
        return None
    normalized = filename.replace('\\', '/').strip()
    normalized = normalized.lstrip('/')
    if not normalized or normalized.startswith('../') or '/..' in normalized:
        return None
    return normalized


def resolve_workspace_path(filename):
    normalized = normalize_relative_path(filename)
    if not normalized:
        return None, None

    abs_path = os.path.normpath(os.path.join(base_dir, normalized))
    try:
        if os.path.commonpath([base_dir, abs_path]) != base_dir:
            return None, None
    except ValueError:
        return None, None

    rel_path = os.path.relpath(abs_path, base_dir).replace('\\', '/')
    parts = rel_path.split('/')
    if any(part in EXCLUDED_DIRS for part in parts):
        return None, None

    basename = os.path.basename(rel_path)
    if basename in EXCLUDED_FILES:
        return None, None

    return rel_path, abs_path


def is_file_allowed(filename):
    rel_path, abs_path = resolve_workspace_path(filename)
    if not rel_path or not abs_path:
        return False
    return rel_path in load_editor_files()


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
BOT_STATUS_URL = os.getenv('BOT_STATUS_URL', '').strip()
BOT_STATUS_TOKEN = os.getenv('BOT_STATUS_TOKEN', '').strip()
BOT_STATUS_TIMEOUT = float(os.getenv('BOT_STATUS_TIMEOUT', '5'))

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


def start_bot():
    global bot_process
    if bot_process is not None:
        return
    if os.path.exists(BOT_SCRIPT_PATH):
        try:
            bot_process = subprocess.Popen(
                [sys.executable, BOT_SCRIPT_PATH],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT
            )
        except Exception as e:
            socketio.emit('bot_log', {'data': f'Error starting local bot: {e}\n'})


def stop_bot():
    global bot_process
    if bot_process is not None:
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
            socketio.emit('bot_log', {'data': '--- Local bot process stopped ---\n'})
        except Exception as e:
            socketio.emit('bot_log', {'data': f'Error stopping local bot: {e}\n'})


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
        return jsonify({'status': 'success'})
    return jsonify({'status': 'error', 'message': 'wrong user/password'}), 401


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
    if not is_file_allowed(filename):
        return jsonify({'error': 'Unauthorized or invalid file'}), 403

    rel_path, path = resolve_workspace_path(filename)
    if not rel_path or not path:
        return jsonify({'error': 'Unauthorized or invalid file'}), 403

    # Prefer workspace path for editing
    if not os.path.exists(path):
        # Fallback to data dir if it's there (for legacy/persistence reasons)
        path = os.path.join(DATA_DIR, rel_path)

    if not os.path.exists(path):
        return jsonify({'content': f'# File {rel_path} not found.'})

    with open(path, 'r', encoding='utf-8') as f:
        return jsonify({'content': f.read()})


@app.route('/list_files')
@login_required
def list_files():
    return jsonify({'files': get_editable_files()})


@app.route('/save_script', methods=['POST'])
@login_required
def save_script():
    content = request.json.get('content')
    filename = request.json.get('file', 'bot.py')
    push = request.json.get('push', False)

    if not is_file_allowed(filename):
        return jsonify({'status': 'Unauthorized or invalid file'}), 403

    rel_path, root_path = resolve_workspace_path(filename)
    if not rel_path or not root_path:
        return jsonify({'status': 'Unauthorized or invalid file'}), 403

    # Save to root
    os.makedirs(os.path.dirname(root_path), exist_ok=True)
    with open(root_path, 'w', encoding='utf-8') as f:
        f.write(content)

    # Mirror to data folder for files that need runtime persistence
    if rel_path in ['bot.py', 'index.json']:
        local_path = os.path.join(DATA_DIR, rel_path)
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        with open(local_path, 'w', encoding='utf-8') as f:
            f.write(content)

    status_msg = f'File {rel_path} saved.'

    if push:
        if not GITHUB_TOKEN or not GITHUB_REPO:
            return jsonify({'status': 'Saved locally, but GITHUB_TOKEN or GITHUB_REPO not set!'})

        try:
            # 1. Get current file info for SHA
            api_url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{rel_path}"
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
                "message": f"Update {rel_path} from Dashboard",
                "content": base64.b64encode(content.encode('utf-8')).decode('utf-8'),
                "branch": GITHUB_BRANCH
            }
            if sha:
                payload["sha"] = sha

            r = requests.put(api_url, headers=headers, json=payload)
            if r.status_code in [200, 201]:
                status_msg = f'File {rel_path} saved and pushed to GitHub! Bot service should restart shortly.'
            else:
                try:
                    err_msg = r.json().get('message', r.text)
                except:
                    err_msg = r.text
                status_msg = f'Saved locally, but GitHub error: {err_msg}'
        except Exception as e:
            status_msg = f'Saved locally, but error pushing to GitHub: {str(e)}'

    # Local restart logic
    # Always try to start/restart if bot.py was edited
    if rel_path == 'bot.py':
        try:
            stop_bot()
            start_bot()
            status_msg += " Bot (re)started locally."
        except Exception as e:
            status_msg += f" Error starting bot locally: {str(e)}"

    return jsonify({'status': status_msg})




@app.route('/push_all_to_github', methods=['POST'])
@login_required
def push_all_to_github():
    if not GITHUB_TOKEN or not GITHUB_REPO:
        return jsonify({'status': 'GITHUB_TOKEN or GITHUB_REPO not set!'})

    try:
        headers = {
            "Authorization": f"token {GITHUB_TOKEN}",
            "Accept": "application/vnd.github.v3+json"
        }

        # 1. Get the latest commit SHA of the branch
        branch_url = f"https://api.github.com/repos/{GITHUB_REPO}/branches/{GITHUB_BRANCH}"
        r = requests.get(branch_url, headers=headers)
        if r.status_code != 200:
            return jsonify({'status': f'Error getting branch info: {r.text}'})

        last_commit_sha = r.json()['commit']['sha']
        base_tree_sha = r.json()['commit']['commit']['tree']['sha']

        # 2. Create a new tree
        tree_entries = []

        # Add editable files
        editable_files = get_editable_files()
        for filename in editable_files:
            local_path = os.path.join(base_dir, filename)

            if os.path.exists(local_path):
                with open(local_path, 'r', encoding='utf-8') as f:
                    content = f.read()
                tree_entries.append({
                    "path": filename,
                    "mode": "100644",
                    "type": "blob",
                    "content": content
                })

        # Add images: Create a separate tree for the images folder to correctly handle deletions
        image_tree_entries = []
        if os.path.exists(UPLOAD_FOLDER):
            for img_file in os.listdir(UPLOAD_FOLDER):
                img_path = os.path.join(UPLOAD_FOLDER, img_file)
                if os.path.isfile(img_path):
                    with open(img_path, 'rb') as f:
                        img_content = base64.b64encode(f.read()).decode('utf-8')

                    # Create blob for image
                    blob_url = f"https://api.github.com/repos/{GITHUB_REPO}/git/blobs"
                    blob_payload = {
                        "content": img_content,
                        "encoding": "base64"
                    }
                    br = requests.post(blob_url, headers=headers, json=blob_payload)
                    if br.status_code == 201:
                        blob_sha = br.json()['sha']
                        image_tree_entries.append({
                            "path": img_file,
                            "mode": "100644",
                            "type": "blob",
                            "sha": blob_sha
                        })

        # Always create a dedicated tree for the images directory, even if empty,
        # so that deletions are reflected on GitHub (the images folder will be replaced).
        tree_url = f"https://api.github.com/repos/{GITHUB_REPO}/git/trees"
        image_tree_payload = {
            "tree": image_tree_entries
        }
        itr = requests.post(tree_url, headers=headers, json=image_tree_payload)
        if itr.status_code == 201:
            image_tree_sha = itr.json()['sha']
            tree_entries.append({
                "path": "images",
                "mode": "040000",
                "type": "tree",
                "sha": image_tree_sha
            })

        tree_url = f"https://api.github.com/repos/{GITHUB_REPO}/git/trees"
        tree_payload = {
            "tree": tree_entries
        }
        r = requests.post(tree_url, headers=headers, json=tree_payload)
        if r.status_code != 201:
            return jsonify({'status': f'Error creating tree: {r.text}'})

        new_tree_sha = r.json()['sha']

        # 3. Create a new commit
        commit_url = f"https://api.github.com/repos/{GITHUB_REPO}/git/commits"
        commit_payload = {
            "message": "Update from Dashboard (All files and images)",
            "tree": new_tree_sha,
            "parents": [last_commit_sha]
        }
        r = requests.post(commit_url, headers=headers, json=commit_payload)
        if r.status_code != 201:
            return jsonify({'status': f'Error creating commit: {r.text}'})

        new_commit_sha = r.json()['sha']

        # 4. Update the branch reference
        ref_url = f"https://api.github.com/repos/{GITHUB_REPO}/git/refs/heads/{GITHUB_BRANCH}"
        ref_payload = {
            "sha": new_commit_sha
        }
        r = requests.patch(ref_url, headers=headers, json=ref_payload)
        if r.status_code == 200:
            return jsonify({'status': 'All changes pushed to GitHub successfully!'})
        else:
            return jsonify({'status': f'Error updating branch: {r.text}'})

    except Exception as e:
        return jsonify({'status': f'Error pushing to GitHub: {str(e)}'})


@app.route('/bot_status')
@login_required
def get_bot_status():
    # If configured, check bot status from the external Render service.
    if BOT_STATUS_URL:
        try:
            headers = {}
            if BOT_STATUS_TOKEN:
                headers['Authorization'] = f'Bearer {BOT_STATUS_TOKEN}'

            response = requests.get(BOT_STATUS_URL, headers=headers, timeout=BOT_STATUS_TIMEOUT)
            if 200 <= response.status_code < 300:
                running = True
                content_type = response.headers.get('Content-Type', '')
                if 'application/json' in content_type.lower():
                    payload = response.json()
                    if isinstance(payload, dict):
                        # Prefer explicit status keys when provided by the bot service.
                        if 'running' in payload:
                            running = bool(payload.get('running'))
                        elif 'online' in payload:
                            running = bool(payload.get('online'))
                        elif 'status' in payload:
                            running = str(payload.get('status', '')).lower() in {'online', 'running', 'ok', 'healthy'}
                return jsonify({'running': running, 'source': 'remote'})

            return jsonify({'running': False, 'source': 'remote', 'error': f'HTTP {response.status_code}'})
        except Exception as e:
            return jsonify({'running': False, 'source': 'remote', 'error': str(e)})

    # Fallback: local process check for single-service setups.
    global bot_process
    return jsonify({'running': bot_process is not None, 'source': 'local'})


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
        ext = file.filename.split('.')[-1].lower() if '.' in file.filename else 'png'
        filename = f"{name}.{ext}"
        file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(file_path)
        pic_link = f"/static/images/{filename}"

    data[name] = {
        "pic_link": pic_link,
        "rarity": rarity
    }

    with open(INDEX_JSON_PATH, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=4)

    # Mirror to root for GitHub sync
    with open(ROOT_INDEX_JSON, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=4)

    return jsonify({'status': 'Image/Vehicle added successfully'})


@app.route('/delete_image', methods=['POST'])
@login_required
def delete_image():
    name = request.json.get('name')
    with open(INDEX_JSON_PATH, 'r', encoding='utf-8') as f:
        data = json.load(f)
    if name in data:
        info = data[name]
        # Delete image file if it exists locally
        if info.get('pic_link') and info['pic_link'].startswith('/static/images/'):
            filename = info['pic_link'].split('/')[-1]
            file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            if os.path.exists(file_path):
                try:
                    os.remove(file_path)
                except:
                    pass
        del data[name]
        with open(INDEX_JSON_PATH, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=4)
        # Mirror to root for GitHub sync
        with open(ROOT_INDEX_JSON, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=4)
        return jsonify({'status': f'{name} deleted'})
    return jsonify({'status': 'Not found'}), 404


@app.route('/edit_image', methods=['POST'])
@login_required
def edit_image():
    old_name = request.form.get('old_name')
    new_name = request.form.get('new_name')
    rarity = request.form.get('rarity')
    file = request.files.get('file')

    with open(INDEX_JSON_PATH, 'r', encoding='utf-8') as f:
        data = json.load(f)

    if old_name in data:
        info = data.pop(old_name)
        info['rarity'] = rarity

        if file:
            # Delete old image if it exists locally
            if info.get('pic_link') and info['pic_link'].startswith('/static/images/'):
                old_filename = info['pic_link'].split('/')[-1]
                old_file_path = os.path.join(app.config['UPLOAD_FOLDER'], old_filename)
                if os.path.exists(old_file_path):
                    try:
                        os.remove(old_file_path)
                    except:
                        pass

            # Save new image with new name
            ext = file.filename.split('.')[-1].lower() if '.' in file.filename else 'png'
            filename = f"{new_name}.{ext}"
            file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            file.save(file_path)
            info['pic_link'] = f"/static/images/{filename}"
        elif old_name != new_name:
            # If name changed but no new file, rename existing file if it exists
            if info.get('pic_link') and info['pic_link'].startswith('/static/images/'):
                old_filename = info['pic_link'].split('/')[-1]
                ext = old_filename.split('.')[-1]
                new_filename = f"{new_name}.{ext}"
                old_path = os.path.join(app.config['UPLOAD_FOLDER'], old_filename)
                new_path = os.path.join(app.config['UPLOAD_FOLDER'], new_filename)
                if os.path.exists(old_path):
                    try:
                        os.rename(old_path, new_path)
                        info['pic_link'] = f"/static/images/{new_filename}"
                    except:
                        pass

        data[new_name] = info
        with open(INDEX_JSON_PATH, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=4)
        # Mirror to root for GitHub sync
        with open(ROOT_INDEX_JSON, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=4)
        return jsonify({'status': 'Updated successfully locally. Use "Push All" to sync with GitHub.'})
    return jsonify({'status': 'Not found'}), 404


@app.route('/create_file', methods=['POST'])
@login_required
def create_file():
    filename = request.json.get('filename', '')
    content = request.json.get('content', '')

    rel_path, abs_path = resolve_workspace_path(filename)
    if not rel_path or not abs_path:
        return jsonify({'status': 'Unauthorized or invalid file path.'}), 403

    if rel_path in load_editor_files():
        return jsonify({'status': f'File {rel_path} already exists in the editor list.'}), 400

    if os.path.exists(abs_path):
        return jsonify({'status': f'File {rel_path} already exists.'}), 400

    os.makedirs(os.path.dirname(abs_path), exist_ok=True)
    with open(abs_path, 'w', encoding='utf-8') as f:
        f.write(content)

    editor_files = load_editor_files()
    editor_files.append(rel_path)
    save_editor_files(editor_files)

    return jsonify({'status': f'File {rel_path} created successfully.'})


@app.route('/delete_file_server', methods=['POST'])
@login_required
def delete_file_server():
    filename = request.json.get('filename', '')

    if not is_file_allowed(filename):
        return jsonify({'status': 'Unauthorized or invalid file path.'}), 403

    rel_path, abs_path = resolve_workspace_path(filename)
    if not rel_path or not abs_path:
        return jsonify({'status': 'Unauthorized or invalid file path.'}), 403

    if not os.path.exists(abs_path):
        return jsonify({'status': f'File {rel_path} not found.'}), 404

    os.remove(abs_path)

    editor_files = [f for f in load_editor_files() if f != rel_path]
    save_editor_files(editor_files)

    return jsonify({'status': f'File {rel_path} deleted successfully.'})


@app.route('/rename_file_server', methods=['POST'])
@login_required
def rename_file_server():
    old_filename = request.json.get('old_filename', '')
    new_filename = request.json.get('new_filename', '')

    if not is_file_allowed(old_filename):
        return jsonify({'status': 'Unauthorized or invalid file path.'}), 403

    old_rel, old_abs = resolve_workspace_path(old_filename)
    new_rel, new_abs = resolve_workspace_path(new_filename)
    if not old_rel or not old_abs or not new_rel or not new_abs:
        return jsonify({'status': 'Unauthorized or invalid file path.'}), 403

    if not os.path.exists(old_abs):
        return jsonify({'status': f'File {old_rel} not found.'}), 404

    if os.path.exists(new_abs):
        return jsonify({'status': f'File {new_rel} already exists.'}), 400

    os.makedirs(os.path.dirname(new_abs), exist_ok=True)
    os.rename(old_abs, new_abs)

    editor_files = load_editor_files()
    editor_files = [new_rel if f == old_rel else f for f in editor_files]
    save_editor_files(editor_files)

    return jsonify({'status': f'Renamed {old_rel} to {new_rel}.'})


if __name__ == '__main__':
    start_bot()
    port = int(os.environ.get('PORT', 8080))
    socketio.run(app, host='0.0.0.0', port=port)
