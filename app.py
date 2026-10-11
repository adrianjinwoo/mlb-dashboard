import time
from datetime import datetime, timedelta
import requests
import streamlit as st

# ==========================================
# 1. 페이지 및 상태 초기화
# ==========================================
st.set_page_config(
    page_title="MLB Pro Betting Dashboard", page_icon="⚾", layout="wide"
)

if "last_briefing" not in st.session_state: st.session_state.last_briefing = None
if "prev_inning" not in st.session_state: st.session_state.prev_inning = None
if "stats_cache" not in st.session_state: st.session_state.stats_cache = {}
if "bullpen_cache" not in st.session_state: st.session_state.bullpen_cache = {}
if "lineup_cache" not in st.session_state: st.session_state.lineup_cache = {}
if "current_game_pk" not in st.session_state: st.session_state.current_game_pk = None

# ==========================================
# 2. 사이드바 (오늘~모레 경기 로드)
# ==========================================
@st.cache_data(ttl=60)
def get_upcoming_games():
    today = datetime.now()
    d_start = (today - timedelta(days=1)).strftime("%Y-%m-%d")
    d_end = (today + timedelta(days=2)).strftime("%Y-%m-%d")
    
    url = f"https://statsapi.mlb.com/api/v1/schedule?sportId=1&startDate={d_start}&endDate={d_end}&hydrate=probablePitcher"
    game_options = {}
    try:
        res = requests.get(url, timeout=4).json()
        for d in res.get("dates", []):
            date_str = d.get("date", "")[5:]
            for g in d.get("games", []):
                pk = str(g["gamePk"])
                away = g["teams"]["away"]["team"]["name"]
                home = g["teams"]["home"]["team"]["name"]
                status = g.get("status", {}).get("detailedState", "Unknown")
                label = f"[{date_str} | {status}] {away} vs {home}"
                
                aw_prob = g["teams"]["away"].get("probablePitcher", {})
                hw_prob = g["teams"]["home"].get("probablePitcher", {})
                
                game_options[label] = {
                    "pk": pk,
                    "away_sp": {"id": aw_prob.get("id"), "fullName": aw_prob.get("fullName", "미정")},
                    "home_sp": {"id": hw_prob.get("id"), "fullName": hw_prob.get("fullName", "미정")}
                }
    except Exception:
        pass
    return game_options

st.sidebar.header("⚙️ 경기 설정")
upcoming_games = get_upcoming_games()

if upcoming_games:
    def sort_key(label):
        if "In Progress" in label or "Live" in label: return 0
        if "Scheduled" in label or "Preview" in label: return 1
        return 2

    sorted_labels = sorted(list(upcoming_games.keys()), key=sort_key)
    selected_label = st.sidebar.selectbox("⚾ 분석할 경기 선택", options=sorted_labels)
    game_info = upcoming_games[selected_label]
    game_pk = game_info["pk"]
    st.sidebar.caption(f"연결된 Game PK: `{game_pk}`")
else:
    game_pk = st.sidebar.text_input("MLB Game PK (수동 입력)", value="746850")
    game_info = {"pk": game_pk, "away_sp": {"id": None, "fullName": "미정"}, "home_sp": {"id": None, "fullName": "미정"}}

poll_interval = st.sidebar.slider("새로고침 주기 (초)", 2, 6, 3)

if st.session_state.current_game_pk != game_pk:
    st.session_state.current_game_pk = game_pk
    st.session_state.last_briefing = None
    st.session_state.prev_inning = None
    st.session_state.lineup_cache = {}
    st.session_state.stats_cache = {}

dashboard_placeholder = st.empty()

# ==========================================
# 3. 데이터 파싱 및 AI 알고리즘 함수
# ==========================================
# [NEW 1] 최근 7일 타격감 (Hot/Cold) 조회
@st.cache_data(ttl=3600)
def get_last_7_days_ops(batter_id):
    today = datetime.now()
    d_start = (today - timedelta(days=7)).strftime("%Y-%m-%d")
    d_end = today.strftime("%Y-%m-%d")
    url = f"https://statsapi.mlb.com/api/v1/people/{batter_id}/stats?stats=byDateRange&group=hitting&startDate={d_start}&endDate={d_end}"
    try:
        res = requests.get(url, timeout=3).json()
        stat = res.get("stats", [{}])[0].get("splits", [{}])[0].get("stat", {})
        ops = stat.get("ops")
        if ops: return float(ops)
    except: pass
    return None

# [NEW 2] 실시간 투구 구속 저하 추적
def get_pitcher_velocity_trend(all_plays, current_pitcher_id):
    fastball_codes = ['FF', 'SI', 'FC'] # 포심, 싱커, 커터
    speeds = []
    for play in all_plays:
        if play.get("matchup", {}).get("pitcher", {}).get("id") == current_pitcher_id:
            for event in play.get("playEvents", []):
                if event.get("isPitch"):
                    details = event.get("details", {})
                    if details.get("type", {}).get("code") in fastball_codes:
                        speed = event.get("pitchData", {}).get("startSpeed")
                        if speed: speeds.append(speed)
                        
    if len(speeds) < 20: return None, 0.0
    baseline = sum(speeds[:15]) / 15
    recent = sum(speeds[-5:]) / 5
    drop = baseline - recent
    return baseline, drop

@st.cache_data(ttl=1800)
def get_pitcher_preview(pitcher_id, opp_team_id):
    if not pitcher_id: return None
    url = f"https://statsapi.mlb.com/api/v1/people/{pitcher_id}/stats?stats=season,gameLog,vsTeam&opposingTeamId={opp_team_id}&group=pitching"
    try:
        res = requests.get(url, timeout=4).json()
        season_str, vs_team_str = "기록 없음", "맞대결 전적 없음"
        recent_list, era_val = [], None
        for st_group in res.get("stats", []):
            s_type = st_group.get("type", {}).get("displayName")
            splits = st_group.get("splits", [])
            if not splits: continue
            if s_type == "season":
                stat = splits[0].get("stat", {})
                era = stat.get("era", "-")
                w, l = stat.get("wins", 0), stat.get("losses", 0)
                season_str = f"ERA {era} ({w}승 {l}패)"
                if era not in ["-", "*.**"]: era_val = era
            elif s_type == "gameLog":
                for g in reversed(splits[-5:]):
                    dt, opp = g.get("date", "")[5:], g.get("opponent", {}).get("name", "Opp").split(" ")[-1]
                    ip, er = g.get("stat", {}).get("inningsPitched", "0"), g.get("stat", {}).get("earnedRuns", "0")
                    recent_list.append(f"{dt} vs {opp}: {ip}이닝 {er}실점")
            elif s_type == "vsTeam":
                stat = splits[0].get("stat", {})
                era, ip, er = stat.get("era", "-"), stat.get("inningsPitched", "0"), stat.get("earnedRuns", "0")
                vs_team_str = f"ERA {era} ({ip}이닝 {er}실점)"
        return {"season": season_str, "recent": recent_list, "vs_team": vs_team_str, "era_val": era_val}
    except: return None

@st.cache_data(ttl=3600)
def check_bullpen_fatigue(team_id):
    today = datetime.now()
    d1, d2 = (today - timedelta(days=2)).strftime("%Y-%m-%d"), (today - timedelta(days=1)).strftime("%Y-%m-%d")
    url = f"https://statsapi.mlb.com/api/v1/schedule?teamId={team_id}&sportId=1&startDate={d1}&endDate={d2}"
    try:
        res = requests.get(url, timeout=5).json()
        game_pks = [g["gamePk"] for d in res.get("dates", []) for g in d.get("games", []) if g["status"]["statusCode"] == "F"]
    except: return []

    apps = {}
    for pk in game_pks:
        try:
            box = requests.get(f"https://statsapi.mlb.com/api/v1.1/game/{pk}/feed/live", timeout=3).json().get("liveData", {}).get("boxscore", {}).get("teams", {})
            t_key = 'away' if box.get('away', {}).get('team', {}).get('id') == team_id else 'home'
            relievers = box.get(t_key, {}).get("pitchers", [])[1:]
            players_info = box.get(t_key, {}).get("players", {})
            for pid in relievers:
                name = players_info.get(f"ID{pid}", {}).get("person", {}).get("fullName", "Unknown")
                apps[name] = apps.get(name, 0) + 1
        except: continue
    return [name for name, count in apps.items() if count >= 2]

def get_starting_lineup(boxscore, team_key):
    players = boxscore.get('teams', {}).get(team_key, {}).get('players', {})
    starters = []
    for pid, p in players.items():
        bo = p.get('battingOrder')
        if bo and bo in ["100", "200", "300", "400", "500", "600", "700", "800", "900"]:
            starters.append((int(bo[0]), p.get('person', {}).get('fullName')))
    starters.sort(key=lambda x: x[0])
    return [name for _, name in starters]

@st.cache_data(ttl=3600)
def get_previous_lineup(team_id):
    today = datetime.now()
    d1, d2 = (today - timedelta(days=5)).strftime("%Y-%m-%d"), (today - timedelta(days=1)).strftime("%Y-%m-%d")
    url = f"https://statsapi.mlb.com/api/v1/schedule?teamId={team_id}&sportId=1&startDate={d1}&endDate={d2}"
    try:
        res = requests.get(url, timeout=5).json()
        games = [g for d in res.get("dates", []) for g in d.get("games", []) if g["status"]["statusCode"] == "F"]
        if not games: return []
        last_game_pk = games[-1]["gamePk"]
        box = requests.get(f"https://statsapi.mlb.com/api/v1.1/game/{last_game_pk}/feed/live", timeout=3).json().get("liveData", {}).get("boxscore", {}).get("teams", {})
        t_key = 'away' if box.get('away', {}).get('team', {}).get('id') == team_id else 'home'
        return get_starting_lineup(box, t_key)
    except: return []

def calculate_win_probability(away_diff, home_diff, away_fatigue, home_fatigue, aw_era, hw_era):
    home_win_prob = 53.5
    home_win_prob -= (len(home_diff) * 2.5)
    home_win_prob += (len(away_diff) * 2.5)
    home_win_prob -= (len(home_fatigue) * 1.5)
    home_win_prob += (len(away_fatigue) * 1.5)
    
    if aw_era and hw_era:
        try:
            home_win_prob += ((float(aw_era) - float(hw_era)) * 4.0)
        except ValueError: pass
    
    home_win_prob = max(30.0, min(70.0, home_win_prob))
    away_win_prob = 100.0 - home_win_prob
    
    return away_win_prob, home_win_prob, 1/(away_win_prob/100), 1/(home_win_prob/100), 1/((away_win_prob/100)*1.045), 1/((home_win_prob/100)*1.045)

def get_batters(boxscore, team_key, offense_info, is_batting_now):
    players = boxscore.get("teams", {}).get(team_key, {}).get("players", {})
    active_lineup_dict = {}
    for pid, p_info in players.items():
        bo = p_info.get("battingOrder")
        if bo and len(bo) == 3:
            o_idx, s_idx = int(bo[0]), int(bo[1:])
            if o_idx not in active_lineup_dict or s_idx > active_lineup_dict[o_idx][0]:
                active_lineup_dict[o_idx] = (s_idx, p_info)
                
    lineup_ordered = [active_lineup_dict[i][1] for i in range(1, 10) if i in active_lineup_dict]
    if not lineup_ordered: return []

    start_idx = 0
    if is_batting_now:
        cur_id = offense_info.get("batter", {}).get("id")
        for i, p in enumerate(lineup_ordered):
            if p.get("person", {}).get("id") == cur_id: start_idx = i; break

    target_batters = []
    for i in range(5):
        idx = (start_idx + i) % len(lineup_ordered)
        p = lineup_ordered[idx]
        target_batters.append({
            "id": p.get("person", {}).get("id"),
            "name": p.get("person", {}).get("fullName"),
            "order": int(p.get("battingOrder", "000")[0])
        })
    return target_batters

def fetch_bvp_and_split(batter_id, pitcher_id, pitcher_hand):
    cache_key = f"{batter_id}_{pitcher_id}_{pitcher_hand}"
    if cache_key in st.session_state.stats_cache: return st.session_state.stats_cache[cache_key]
    hand_key, res_data = "vs Left" if pitcher_hand == "L" else "vs Right", {"bvp_text": "전적 없음", "bvp_ops": None, "split_ops": 0.700}
    try:
        b_res = requests.get(f"https://statsapi.mlb.com/api/v1/people/{batter_id}/stats?stats=vsPlayer&opposingPlayerId={pitcher_id}&group=hitting", timeout=3).json()
        b_splits = b_res.get("stats", [{}])[0].get("splits", [])
        if b_splits:
            stat = b_splits[0].get("stat", {})
            ops = float(stat.get("ops", ".000"))
            res_data["bvp_text"] = f"{stat.get('atBats', 0)}수 {stat.get('hits', 0)}안 (OPS {ops:.3f})"
            res_data["bvp_ops"] = ops
        s_res = requests.get(f"https://statsapi.mlb.com/api/v1/people/{batter_id}/stats?stats=statSplits&group=hitting&sitCodes=vl,vr", timeout=3).json()
        s_splits = s_res.get("stats", [{}])[0].get("splits", [])
        for s in s_splits:
            if s.get("split", {}).get("description") == hand_key: res_data["split_ops"] = float(s.get("stat", {}).get("ops", 0.700)); break
    except: pass
    st.session_state.stats_cache[cache_key] = res_data
    return res_data

def evaluate_team_matchup(batters, pitcher_id, pitcher_hand):
    if not pitcher_id or not batters: return 0, []
    score = 0
    details = []
    for b in batters:
        d = fetch_bvp_and_split(b["id"], pitcher_id, pitcher_hand)
        l7_ops = get_last_7_days_ops(b["id"]) # [NEW] 최근 7일 스탯 결합
        
        sp_ops, bvp_ops = d["split_ops"], d["bvp_ops"]
        if sp_ops >= 0.800: score += 1
        elif sp_ops <= 0.650: score -= 1
        if bvp_ops is not None:
            if bvp_ops >= 0.900: score += 1.5
            elif bvp_ops <= 0.500: score -= 1.5
            
        hot_cold_str = ""
        if l7_ops:
            if l7_ops >= 0.900: score += 1; hot_cold_str = f" | 🔥7일 OPS `{l7_ops:.3f}`"
            elif l7_ops <= 0.550: score -= 1; hot_cold_str = f" | ❄️7일 OPS `{l7_ops:.3f}`"
            else: hot_cold_str = f" | 📊7일 OPS `{l7_ops:.3f}`"
            
        details.append(f"**{b['order']}번 {b['name']}**: {pitcher_hand}투 OPS `{sp_ops:.3f}`{hot_cold_str} | {d['bvp_text']}")
    return score, details

def extract_runners(offense, boxscore, is_top):
    team_key = "away" if is_top else "home"
    players = boxscore.get("teams", {}).get(team_key, {}).get("players", {})
    def get_name(base_key):
        base_obj = offense.get(base_key)
        if not base_obj: return None
        if "fullName" in base_obj: return base_obj["fullName"]
        return players.get(f"ID{base_obj.get('id')}", {}).get("person", {}).get("fullName", f"Runner")
    r1, r2, r3 = get_name("first"), get_name("second"), get_name("third")
    names = [f"{b}루: {r}" for b, r in zip([3,2,1], [r3,r2,r1]) if r]
    return {"icon": f"[{'🔴' if r3 else '⚪'}{'🔴' if r2 else '⚪'}{'🔴' if r1 else '⚪'}]", "text": " | ".join(names) if names else "주자 없음"}

# [NEW 3] 구장(Park Factor) 및 날씨(Weather)를 반영한 실시간 토탈 계산
def get_live_total_recommendation(current_inning, total_runs, total_fatigued, temp, wind, venue):
    if current_inning <= 4: return "<p style='color:gray;'>⏳ 5회 이전은 변동성이 커서 잔여 토탈 가이드를 제공하지 않습니다.</p>"

    l1_over, l1_under = (2.30, 1.50) if current_inning >= 8 else (1.85, 2.05)
    l2_over, l2_under = (3.50, 1.25) if current_inning >= 8 else (2.40, 1.60)

    # 불펜 피로도 보정
    if total_fatigued >= 3:
        l1_over -= 0.15; l1_under += 0.25; l2_over -= 0.30; l2_under += 0.15
    elif total_fatigued == 0 and current_inning >= 6:
        l1_over += 0.25; l1_under -= 0.15; l2_over += 0.35; l2_under -= 0.10

    # 파크 팩터 및 날씨 보정 (오버 친화적일수록 Over 배당컷 낮춤)
    weather_desc = ""
    try:
        temp_val = int(temp)
        if temp_val >= 80: l1_over -= 0.05; l2_over -= 0.05; weather_desc += "🌡️폭염(타자 유리) "
        elif temp_val <= 55: l1_over += 0.05; l2_over += 0.05; weather_desc += "🥶추위(투수 유리) "
    except: pass
    
    if "Out" in wind: l1_over -= 0.10; l2_over -= 0.10; weather_desc += "🌬️외야 강풍(오버 주의) "
    elif "In" in wind: l1_over += 0.10; l2_over += 0.10; weather_desc += "🌬️역풍(언더 유리) "
        
    hitter_parks = ["Coors Field", "Great American Ball Park", "Fenway Park"]
    pitcher_parks = ["T-Mobile Park", "Oakland Coliseum", "Petco Park"]
    if venue in hitter_parks: l1_over -= 0.15; l2_over -= 0.15
    elif venue in pitcher_parks: l1_over += 0.15; l2_over += 0.15

    return f"""<div style="background-color: rgba(255, 255, 255, 0.05); padding: 15px; border-radius: 8px; margin-top: 15px; border: 1px solid rgba(255,255,255,0.1);">
<h4 style="margin-top:0; color:#4fc3f7;">💡 실시간 라이브 토탈 (현재 합산 {total_runs}점)</h4>
<p style="color:#b0bec5; font-size:0.9em; margin-bottom:10px;">{weather_desc}</p>
<p style="margin-bottom: 5px; font-size:1.1em;">▶ <b>[{total_runs + 1.5} 기준점]</b></p>
<ul style="margin-bottom: 15px;">
    <li><b>언더:</b> <code style="color:#00d26a;">{l1_under:.2f} 이상</code> 추천</li>
    <li><b>오버:</b> <code style="color:#ff4b4b;">{l1_over:.2f} 이상</code> 추천</li>
</ul>
<p style="margin-bottom: 5px; font-size:1.1em;">▶ <b>[{total_runs + 2.5} 기준점]</b></p>
<ul style="margin-bottom: 0;">
    <li><b>언더:</b> <code style="color:#00d26a;">{l2_under:.2f} 이상</code> 추천</li>
    <li><b>오버:</b> <code style="color:#ff4b4b;">{l2_over:.2f} 이상</code> 추천</li>
</ul></div>"""

# ==========================================
# 4. 실시간 메인 루프 
# ==========================================
while True:
    try:
        res = requests.get(f"https://statsapi.mlb.com/api/v1.1/game/{game_pk}/feed/live", timeout=3).json()
        live_data, game_data = res.get("liveData", {}), res.get("gameData", {})
        linescore, boxscore = live_data.get("linescore", {}), live_data.get("boxscore", {})
    except Exception:
        time.sleep(poll_interval)
        continue

    status_state = game_data.get("status", {}).get("abstractGameState", "")
    detailed_state = game_data.get("status", {}).get("detailedState", "")
    weather = game_data.get("weather", {})
    venue_name = game_data.get("venue", {}).get("name", "Unknown Venue")
    temp = weather.get("temp", "N/A")
    wind = weather.get("wind", "N/A")
    
    away_id = game_data.get("teams", {}).get("away", {}).get("id")
    home_id = game_data.get("teams", {}).get("home", {}).get("id")
    away_name = game_data.get("teams", {}).get("away", {}).get("teamName", "Away")
    home_name = game_data.get("teams", {}).get("home", {}).get("teamName", "Home")
    away_score = linescore.get("teams", {}).get("away", {}).get("runs", 0)
    home_score = linescore.get("teams", {}).get("home", {}).get("runs", 0)

    if away_id and f"{away_id}_fatigue" not in st.session_state.bullpen_cache:
        st.session_state.bullpen_cache[f"{away_id}_fatigue"] = check_bullpen_fatigue(away_id)
        st.session_state.bullpen_cache[f"{home_id}_fatigue"] = check_bullpen_fatigue(home_id)
    away_fatigued = st.session_state.bullpen_cache.get(f"{away_id}_fatigue", [])
    home_fatigued = st.session_state.bullpen_cache.get(f"{home_id}_fatigue", [])

    with dashboard_placeholder.container():
        
        # --- 공통 상단 환경 패널 ---
        st.markdown(f"<div style='text-align:center; color:#b0bec5; font-size:1.1rem; padding:10px;'>🏟️ <b>구장:</b> {venue_name} &nbsp;|&nbsp; 🌡️ <b>날씨:</b> {temp}℉ &nbsp;|&nbsp; 🌬️ <b>바람:</b> {wind}</div>", unsafe_allow_html=True)
        
        # ----------------------------------------------------
        # 브랜치 A: 시작 전 경기 (Scheduled / Preview)
        # ----------------------------------------------------
        if status_state in ["Preview", "Scheduled", "Pre-Game"]:
            st.markdown(f"<h1 style='text-align: center; font-size: 3rem;'>🏟️ {away_name} vs {home_name}</h1>", unsafe_allow_html=True)
            st.markdown(f"<h3 style='text-align: center; color: #ffeb3b;'>⏳ 경기 시작 전 ({detailed_state})</h3>", unsafe_allow_html=True)
            st.divider()

            if "away_lineup" not in st.session_state.lineup_cache:
                cur_away_lineup = get_starting_lineup(boxscore, "away")
                cur_home_lineup = get_starting_lineup(boxscore, "home")
                prev_away_lineup = get_previous_lineup(away_id)
                prev_home_lineup = get_previous_lineup(home_id)
                away_diff = [p for p in prev_away_lineup if p not in cur_away_lineup] if cur_away_lineup else []
                home_diff = [p for p in prev_home_lineup if p not in cur_home_lineup] if cur_home_lineup else []
                
                away_sp_info, home_sp_info = game_info.get("away_sp", {}), game_info.get("home_sp", {})
                if not away_sp_info.get("id"): away_sp_info = {"id": game_data.get("probablePitchers", {}).get("away", {}).get("id"), "fullName": game_data.get("probablePitchers", {}).get("away", {}).get("fullName", "미정")}
                if not home_sp_info.get("id"): home_sp_info = {"id": game_data.get("probablePitchers", {}).get("home", {}).get("id"), "fullName": game_data.get("probablePitchers", {}).get("home", {}).get("fullName", "미정")}

                aw_p_data, hw_p_data = get_pitcher_preview(away_sp_info.get("id"), home_id), get_pitcher_preview(home_sp_info.get("id"), away_id)
                aw_prob, hw_prob, aw_fair, hw_fair, aw_market, hw_market = calculate_win_probability(
                    away_diff, home_diff, away_fatigued, home_fatigued, 
                    aw_p_data["era_val"] if aw_p_data else None, hw_p_data["era_val"] if hw_p_data else None
                )
                
                st.session_state.lineup_cache.update({
                    "away_lineup": cur_away_lineup, "home_lineup": cur_home_lineup,
                    "away_diff": away_diff, "home_diff": home_diff,
                    "aw_prob": aw_prob, "hw_prob": hw_prob, "aw_fair": aw_fair, "hw_fair": hw_fair, "aw_market": aw_market, "hw_market": hw_market,
                    "aw_p_name": away_sp_info.get("fullName", "미정"), "hw_p_name": home_sp_info.get("fullName", "미정"),
                    "aw_p_data": aw_p_data, "hw_p_data": hw_p_data
                })
            
            lc = st.session_state.lineup_cache
            st.markdown("### 🔮 프리게임(Pre-game) 승패 예측 & 선발/라인업 변동 분석")
            pred_col1, pred_col2 = st.columns(2)
            with pred_col1:
                st.info(f"#### 🚀 {away_name} (원정)")
                st.markdown(f"**AI 승률 예측:** <span style='font-size:1.3rem; color:#00d26a;'>{lc['aw_prob']:.1f}%</span>", unsafe_allow_html=True)
                st.markdown(f"**적정 진입 배당 (Fair):** <span style='font-size:1.5rem; font-weight:bold; color:#ffeb3b;'>{lc['aw_fair']:.2f} 이상</span>", unsafe_allow_html=True)
                st.markdown("**선발 타순:** " + (", ".join(lc["away_lineup"][:6]) + " 등" if lc["away_lineup"] else "⚠️ 미발표"))
                st.markdown(f"🚨 **직전 경기 대비 타자 결장자:** <span style='color:#ff4b4b;'>{', '.join(lc['away_diff']) if lc['away_diff'] else '없음 (풀주전)'}</span>", unsafe_allow_html=True)
                st.markdown(f"🛑 **3연투 이탈 불펜:** <span style='color:#ff4b4b;'>{', '.join(away_fatigued) if away_fatigued else '없음'}</span>", unsafe_allow_html=True)
                st.markdown("<hr style='margin:12px 0;'>", unsafe_allow_html=True)
                st.markdown(f"⚾ **선발 투수:** <span style='color:#00e5ff; font-size:1.2rem; font-weight:bold;'>{lc['aw_p_name']}</span>", unsafe_allow_html=True)
                if lc["aw_p_data"]:
                    st.markdown(f"**📌 올시즌:** `{lc['aw_p_data']['season']}` | **⚔️ 상대전적:** `{lc['aw_p_data']['vs_team']}`")
            with pred_col2:
                st.warning(f"#### 🏠 {home_name} (홈)")
                st.markdown(f"**AI 승률 예측:** <span style='font-size:1.3rem; color:#00d26a;'>{lc['hw_prob']:.1f}%</span>", unsafe_allow_html=True)
                st.markdown(f"**적정 진입 배당 (Fair):** <span style='font-size:1.5rem; font-weight:bold; color:#ffeb3b;'>{lc['hw_fair']:.2f} 이상</span>", unsafe_allow_html=True)
                st.markdown("**선발 타순:** " + (", ".join(lc["home_lineup"][:6]) + " 등" if lc["home_lineup"] else "⚠️ 미발표"))
                st.markdown(f"🚨 **직전 경기 대비 타자 결장자:** <span style='color:#ff4b4b;'>{', '.join(lc['home_diff']) if lc['home_diff'] else '없음 (풀주전)'}</span>", unsafe_allow_html=True)
                st.markdown(f"🛑 **3연투 이탈 불펜:** <span style='color:#ff4b4b;'>{', '.join(home_fatigued) if home_fatigued else '없음'}</span>", unsafe_allow_html=True)
                st.markdown("<hr style='margin:12px 0;'>", unsafe_allow_html=True)
                st.markdown(f"⚾ **선발 투수:** <span style='color:#00e5ff; font-size:1.2rem; font-weight:bold;'>{lc['hw_p_name']}</span>", unsafe_allow_html=True)
                if lc["hw_p_data"]:
                    st.markdown(f"**📌 올시즌:** `{lc['hw_p_data']['season']}` | **⚔️ 상대전적:** `{lc['hw_p_data']['vs_team']}`")

        # ----------------------------------------------------
        # 브랜치 B: 진행 중인 경기 (Live)
        # ----------------------------------------------------
        elif status_state in ["Live", "In Progress"]:
            inning_str = linescore.get("currentInningOrdinal", "-")
            is_top = linescore.get("isTopInning")
            half_str = "초 (Top)" if is_top else "말 (Bot)"
            current_inning = linescore.get("currentInning", 1)
            outs, balls, strikes = linescore.get("outs", 0), linescore.get("balls", 0), linescore.get("strikes", 0)
            offense = linescore.get("offense", {})
            runners = extract_runners(offense, boxscore, is_top)
            
            cur_match = live_data.get("plays", {}).get("currentPlay", {}).get("matchup", {})
            cur_pitcher = cur_match.get("pitcher", {}).get("fullName", "Unknown")
            cur_batter = offense.get("batter", {}).get("fullName", "Unknown")
            p_id = cur_match.get("pitcher", {}).get("id")
            def_key = "home" if is_top else "away"
            pitches = boxscore.get("teams", {}).get(def_key, {}).get("players", {}).get(f"ID{p_id}", {}).get("stats", {}).get("pitching", {}).get("numberOfPitches", 0)

            # [NEW 2 적용] 구속 트래커 실행
            base_vel, vel_drop = get_pitcher_velocity_trend(live_data.get("plays", {}).get("allPlays", []), p_id)
            vel_alert_html = ""
            if base_vel and vel_drop >= 1.5:
                vel_alert_html = f"<br><span style='color:#ff4b4b; font-weight:bold; font-size:1.1rem;'>🚨 구속 저하 감지! (직구 평균 {base_vel:.1f}mph ➔ 최근 {base_vel-vel_drop:.1f}mph) - 장타 허용 위험 증가</span>"

            if is_top and current_inning != st.session_state.prev_inning:
                st.session_state.prev_inning = current_inning
                home_pid = boxscore.get("teams", {}).get("home", {}).get("pitchers", [-1])[-1]
                away_pid = boxscore.get("teams", {}).get("away", {}).get("pitchers", [-1])[-1]
                h_hand = boxscore.get("teams", {}).get("home", {}).get("players", {}).get(f"ID{home_pid}", {}).get("pitchHand", {}).get("code", "R")
                a_hand = boxscore.get("teams", {}).get("away", {}).get("players", {}).get(f"ID{away_pid}", {}).get("pitchHand", {}).get("code", "R")

                s_away, det_away = evaluate_team_matchup(get_batters(boxscore, "away", offense, is_top), home_pid, h_hand)
                s_home, det_home = evaluate_team_matchup(get_batters(boxscore, "home", offense, not is_top), away_pid, a_hand)
                tot_score = s_away + s_home

                if tot_score >= 4.5: sig, odds = "🔥 강력 오버", "2.05 이상"
                elif tot_score <= -4.5: sig, odds = "🛡️ 강력 언더", "1.85 이상"
                else: sig, odds = "🔍 관망 (상성 팽팽함)", "진입 패스"

                st.session_state.last_briefing = {
                    "title": f"{inning_str} 풀이닝 5타자 상성 연쇄",
                    "signal": sig, "odds": odds, "score": tot_score,
                    "det_away": det_away, "det_home": det_home,
                }

            st.markdown(f"<h1 style='text-align: center; font-size: 3rem;'>🏟️ {away_name} <span style='color:#00e5ff;'>{away_score} : {home_score}</span> {home_name}</h1>", unsafe_allow_html=True)
            st.markdown(f"<h3 style='text-align: center; color: #ffeb3b;'>{inning_str} {half_str} &nbsp;|&nbsp; {outs} OUT &nbsp;|&nbsp; B-S: {balls}-{strikes} &nbsp;|&nbsp; 베이스: {runners['icon']}</h3>", unsafe_allow_html=True)
            st.markdown(f"<h5 style='text-align: center; color: #b0bec5; margin-bottom: 25px;'>🏃 <b>현재 주자:</b> {runners['text']} <br><br>⚾ <b>투수:</b> {cur_pitcher} ({pitches}구){vel_alert_html} <br><br>🏏 <b>타자:</b> {cur_batter}</h5>", unsafe_allow_html=True)
            st.divider()

            col_full_game, col_inning = st.columns(2)
            with col_full_game:
                st.success("### 📊 라이브 이닝 토탈 & 불펜 분석")
                tf = len(away_fatigued) + len(home_fatigued)
                fg_sig = "🔥 후반 이닝 오버 유리" if tf >= 3 else ("🛡️ 후반 이닝 언더 유리" if tf == 0 and current_inning >= 6 else "⚖️ 불펜 중립")
                st.markdown(f"""<div style="font-size: 1.3rem; font-weight: bold; color: #ff4b4b; padding: 10px; border-left: 5px solid #ff4b4b; background-color: rgba(255, 75, 75, 0.1); margin-bottom: 15px;">🏁 전체 흐름 판정: {fg_sig}</div>""", unsafe_allow_html=True)
                
                # [NEW 3 적용] 날씨/구장 팩터를 반영한 토탈 계산기
                st.markdown(get_live_total_recommendation(current_inning, away_score + home_score, tf, temp, wind, venue_name), unsafe_allow_html=True)
                
                st.markdown("<br>#### 🚨 오늘 등판 불가 유력 불펜 (3연투 경고)", unsafe_allow_html=True)
                st.info(f"**{away_name} 불펜:** {', '.join(away_fatigued) if away_fatigued else '없음'}")
                st.warning(f"**{home_name} 불펜:** {', '.join(home_fatigued) if home_fatigued else '없음'}")

            with col_inning:
                st.info("### 🎯 1이닝(초+말) 향후 5타자 상성 연결")
                if st.session_state.last_briefing:
                    b = st.session_state.last_briefing
                    st.markdown(f"""<div style="font-size: 1.3rem; font-weight: bold; color: #00d26a; padding: 10px; border-left: 5px solid #00d26a; background-color: rgba(0, 210, 106, 0.1); margin-bottom: 15px;">🎯 추천 포지션: {b['signal']}</div>""", unsafe_allow_html=True)
                    st.markdown(f"**적용 이닝:** `{b['title']}`  |  **권장 배당:** `{b['odds']}`")
                    with st.expander("🔍 세부 5타자 매치업 전적 (BvP & 최근 7일 스탯)", expanded=False):
                        st.markdown(f"**[초 공격] 원정 타선(5명) vs 홈 투수**")
                        for d in b["det_away"]: st.markdown(d)
                        st.markdown(f"<br>**[말 공격] 홈 타선(5명) vs 원정 투수**", unsafe_allow_html=True)
                        for d in b["det_home"]: st.markdown(d)
                else:
                    st.write("다음 이닝(N회초) 시작 시 갱신됩니다.")

        elif status_state == "Final":
            st.markdown(f"<h1 style='text-align: center; font-size: 3rem;'>🏟️ {away_name} {away_score} : {home_score} {home_name}</h1>", unsafe_allow_html=True)
            st.info("종료된 경기입니다.")

    time.sleep(poll_interval)