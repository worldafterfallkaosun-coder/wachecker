cd ~ && cat > checker9.py << 'ENDOFSCRIPT'
import asyncio
import aiohttp
import aiofiles
import os, time, random, sys, re, json
from datetime import datetime
from pathlib import Path

# ─── CONFIG ───────────────────────────────────────────────
NUMBERS_FILE  = "Peru_62950.txt"
ACTIVE_OUTPUT = "Peru_Active_Gacor.txt"
DEAD_OUTPUT   = "Peru_Dead.txt"
INFO_OUTPUT   = "Peru_FullInfo.txt"
CACHE_FILE    = "checked_cache.json"
RESULTS_FILE  = "Peru_Results_Session.json"
BATCH_SIZE    = 35
CONCURRENCY   = 10
TIMEOUT       = 14
TARGET_ACTIVE = 100

PERU_PREFIX  = "51"
VALID_LENGTH = 11

USER_AGENTS = [
    "Mozilla/5.0 (Linux; Android 13; SM-S918B) AppleWebKit/537.36 Chrome/120.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 12; Pixel 6) AppleWebKit/537.36 Chrome/114.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0) AppleWebKit/605.1.15 Version/17.0 Mobile Safari/604.1",
    "Mozilla/5.0 (Linux; Android 11; Redmi Note 10) AppleWebKit/537.36 Chrome/109.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 13; CPH2451) AppleWebKit/537.36 Chrome/118.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Linux; Android 12; M2101K6G) AppleWebKit/537.36 Chrome/107.0.0.0 Mobile Safari/537.36",
]

# ─── COLORS ───────────────────────────────────────────────
RS = "\033[0m"
G  = "\033[92m"
RD = "\033[91m"
Y  = "\033[93m"
C  = "\033[96m"
GR = "\033[90m"
W  = "\033[97m"
B  = "\033[94m"
M  = "\033[95m"
BL = "\033[1m"
DM = "\033[2m"

def cl(t,c): return f"{c}{t}{RS}"
def cls(): os.system("cls" if os.name=="nt" else "clear")

stop_event = asyncio.Event()
stats_lock = asyncio.Lock()
stats = {"active":0,"dead":0,"timeout":0,"skipped":0,"total":0}
results_log = []

# ─── CACHE ────────────────────────────────────────────────
def load_cache():
    if Path(CACHE_FILE).exists():
        try:
            with open(CACHE_FILE,"r",encoding="utf-8") as f: return json.load(f)
        except: return {}
    return {}

def save_cache(cache):
    with open(CACHE_FILE,"w",encoding="utf-8") as f:
        json.dump(cache,f,ensure_ascii=False,indent=2)

def load_results():
    if Path(RESULTS_FILE).exists():
        try:
            with open(RESULTS_FILE,"r",encoding="utf-8") as f: return json.load(f)
        except: return []
    return []

def save_results(data):
    with open(RESULTS_FILE,"w",encoding="utf-8") as f:
        json.dump(data,f,ensure_ascii=False,indent=2)

async def wfile(path, content):
    async with aiofiles.open(path,"a",encoding="utf-8") as f:
        await f.write(content+"\n")

# ─── VALIDATOR ────────────────────────────────────────────
def validate_peru(raw):
    n = raw.strip().replace("+","").replace(" ","").replace("-","").replace("(","").replace(")","")
    if not n.isdigit(): return None
    if len(n)==9 and n.startswith("9"): n=PERU_PREFIX+n
    if not n.startswith(PERU_PREFIX): return None
    if len(n)!=VALID_LENGTH: return None
    if not n[2:].startswith("9"): return None
    if len(set(n))<=2: return None
    return n

# ─── CARRIER ──────────────────────────────────────────────
def get_carrier(phone):
    p={
        "51990":"Claro","51991":"Claro","51992":"Claro","51993":"Claro",
        "51994":"Claro","51995":"Claro","51996":"Claro","51997":"Claro",
        "51998":"Claro","51999":"Claro",
        "51900":"Movistar","51901":"Movistar","51902":"Movistar",
        "51910":"Movistar","51911":"Movistar","51912":"Movistar",
        "51913":"Movistar","51914":"Movistar","51915":"Movistar",
        "51920":"Entel","51921":"Entel","51922":"Entel","51923":"Entel",
        "51924":"Entel","51925":"Entel",
        "51930":"Bitel","51931":"Bitel","51932":"Bitel","51933":"Bitel",
        "51934":"Bitel","51935":"Bitel",
    }
    for k,v in p.items():
        if phone.startswith(k): return v
    return "Unknown"

# ─── SCORE SYSTEM ─────────────────────────────────────────
# Score 0-100 → determines RECOMMENDED / MEDIUM / LOW tier
def calc_score(phone, ms, http_status, votes_active, age_raw, carrier):
    score = 0

    # Response time (lower = better)
    if ms < 400:   score += 30
    elif ms < 700: score += 22
    elif ms < 1200:score += 14
    elif ms < 2000:score += 7
    else:          score += 0

    # Vote confidence
    score += votes_active * 18  # max 54 (3 votes * 18)

    # HTTP status
    if http_status == 200: score += 10
    elif http_status == 302: score += 6

    # Carrier trust (Claro/Movistar = more popular = higher hit rate)
    carrier_pts = {"Claro":6,"Movistar":5,"Entel":3,"Bitel":2,"Unknown":0}
    score += carrier_pts.get(carrier, 0)

    # Age heuristic
    age_pts = {"5+ yr":8,"3-5 yr":7,"1-3 yr":5,"6-12 mo":3,"<6 mo":1,"Unknown":0}
    score += age_pts.get(age_raw, 0)

    return min(score, 100)

def tier_label(score):
    if score >= 75:  return "RECOMMENDED", G,    "🟢"
    elif score >= 50: return "MEDIUM",      Y,    "🟡"
    else:             return "LOW",          RD,   "🔴"

# ─── AGE ──────────────────────────────────────────────────
def estimate_age(phone, html=""):
    for pat in [r'"created_at"\s*:\s*(\d{10})',r'"t"\s*:\s*(\d{10})']:
        m=re.search(pat,html)
        if m:
            try:
                ts=int(m.group(1)); reg=datetime.fromtimestamp(ts)
                d=(datetime.now()-reg).days
                y,mo=d//365,(d%365)//30
                lbl=f"{y}yr {mo}mo" if y>0 else f"{mo}mo"
                return f"~{lbl}","High"
            except: pass
    try:
        s4=int(phone[-4:]); s6=int(phone[-6:])
        if s6<30000:   return "5+ yr","Med"
        elif s4<1000:  return "3-5 yr","Med"
        elif s4<3500:  return "1-3 yr","Med"
        elif s4<6500:  return "6-12 mo","Low"
        else:          return "<6 mo","Low"
    except: return "Unknown","Low"

# ─── OTP ──────────────────────────────────────────────────
def get_otp(http_status, headers, ms):
    ra=headers.get("Retry-After","")
    xl=headers.get("X-RateLimit-Remaining","")
    if http_status==429: return "RATE LIMITED", 0
    if http_status==403: return "BLOCKED", 0
    if http_status==503: return "SVC DOWN", 0
    if ra:               return f"THROTTLED", 0
    if xl=="0":          return "LIMIT 0", 0
    if http_status==200:
        if ms<800:       return "CLEAN", 1
        elif ms<2500:    return "SLOW", 0
        else:            return "THROTTLED", 0
    return "UNKNOWN", 0

# ─── CORE CHECK ───────────────────────────────────────────
async def check_wa(session, phone):
    html=""; http_status=0; resp_hdrs={}; resp_ms=0
    votes_a=0; votes_d=0
    results=[]

    # Method A — api.whatsapp.com
    try:
        t0=time.time()
        async with session.get(
            f"https://api.whatsapp.com/send?phone={phone}&text=",
            headers={
                "User-Agent":random.choice(USER_AGENTS),
                "Accept":"text/html,*/*;q=0.8",
                "Accept-Language":"es-PE,es;q=0.9,en;q=0.7",
                "Cache-Control":"no-cache","Pragma":"no-cache",
            },
            timeout=aiohttp.ClientTimeout(total=TIMEOUT),
            allow_redirects=True, ssl=False
        ) as r:
            html=await r.text(errors="ignore")
            http_status=r.status; resp_hdrs=dict(r.headers)
            resp_ms=round((time.time()-t0)*1000)
            hl=html.lower()
            dead_signals=["phone_number_invalid","invalid phone number",
                          "this phone number is not registered","number does not exist"]
            active_signals=[f'"{phone}"',f"phone%3D{phone}","wa-param-number",
                            "whatsapp://send?phone=",'"phone":"']
            if any(s in hl for s in dead_signals) or r.status in [404,410]:
                results.append("DEAD"); votes_d+=1
            elif any(s in html for s in active_signals) and "invalid" not in hl:
                results.append("ACTIVE"); votes_a+=1
            elif phone[2:] in html and "invalid" not in hl:
                results.append("ACTIVE"); votes_a+=1
            else:
                results.append("UNKNOWN")
    except asyncio.TimeoutError: return "TIMEOUT","",0,{},0,0
    except: results.append("UNKNOWN")

    # Method B — wa.me HEAD
    try:
        async with session.head(
            f"https://wa.me/{phone}",
            headers={"User-Agent":"Mozilla/5.0 (compatible; Googlebot/2.1)"},
            timeout=aiohttp.ClientTimeout(total=7),
            allow_redirects=False, ssl=False
        ) as r:
            loc=r.headers.get("Location","")
            if "whatsapp.com/send" in loc or phone[2:] in loc:
                results.append("ACTIVE"); votes_a+=1
            elif r.status in [404,400,410]:
                results.append("DEAD"); votes_d+=1
            else: results.append("UNKNOWN")
            if not http_status: http_status=r.status; resp_hdrs=dict(r.headers)
    except: results.append("UNKNOWN")

    # Method C — web.whatsapp.com
    try:
        async with session.get(
            f"https://web.whatsapp.com/send?phone={phone}",
            headers={"User-Agent":random.choice(USER_AGENTS),"Accept-Language":"es-PE"},
            timeout=aiohttp.ClientTimeout(total=9),
            allow_redirects=True, ssl=False
        ) as r:
            body=await r.text(errors="ignore"); bl=body.lower()
            if "invalid" in bl or "not registered" in bl:
                results.append("DEAD"); votes_d+=1
            elif phone[2:] in body and "invalid" not in bl:
                results.append("ACTIVE"); votes_a+=1
            else: results.append("UNKNOWN")
    except: results.append("UNKNOWN")

    # Final
    if votes_a>=1:   final="ACTIVE"
    elif votes_d>=2: final="DEAD"
    elif votes_d==1 and votes_a==0: final="DEAD"
    else:             final="UNKNOWN"

    return final, html, http_status, resp_hdrs, resp_ms, votes_a

# ─── PROGRESS ─────────────────────────────────────────────
def draw_progress(cur, total, active, dead, timeout):
    pct=cur/total if total else 0
    fill=int(32*pct); bar=cl("█"*fill,G)+cl("░"*(32-fill),GR)
    af=min(int(20*active/TARGET_ACTIVE),20)
    abar=cl("█"*af,M)+cl("░"*(20-af),GR)
    apct=round(active/TARGET_ACTIVE*100,1)
    line=(f"\r  {bar} {cl(f'{round(pct*100,1)}%',W)} [{cur}/{total}]  "
          f"✅{cl(str(active),G)} ❌{cl(str(dead),RD)} "
          f"⏱{cl(str(timeout),GR)}  🎯{abar}{cl(f'{apct}%',M)}")
    print(line, end="", flush=True)

# ─── PROCESSOR ────────────────────────────────────────────
async def process(session, sem, phone, cache, cur_ref):
    async with sem:
        if stop_event.is_set(): return
        wa,html,hstat,hdrs,ms,va = await check_wa(session,phone)
        carrier=get_carrier(phone)
        age_raw,age_c=estimate_age(phone,html)
        otp_lbl,otp_ok=get_otp(hstat,hdrs,ms)
        score=calc_score(phone,ms,hstat,va,age_raw,carrier)
        tier,tier_color,tier_icon=tier_label(score)
        checked_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        async with stats_lock:
            if stop_event.is_set(): return
            stats["total"]+=1; cur_ref[0]+=1
            cache[phone]={"status":wa,"checked_at":checked_at,
                          "carrier":carrier,"score":score}
            if stats["total"]%10==0: save_cache(cache)

            if wa=="ACTIVE":
                stats["active"]+=1; n=stats["active"]
                entry={
                    "no":n,"phone":phone,"carrier":carrier,
                    "age":age_raw,"age_c":age_c,"otp":otp_lbl,
                    "ms":ms,"score":score,"tier":tier,
                    "tier_color":tier_color,"tier_icon":tier_icon,
                    "at":checked_at,"votes":va
                }
                results_log.append(entry)
                info=(f"{'─'*62}\n"
                      f"  📱 Number  : {phone}\n"
                      f"  ✅ Status  : ACTIVE\n"
                      f"  🎯 Score   : {score}/100 [{tier}]\n"
                      f"  📡 Carrier : {carrier} — Peru\n"
                      f"  📅 Age     : {age_raw} [{age_c}]\n"
                      f"  📨 OTP     : {otp_lbl}\n"
                      f"  🗳  Votes   : {va}/3 methods confirmed\n"
                      f"  🌐 HTTP    : {hstat} | {ms}ms\n"
                      f"  ⏰ At      : {checked_at}\n")
                await wfile(ACTIVE_OUTPUT,phone)
                await wfile(INFO_OUTPUT,info)
                if n>=TARGET_ACTIVE: stop_event.set()
            elif wa=="DEAD":
                stats["dead"]+=1; await wfile(DEAD_OUTPUT,phone)
            elif wa=="TIMEOUT":
                stats["timeout"]+=1
            else:
                stats["dead"]+=1; await wfile(DEAD_OUTPUT,phone)

# ─── DISPLAY HELPERS ──────────────────────────────────────
def header_box(title, color=C):
    w=60
    print(cl("╔"+"═"*(w-2)+"╗",color))
    print(cl("║"+f"{title:^{w-2}}"+"║",color))
    print(cl("╚"+"═"*(w-2)+"╝",color))

def divider(c=GR): print(cl("─"*60,c))

def fmt_num(phone):
    return f"+{phone[:2]} {phone[2:5]} {phone[5:8]} {phone[8:]}"

# ─── RESULT VIEWS ─────────────────────────────────────────
def show_tier(tier_filter=None, min_score=0, max_score=100, label="ALL RESULTS"):
    cls()
    header_box(f"📊 {label}", C)
    print()
    data=results_log if not tier_filter else [r for r in results_log if r["tier"]==tier_filter]
    data=[r for r in data if min_score<=r["score"]<=max_score]
    if not data:
        print(cl(f"  ⚠  No results in this tier.","Y"))
        input(cl("\n  [ENTER] Back to menu","GR"))
        return

    # Stats for this tier
    carriers={}
    for r in data:
        carriers[r["carrier"]]=carriers.get(r["carrier"],0)+1
    avg_score=round(sum(r["score"] for r in data)/len(data),1)
    avg_ms=round(sum(r["ms"] for r in data)/len(data))

    print(cl(f"  Total   : {len(data)} numbers",W))
    print(cl(f"  Avg Score: {avg_score}/100  Avg Response: {avg_ms}ms",W))
    carr_str=" | ".join(f"{k}:{v}" for k,v in carriers.items())
    print(cl(f"  Carriers: {carr_str}",GR))
    print()
    divider()
    print(cl(f"  {'#':<5}{'NUMBER':<16}{'CARRIER':<10}{'AGE':<9}{'SCORE':<8}{'OTP':<14}{'MS'}",GR))
    divider()
    for r in data:
        sc=r["score"]
        sc_color=G if sc>=75 else Y if sc>=50 else RD
        otp_c=G if r["otp"]=="CLEAN" else Y
        line=(f"  {cl(f'#{r[\"no\"]}',r['tier_color']):<14}"
              f"{cl(fmt_num(r['phone']),W):<24}"
              f"{cl(r['carrier'],B):<18}"
              f"{cl(r['age'],Y):<17}"
              f"{cl(str(sc)+'/100',sc_color):<16}"
              f"{cl(r['otp'],otp_c):<20}"
              f"{cl(str(r['ms'])+'ms',GR)}")
        print(line)
    divider()
    print()
    print(cl(f"  📁 Full details → {INFO_OUTPUT}",C))
    print(cl(f"  📁 Numbers only → {ACTIVE_OUTPUT}",C))
    print()
    input(cl("  [ENTER] Back to menu","GR"))

def show_recommended():
    # Only RECOMMENDED tier from results
    cls()
    header_box("🟢 RECOMMENDED — Score 75-100", G)
    print()
    rec=[r for r in results_log if r["tier"]=="RECOMMENDED"]
    if not rec:
        print(cl("  No RECOMMENDED numbers found yet. Run a scan first.","Y"))
        input(cl("\n  [ENTER] Back","GR"))
        return
    print(cl(f"  {len(rec)} numbers scored 75+ out of 100",G))
    print(cl("  These are highest confidence — clean response, multi-vote confirmed",GR))
    print()
    divider(G)
    print(cl(f"  {'#':<5}{'NUMBER':<18}{'CARRIER':<11}{'SCORE':<8}{'AGE':<9}{'VOTES':<8}{'MS'}",GR))
    divider(G)
    for r in rec:
        print(f"  {cl(f'#{r[\"no\"]}',G):<14}"
              f"{cl(fmt_num(r['phone']),W):<26}"
              f"{cl(r['carrier'],B):<19}"
              f"{cl(str(r['score'])+'/100',G):<16}"
              f"{cl(r['age'],Y):<17}"
              f"{cl(str(r['votes'])+'/3',C):<16}"
              f"{cl(str(r['ms'])+'ms',GR)}")
    divider(G)
    print()
    # Save recommended only
    rec_file="Peru_Recommended_Only.txt"
    with open(rec_file,"w",encoding="utf-8") as f:
        for r in rec: f.write(r["phone"]+"\n")
    print(cl(f"  💾 Saved recommended → {rec_file}",C))
    print()
    input(cl("  [ENTER] Back to menu","GR"))

def show_medium():
    cls()
    header_box("🟡 MEDIUM — Score 50-74", Y)
    print()
    med=[r for r in results_log if r["tier"]=="MEDIUM"]
    if not med:
        print(cl("  No MEDIUM tier numbers found.","Y"))
        input(cl("\n  [ENTER] Back","GR"))
        return
    print(cl(f"  {len(med)} numbers scored 50-74",Y))
    print(cl("  Good but not fully confirmed — usable with caution",GR))
    print()
    divider(Y)
    print(cl(f"  {'#':<5}{'NUMBER':<18}{'CARRIER':<11}{'SCORE':<8}{'AGE':<9}{'MS'}",GR))
    divider(Y)
    for r in med:
        print(f"  {cl(f'#{r[\"no\"]}',Y):<14}"
              f"{cl(fmt_num(r['phone']),W):<26}"
              f"{cl(r['carrier'],B):<19}"
              f"{cl(str(r['score'])+'/100',Y):<16}"
              f"{cl(r['age'],Y):<17}"
              f"{cl(str(r['ms'])+'ms',GR)}")
    divider(Y)
    print()
    input(cl("  [ENTER] Back to menu","GR"))

def show_low():
    cls()
    header_box("🔴 LOW — Score 0-49", RD)
    print()
    low=[r for r in results_log if r["tier"]=="LOW"]
    if not low:
        print(cl("  No LOW tier numbers.","GR"))
        input(cl("\n  [ENTER] Back","GR"))
        return
    print(cl(f"  {len(low)} numbers scored below 50",RD))
    print(cl("  Active on WA but low confidence — slow response or single-vote",GR))
    print()
    divider(RD)
    for r in low:
        print(f"  {cl(f'#{r[\"no\"]}',RD):<14}"
              f"{cl(fmt_num(r['phone']),W):<26}"
              f"{cl(r['carrier'],B):<19}"
              f"{cl(str(r['score'])+'/100',RD):<16}"
              f"{cl(str(r['ms'])+'ms',GR)}")
    divider(RD)
    print()
    input(cl("  [ENTER] Back to menu","GR"))

def show_summary():
    cls()
    header_box("📈 FULL SESSION SUMMARY", C)
    print()
    if not results_log:
        print(cl("  No data yet. Run a scan first.","Y"))
        input(cl("\n  [ENTER] Back","GR"))
        return
    rec =len([r for r in results_log if r["tier"]=="RECOMMENDED"])
    med =len([r for r in results_log if r["tier"]=="MEDIUM"])
    low =len([r for r in results_log if r["tier"]=="LOW"])
    total_active=len(results_log)
    avg_score=round(sum(r["score"] for r in results_log)/total_active,1) if total_active else 0

    print(cl("  ┌─ SCAN STATS ─────────────────────────────┐",W))
    print(cl(f"  │  Total Scanned   : {stats['total']:<24}│",W))
    print(cl(f"  │  Active Found    : {cl(str(total_active),G):<33}│",W))
    print(cl(f"  │  Dead/Unknown    : {cl(str(stats['dead']),RD):<33}│",W))
    print(cl(f"  │  Timeout         : {cl(str(stats['timeout']),GR):<33}│",W))
    print(cl(f"  │  Skipped(cache)  : {cl(str(stats['skipped']),Y):<33}│",W))
    hitrate=round(total_active/stats['total']*100,1) if stats['total'] else 0
    print(cl(f"  │  Hit Rate        : {cl(str(hitrate)+'%',M):<33}│",W))
    print(cl(f"  │  Avg Score       : {cl(str(avg_score)+'/100',C):<33}│",W))
    print(cl("  └──────────────────────────────────────────┘",W))
    print()
    print(cl("  ┌─ QUALITY BREAKDOWN ──────────────────────┐",W))
    rbar="█"*min(int(20*rec/total_active),20) if total_active else ""
    mbar="█"*min(int(20*med/total_active),20) if total_active else ""
    lbar="█"*min(int(20*low/total_active),20) if total_active else ""
    print(f"  │  🟢 RECOMMENDED  {cl(rbar,G):<30} {cl(str(rec),G)} │")
    print(f"  │  🟡 MEDIUM       {cl(mbar,Y):<30} {cl(str(med),Y)} │")
    print(f"  │  🔴 LOW          {cl(lbar,RD):<30} {cl(str(low),RD)} │")
    print(cl("  └──────────────────────────────────────────┘",W))
    print()
    input(cl("  [ENTER] Back to menu","GR"))

# ─── SCAN ENGINE ──────────────────────────────────────────
async def run_scan(rescan=False):
    global results_log
    results_log=[]
    stop_event.clear()
    for k in stats: stats[k]=0

    cache=load_cache()
    if rescan:
        print(cl("\n  ♻  RESCAN MODE — clearing cache for fresh check","Y"))
        cache={}

    if not os.path.exists(NUMBERS_FILE):
        print(cl(f"\n  [!] {NUMBERS_FILE} not found!","RD"))
        input(cl("  [ENTER] Back","GR")); return

    with open(NUMBERS_FILE,"r",encoding="utf-8",errors="ignore") as f:
        raw=[l.strip() for l in f if l.strip()]

    seen=set(); numbers=[]; skip_inv=0; skip_c=0
    for n in raw:
        cl_n=validate_peru(n)
        if not cl_n: skip_inv+=1; continue
        if cl_n in seen: continue
        seen.add(cl_n)
        if cl_n in cache and not rescan:
            skip_c+=1; stats["skipped"]+=1; continue
        numbers.append(cl_n)

    total=len(numbers)
    cls()
    header_box("⚡ SCANNING",C)
    print()
    print(cl(f"  📋 File      : {len(raw)} lines",W))
    print(cl(f"  ✅ Valid     : {total} new numbers to check",G))
    print(cl(f"  🚫 Invalid   : {skip_inv}",RD))
    print(cl(f"  ⏭  Cached    : {skip_c}",Y))
    print(cl(f"  🎯 Target    : {TARGET_ACTIVE} active → auto stop",M))
    print()
    divider()
    print()

    if total==0:
        print(cl("  ⚠  Nothing to scan. Use rescan to ignore cache.","Y"))
        input(cl("\n  [ENTER] Back","GR")); return

    cur_ref=[0]
    connector=aiohttp.TCPConnector(
        limit=CONCURRENCY+15,force_close=False,
        enable_cleanup_closed=True,ssl=False,ttl_dns_cache=300
    )
    sem=asyncio.Semaphore(CONCURRENCY)
    t0=time.time()

    async with aiohttp.ClientSession(connector=connector) as session:
        for i in range(0,total,BATCH_SIZE):
            if stop_event.is_set(): break
            batch=numbers[i:i+BATCH_SIZE]
            await asyncio.gather(*[process(session,sem,n,cache,cur_ref) for n in batch])
            if stop_event.is_set(): break
            draw_progress(cur_ref[0],total,stats["active"],stats["dead"],stats["timeout"])
            await asyncio.sleep(random.uniform(1.5,3.0))

    print()
    save_cache(cache)
    save_results(results_log)
    elapsed=round(time.time()-t0,1)

    print()
    divider(G)
    print(cl(f"  ✅ DONE in {elapsed}s — {stats['active']} active found",G))
    rec=len([r for r in results_log if r["tier"]=="RECOMMENDED"])
    med=len([r for r in results_log if r["tier"]=="MEDIUM"])
    low=len([r for r in results_log if r["tier"]=="LOW"])
    print(cl(f"  🟢 Recommended: {rec}  🟡 Medium: {med}  🔴 Low: {low}",W))
    divider(G)
    print()
    input(cl("  [ENTER] Go to menu","GR"))

# ─── MAIN MENU ────────────────────────────────────────────
def menu():
    global results_log
    # Load previous session if exists
    prev=load_results()
    if prev: results_log=prev

    while True:
        cls()
        header_box("WA CHECKER v9 — PERU MOBILE", C)
        print()
        # Quick stats
        total_a=len(results_log)
        rec=len([r for r in results_log if r["tier"]=="RECOMMENDED"])
        med=len([r for r in results_log if r["tier"]=="MEDIUM"])
        low=len([r for r in results_log if r["tier"]=="LOW"])
        cache_count=len(load_cache())

        print(cl(f"  💾 Cache       : {cache_count} checked",GR))
        if total_a:
            print(cl(f"  📊 Last Session: {total_a} active  🟢{rec}  🟡{med}  🔴{low}",W))
        print()
        divider()
        print()
        print(cl("  [1]","G")+cl("  Scan numbers","W"))
        print(cl("  [2]","Y")+cl("  Rescan (ignore cache, fresh check)","W"))
        print(cl("  [3]","G")+cl("  Show RECOMMENDED results  (score 75-100) 🟢","W"))
        print(cl("  [4]","Y")+cl("  Show MEDIUM results       (score 50-74)  🟡","W"))
        print(cl("  [5]","RD")+cl(" Show LOW results          (score 0-49)   🔴","W"))
        print(cl("  [6]","C")+cl("  Show ALL results (full table)","W"))
        print(cl("  [7]","M")+cl("  Show session summary","W"))
        print(cl("  [8]","GR")+cl(" Clear cache","W"))
        print(cl("  [0]","GR")+cl(" Exit","W"))
        print()
        divider()
        print()
        choice=input(cl("  › ","C")).strip()

        if choice=="1":
            asyncio.run(run_scan(rescan=False))
        elif choice=="2":
            asyncio.run(run_scan(rescan=True))
        elif choice=="3":
            show_recommended()
        elif choice=="4":
            show_medium()
        elif choice=="5":
            show_low()
        elif choice=="6":
            show_tier(label="ALL ACTIVE RESULTS")
        elif choice=="7":
            show_summary()
        elif choice=="8":
            if Path(CACHE_FILE).exists(): os.remove(CACHE_FILE)
            print(cl("\n  ✅ Cache cleared.","G"))
            time.sleep(1)
        elif choice=="0":
            cls()
            print(cl("\n  bye chief.\n","GR"))
            sys.exit(0)

if __name__=="__main__":
    menu()
ENDOFSCRIPT
