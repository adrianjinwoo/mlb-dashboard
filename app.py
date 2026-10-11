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
    
    url = f"https://statsapi.mlb.com/api/v1/schedule?sportId=1&startDate={d_start}&endDate={d_end}"
    game_options = {}
    try:
        res = requests.get(url, timeout=3).json()
        for d in res.get("dates", []):
            date_str = d.get("date", "")[5:]
            for g in d.get("games", []):
                pk = str(g["gamePk"])
                away = g["teams"]["away"]["team"]["name"]
                home = g["teams"]["home"]["team"]["name"]
                status = g.get("status", {}).get("detailedState", "Unknown")
                label = f"[{date_str} | {status}] {away} vs {home}"
                game_options[label] = pk
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
    game_pk = upcoming_games[selected_label]
    st.sidebar.caption(f"연결된 Game PK: `{game_pk}`")
else:
    game_pk = st.sidebar.text_input("MLB Game PK (수동 입력)", value="746850")

poll_interval = st.sidebar.slider("새로고침 주기 (초)", 2, 6, 3)

if st.session_state.current_game_pk != game_pk:
    st.session_state.current_game_pk = game_pk
    st.session_state.last_briefing = None
    st.session_state.prev_inning = None
    st.session_state.lineup_cache = {}

dashboard_placeholder = st.empty()


# ==========================================
# 3. 데이터 파싱 및 예측 함수 
# ==========================================
# [NEW] 선발 투수 스탯 조회 함수 (시즌, 상대 전적, 최근 5경기)
@st.cache_data(ttl=3600)
def get_pitcher_preview(pitcher_id, opp_team_id):
    if not pitcher_id: return None
    url = f"https://statsapi.mlb.com/api/v1/people/{pitcher_id}/stats?stats=season,gameLog,vsTeam&opposingTeamId={opp_team_id}&group=pitching"
    try:
        res = requests.get(url, timeout=5).json()
        season_str, vs_team_str = "기록 없음", "맞대결 기록 없음"
        recent_list = []
        era_val = None
        
        for st_group in res.get("stats", []):
            s_type = st_group.get("type", {}).get("displayName")
            splits = st_group.get("splits", [])
            if not splits: continue
            
            if s_type == "season":
                stat = splits[0].get("stat", {})
                era = stat.get("era", "-")
                w, l = stat.get("wins", 0), stat.get("losses", 0)
                season_str = f"ERA {era} ({w}승 {l}패)"
                if era != "-" and era != "*.**": era_val = era
            
            elif s_type == "gameLog":
                # 최신 5경기 추출
                for g in reversed(splits[-5:]):
                    dt = g.get("date", "")[5:]
                    opp = g.get("opponent", {}).get("name", "U").split(" ")[-1]
                    ip = g.get("stat", {}).get("inningsPitched", "0")
                    er = g.get("stat", {}).get("earnedRuns", "0")
                    recent_list.append(f"{dt} vs {opp}: {ip}이닝 {er}실점")
                    
            elif s_type == "vsTeam":
                stat = splits[0].get("stat", {})
                era = stat.get("era", "-")
                ip = stat.get("inningsPitched", "0")
                er = stat.get("earnedRuns", "0")
                vs_team_str = f"ERA {era} ({ip}이닝 {er}실점)"
                
        return {
            "season": season_str,
            "recent": recent_list,
            "vs_team": vs_team_str,
            "era_val": era_val
        }
    except Exception:
        return None

@st.cache_data(ttl=3600)
def check_bullpen_fatigue(team_id):
    today = datetime.now()
    d1 = (today - timedelta(days=2)).strftime("%Y-%m-%d")
    d2 = (today - timedelta(days=1)).strftime("%Y-%m-%d")
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
        if bo and len(bo) == 3 and bo.endswith('00'):
            starters.append((int(bo), p.get('person', {}).get('fullName')))
    return [name for _, name in sorted(starters)]

@st.cache_data(ttl=3600)
def get_previous_lineup(team_id):
    today = datetime.now()
    d1 = (today - timedelta(days=5)).strftime("%Y-%m-%d")
    d2 = (today - timedelta(days=1)).strftime("%Y-%m-%d")
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

# ★ 배당 계산 로직 업데이트 (선발 투수 ERA 반영)
def calculate_win_probability(away_diff, home_diff, away_fatigue, home_fatigue, aw_era, hw_era):
    home_win_prob = 53.5
    # 결장자 감점
    home_win_prob -= (len(home_diff) * 2.5)
    home_win_prob += (len(away_diff) * 2.5)
    # 불펜 이탈 감점
    home_win_prob -= (len(home_fatigue) * 1.5)
    home_win_prob += (len(away_fatigue) * 1.5)
    
    # 선발 투수 ERA 보정 (ERA 1.0 차이당 승률 약 4% 가중치 반영)
    if aw_era and hw_era:
        try:
            era_diff = float(aw_era) - float(hw_era)
            home_win_prob += (era_diff * 4.0) # 원정 방어율이 낮아 음수가 되면 홈 승률 하락
        except ValueError:
            pass
    
    home_win_prob = max(30.0, min(70.0, home_win_prob))
    away_win_prob = 100.0 - home_win_prob
    
    home_fair_odds = 1 / (home_win_prob / 100)
    away_fair_odds = 1 / (away_win_prob / 100)
    bookmaker_margin = 1.045
    home_market_odds = 1 / ((home_win_prob / 100) * bookmaker_margin)
    away_market_odds = 1 / ((away_win_prob / 100) * bookmaker_margin)

    return away_win_prob, home_win_prob, away_fair_odds, home_fair_odds, away_market_odds, home_market_odds

def fetch_bvp_and_split(batter_id, pitcher_id, pitcher_hand):
    cache_key = f"{batter_id}_{pitcher_id}_{pitcher_hand}"
    if cache_key in st.session_state.stats_cache: return st.session_state.stats_cache[cache_key]
    hand_key = "vs Left" if pitcher_hand == "L" else "vs Right"
    res_data = {"bvp_text": "전적 없음", "bvp_ops": None, "split_ops": 0.700}
    try:
        b_url = f"https://statsapi.mlb.com/api/v1/people/{batter_id}/stats?stats=vsPlayer&opposingPlayerId={pitcher_id}&group=hitting"
        b_res = requests.get(b_url, timeout=3).json()
        b_splits = b_res.get("stats", [{}])[0].get("splits", [])
        if b_splits:
            stat = b_splits[0].get("stat", {})
            ops = float(stat.get("ops", ".000"))
            res_data["bvp_text"] = f"{stat.get('atBats', 0)}수 {stat.get('hits', 0)}안 {stat.get('homeRuns', 0)}홈 (OPS {ops:.3f})"
            res_data["bvp_ops"] = ops
        s_url = f"https://statsapi.mlb.com/api/v1/people/{batter_id}/stats?stats=statSplits&group=hitting&sitCodes=vl,vr"
        s_res = requests.get(s_url, timeout=3).json()
        s_splits = s_res.get("stats", [{}])[0].get("splits", [])
        for s in s_splits:
            if s.get("split", {}).get("description") == hand_key:
                res_data["split_ops"] = float(s.get("stat", {}).get("ops", 0.700))
                break
    except: pass
    st.session_state.stats_cache[cache_key] = res_data
    return res_data

def evaluate_team_matchup(batters, pitcher_id, pitcher_hand):
    if not pitcher_id or not batters: return 0, []
    score = 0
    details = []
    for b in batters:
        d = fetch_bvp_and_split(b["id"], pitcher_id, pitcher_hand)
        sp_ops, bvp_ops = d["split_ops"], d["bvp_ops"]
        if sp_ops >= 0.800: score += 1
        elif sp_ops <= 0.650: score -= 1
        if bvp_ops is not None:
            if bvp_ops >= 0.900: score += 1.5
            elif bvp_ops <= 0.500: score -= 1.5
        details.append(f"**{b['order']}번 {b['name']}**: {pitcher_hand}투 OPS `{sp_ops:.3f}` | {d['bvp_text']}")
    return score, details

def get_batters(boxscore, team_key, offense_info, is_batting_now):
    players = boxscore.get("teams", {}).get(team_key, {}).get("players", {})
    batters = []
    if is_batting_now:
        for k in ["batter", "onDeck", "inHole"]:
            p = offense_info.get(k)
            if p:
                pid, name = p.get("id"), p.get("fullName", "Unknown")
                ord_str = players.get(f"ID{pid}", {}).get("battingOrder", "000")
                batters.append({"id": pid, "name": name, "order": int(ord_str[0]) if ord_str.isdigit() else 0})
    else:
        for pid in boxscore.get("teams", {}).get(team_key, {}).get("batters", [])[:3]:
            p_inf = players.get(f"ID{pid}", {})
            name = p_inf.get("person", {}).get("fullName", "Unknown")
            ord_str = p_inf.get("battingOrder", "000")
            batters.append({"id": pid, "name": name, "order": int(ord_str[0]) if ord_str.isdigit() else 0})
    return batters

def extract_runners(offense, boxscore, is_top):
    team_key = "away" if is_top else "home"
    players = boxscore.get("teams", {}).get(team_key, {}).get("players", {})
    def get_name(base_key):
        base_obj = offense.get(base_key)
        if not base_obj: return None
        if "fullName" in base_obj: return base_obj["fullName"]
        pid = base_obj.get("id")
        return players.get(f"ID{pid}", {}).get("person", {}).get("fullName", f"Runner({pid})")
    r1, r2, r3 = get_name("first"), get_name("second"), get_name("third")
    icon = f"[{'🔴' if r3 else '⚪'}{'🔴' if r2 else '⚪'}{'🔴' if r1 else '⚪'}]"
    names = []
    if r3: names.append(f"3루: {r3}")
    if r2: names.append(f"2루: {r2}")
    if r1: names.append(f"1루: {r1}")
    return {"icon": icon, "text": " | ".join(names) if names else "주자 없음"}

def get_live_total_recommendation(current_inning, total_runs, total_fatigued):
    if current_inning <= 4:
        return "<p style='color:gray;'>⏳ 5회 이전은 변동성이 커서 잔여 토탈 가이드를 제공하지 않습니다.</p>"

    line1 = total_runs + 1.5
    line2 = total_runs + 2.5

    l1_over, l1_under = 1.85, 2.05
    l2_over, l2_under = 2.40, 1.60

    if current_inning >= 8:
        l1_over, l1_under = 2.30, 1.50
        l2_over, l2_under = 3.50, 1.25

    if total_fatigued >= 3:
        l1_over -= 0.15; l1_under += 0.25; l2_over -= 0.30; l2_under += 0.15
    elif total_fatigued == 0 and current_inning >= 6:
        l1_over += 0.25; l1_under -= 0.15; l2_over += 0.35; l2_under -= 0.10

    pass_msg = "<span style='color:gray; font-size:0.85em;'>(리스크 높아 패스 권장)</span>"
    
    html = f"""<div style="background-color: rgba(255, 255, 255, 0.05); padding: 15px; border-radius: 8px; margin-top: 15px; border: 1px solid rgba(255,255,255,0.1);">
<h4 style="margin-top:0; color:#4fc3f7;">💡 실시간 라이브 토탈 (현재 합산 {total_runs}점)</h4>
<p style="margin-bottom: 5px; font-size:1.1em;">▶ <b>[{line1} 기준점]</b> <span style="font-size:0.9em; color:gray;">(잔여 1.5점 기준)</span></p>
<ul style="margin-bottom: 15px;">
    <li><b>언더 (잔여 0~1점):</b> <code style="color:#00d26a; font-size:1.1em;">{l1_under:.2f} 이상</code> 추천 {pass_msg if l1_under < 1.45 else ""}</li>
    <li><b>오버 (잔여 2점 이상):</b> <code style="color:#ff4b4b; font-size:1.1em;">{l1_over:.2f} 이상</code> 추천</li>
</ul>
<p style="margin-bottom: 5px; font-size:1.1em;">▶ <b>[{line2} 기준점]</b> <span style="font-size:0.9em; color:gray;">(잔여 2.5점 기준)</span></p>
<ul style="margin-bottom: 0;">
    <li><b>언더 (잔여 0~2점):</b> <code style="color:#00d26a; font-size:1.1em;">{l2_under:.2f} 이상</code> 추천 {pass_msg if l2_under < 1.45 else ""}</li>
    <li><b>오버 (잔여 3점 이상):</b> <code style="color:#ff4b4b; font-size:1.1em;">{l2_over:.2f} 이상</code> 추천</li>
</ul>
</div>"""
    return html


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
    
    away_id = game_data.get("teams", {}).get("away", {}).get("id")
    home_id = game_data.get("teams", {}).get("home", {}).get("id")
    away_name = game_data.get("teams", {}).get("away", {}).get("teamName", "Away")
    home_name = game_data.get("teams", {}).get("home", {}).get("teamName", "Home")
    away_score = linescore.get("teams", {}).get("away", {}).get("runs", 0)
    home_score = linescore.get("teams", {}).get("home", {}).get("runs", 0)

    if away_id and "away_fatigue" not in st.session_state.bullpen_cache:
        st.session_state.bullpen_cache["away_fatigue"] = check_bullpen_fatigue(away_id)
        st.session_state.bullpen_cache["home_fatigue"] = check_bullpen_fatigue(home_id)
    away_fatigued = st.session_state.bullpen_cache.get("away_fatigue", [])
    home_fatigued = st.session_state.bullpen_cache.get("home_fatigue", [])

    with dashboard_placeholder.container():
        
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
                
                # 선발 투수 조회
                away_p = game_data.get("probablePitchers", {}).get("away", {})
                home_p = game_data.get("probablePitchers", {}).get("home", {})
                aw_p_data = get_pitcher_preview(away_p.get("id"), home_id)
                hw_p_data = get_pitcher_preview(home_p.get("id"), away_id)
                
                aw_era = aw_p_data["era_val"] if aw_p_data else None
                hw_era = hw_p_data["era_val"] if hw_p_data else None
                
                aw_prob, hw_prob, aw_fair, hw_fair, aw_market, hw_market = calculate_win_probability(
                    away_diff, home_diff, away_fatigued, home_fatigued, aw_era, hw_era
                )
                
                st.session_state.lineup_cache.update({
                    "away_lineup": cur_away_lineup, "home_lineup": cur_home_lineup,
                    "away_diff": away_diff, "home_diff": home_diff,
                    "aw_prob": aw_prob, "hw_prob": hw_prob,
                    "aw_fair": aw_fair, "hw_fair": hw_fair,
                    "aw_market": aw_market, "hw_market": hw_market,
                    "aw_p_name": away_p.get("fullName", "미정"),
                    "hw_p_name": home_p.get("fullName", "미정"),
                    "aw_p_data": aw_p_data,
                    "hw_p_data": hw_p_data
                })
            
            lc = st.session_state.lineup_cache
            st.markdown("### 🔮 프리게임(Pre-game) 승패 예측 & 선발/라인업 변동 분석")
            
            pred_col1, pred_col2 = st.columns(2)
            with pred_col1:
                st.info(f"#### 🚀 {away_name} (원정)")
                st.markdown(f"**AI 승률 예측:** <span style='font-size:1.3rem; color:#00d26a;'>{lc['aw_prob']:.1f}%</span>", unsafe_allow_html=True)
                st.markdown(f"**실제 시장 배당 (예상):** <span style='font-size:1.1rem; color:#b0bec5;'>{lc['aw_market']:.2f}</span>", unsafe_allow_html=True)
                st.markdown(f"**적정 진입 배당 (Fair):** <span style='font-size:1.5rem; font-weight:bold; color:#ffeb3b;'>{lc['aw_fair']:.2f} 이상</span>", unsafe_allow_html=True)
                
                # ★ 타자 라인업 & 불펜 정보
                st.markdown("**선발 타순:** " + (", ".join(lc["away_lineup"][:6]) + " 등" if lc["away_lineup"] else "⚠️ 미발표"))
                st.markdown(f"🚨 **직전 경기 대비 타자 결장자:** <span style='color:#ff4b4b;'>{', '.join(lc['away_diff']) if lc['away_diff'] else '없음 (풀주전)'}</span>", unsafe_allow_html=True)
                st.markdown(f"🛑 **3연투 이탈 불펜:** <span style='color:#ff4b4b;'>{', '.join(away_fatigued) if away_fatigued else '없음 (전원 대기)'}</span>", unsafe_allow_html=True)

                # ★ 추가된 선발 투수 스탯 패널
                st.markdown("<hr style='margin:12px 0;'>", unsafe_allow_html=True)
                st.markdown(f"⚾ **선발 투수:** <span style='color:#00e5ff; font-size:1.2rem; font-weight:bold;'>{lc['aw_p_name']}</span>", unsafe_allow_html=True)
                if lc["aw_p_data"]:
                    st.markdown(f"**📌 올시즌 기록:** `{lc['aw_p_data']['season']}`")
                    st.markdown(f"**⚔️ 오늘 상대전적:** `{lc['aw_p_data']['vs_team']}`")
                    with st.expander("📉 최근 5경기 투구 내용", expanded=False):
                        for r in lc["aw_p_data"]["recent"]:
                            st.markdown(f"- {r}")

            with pred_col2:
                st.warning(f"#### 🏠 {home_name} (홈)")
                st.markdown(f"**AI 승률 예측:** <span style='font-size:1.3rem; color:#00d26a;'>{lc['hw_prob']:.1f}%</span>", unsafe_allow_html=True)
                st.markdown(f"**실제 시장 배당 (예상):** <span style='font-size:1.1rem; color:#b0bec5;'>{lc['hw_market']:.2f}</span>", unsafe_allow_html=True)
                st.markdown(f"**적정 진입 배당 (Fair):** <span style='font-size:1.5rem; font-weight:bold; color:#ffeb3b;'>{lc['hw_fair']:.2f} 이상</span>", unsafe_allow_html=True)
                
                # ★ 타자 라인업 & 불펜 정보
                st.markdown("**선발 타순:** " + (", ".join(lc["home_lineup"][:6]) + " 등" if lc["home_lineup"] else "⚠️ 미발표"))
                st.markdown(f"🚨 **직전 경기 대비 타자 결장자:** <span style='color:#ff4b4b;'>{', '.join(lc['home_diff']) if lc['home_diff'] else '없음 (풀주전)'}</span>", unsafe_allow_html=True)
                st.markdown(f"🛑 **3연투 이탈 불펜:** <span style='color:#ff4b4b;'>{', '.join(home_fatigued) if home_fatigued else '없음 (전원 대기)'}</span>", unsafe_allow_html=True)

                # ★ 추가된 선발 투수 스탯 패널
                st.markdown("<hr style='margin:12px 0;'>", unsafe_allow_html=True)
                st.markdown(f"⚾ **선발 투수:** <span style='color:#00e5ff; font-size:1.2rem; font-weight:bold;'>{lc['hw_p_name']}</span>", unsafe_allow_html=True)
                if lc["hw_p_data"]:
                    st.markdown(f"**📌 올시즌 기록:** `{lc['hw_p_data']['season']}`")
                    st.markdown(f"**⚔️ 오늘 상대전적:** `{lc['hw_p_data']['vs_team']}`")
                    with st.expander("📉 최근 5경기 투구 내용", expanded=False):
                        for r in lc["hw_p_data"]["recent"]:
                            st.markdown(f"- {r}")

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

            if is_top and current_inning != st.session_state.prev_inning:
                st.session_state.prev_inning = current_inning
                home_pid = boxscore.get("teams", {}).get("home", {}).get("pitchers", [-1])[-1]
                away_pid = boxscore.get("teams", {}).get("away", {}).get("pitchers", [-1])[-1]
                h_hand = boxscore.get("teams", {}).get("home", {}).get("players", {}).get(f"ID{home_pid}", {}).get("pitchHand", {}).get("code", "R")
                a_hand = boxscore.get("teams", {}).get("away", {}).get("players", {}).get(f"ID{away_pid}", {}).get("pitchHand", {}).get("code", "R")

                s_away, det_away = evaluate_team_matchup(get_batters(boxscore, "away", offense, True), home_pid, h_hand)
                s_home, det_home = evaluate_team_matchup(get_batters(boxscore, "home", offense, False), away_pid, a_hand)
                tot_score = s_away + s_home

                if tot_score >= 3.0: sig, odds = "🔥 강력 오버", "2.05 이상"
                elif tot_score <= -3.0: sig, odds = "🛡️ 강력 언더", "1.85 이상"
                else: sig, odds = "🔍 관망 (상성 팽팽함)", "진입 패스"

                st.session_state.last_briefing = {
                    "title": f"{inning_str} 풀이닝 단기 상성",
                    "signal": sig, "odds": odds, "score": tot_score,
                    "det_away": det_away, "det_home": det_home,
                }

            st.markdown(f"<h1 style='text-align: center; font-size: 3rem;'>🏟️ {away_name} <span style='color:#00e5ff;'>{away_score} : {home_score}</span> {home_name}</h1>", unsafe_allow_html=True)
            st.markdown(f"<h3 style='text-align: center; color: #ffeb3b;'>{inning_str} {half_str} &nbsp;|&nbsp; {outs} OUT &nbsp;|&nbsp; B-S: {balls}-{strikes} &nbsp;|&nbsp; 베이스: {runners['icon']}</h3>", unsafe_allow_html=True)
            st.markdown(f"<h5 style='text-align: center; color: #b0bec5; margin-bottom: 25px;'>🏃 <b>현재 주자:</b> {runners['text']} <br><br>⚾ <b>투수:</b> {cur_pitcher} ({pitches}구) &nbsp; vs &nbsp; 🏏 <b>타자:</b> {cur_batter}</h5>", unsafe_allow_html=True)
            st.divider()

            col_full_game, col_inning = st.columns(2)
            
            with col_full_game:
                st.success("### 📊 라이브 이닝 토탈 & 불펜 분석")
                tf = len(away_fatigued) + len(home_fatigued)
                if tf >= 3: fg_sig = "🔥 후반 이닝 오버 (OVER) 유리"
                elif tf == 0 and current_inning >= 6: fg_sig = "🛡️ 후반 이닝 언더 (UNDER) 유리"
                else: fg_sig = "⚖️ 불펜 중립 (이닝별 상성으로 접근)"
                
                st.markdown(f"""<div style="font-size: 1.3rem; font-weight: bold; color: #ff4b4b; padding: 10px; border-left: 5px solid #ff4b4b; background-color: rgba(255, 75, 75, 0.1); margin-bottom: 15px;">🏁 전체 흐름 판정: {fg_sig}</div>""", unsafe_allow_html=True)
                st.markdown(get_live_total_recommendation(current_inning, away_score + home_score, tf), unsafe_allow_html=True)
                
                st.markdown("<br>#### 🚨 오늘 등판 불가 유력 불펜 (3연투 경고)", unsafe_allow_html=True)
                st.info(f"**{away_name} (원정) 불펜:** {', '.join(away_fatigued) if away_fatigued else '없음 (전원 대기)'}")
                st.warning(f"**{home_name} (홈) 불펜:** {', '.join(home_fatigued) if home_fatigued else '없음 (전원 대기)'}")

            with col_inning:
                st.info(f"### 🎯 1이닝(초+말) 단기 타격 상성")
                if st.session_state.last_briefing:
                    b = st.session_state.last_briefing
                    st.markdown(f"""<div style="font-size: 1.3rem; font-weight: bold; color: #00d26a; padding: 10px; border-left: 5px solid #00d26a; background-color: rgba(0, 210, 106, 0.1); margin-bottom: 15px;">🎯 추천 포지션: {b['signal']}</div>""", unsafe_allow_html=True)
                    st.markdown(f"**적용 이닝:** `{b['title']}`  |  **권장 배당:** `{b['odds']}`")
                    with st.expander("🔍 세부 매치업 전적 (BvP & 스플릿) 보기", expanded=False):
                        st.markdown(f"**[초 공격] 원정 타선 vs 홈 투수**")
                        for d in b["det_away"]: st.markdown(d)
                        st.markdown(f"**[말 공격] 홈 타선 vs 원정 투수**")
                        for d in b["det_home"]: st.markdown(d)
                else:
                    st.write("다음 이닝(N회초) 시작 시 데이터가 분석되어 표시됩니다.")

        # ----------------------------------------------------
        # 브랜치 C: 종료된 경기 (Final)
        # ----------------------------------------------------
        elif status_state == "Final":
            st.markdown(f"<h1 style='text-align: center; font-size: 3rem;'>🏟️ {away_name} <span style='color:gray;'>{away_score} : {home_score}</span> {home_name}</h1>", unsafe_allow_html=True)
            st.markdown(f"<h3 style='text-align: center; color: #b0bec5;'>🏁 종료된 경기 (Final)</h3>", unsafe_allow_html=True)
            st.divider()
            st.info("해당 경기는 종료되었습니다. 사이드바에서 Live 또는 Scheduled 경기를 선택해 주세요.")

    time.sleep(poll_interval)