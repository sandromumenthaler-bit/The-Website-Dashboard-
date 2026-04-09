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

# Helper to get all editable files dynamically
def get_editable_files():
    files = []
    ignore_dirs = {'.venv', 'venv', '.git', '__pycache__', '.idea', 'data', 'images', 'node_modules'}
    ignore_files = {'.env', 'users.json'}
    for root, dirs, filenames in os.walk(base_dir):
        dirs[:] = [d for d in dirs if d not in ignore_dirs and not d.startswith('.')]
        for f in filenames:
            if f in ignore_files or f.startswith('.'):
                continue
            rel_path = os.path.relpath(os.path.join(root, f), base_dir)
            files.append(rel_path.replace('\\', '/'))
    return sorted(files)


def is_file_allowed(filename):
    if not filename: return False
    filename = filename.replace('\\', '/')
    if '..' in filename or filename.startswith('/'): return False
    parts = filename.split('/')
    if any(p.startswith('.') for p in parts): return False
    forbidden = {'.venv', 'venv', '.git', '__pycache__', '.idea', 'data', 'images', 'node_modules'}
    if any(p in forbidden for p in parts): return False
    if parts[-1] in {'.env', 'users.json'}: return False
    return True

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
RENDER_API_KEY = os.getenv('RENDER_API_KEY')
RENDER_SERVICE_ID = os.getenv('RENDER_SERVICE_ID')

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

    # Prefer root path for editing
    path = os.path.join(base_dir, filename)
    if not os.path.exists(path):
        # Fallback to data dir if it's there (for legacy/persistence reasons)
        path = os.path.join(DATA_DIR, filename)

    if not os.path.exists(path):
        return jsonify({'content': f'# File {filename} not found.'})

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

    # Save to root
    root_path = os.path.join(base_dir, filename)
    os.makedirs(os.path.dirname(root_path), exist_ok=True)
    with open(root_path, 'w', encoding='utf-8') as f:
        f.write(content)

    # Mirror to data folder for files that need runtime persistence
    if filename in ['bot.py', 'index.json']:
        local_path = os.path.join(DATA_DIR, filename)
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        with open(local_path, 'w', encoding='utf-8') as f:
            f.write(content)

    status_msg = f'File {filename} saved.'

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
    # Always try to restart if bot.py was edited and it's currently running
    if bot_process is not None and filename == 'bot.py':
        try:
            stop_bot()
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


@app.route('/stop_render_service', methods=['POST'])
@login_required
def stop_render_service():
    if not RENDER_API_KEY or not RENDER_SERVICE_ID:
        return jsonify({'status': 'RENDER_API_KEY or RENDER_SERVICE_ID not set in Environment Variables.'})
    try:
        url = f"https://api.render.com/v1/services/{RENDER_SERVICE_ID}/suspend"
        headers = {
            "Authorization": f"Bearer {RENDER_API_KEY}",
            "Accept": "application/json"
        }
        r = requests.post(url, headers=headers)
        if r.status_code == 204:  # Success (No Content)
            return jsonify({'status': 'Bot service suspended on Render!'})
        else:
            try:
                err = r.json().get('message', r.text)
            except:
                err = r.text
            return jsonify({'status': f'Error suspending service: {err}'})
    except Exception as e:
        return jsonify({'status': f'Error stopping service: {str(e)}'})


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
            "base_tree": base_tree_sha,
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
    return jsonify({'running': False, 'local_control_disabled': True})


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
        return jsonify({'status': 'Updated successfully locally. Use "Push All" to sync with GitHub.'})
    return jsonify({'status': 'Not found'}), 404


@app.route('/create_file', methods=['POST'])
@login_required
def create_file():
    filename = request.json.get('filename')
    if not is_file_allowed(filename):
        return jsonify({'status': 'Error: Invalid or unauthorized filename'}), 400
    
    path = os.path.join(base_dir, filename)
    if os.path.exists(path):
        return jsonify({'status': 'Error: File already exists'}), 400
    
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            f.write('')
        return jsonify({'status': f'File {filename} created successfully'})
    except Exception as e:
        return jsonify({'status': f'Error creating file: {str(e)}'}), 500


@app.route('/delete_file_server', methods=['POST'])
@login_required
def delete_file_server():
    filename = request.json.get('filename')
    if not is_file_allowed(filename):
        return jsonify({'status': 'Error: Invalid or unauthorized filename'}), 400
    
    if filename == 'app.py':
        return jsonify({'status': 'Error: Cannot delete app.py'}), 400

    path = os.path.join(base_dir, filename)
    if not os.path.exists(path):
        return jsonify({'status': 'Error: File not found'}), 404
        
    try:
        os.remove(path)
        # Also remove from data/ if mirrored
        if filename in ['bot.py', 'index.json']:
            data_path = os.path.join(DATA_DIR, filename)
            if os.path.exists(data_path):
                os.remove(data_path)
        return jsonify({'status': f'File {filename} deleted successfully'})
    except Exception as e:
        return jsonify({'status': f'Error deleting file: {str(e)}'}), 500


@app.route('/rename_file_server', methods=['POST'])
@login_required
def rename_file_server():
    old_name = request.json.get('old_name')
    new_name = request.json.get('new_name')
    
    if not is_file_allowed(old_name) or not is_file_allowed(new_name):
        return jsonify({'status': 'Error: Invalid or unauthorized filename'}), 400
        
    old_path = os.path.join(base_dir, old_name)
    new_path = os.path.join(base_dir, new_name)
    
    if not os.path.exists(old_path):
        return jsonify({'status': 'Error: Original file not found'}), 404
    if os.path.exists(new_path):
        return jsonify({'status': 'Error: Target filename already exists'}), 400
        
    try:
        os.makedirs(os.path.dirname(new_path), exist_ok=True)
        os.rename(old_path, new_path)
        # Mirror rename in data/ if it's there
        if old_name in ['bot.py', 'index.json']:
            old_data_path = os.path.join(DATA_DIR, old_name)
            new_data_path = os.path.join(DATA_DIR, new_name)
            if os.path.exists(old_data_path):
                os.makedirs(os.path.dirname(new_data_path), exist_ok=True)
                os.rename(old_data_path, new_data_path)
            
        return jsonify({'status': f'File renamed to {new_name}'})
    except Exception as e:
        return jsonify({'status': f'Error renaming file: {str(e)}'}), 500


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    socketio.run(app, host='0.0.0.0', port=port)
