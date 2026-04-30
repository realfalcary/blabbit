from flask import Flask, render_template, request, session, jsonify, make_response
from flask_socketio import SocketIO, emit, join_room, leave_room
from datetime import datetime, date, timedelta
import sqlite3
import hashlib
import secrets
import os
import sys

app = Flask(__name__)

def get_data_dir():
    # Check for explicit env var first (set this in Railway to /data)
    env_dir = os.environ.get('BLABBIT_DATA_DIR')
    if env_dir:
        os.makedirs(env_dir, exist_ok=True)
        return env_dir
    # Railway persistent volume
    if os.path.ismount('/data'):
        return '/data'
    # Fallback
    if getattr(sys, 'frozen', False):
        data_dir = os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')), 'Blabbit')
    else:
        data_dir = os.path.dirname(os.path.abspath(__file__))
    os.makedirs(data_dir, exist_ok=True)
    return data_dir

def get_app_base_dir():
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))

APP_BASE = get_app_base_dir()
app.template_folder = os.path.join(APP_BASE, 'templates')
app.static_folder   = os.path.join(APP_BASE, 'static')
app.config['TEMPLATES_AUTO_RELOAD'] = True
app.jinja_env.auto_reload = True

DATA_DIR = get_data_dir()

_key_file = os.path.join(DATA_DIR, 'secret.key')
if os.path.exists(_key_file):
    with open(_key_file) as f:
        _secret = f.read().strip()
else:
    _secret = secrets.token_hex(32)
    with open(_key_file, 'w') as f:
        f.write(_secret)

app.config['SECRET_KEY'] = _secret
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE'] = False
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=365)
app.config['SESSION_COOKIE_NAME'] = 'blabbit_session'
socketio = SocketIO(app, cors_allowed_origins="*")

DB_PATH = os.path.join(DATA_DIR, 'blabbit.db')
FOUNDER_USERNAME = 'cora'

@app.before_request
def make_session_permanent():
    session.permanent = True

online_users = {}   # sid -> {username, channel}
avatars = {}        # username -> dataURL


# ── Database ──────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as db:
        db.execute('''CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL, email TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL, created TEXT NOT NULL, avatar TEXT DEFAULT NULL
        )''')
        db.execute('''CREATE TABLE IF NOT EXISTS badges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL, badge TEXT NOT NULL, granted TEXT NOT NULL,
            UNIQUE(username, badge)
        )''')
        db.execute('''CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel TEXT NOT NULL, username TEXT NOT NULL,
            text TEXT NOT NULL, timestamp TEXT NOT NULL
        )''')
        db.execute('''CREATE TABLE IF NOT EXISTS friends (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_a TEXT NOT NULL, user_b TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            requester TEXT NOT NULL,
            UNIQUE(user_a, user_b)
        )''')
        db.execute('''CREATE TABLE IF NOT EXISTS servers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            owner TEXT NOT NULL,
            invite_code TEXT UNIQUE NOT NULL,
            created TEXT NOT NULL,
            icon TEXT DEFAULT NULL
        )''')
        db.execute('''CREATE TABLE IF NOT EXISTS server_channels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            server_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            FOREIGN KEY(server_id) REFERENCES servers(id)
        )''')
        db.execute('''CREATE TABLE IF NOT EXISTS server_members (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            server_id INTEGER NOT NULL,
            username TEXT NOT NULL,
            UNIQUE(server_id, username)
        )''')
        try: db.execute('ALTER TABLE users ADD COLUMN avatar TEXT DEFAULT NULL')
        except: pass
        try: db.execute('ALTER TABLE users ADD COLUMN pronouns TEXT DEFAULT NULL')
        except: pass
        try: db.execute('ALTER TABLE users ADD COLUMN about_me TEXT DEFAULT NULL')
        except: pass
        db.commit()

def _dm_channel(a, b):
    """Stable DM channel name for two users."""
    return 'dm:' + ':'.join(sorted([a, b]))

def save_message(channel, username, text, timestamp):
    with get_db() as db:
        cursor = db.execute('INSERT INTO messages (channel,username,text,timestamp) VALUES (?,?,?,?)',
                   (channel, username, text, timestamp))
        msg_id = cursor.lastrowid
        db.commit()
        db.execute('''DELETE FROM messages WHERE channel=? AND id NOT IN (
                        SELECT id FROM messages WHERE channel=? ORDER BY id DESC LIMIT 200)''',
                   (channel, channel))
        db.commit()
    return msg_id

def load_messages(channel):
    with get_db() as db:
        rows = db.execute(
            'SELECT id,username,text,timestamp,channel FROM messages WHERE channel=? ORDER BY id ASC',
            (channel,)).fetchall()
    return [{'id':r['id'],'username':r['username'],'text':r['text'],'timestamp':r['timestamp'],'channel':r['channel']} for r in rows]

def save_avatar(username, dataurl):
    with get_db() as db:
        db.execute('UPDATE users SET avatar=? WHERE username=?', (dataurl, username))
        db.commit()

def load_avatar(username):
    with get_db() as db:
        row = db.execute('SELECT avatar FROM users WHERE username=?', (username,)).fetchone()
    return row['avatar'] if row else None

def load_all_avatars():
    with get_db() as db:
        rows = db.execute('SELECT username,avatar FROM users WHERE avatar IS NOT NULL').fetchall()
    return {r['username']: r['avatar'] for r in rows}

def hash_password(p): return hashlib.sha256(p.encode()).hexdigest()

def get_badges(username):
    today = date.today().isoformat()
    with get_db() as db:
        user = db.execute('SELECT created FROM users WHERE username=?', (username,)).fetchone()
        if user and user['created'][:10] == today:
            try:
                db.execute('INSERT OR IGNORE INTO badges (username,badge,granted) VALUES (?,?,?)',
                           (username, 'early_tester', today))
                db.commit()
            except: pass
        rows = db.execute('SELECT badge FROM badges WHERE username=?', (username,)).fetchall()
        badges = [r['badge'] for r in rows]
    if username == FOUNDER_USERNAME and 'founder' not in badges:
        badges.append('founder')
    return badges

def get_friends(username):
    """Return dict with accepted friends and pending requests."""
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM friends WHERE (user_a=? OR user_b=?)",
            (username, username)).fetchall()
    accepted = []
    incoming = []
    outgoing = []
    for r in rows:
        other = r['user_b'] if r['user_a'] == username else r['user_a']
        if r['status'] == 'accepted':
            accepted.append(other)
        elif r['status'] == 'pending':
            if r['requester'] == username:
                outgoing.append(other)
            else:
                incoming.append(other)
    return {'accepted': accepted, 'incoming': incoming, 'outgoing': outgoing}

def friendship_status(a, b):
    with get_db() as db:
        row = db.execute(
            'SELECT * FROM friends WHERE (user_a=? AND user_b=?) OR (user_a=? AND user_b=?)',
            (a, b, b, a)).fetchone()
    if not row: return None
    return dict(row)


# ── Auth ──────────────────────────────────────────────────
@app.route('/')
def index():
    resp = render_template('index.html', founder=FOUNDER_USERNAME)
    from flask import make_response
    r = make_response(resp)
    r.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    r.headers['Pragma'] = 'no-cache'
    r.headers['Expires'] = '0'
    return r

@app.route('/api/signup', methods=['POST'])
def signup():
    data = request.get_json()
    username = (data.get('username') or '').strip()
    email    = (data.get('email')    or '').strip().lower()
    password = (data.get('password') or '').strip()
    if not username or not email or not password:
        return jsonify({'ok':False,'error':'All fields are required.'}), 400
    if len(username) < 2: return jsonify({'ok':False,'error':'Username must be at least 2 characters.'}), 400
    if len(password) < 6: return jsonify({'ok':False,'error':'Password must be at least 6 characters.'}), 400
    if '@' not in email:  return jsonify({'ok':False,'error':'Enter a valid email address.'}), 400
    created = datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')
    try:
        with get_db() as db:
            db.execute('INSERT INTO users (username,email,password,created) VALUES (?,?,?,?)',
                       (username, email, hash_password(password), created))
            db.commit()
    except sqlite3.IntegrityError as e:
        msg = str(e)
        if 'username' in msg: return jsonify({'ok':False,'error':'Username already taken.'}), 409
        if 'email'    in msg: return jsonify({'ok':False,'error':'Email already registered.'}), 409
        return jsonify({'ok':False,'error':'Account creation failed.'}), 409
    session['username'] = username
    return jsonify({'ok':True,'username':username})

@app.route('/api/login', methods=['POST'])
def login():
    data = request.get_json()
    username = (data.get('username') or '').strip()
    password = (data.get('password') or '').strip()
    if not username or not password:
        return jsonify({'ok':False,'error':'Username and password are required.'}), 400
    with get_db() as db:
        row = db.execute('SELECT * FROM users WHERE username=? AND password=?',
                         (username, hash_password(password))).fetchone()
    if not row: return jsonify({'ok':False,'error':'Incorrect username or password.'}), 401
    session['username'] = username
    return jsonify({'ok':True,'username':username})

@app.route('/api/update-username', methods=['POST'])
def update_username():
    caller = session.get('username')
    if not caller: return jsonify({'ok':False,'error':'Not logged in.'}), 401
    data = request.get_json()
    new_name = (data.get('username') or '').strip()
    if not new_name or len(new_name) < 2:
        return jsonify({'ok':False,'error':'Username must be at least 2 characters.'}), 400
    if new_name == caller: return jsonify({'ok':True,'username':new_name})
    try:
        with get_db() as db:
            db.execute('UPDATE users SET username=? WHERE username=?', (new_name, caller))
            db.execute('UPDATE badges SET username=? WHERE username=?', (new_name, caller))
            db.execute('UPDATE messages SET username=? WHERE username=?', (new_name, caller))
            db.execute('UPDATE friends SET user_a=? WHERE user_a=?', (new_name, caller))
            db.execute('UPDATE friends SET user_b=? WHERE user_b=?', (new_name, caller))
            db.execute('UPDATE friends SET requester=? WHERE requester=?', (new_name, caller))
            db.commit()
    except sqlite3.IntegrityError:
        return jsonify({'ok':False,'error':'Username already taken.'}), 409
    session['username'] = new_name
    return jsonify({'ok':True,'username':new_name})

@app.route('/api/messages/delete', methods=['POST'])
def delete_message():
    caller = session.get('username')
    if not caller: return jsonify({'ok': False, 'error': 'Not logged in.'}), 401
    data   = request.get_json()
    msg_id = data.get('id')
    if not msg_id: return jsonify({'ok': False, 'error': 'No message ID.'}), 400
    with get_db() as db:
        row = db.execute('SELECT username, channel FROM messages WHERE id=?', (msg_id,)).fetchone()
        if not row: return jsonify({'ok': False, 'error': 'Message not found.'}), 404
        if row['username'] != caller and caller != FOUNDER_USERNAME:
            return jsonify({'ok': False, 'error': 'Not authorised.'}), 403
        db.execute('DELETE FROM messages WHERE id=?', (msg_id,))
        db.commit()
    # Broadcast deletion to everyone in that channel
    socketio.emit('message_deleted', {'id': msg_id}, to=row['channel'])
    return jsonify({'ok': True})


@app.route('/api/update-pronouns', methods=['POST'])
def update_pronouns():
    caller = session.get('username')
    if not caller: return jsonify({'ok': False}), 401
    data = request.get_json()
    pronouns = (data.get('pronouns') or '').strip()[:30]
    with get_db() as db:
        db.execute('UPDATE users SET pronouns=? WHERE username=?', (pronouns or None, caller))
        db.commit()
    return jsonify({'ok': True})

@app.route('/api/update-aboutme', methods=['POST'])
def update_aboutme():
    caller = session.get('username')
    if not caller: return jsonify({'ok': False}), 401
    data = request.get_json()
    about_me = (data.get('about_me') or '').strip()[:200]
    with get_db() as db:
        db.execute('UPDATE users SET about_me=? WHERE username=?', (about_me or None, caller))
        db.commit()
    return jsonify({'ok': True})

@app.route('/api/logout', methods=['POST'])
def logout():
    session.pop('username', None)
    return jsonify({'ok':True})

@app.route('/api/me')
def me():
    username = session.get('username')
    if not username: return jsonify({'ok':False}), 401
    return jsonify({'ok':True,'username':username})


# ── Profile ───────────────────────────────────────────────
@app.route('/api/profile/<username>')
def profile(username):
    caller = session.get('username')
    with get_db() as db:
        row = db.execute('SELECT username,created,pronouns,about_me FROM users WHERE username=? COLLATE NOCASE', (username,)).fetchone()
    if not row: return jsonify({'ok':False,'error':'User not found.'}), 404
    badges = get_badges(username)
    avatar = avatars.get(username)
    fs = friendship_status(caller, username) if caller and caller != username else None
    friend_state = 'self'
    if caller and caller != username:
        if not fs:
            friend_state = 'none'
        elif fs['status'] == 'accepted':
            friend_state = 'friends'
        elif fs['status'] == 'pending' and fs['requester'] == caller:
            friend_state = 'outgoing'
        else:
            friend_state = 'incoming'
    return jsonify({'ok':True,'username':row['username'],'created':row['created'],
                    'badges':badges,'avatar':avatar,'friend_state':friend_state,
                    'pronouns': row['pronouns'] or '', 'about_me': row['about_me'] or ''})


# ── Friends API ───────────────────────────────────────────
@app.route('/api/friends', methods=['GET'])
def list_friends():
    caller = session.get('username')
    if not caller: return jsonify({'ok':False}), 401
    return jsonify({'ok':True, **get_friends(caller)})

@app.route('/api/friends/request', methods=['POST'])
def friend_request():
    caller = session.get('username')
    if not caller: return jsonify({'ok':False,'error':'Not logged in.'}), 401
    target = (request.get_json().get('username') or '').strip()
    if not target or target == caller:
        return jsonify({'ok':False,'error':'Invalid target.'}), 400
    with get_db() as db:
        exists = db.execute('SELECT id FROM users WHERE username=?', (target,)).fetchone()
        if not exists: return jsonify({'ok':False,'error':'User not found.'}), 404
        existing = db.execute(
            'SELECT * FROM friends WHERE (user_a=? AND user_b=?) OR (user_a=? AND user_b=?)',
            (caller,target,target,caller)).fetchone()
        if existing: return jsonify({'ok':False,'error':'Request already exists.'}), 409
        a,b = sorted([caller,target])
        db.execute('INSERT INTO friends (user_a,user_b,status,requester) VALUES (?,?,?,?)',
                   (a,b,'pending',caller))
        db.commit()
    # Notify target if online
    _notify_friend_update(target)
    return jsonify({'ok':True})

@app.route('/api/friends/accept', methods=['POST'])
def friend_accept():
    caller = session.get('username')
    if not caller: return jsonify({'ok':False}), 401
    target = (request.get_json().get('username') or '').strip()
    with get_db() as db:
        db.execute(
            "UPDATE friends SET status='accepted' WHERE requester=? AND status='pending' AND (user_a=? OR user_b=?)",
            (target, caller, caller))
        db.commit()
    _notify_friend_update(caller)
    _notify_friend_update(target)
    return jsonify({'ok':True})

@app.route('/api/friends/deny', methods=['POST'])
def friend_deny():
    caller = session.get('username')
    if not caller: return jsonify({'ok':False}), 401
    target = (request.get_json().get('username') or '').strip()
    with get_db() as db:
        db.execute(
            'DELETE FROM friends WHERE (user_a=? AND user_b=?) OR (user_a=? AND user_b=?)',
            (caller,target,target,caller))
        db.commit()
    _notify_friend_update(caller)
    _notify_friend_update(target)
    return jsonify({'ok':True})

@app.route('/api/friends/remove', methods=['POST'])
def friend_remove():
    caller = session.get('username')
    if not caller: return jsonify({'ok':False}), 401
    target = (request.get_json().get('username') or '').strip()
    with get_db() as db:
        db.execute(
            'DELETE FROM friends WHERE (user_a=? AND user_b=?) OR (user_a=? AND user_b=?)',
            (caller,target,target,caller))
        db.commit()
    _notify_friend_update(caller)
    _notify_friend_update(target)
    return jsonify({'ok':True})

def _notify_friend_update(username):
    """Push updated friends list to a user if they're online."""
    for sid, info in online_users.items():
        if info['username'] == username:
            friends = get_friends(username)
            socketio.emit('friends_update', friends, to=sid)

def get_user_servers(username):
    with get_db() as db:
        rows = db.execute('''
            SELECT s.id, s.name, s.owner, s.invite_code, s.icon
            FROM servers s
            JOIN server_members sm ON s.id = sm.server_id
            WHERE sm.username = ?
            ORDER BY s.id ASC
        ''', (username,)).fetchall()
    result = []
    for r in rows:
        with get_db() as db:
            channels = db.execute(
                'SELECT id, name FROM server_channels WHERE server_id=? ORDER BY id ASC',
                (r['id'],)).fetchall()
        result.append({
            'id': r['id'], 'name': r['name'], 'owner': r['owner'],
            'invite_code': r['invite_code'], 'icon': r['icon'],
            'channels': [{'id': c['id'], 'name': c['name']} for c in channels]
        })
    return result


# ── Server API ────────────────────────────────────────────
@app.route('/api/servers', methods=['GET'])
def list_servers():
    caller = session.get('username')
    if not caller: return jsonify({'ok': False}), 401
    return jsonify({'ok': True, 'servers': get_user_servers(caller)})

@app.route('/api/servers/create', methods=['POST'])
def create_server():
    caller = session.get('username')
    if not caller: return jsonify({'ok': False}), 401
    data = request.get_json()
    name = (data.get('name') or '').strip()
    if not name or len(name) < 2:
        return jsonify({'ok': False, 'error': 'Server name must be at least 2 characters.'}), 400
    invite_code = secrets.token_urlsafe(8)
    created = datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')
    with get_db() as db:
        cur = db.execute(
            'INSERT INTO servers (name, owner, invite_code, created) VALUES (?,?,?,?)',
            (name, caller, invite_code, created))
        server_id = cur.lastrowid
        # Create default general channel
        db.execute('INSERT INTO server_channels (server_id, name) VALUES (?,?)', (server_id, 'general'))
        # Add owner as member
        db.execute('INSERT INTO server_members (server_id, username) VALUES (?,?)', (server_id, caller))
        db.commit()
    return jsonify({'ok': True, 'server_id': server_id, 'invite_code': invite_code})

@app.route('/api/servers/join', methods=['POST'])
def join_server():
    caller = session.get('username')
    if not caller: return jsonify({'ok': False}), 401
    data = request.get_json()
    invite_code = (data.get('invite_code') or '').strip()
    with get_db() as db:
        server = db.execute('SELECT * FROM servers WHERE invite_code=?', (invite_code,)).fetchone()
        if not server: return jsonify({'ok': False, 'error': 'Invalid invite code.'}), 404
        try:
            db.execute('INSERT INTO server_members (server_id, username) VALUES (?,?)',
                       (server['id'], caller))
            db.commit()
        except sqlite3.IntegrityError:
            return jsonify({'ok': False, 'error': 'Already a member.'}), 409
    return jsonify({'ok': True, 'server_id': server['id']})

@app.route('/api/servers/<int:server_id>/channels', methods=['POST'])
def create_channel(server_id):
    caller = session.get('username')
    if not caller: return jsonify({'ok': False}), 401
    with get_db() as db:
        server = db.execute('SELECT * FROM servers WHERE id=?', (server_id,)).fetchone()
        if not server: return jsonify({'ok': False, 'error': 'Server not found.'}), 404
        if server['owner'] != caller:
            return jsonify({'ok': False, 'error': 'Only the server owner can add channels.'}), 403
        data = request.get_json()
        name = (data.get('name') or '').strip().lower().replace(' ', '-')
        if not name: return jsonify({'ok': False, 'error': 'Channel name required.'}), 400
        cur = db.execute('INSERT INTO server_channels (server_id, name) VALUES (?,?)', (server_id, name))
        channel_id = cur.lastrowid
        db.commit()
    return jsonify({'ok': True, 'channel_id': channel_id, 'name': name})

@app.route('/api/servers/<int:server_id>/leave', methods=['POST'])
def leave_server(server_id):
    caller = session.get('username')
    if not caller: return jsonify({'ok': False}), 401
    with get_db() as db:
        db.execute('DELETE FROM server_members WHERE server_id=? AND username=?', (server_id, caller))
        db.commit()
    return jsonify({'ok': True})

@app.route('/api/servers/<int:server_id>/members', methods=['GET'])
def server_members(server_id):
    caller = session.get('username')
    if not caller: return jsonify({'ok': False}), 401
    with get_db() as db:
        members = db.execute(
            'SELECT username FROM server_members WHERE server_id=?', (server_id,)).fetchall()
    return jsonify({'ok': True, 'members': [m['username'] for m in members]})


# ── Badges ────────────────────────────────────────────────
@app.route('/api/badge/grant', methods=['POST'])
def grant_badge():
    caller = session.get('username')
    if caller != FOUNDER_USERNAME: return jsonify({'ok':False,'error':'Not authorised.'}), 403
    data = request.get_json()
    target   = (data.get('username') or '').strip()
    badge_id = (data.get('badge')    or '').strip()
    if badge_id not in {'founder','early_tester','amethyst','iolite','iris'}:
        return jsonify({'ok':False,'error':'Unknown badge.'}), 400
    with get_db() as db:
        if not db.execute('SELECT id FROM users WHERE username=?', (target,)).fetchone():
            return jsonify({'ok':False,'error':'User not found.'}), 404
        db.execute('INSERT OR IGNORE INTO badges (username,badge,granted) VALUES (?,?,?)',
                   (target, badge_id, date.today().isoformat()))
        db.commit()
    return jsonify({'ok':True})

@app.route('/api/badge/revoke', methods=['POST'])
def revoke_badge():
    caller = session.get('username')
    if caller != FOUNDER_USERNAME: return jsonify({'ok':False,'error':'Not authorised.'}), 403
    data = request.get_json()
    target   = (data.get('username') or '').strip()
    badge_id = (data.get('badge')    or '').strip()
    with get_db() as db:
        db.execute('DELETE FROM badges WHERE username=? AND badge=?', (target, badge_id))
        db.commit()
    return jsonify({'ok':True})


# ── Socket events ─────────────────────────────────────────
def _get_founder_usernames():
    with get_db() as db:
        rows = db.execute("SELECT username FROM badges WHERE badge='founder'").fetchall()
    founders = set(r['username'] for r in rows)
    founders.add(FOUNDER_USERNAME)
    return list(founders)

@socketio.on('join')
def on_join(data):
    username = data.get('username','').strip()
    channel  = data.get('channel', None)
    if not username: return

    # Default to home view (no channel)
    if not channel:
        channel = f'home:{username}'

    online_users[request.sid] = {'username': username, 'channel': channel}

    if username not in avatars:
        db_avatar = load_avatar(username)
        if db_avatar: avatars[username] = db_avatar

    join_room(channel)

    # Join all accepted DM rooms
    friends = get_friends(username)
    for friend in friends['accepted']:
        join_room(_dm_channel(username, friend))

    # Join all server channels the user is a member of
    for srv in get_user_servers(username):
        for ch in srv['channels']:
            join_room(f"server:{srv['id']}:{ch['id']}")

    emit('avatar_sync',  avatars)
    emit('founder_sync', {'founders': _get_founder_usernames()})
    emit('friends_update', friends)
    emit('servers_update', {'servers': get_user_servers(username)})

    if channel.startswith('server:'):
        emit('history', {'messages': load_messages(channel)})
    _broadcast_users()

@socketio.on('update_avatar')
def on_update_avatar(data):
    username = data.get('username','')
    avatar   = data.get('avatar', None)
    if not username: return
    if avatar:
        avatars[username] = avatar
        save_avatar(username, avatar)
    elif username in avatars:
        del avatars[username]
        save_avatar(username, None)
    socketio.emit('avatar_update', {'username': username, 'avatar': avatar})

@socketio.on('switch_channel')
def on_switch_channel(data):
    if request.sid not in online_users: return
    user = online_users[request.sid]
    old_ch = user['channel']
    new_ch = data.get('channel', '')
    if not new_ch: return

    leave_room(old_ch)
    join_room(new_ch)
    user['channel'] = new_ch

    emit('history', {'messages': load_messages(new_ch)})
    emit('avatar_sync', avatars)
    emit('founder_sync', {'founders': _get_founder_usernames()})
    _broadcast_users()

@socketio.on('message')
def on_message(data):
    if request.sid not in online_users: return
    user = online_users[request.sid]
    text = data.get('text','').strip()
    if not text: return
    ts  = _now()
    ch  = user['channel']
    msg_id = save_message(ch, user['username'], text, ts)
    msg = {'id': msg_id, 'username': user['username'], 'text': text, 'timestamp': ts, 'channel': ch}

    if ch.startswith('dm:'):
        # Emit directly to each participant's SID
        participants = ch.replace('dm:', '').split(':')
        delivered = set()
        for sid, info in online_users.items():
            if info['username'] in participants and sid not in delivered:
                socketio.emit('message', msg, to=sid)
                delivered.add(sid)
    else:
        # Server channel — emit to the room
        emit('message', msg, to=ch)

@socketio.on('disconnect')
def on_disconnect():
    if request.sid not in online_users: return
    online_users.pop(request.sid)
    _broadcast_users()


def _now(): return datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')

def _broadcast_users():
    ubc = {}
    for info in online_users.values():
        ubc.setdefault(info['channel'], []).append(info['username'])
    socketio.emit('users', {'users': ubc})


if __name__ == '__main__':
    init_db()
    avatars.update(load_all_avatars())
    port = int(os.environ.get('PORT', 5000))
    print(f"Blabbit running at http://localhost:{port}")
    socketio.run(app, host='0.0.0.0', port=port, debug=False, allow_unsafe_werkzeug=True)
else:
    # Running under gunicorn on Railway
    init_db()
    avatars.update(load_all_avatars())
