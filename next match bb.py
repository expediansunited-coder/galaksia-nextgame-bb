import io
import os
import re
import json
import time
import requests
import unicodedata
from datetime import datetime

import gspread
from oauth2client.service_account import ServiceAccountCredentials
from googleapiclient.discovery import build
from PIL import Image, ImageDraw, ImageFont

try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
except Exception as _e:
    print('  [heic] pillow-heif not available: %s' % _e)


# ============================================================
# CONFIG
# ============================================================
CREDENTIALS_FILE = 'credentials.json'

# --- Fixtures sheet ---
FIXTURES_SHEET_ID = '1j6ZN3N8aXnB9vKFdWeXhY-fyo8aH1JlmhWZWHwzgu-E'
FRIENDLY_TAB = 'Friendly Fixtures'
LEAGUECUP_TAB = 'League & Cup Fixtures'
INDEX_TAB = 'Index'

# Fixture columns (0-based): Date Time Home Away Location MatchType Round Status
FX_DATE, FX_TIME, FX_HOME, FX_AWAY, FX_LOC, FX_TYPE, FX_ROUND, FX_STATUS = range(8)

# Index columns: Team | Calendar ID | League | Training Calendar ID
IDX_TEAM, IDX_CAL, IDX_LEAGUE = 0, 1, 2

# Fixture/team logic uses these names
OUR_TEAMS = ['BB A', 'BB B']

# Design/output displays these names
DISPLAY_TEAM = {
    'BB A': 'GP23 A',
    'BB B': 'GP23 B',
}

FULL_DISPLAY_TEAM = {
    'BB A': 'Galaksia Praha 23 A',
    'BB B': 'Galaksia Praha 23 B',
}

TEAM_LABEL = {
    'BB A': 'A TEAM',
    'BB B': 'B TEAM',
}

TEAM_PARTNER = {
    'BB A': '@czechwise_official',
    'BB B': '@teflworldwideprague',
}

# Template/background file names
BACKGROUND_BY_TEAM = {
    'BB A': 'BB A',
    'BB B': 'BB B',
}

# --- Drive folders ---
LOGO_FOLDER_ID = '19NNyf1trl1LoA7Tth7PFMbRAv65oXeeR'
ASSETS_FOLDER_ID = '1aQ1ay_nCSQptlPyigVvzwbkReIdKioZV'
FONT_FOLDER_ID = '1-MAJwpIAjQvzXQdsPdqmkX4NGrM8YFt5'

FONT_NAME = 'Etna'
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
MAIN_FONT_LOCAL = os.path.join(SCRIPT_DIR, 'LeagueSpartan-Bold.otf')
TEAM_LABEL_FONT_LOCAL = os.path.join(SCRIPT_DIR, 'Benedict Regular.otf')
GALAKSIA_LOGO_NAME = 'galaksia praha 23'

# --- Canvas ---
CANVAS_W = 768
CANVAS_H = 960

# --- Colours ---
WHITE = (255, 255, 255)
BLACK = (0, 0, 0)

# --- Output ---
OUTPUT_DIR = 'output'
_FONT_LOCAL = os.path.join(OUTPUT_DIR, '_etna.ttf')

IMG_EXT = ('.png', '.jpg', '.jpeg', '.webp', '.heic', '.heif')

# --- Layout ---
TOP_MATCHUP_X = int(CANVAS_W * 0.065)
TOP_MATCHUP_RIGHT_X = int(CANVAS_W * 0.94)
TOP_MATCHUP_CY = int(CANVAS_H * 0.064)
TOP_MATCHUP_SIZE = int(CANVAS_H * 0.032)
TOP_MATCHUP_MAX_W = TOP_MATCHUP_RIGHT_X - TOP_MATCHUP_X

LOGO_CY = int(CANVAS_H * 0.470)
LOGO_LEFT_CX = int(CANVAS_W * 0.315)
LOGO_RIGHT_CX = int(CANVAS_W * 0.685)
LOGO_MAX = int(CANVAS_W * 0.294)

TEAM_LABEL_CY = int(CANVAS_H * 0.610)
TEAM_LABEL_SIZE = int(CANVAS_H * 0.048)

COMP_CY = int(CANVAS_H * 0.742)
COMP_SIZE = int(CANVAS_H * 0.045)
COMP_MAX_W = int(CANVAS_W * 0.90)

DATETIME_CY = int(CANVAS_H * 0.807)
DATETIME_SIZE = int(CANVAS_H * 0.050)
DATETIME_MAX_W = int(CANVAS_W * 0.90)

LOCATION_CY = int(CANVAS_H * 0.872)
LOCATION_SIZE = int(CANVAS_H * 0.031)
LOCATION_MAX_W = int(CANVAS_W * 0.92)

STORY_W = 1080
STORY_H = 1920

GRAPH = 'https://graph.facebook.com/v20.0'


# ============================================================
# AUTH
# ============================================================
def get_creds():
    scope = [
        'https://spreadsheets.google.com/feeds',
        'https://www.googleapis.com/auth/drive'
    ]
    return ServiceAccountCredentials.from_json_keyfile_name(CREDENTIALS_FILE, scope)


def get_gspread_client():
    return gspread.authorize(get_creds())


def get_drive_service():
    return build('drive', 'v3', credentials=get_creds())


def load_meta_config():
    with open('meta_config.json') as f:
        return json.load(f)


# ============================================================
# API RETRY
# ============================================================
from gspread.exceptions import APIError

def with_retry(func, *args, retries=6, base_delay=3, **kwargs):
    import random as _r
    for attempt in range(retries):
        try:
            return func(*args, **kwargs)
        except APIError as e:
            code = None
            try:
                code = e.response.status_code
            except Exception:
                pass
            transient = code in (429, 500, 502, 503) or code is None
            if not transient or attempt == retries - 1:
                raise
            delay = base_delay * (2 ** attempt) + _r.uniform(0, 1)
            print('  [retry] API %s - attempt %d/%d, waiting %.1fs'
                  % (code, attempt + 1, retries, delay))
            time.sleep(delay)


# ============================================================
# NORMALIZE / MATCH
# ============================================================
def _norm(s):
    if not s:
        return ''
    s = unicodedata.normalize('NFKD', str(s))
    s = ''.join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = re.sub(r'[^a-z0-9]+', '', s)
    return s


def _tokens(s):
    if not s:
        return []
    s = unicodedata.normalize('NFKD', str(s))
    s = ''.join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    return [t for t in re.split(r'[^a-z0-9]+', s) if t]


# ============================================================
# DRIVE HELPERS
# ============================================================
def list_folder(drive, folder_id):
    out = []
    page_token = None
    while True:
        resp = drive.files().list(
            q="'%s' in parents and trashed = false" % folder_id,
            fields='nextPageToken, files(id,name,mimeType)',
            pageToken=page_token
        ).execute()
        out.extend(resp.get('files', []))
        page_token = resp.get('nextPageToken')
        if not page_token:
            break
    return out


def download_bytes(drive, file_id):
    return drive.files().get_media(fileId=file_id).execute()


def find_by_basename(files, name):
    target = _norm(name)
    for f in files:
        base = os.path.splitext(f['name'])[0]
        if _norm(base) == target:
            return f
    return None


def download_image_from(drive, files, name):
    f = find_by_basename(files, name)
    if not f:
        return None
    data = download_bytes(drive, f['id'])
    return Image.open(io.BytesIO(data)).convert('RGBA')


def ensure_font(drive, font_files):
    if os.path.exists(_FONT_LOCAL):
        return _FONT_LOCAL

    f = find_by_basename(font_files, FONT_NAME)
    if not f:
        print('  Font "%s" not found; using default font.' % FONT_NAME)
        return None

    data = download_bytes(drive, f['id'])
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(_FONT_LOCAL, 'wb') as fh:
        fh.write(data)

    return _FONT_LOCAL


def find_logo(drive, logo_files, sheet_name):
    def try_match(name):
        target = _norm(name)
        if not target:
            return None

        f = find_by_basename(logo_files, name)
        if f:
            return f

        tset = set(_tokens(name))
        best = None

        for lf in logo_files:
            base = os.path.splitext(lf['name'])[0]
            lset = set(_tokens(base))

            if not lset:
                continue

            if tset == lset or tset <= lset or lset <= tset:
                return lf

            overlap = len(tset & lset)
            if overlap and overlap >= max(1, min(len(tset), len(lset))):
                best = lf

        return best

    f = try_match(sheet_name)
    if f:
        return f

    toks = _tokens(sheet_name)
    if toks and toks[-1] in ('a', 'b', 'c', 'd', 'vet', 'vets'):
        stripped = ' '.join(toks[:-1])
        f = try_match(stripped)
        if f:
            return f

    return None


# ============================================================
# LOGO BACKGROUND REMOVE
# ============================================================
def remove_edge_background(img, tol=40):
    img = img.convert('RGBA')
    w, h = img.size
    px = img.load()

    corners = [px[0, 0], px[w - 1, 0], px[0, h - 1], px[w - 1, h - 1]]
    br = sum(c[0] for c in corners) // 4
    bg = sum(c[1] for c in corners) // 4
    bb = sum(c[2] for c in corners) // 4

    def close(c):
        return (
            abs(c[0] - br) <= tol and
            abs(c[1] - bg) <= tol and
            abs(c[2] - bb) <= tol
        )

    from collections import deque

    visited = bytearray(w * h)
    dq = deque()

    for x in range(w):
        dq.append((x, 0))
        dq.append((x, h - 1))

    for y in range(h):
        dq.append((0, y))
        dq.append((w - 1, y))

    while dq:
        x, y = dq.popleft()
        idx = y * w + x

        if visited[idx]:
            continue

        visited[idx] = 1
        c = px[x, y]

        if c[3] == 0 or close(c):
            px[x, y] = (c[0], c[1], c[2], 0)

            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nx, ny = x + dx, y + dy

                if 0 <= nx < w and 0 <= ny < h and not visited[ny * w + nx]:
                    dq.append((nx, ny))

    cb = img.getbbox()
    return img.crop(cb) if cb else img


# ============================================================
# IMAGE HELPERS
# ============================================================
def load_font(font_path, size):
    if font_path and os.path.exists(font_path):
        try:
            return ImageFont.truetype(font_path, size)
        except Exception:
            pass
    return ImageFont.load_default()


def fit_font(font_path, text, max_w, start_size, min_size=14):
    size = start_size
    tmp = Image.new('RGBA', (10, 10))
    d = ImageDraw.Draw(tmp)

    while size >= min_size:
        f = load_font(font_path, size)
        b = d.textbbox((0, 0), text, font=f)

        shadow = max(2, int(size * 0.08))
        text_w = (b[2] - b[0]) + shadow

        if text_w <= max_w:
            return f

        size -= 2

    return load_font(font_path, min_size)



def _shadow_offset(font):
    return max(2, int(getattr(font, 'size', 30) * 0.08))


def draw_centered_text(draw, text, cx, cy, font, fill=WHITE,
                       stroke_width=0, stroke_fill=BLACK):
    b = draw.textbbox((0, 0), text, font=font)
    w = b[2] - b[0]
    h = b[3] - b[1]

    x = int(cx - w / 2)
    y = int(cy - h / 2)

    off = _shadow_offset(font)

    draw.text(
        (x + off, y + off),
        text,
        font=font,
        fill=BLACK
    )

    draw.text(
        (x, y),
        text,
        font=font,
        fill=fill
    )


def draw_left_text(draw, text, x, cy, font, fill=WHITE):
    b = draw.textbbox((0, 0), text, font=font)
    h = b[3] - b[1]

    y = int(cy - h / 2)
    off = _shadow_offset(font)

    draw.text(
        (x + off, y + off),
        text,
        font=font,
        fill=BLACK
    )

    draw.text(
        (x, y),
        text,
        font=font,
        fill=fill
    )


def paste_centered(bg, img, cx, cy):
    x = int(cx - img.width / 2)
    y = int(cy - img.height / 2)

    off = max(4, int(max(img.width, img.height) * 0.035))

    alpha = img.getchannel('A')
    shadow = Image.new('RGBA', img.size, BLACK + (0,))
    shadow.putalpha(alpha.point(lambda p: int(p * 0.75)))

    bg.alpha_composite(shadow, (x + off, y + off))
    bg.alpha_composite(img, (x, y))


def prep_logo(lg):
    if lg is None:
        return None

    l = remove_edge_background(lg)

    cb = l.getbbox()
    if cb:
        l = l.crop(cb)

    return l


def scale_logo(l):
    if l is None:
        return None

    scale = LOGO_MAX / max(l.width, l.height)

    l = l.resize(
        (
            max(1, int(l.width * scale)),
            max(1, int(l.height * scale))
        ),
        Image.LANCZOS
    )

    return l


# ============================================================
# LABELS / FORMAT
# ============================================================
def display_short(team):
    return DISPLAY_TEAM.get((team or '').strip().upper(), (team or '').strip().upper())


def display_full(team):
    return FULL_DISPLAY_TEAM.get((team or '').strip().upper(), 'Galaksia Praha 23')


def display_label(team):
    return TEAM_LABEL.get((team or '').strip().upper(), '')


def clean_team_name(name):
    s = (name or '').strip()
    s = re.sub(r'["“”„]\s*([A-Za-z])\s*["“”„]', r' \1', s)
    s = re.sub(r'\s*,?\s*(z\.\s*s\.|a\.\s*s\.)\s*', ' ', s, flags=re.I)
    s = re.sub(r'\s+', ' ', s).strip()
    return s


def ordinal_lower(n):
    if 10 <= n % 100 <= 20:
        suf = 'th'
    else:
        suf = {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')
    return '%d%s' % (n, suf)


def date_image_line(match_date):
    months = [
        'JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN',
        'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC'
    ]

    return '%s %s' % (
        months[match_date.month - 1],
        ordinal_lower(match_date.day)
    )


def date_caption_line(match_date):
    months = [
        'January', 'February', 'March', 'April', 'May', 'June',
        'July', 'August', 'September', 'October', 'November', 'December'
    ]

    days = [
        'Monday', 'Tuesday', 'Wednesday', 'Thursday',
        'Friday', 'Saturday', 'Sunday'
    ]

    return '%s %s %s' % (
        days[match_date.weekday()],
        ordinal_lower(match_date.day),
        months[match_date.month - 1]
    )


def parse_date(value):
    s = (value or '').strip()

    if not s:
        return None

    for fmt in ('%m/%d/%Y', '%m/%d/%y'):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue

    return None


def parse_time(value):
    s = (value or '').strip()

    if not s:
        return None

    s2 = s.replace('h', ':').replace('H', ':')

    m = re.match(r'^(\d{1,2}):(\d{2})', s2)
    if m:
        return '%02d:%02d' % (int(m.group(1)), int(m.group(2)))

    m = re.match(r'^(\d{1,2})$', s2)
    if m:
        return '%02d:00' % int(m.group(1))

    return None


def image_time(time_str):
    return (time_str or '').replace(':', 'H')


def header_text(match_type, league, round_num):
    mt = (match_type or '').strip().lower()

    if mt == 'friendly':
        return 'FRIENDLY'

    base = (league or '').strip().upper()

    if round_num:
        return '%s - ROUND %s' % (base, round_num)

    return base


def build_team_league_map(client):
    ss = with_retry(client.open_by_key, FIXTURES_SHEET_ID)
    ws = ss.worksheet(INDEX_TAB)
    data = with_retry(ws.get_all_values)

    mapping = {}

    for row in data[1:]:
        if len(row) <= max(IDX_TEAM, IDX_LEAGUE):
            continue

        team = (row[IDX_TEAM] or '').strip()
        league = (row[IDX_LEAGUE] or '').strip()

        if team:
            mapping[team.lower()] = league

    return mapping


def which_side(home, away):
    our_upper = [t.upper() for t in OUR_TEAMS]

    home_is = home.strip().upper() in our_upper
    away_is = away.strip().upper() in our_upper

    if home_is and away_is:
        return home.strip(), away.strip(), 'left', True

    if home_is:
        return home.strip(), away.strip(), 'left', False

    if away_is:
        return away.strip(), home.strip(), 'right', False

    return None, None, None, False


# ============================================================
# IMAGE BUILD
# ============================================================
def build_image(bg_src, font_path,
                gp_logo, opp_logo,
                gp_side, gp_team, opp_name,
                league, round_num, match_type,
                match_date, time_str, loc_str):

    bg = bg_src.copy().convert('RGBA')

    if bg.size != (CANVAS_W, CANVAS_H):
        bg = bg.resize((CANVAS_W, CANVAS_H), Image.LANCZOS)

    draw = ImageDraw.Draw(bg)

    gp_short = display_short(gp_team)
    opp_clean = clean_team_name(opp_name)

    if gp_side == 'left':
        matchup = '%s VS %s' % (gp_short, opp_clean.upper())
        left_logo = gp_logo
        right_logo = opp_logo
    else:
        matchup = '%s VS %s' % (opp_clean.upper(), gp_short)
        left_logo = opp_logo
        right_logo = gp_logo

    matchup_font = fit_font(font_path, matchup, TOP_MATCHUP_MAX_W, TOP_MATCHUP_SIZE)
    draw_left_text(
        draw,
        matchup,
        TOP_MATCHUP_X,
        TOP_MATCHUP_CY,
        matchup_font,
        fill=WHITE
    )

    ll = scale_logo(prep_logo(left_logo))
    rl = scale_logo(prep_logo(right_logo))

    if ll is not None:
        paste_centered(bg, ll, LOGO_LEFT_CX, LOGO_CY)

    if rl is not None:
        paste_centered(bg, rl, LOGO_RIGHT_CX, LOGO_CY)

    label = display_label(gp_team)
    if label:
        label_font = load_font(TEAM_LABEL_FONT_LOCAL, TEAM_LABEL_SIZE)
        label_cx = LOGO_LEFT_CX if gp_side == 'left' else LOGO_RIGHT_CX

        draw_centered_text(
            draw,
            label,
            label_cx,
            TEAM_LABEL_CY,
            label_font,
            fill=WHITE,
            stroke_width=max(1, int(TEAM_LABEL_SIZE * 0.04)),
            stroke_fill=BLACK
        )

    comp = header_text(match_type, league, round_num)
    comp_font = fit_font(font_path, comp, COMP_MAX_W, COMP_SIZE)
    draw_centered_text(
        draw,
        comp,
        CANVAS_W // 2,
        COMP_CY,
        comp_font,
        fill=WHITE,
        stroke_width=max(1, int(COMP_SIZE * 0.09)),
        stroke_fill=BLACK
    )

    dt = '%s  | %s' % (date_image_line(match_date).upper(), image_time(time_str))
    dt_font = fit_font(font_path, dt, DATETIME_MAX_W, DATETIME_SIZE)
    draw_centered_text(
        draw,
        dt,
        CANVAS_W // 2,
        DATETIME_CY,
        dt_font,
        fill=WHITE,
        stroke_width=max(1, int(DATETIME_SIZE * 0.09)),
        stroke_fill=BLACK
    )

    loc = (loc_str or '').upper()
    loc_font = fit_font(font_path, loc, LOCATION_MAX_W, LOCATION_SIZE)
    draw_centered_text(
        draw,
        loc,
        CANVAS_W // 2,
        LOCATION_CY,
        loc_font,
        fill=WHITE,
        stroke_width=max(1, int(LOCATION_SIZE * 0.08)),
        stroke_fill=BLACK
    )

    return bg.convert('RGB')


def make_story_version(feed_img_path):
    from PIL import ImageFilter

    feed = Image.open(feed_img_path).convert('RGB')

    bg = feed.copy()
    scale = max(STORY_W / bg.width, STORY_H / bg.height)
    bg = bg.resize((int(bg.width * scale), int(bg.height * scale)), Image.LANCZOS)

    left = (bg.width - STORY_W) // 2
    top = (bg.height - STORY_H) // 2
    bg = bg.crop((left, top, left + STORY_W, top + STORY_H))
    bg = bg.filter(ImageFilter.GaussianBlur(40))

    fg = feed.copy()
    fscale = min(STORY_W / fg.width, STORY_H / fg.height) * 0.92
    fg = fg.resize((int(fg.width * fscale), int(fg.height * fscale)), Image.LANCZOS)

    bg.paste(fg, ((STORY_W - fg.width) // 2, (STORY_H - fg.height) // 2))

    out_path = feed_img_path.replace('.png', '_story.png')
    bg.save(out_path, 'PNG', quality=95)

    return out_path


# ============================================================
# CAPTION
# ============================================================
def build_caption(gp_team, opp_name, gp_side, match_type, league,
                  round_num, match_date, loc_str, time_str):

    gp_label = display_full(gp_team)
    opp_display = clean_team_name(opp_name)

    team_partner = TEAM_PARTNER.get((gp_team or '').strip().upper(), '@czechwise_official')

    if gp_side == 'left':
        matchup = '%s vs %s' % (gp_label, opp_display)
        home_away = 'at home'
    else:
        matchup = '%s vs %s' % (opp_display, gp_label)
        home_away = 'away'

    mt = (match_type or '').strip().lower()

    if mt == 'friendly':
        comp = 'Friendly'
    else:
        comp = header_text(match_type, league, round_num).title()

    caption = (
        "🏀 NEXT MATCH!\n\n"
        "%s\n"
        "🏆 %s\n"
        "📅 %s\n"
        "🕒 Tip-off %s\n"
        "📍 %s (%s)\n\n"
        "Team partner: %s\n\n"
        "Win as one team.\n\n"
        "#GalaksiaPraha23 #GP23 #Basketball #Basketbal #Prague #Praha "
        "#GameDay #NextMatch #TeamBasketball"
        % (
            matchup,
            comp,
            date_caption_line(match_date),
            time_str,
            loc_str,
            home_away,
            team_partner
        )
    )

    return caption


# ============================================================
# META
# ============================================================
def _fb_page_photo(page_id, token, image_url, caption, published=True):
    r = requests.post(
        '%s/%s/photos' % (GRAPH, page_id),
        data={
            'url': image_url,
            'caption': caption,
            'published': 'true' if published else 'false',
            'access_token': token
        }
    )
    r.raise_for_status()
    return r.json()


def _fb_story(page_id, token, photo_id):
    r = requests.post(
        '%s/%s/photo_stories' % (GRAPH, page_id),
        data={
            'photo_id': photo_id,
            'access_token': token
        }
    )
    r.raise_for_status()
    return r.json()


def _ig_publish(ig_id, token, image_url, caption=None, is_story=False):
    data = {
        'image_url': image_url,
        'access_token': token
    }

    if is_story:
        data['media_type'] = 'STORIES'
    elif caption:
        data['caption'] = caption

    c = requests.post('%s/%s/media' % (GRAPH, ig_id), data=data)
    c.raise_for_status()

    creation_id = c.json()['id']

    for _ in range(10):
        st = requests.get(
            '%s/%s' % (GRAPH, creation_id),
            params={
                'fields': 'status_code',
                'access_token': token
            }
        )

        code = st.json().get('status_code')

        if code == 'FINISHED':
            break

        if code == 'ERROR':
            raise RuntimeError('IG container error: %s' % st.text)

        time.sleep(3)

    p = requests.post(
        '%s/%s/media_publish' % (GRAPH, ig_id),
        data={
            'creation_id': creation_id,
            'access_token': token
        }
    )
    p.raise_for_status()

    return p.json()


def _get_page_token(page_id, user_token):
    r = requests.get(
        '%s/me/accounts' % GRAPH,
        params={
            'access_token': user_token,
            'limit': 200
        }
    )
    r.raise_for_status()

    for p in r.json().get('data', []):
        if str(p.get('id')) == str(page_id):
            return p['access_token']

    raise RuntimeError('Page %s not found in me/accounts' % page_id)


def post_to_meta(image_path, caption, image_url=None, story_url=None):
    cfg = load_meta_config()

    page_id = cfg['page_id']
    ig_id = cfg['ig_user_id']
    user_token = cfg['page_access_token']

    if not image_url:
        print('    [meta] ERROR: no public image_url provided; cannot post.')
        return

    try:
        token = _get_page_token(page_id, user_token)
    except Exception as e:
        print('    [meta] could not derive Page token: %s' % e)
        token = user_token

    ig_token = user_token

    try:
        _fb_page_photo(page_id, token, image_url, caption, published=True)
        print('    [meta] FB feed OK')
    except Exception as e:
        print('    [meta] FB feed FAILED: %s' % e)

    try:
        su = story_url or image_url
        photo = _fb_page_photo(page_id, token, su, '', published=False)
        _fb_story(page_id, token, photo['id'])
        print('    [meta] FB story OK')
    except Exception as e:
        print('    [meta] FB story FAILED: %s' % e)

    try:
        _ig_publish(ig_id, ig_token, image_url, caption=caption, is_story=False)
        print('    [meta] IG feed OK')
    except Exception as e:
        print('    [meta] IG feed FAILED: %s' % e)

    try:
        _ig_publish(ig_id, ig_token, story_url or image_url, is_story=True)
        print('    [meta] IG story OK')
    except Exception as e:
        print('    [meta] IG story FAILED: %s' % e)


# ============================================================
# ERROR EMAIL
# ============================================================
def send_error_email(errors):
    if not errors:
        return

    print('  [error-email] %d error(s):' % len(errors))
    for e in errors:
        print('    - %s' % e)


# ============================================================
# MAIN
# ============================================================
def run_basketball_next_match_generator():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    errors = []

    print('Auth...')
    client = get_gspread_client()
    drive = get_drive_service()

    print('Reading Index...')
    team_league = build_team_league_map(client)

    print('Listing Drive folders...')
    logo_files = list_folder(drive, LOGO_FOLDER_ID)
    asset_files = list_folder(drive, ASSETS_FOLDER_ID)
    font_files = list_folder(drive, FONT_FOLDER_ID)

    print('Loading fonts...')
    font_path = MAIN_FONT_LOCAL

    print('Loading Galaksia logo...')
    gp_logo = download_image_from(drive, logo_files, GALAKSIA_LOGO_NAME)

    if gp_logo is None:
        errors.append('Galaksia logo "%s" not found in logo folder.' % GALAKSIA_LOGO_NAME)

    today = datetime.now().date()
    print('Today: %s' % today)

    opp_logo_cache = {}
    background_cache = {}
    generated = 0

    ss = with_retry(client.open_by_key, FIXTURES_SHEET_ID)

    all_fx = []
    relevant_seen = []

    print('Scanning basketball fixtures...')

    for tab in (FRIENDLY_TAB, LEAGUECUP_TAB):
        ws = ss.worksheet(tab)
        data = with_retry(ws.get_all_values)

        tab_seen = 0
        tab_collected = 0

        for i, row in enumerate(data[1:], start=2):
            if len(row) <= FX_STATUS:
                continue

            m_date = parse_date(row[FX_DATE])
            if not m_date:
                continue

            status = (row[FX_STATUS] or '').strip()

            if status not in ('Completed', 'Next Game Posted'):
                continue

            home = (row[FX_HOME] or '').strip()
            away = (row[FX_AWAY] or '').strip()

            gp_team, opp_name, gp_side, is_derby = which_side(home, away)
            if gp_team is None:
                continue

            tab_seen += 1

            if not POST_ONLY and m_date < today:
                continue

            relevant_seen.append(
                '%s row %d: %s %s | %s vs %s'
                % (tab, i, m_date.isoformat(), row[FX_TIME], home, away)
            )

            all_fx.append({
                'tab': tab,
                'row_i': i,
                'row': row,
                'date': m_date,
                'home': home,
                'away': away,
                'gp_team': gp_team.upper(),
                'opp_name': opp_name,
                'gp_side': gp_side,
                'is_derby': is_derby,
                'match_type': (row[FX_TYPE] or '').strip(),
                'round': (row[FX_ROUND] or '').strip(),
                'loc': (row[FX_LOC] or '').strip(),
                'ws': ws,
                'status': status,
            })

            tab_collected += 1

        print('  %s: %d basketball fixture(s), %d current/future collected'
              % (tab, tab_seen, tab_collected))

    print('Collected %d current/future basketball fixture(s).' % len(all_fx))
    for item in relevant_seen:
        print('  - %s' % item)

    def should_post(fx):
        if fx.get('status') != 'Completed':
            return False

        days = (fx['date'] - today).days
        our_upper = [t.upper() for t in OUR_TEAMS]

        if days < 0:
            return False

        if days >= 3:
            match_days = set(
                (g['date'] - today).days
                for g in all_fx
                if g['gp_team'] in our_upper
                and (g['date'] - today).days >= 0
            )

            if 3 in match_days:
                for d in range(3, days + 1):
                    if d not in match_days:
                        break
                else:
                    return True

        if days == 3:
            between = [
                g for g in all_fx
                if g['gp_team'] in our_upper
                and today <= g['date'] < fx['date']
            ]

            if not between:
                return True

            latest_between_date = max(g['date'] for g in between)

            if (fx['date'] - latest_between_date).days == 1:
                return True

        if 1 <= days <= 3:
            prev = [
                g['date'] for g in all_fx
                if g['gp_team'] in our_upper
                and g['date'] < today
            ]

            if prev and (today - max(prev)).days == 1:
                return True

        return False

    def _sort_key(fx):
        t = parse_time(fx['row'][FX_TIME]) or '99:99'
        team_order = {'BB A': 1, 'BB B': 2}
        return (fx['date'], t, team_order.get(fx['gp_team'], 99))

    all_fx.sort(key=_sort_key)

    for fx in all_fx:
        if not should_post(fx):
            continue

        tab = fx['tab']
        i = fx['row_i']
        row = fx['row']

        gp_team = fx['gp_team']
        opp_name = clean_team_name(fx['opp_name'])
        gp_side = fx['gp_side']
        is_derby = fx['is_derby']
        match_type = fx['match_type']
        round_num = fx['round']
        loc = fx['loc']
        m_date = fx['date']

        tag = '%s row %d [%s vs %s]' % (tab, i, fx['home'], fx['away'])
        print('Processing %s' % tag)

        time_str = parse_time(row[FX_TIME])
        if not time_str:
            errors.append('%s: missing/invalid tip-off time.' % tag)
            continue

        league = team_league.get(gp_team.lower(), '')
        if not league:
            errors.append('%s: no league found in Index for team %s.' % (tag, gp_team))

        bg_name = BACKGROUND_BY_TEAM.get(gp_team)
        if bg_name not in background_cache:
            background_cache[bg_name] = download_image_from(drive, asset_files, bg_name)

        background = background_cache.get(bg_name)

        if background is None:
            errors.append('%s: basketball background "%s" not found.' % (tag, bg_name))
            continue

        if is_derby:
            opp_logo = gp_logo
        else:
            key = _norm(opp_name)

            if key not in opp_logo_cache:
                lf = find_logo(drive, logo_files, opp_name)

                if not lf:
                    errors.append('%s: NO LOGO found for opponent "%s" - using placeholder.'
                                  % (tag, opp_name))
                    lf = find_by_basename(logo_files, 'no logo')

                if lf:
                    try:
                        d = download_bytes(drive, lf['id'])
                        opp_logo_cache[key] = Image.open(io.BytesIO(d)).convert('RGBA')
                    except Exception as e:
                        opp_logo_cache[key] = None
                        errors.append('%s: opponent logo load failed (%s): %s'
                                      % (tag, opp_name, e))
                else:
                    opp_logo_cache[key] = None
                    errors.append('%s: "no logo" placeholder also not found.' % tag)

            opp_logo = opp_logo_cache[key]

        if gp_logo is None or opp_logo is None:
            continue

        try:
            img = build_image(
                background,
                font_path,
                gp_logo,
                opp_logo,
                gp_side,
                gp_team,
                opp_name,
                league,
                round_num,
                match_type,
                m_date,
                time_str,
                loc
            )
        except Exception as e:
            errors.append('%s: image build failed: %s' % (tag, e))
            continue

        safe = re.sub(r'[^A-Za-z0-9]+', '_', '%s_%s' % (display_short(gp_team), opp_name))
        out_path = os.path.join(
            OUTPUT_DIR,
            'basketball_nextmatch_%s_%s.png' % (safe, m_date.isoformat())
        )

        img.save(out_path, 'PNG', quality=95)
        print('  saved %s' % out_path)
        generated += 1

        story_path = make_story_version(out_path)

        caption = build_caption(
            gp_team,
            opp_name,
            gp_side,
            match_type,
            league,
            round_num,
            m_date,
            loc,
            time_str
        )

        if POST_ONLY:
            repo_raw = 'https://raw.githubusercontent.com/expediansunited-coder/galaksia-basketball-nextgame/main/'
            feed_url = repo_raw + out_path.replace('\\', '/')
            story_url = repo_raw + story_path.replace('\\', '/')

            print('  feed url: %s' % feed_url)
            print('  story url: %s' % story_url)

            try:
                post_to_meta(out_path, caption, image_url=feed_url, story_url=story_url)
                with_retry(fx['ws'].update_cell, i, FX_STATUS + 1, 'Next Game Posted')
                fx['status'] = 'Next Game Posted'
                print('  status updated: Next Game Posted')
            except Exception as e:
                errors.append('%s: Meta posting failed: %s' % (tag, e))
        else:
            print('  generate-only: image saved, not posting.')

    print('Done. Generated %d image(s), %d error(s).' % (generated, len(errors)))
    send_error_email(errors)


import sys

GENERATE_ONLY = '--generate-only' in sys.argv
POST_ONLY = '--post-only' in sys.argv

if not GENERATE_ONLY and not POST_ONLY:
    GENERATE_ONLY = True
    POST_ONLY = True

if __name__ == '__main__':
    run_basketball_next_match_generator()
