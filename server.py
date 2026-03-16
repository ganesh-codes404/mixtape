from flask import Flask,send_from_directory,request,jsonify,Response,stream_with_context
import os,json,urllib.request,threading,time,re,socket,logging

app=Flask(__name__)
UPLOAD_FOLDER="songs"
PLAYLISTS_FILE="playlists.json"
os.makedirs(UPLOAD_FOLDER,exist_ok=True)
_dl_jobs={}

def load_playlists():
    if os.path.exists(PLAYLISTS_FILE):
        with open(PLAYLISTS_FILE) as f: return json.load(f)
    return {}

def save_playlists(data):
    with open(PLAYLISTS_FILE,"w") as f: json.dump(data,f)

def read_id3_text(filepath):
    artist,title=None,None
    try:
        with open(filepath,'rb') as f:
            header=f.read(10)
            if header[:3]!=b'ID3': raise Exception()
            version=header[3]
            size=(header[6]&0x7f)<<21|(header[7]&0x7f)<<14|(header[8]&0x7f)<<7|(header[9]&0x7f)
            data=f.read(size)
        pos=0
        while pos<len(data)-10 and (not artist or not title):
            if version>=3:
                fid=data[pos:pos+4]
                if fid==b'\x00\x00\x00\x00': break
                fsize=int.from_bytes(data[pos+4:pos+8],'big'); pos+=10
            else:
                fid=data[pos:pos+3]
                if fid==b'\x00\x00\x00': break
                fsize=int.from_bytes(data[pos+3:pos+6],'big'); pos+=6
            if fsize<=0 or pos+fsize>len(data): break
            raw=data[pos:pos+fsize]; pos+=fsize
            if fid in(b'TIT2',b'TT2') and not title:
                enc=raw[0]; b=raw[1:]
                try:
                    if enc==0: title=b.rstrip(b'\x00').decode('latin-1')
                    elif enc in(1,2):
                        if b[:2] in(b'\xff\xfe',b'\xfe\xff'): b=b[2:]
                        title=b.rstrip(b'\x00').decode('utf-16','ignore')
                    else: title=b.rstrip(b'\x00').decode('utf-8','ignore')
                except: pass
            elif fid in(b'TPE1',b'TP1') and not artist:
                enc=raw[0]; b=raw[1:]
                try:
                    if enc==0: artist=b.rstrip(b'\x00').decode('latin-1')
                    elif enc in(1,2):
                        if b[:2] in(b'\xff\xfe',b'\xfe\xff'): b=b[2:]
                        artist=b.rstrip(b'\x00').decode('utf-16','ignore')
                    else: artist=b.rstrip(b'\x00').decode('utf-8','ignore')
                except: pass
    except: pass
    base=os.path.splitext(os.path.basename(filepath))[0]
    if ' - ' in base:
        parts=base.split(' - ',1)
        if not artist: artist=parts[0].strip()
        if not title:  title=parts[1].strip()
    else:
        if not title: title=base
    return artist or '', title or base

@app.route("/meta/<path:filename>")
def meta(filename):
    fp=os.path.join(UPLOAD_FOLDER,filename)
    if not os.path.exists(fp): return jsonify({"error":"not found"}),404
    artist,title=read_id3_text(fp)
    return jsonify({"artist":artist,"title":title})

def ia_extract_identifier(url):
    m=re.search(r'archive\.org/(?:compress|details|download)/([^/?&]+)',url)
    return m.group(1) if m else None

def ia_download_worker(job_id,identifier,tape_name):
    job=_dl_jobs[job_id]
    try:
        job['msg']='Fetching track list...'
        with urllib.request.urlopen(f'https://archive.org/metadata/{identifier}',timeout=30) as r:
            meta=json.loads(r.read())
        item_title=meta.get('metadata',{}).get('title','') or tape_name or identifier
        job['title']=item_title
        all_files=meta.get('files',[])
        vbr=[f for f in all_files if f.get('format')=='VBR MP3']
        mp3_64=[f for f in all_files if f.get('format')=='64Kbps MP3']
        any_mp3=[f for f in all_files if f.get('name','').lower().endswith('.mp3')]
        candidates=vbr or mp3_64 or any_mp3
        if not candidates:
            job['msg']='No MP3 files found.'; job['done']=True; job['error']=True; return
        seen={}
        for f in candidates:
            b=os.path.basename(f['name'])
            if b not in seen: seen[b]=f
        files_to_get=list(seen.values())
        total=len(files_to_get)
        job['total']=total
        saved=[];existing=[]
        for i,f in enumerate(files_to_get):
            fname=os.path.basename(f['name'])
            dest=os.path.join(UPLOAD_FOLDER,fname)
            job['progress']=i+1
            if os.path.exists(dest):
                existing.append(fname); job['msg']=f'[{i+1}/{total}] Already have: {fname}'; continue
            job['msg']=f'[{i+1}/{total}] {fname}'
            dl_url=f'https://archive.org/download/{identifier}/{urllib.request.quote(f["name"])}'
            try:
                req=urllib.request.Request(dl_url,headers={'User-Agent':'Mozilla/5.0'})
                with urllib.request.urlopen(req,timeout=60) as resp:
                    with open(dest,'wb') as out:
                        while True:
                            chunk=resp.read(262144)
                            if not chunk: break
                            out.write(chunk)
                saved.append(fname)
            except Exception as e:
                job['msg']=f'[{i+1}/{total}] Failed: {fname}'; time.sleep(0.3)
        all_songs=saved+existing
        if all_songs:
            pls=load_playlists()
            pid='pl_'+str(int(time.time()))
            pls[pid]={'name':item_title,'songs':all_songs,'color':'#e8003a'}
            save_playlists(pls)
            job['playlist_id']=pid; job['songs']=all_songs
            job['new_count']=len(saved); job['existing_count']=len(existing)
            job['msg']=f'Done! {len(saved)} new + {len(existing)} existing'
        else:
            job['msg']='No tracks downloaded.'; job['error']=True
        job['done']=True
    except Exception as e:
        job['msg']=f'Error: {e}'; job['done']=True; job['error']=True

@app.route("/ia/start",methods=["POST"])
def ia_start():
    data=request.get_json()
    url=data.get('url','').strip()
    identifier=ia_extract_identifier(url)
    if not identifier: return jsonify({"error":"Could not find an Archive.org identifier in that URL"}),400
    job_id='ia_'+str(int(time.time()*1000))
    tape_name=data.get('tape_name','') or identifier.replace('-',' ').replace('_',' ').title()
    _dl_jobs[job_id]={'msg':'Starting...','done':False,'error':False,'songs':[],'title':tape_name,'new_count':0,'existing_count':0,'progress':0,'total':0}
    threading.Thread(target=ia_download_worker,args=(job_id,identifier,tape_name),daemon=True).start()
    return jsonify({"job_id":job_id})

@app.route("/ia/progress/<job_id>")
def ia_progress(job_id):
    def generate():
        while True:
            job=_dl_jobs.get(job_id)
            if not job:
                yield f"data: {json.dumps({'msg':'Job not found','done':True,'error':True})}\n\n"; return
            yield f"data: {json.dumps(job)}\n\n"
            if job['done']: return
            time.sleep(0.5)
    return Response(stream_with_context(generate()),mimetype='text/event-stream',
                    headers={'Cache-Control':'no-cache','X-Accel-Buffering':'no'})

@app.route("/")
def index():
    all_songs=[]
    for root,dirs,files in os.walk(UPLOAD_FOLDER):
        for f in sorted(files):
            if f.lower().endswith(".mp3"):
                rel=os.path.relpath(os.path.join(root,f),UPLOAD_FOLDER).replace("\\","/")
                all_songs.append(rel)
    playlists=load_playlists()
    songs_json=json.dumps(all_songs)
    playlists_json=json.dumps(playlists)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<title>Mixtape</title>
<link rel="manifest" href="/manifest.json">
<link rel="apple-touch-icon" href="/icon.png">
<link rel="apple-touch-icon" sizes="192x192" href="/icon.png">
<link rel="apple-touch-icon" sizes="512x512" href="/icon.png">
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<style>
:root{{
  --bg:#0a0a0a;--surface:#141414;--card:#1a1a1a;--border:#252525;
  --accent:#e8003a;--accent2:#ff6b35;--tb:#1c0e06;
  --text:#f0f0f0;--muted:#4a4a4a;--ts:#7a7a7a;
  --font:'Space Grotesk',sans-serif;
  --safe-b:env(safe-area-inset-bottom,0px);
  --safe-t:env(safe-area-inset-top,0px);
}}
*{{margin:0;padding:0;box-sizing:border-box;-webkit-tap-highlight-color:transparent}}
html,body{{height:100%;overflow:hidden;background:var(--bg);color:var(--text);font-family:var(--font)}}

/* ══════════════════ DESKTOP ══════════════════ */
.app{{display:grid;grid-template-columns:400px 1fr;height:100vh;overflow:hidden}}
.panel-player{{background:var(--surface);border-right:1px solid var(--border);display:flex;flex-direction:column;height:100vh;overflow:hidden}}
.panel-right{{display:flex;flex-direction:column;height:100vh;overflow:hidden;background:var(--bg)}}

/* ══════════════════ MOBILE LAYOUT ══════════════════ */
@media(max-width:768px){{
  .app{{display:block;position:fixed;inset:0}}
  .panel-player{{
    position:absolute;inset:0;z-index:1;
    display:flex;flex-direction:column;
    background:#111;overflow:hidden;
  }}
  .song-panel-desktop,.upload-area,.panel-right{{display:none !important}}

  .tape-display{{
    /* fixed height — leaves room for progress + controls + pills */
    height:52vh;
    min-height:0;flex-shrink:0;
    display:flex;flex-direction:column;
    background:#111;
    overflow:hidden;
  }}

  /* cassette — full width, no margins, fills all available height */
  .cassette-card{{
    flex:1;min-height:0;width:100%;
    position:relative;
    display:flex;flex-direction:column;
    background:#1c1c1c;
    overflow:hidden;
  }}
  .cassette-card::before{{
    content:'';position:absolute;inset:0;z-index:0;
    background:repeating-linear-gradient(-45deg,transparent,transparent 18px,rgba(255,255,255,.018) 18px,rgba(255,255,255,.018) 19px);
    pointer-events:none;
  }}
  .cassette-notch{{display:none !important}}

  .screw{{
    position:absolute;width:14px;height:14px;border-radius:50%;z-index:10;
    background:radial-gradient(circle at 40% 35%,#888 0%,#333 50%,#1a1a1a 100%);
    border:1px solid #555;
    box-shadow:0 2px 6px rgba(0,0,0,.9),inset 0 1px 0 rgba(255,255,255,.15);
  }}
  .screw::before{{content:'';position:absolute;top:50%;left:50%;width:8px;height:1.5px;background:rgba(0,0,0,.7);transform:translate(-50%,-50%) rotate(0deg);border-radius:1px;}}
  .screw::after{{content:'';position:absolute;top:50%;left:50%;width:8px;height:1.5px;background:rgba(0,0,0,.7);transform:translate(-50%,-50%) rotate(90deg);border-radius:1px;}}
  .screw.tl{{top:10px;left:10px}}
  .screw.tr{{top:10px;right:10px}}
  .screw.bl{{bottom:10px;left:10px}}
  .screw.br{{bottom:10px;right:10px}}

  /* inner: zero padding, window + label stacked */
  .cassette-inner{{
    position:relative;z-index:1;
    flex:1;min-height:0;
    display:flex;flex-direction:column;
    padding:18px 12px 10px;
    gap:8px;
  }}

  /* tape window — dark rectangle, fills most of card */
  .tape-window{{
    flex:1;min-height:0;
    background:#080808;
    border:2px solid #2a2a2a;
    border-radius:14px;
    position:relative;overflow:hidden;
    box-shadow:inset 0 8px 30px rgba(0,0,0,.98);
    display:flex;
    align-items:center;
    justify-content:space-evenly;
    padding:20px 10px;
  }}
  .tape-window::before{{
    content:'';position:absolute;
    bottom:14px;left:30%;right:30%;height:2px;
    background:#1c1c1c;border-radius:2px;
  }}
  .tape-window-mid{{display:none}}

  /* ── REELS ── */
  .reel{{
    width:28vw;height:28vw;
    max-width:110px;max-height:110px;
    border-radius:50%;
    background:radial-gradient(circle at 50% 50%,#282828 0%,#111 55%,#050505 100%);
    border:4px solid #c8c8c8;
    position:relative;
    display:flex;align-items:center;justify-content:center;
    box-shadow:0 4px 16px rgba(0,0,0,.9),inset 0 0 24px rgba(0,0,0,.7);
    flex-shrink:0;z-index:2;
  }}
  /* hub */
  .reel::before{{
    content:'';position:absolute;
    width:20%;height:20%;border-radius:50%;
    background:radial-gradient(circle at 40% 35%,#666,#222);
    border:2px solid #555;
    box-shadow:inset 0 2px 4px rgba(0,0,0,.9);
    z-index:5;
  }}
  /* inner track ring */
  .reel::after{{
    content:'';position:absolute;
    inset:4px;border-radius:50%;
    border:1px solid #1e1e1e;
    z-index:1;
  }}
  /* spokes — 3 vertical parallel lines, same Y axis, evenly spaced */
  .reel-spokes{{
    position:absolute;width:60%;height:80%;z-index:3;
    display:flex;align-items:stretch;justify-content:space-between;
  }}
  .reel-spokes::before,.reel-spokes::after,.reel-spokes span{{
    content:'';display:block;
    width:2px;
    background:rgba(55,55,55,.95);
    border-radius:1px;
    flex-shrink:0;
  }}
  .reel.spinning{{animation:spin 1.6s linear infinite}}
  @keyframes spin{{to{{transform:rotate(360deg)}}}}

  /* label */
  .tape-label{{
    background:linear-gradient(170deg,#f6efdc,#eee4c4);
    border-radius:8px;padding:10px 14px 9px;
    position:relative;overflow:hidden;flex-shrink:0;
    box-shadow:0 3px 10px rgba(0,0,0,.6);
  }}
  .tape-label::before{{content:'';position:absolute;inset:0;background:repeating-linear-gradient(45deg,transparent,transparent 7px,rgba(0,0,0,.016) 7px,rgba(0,0,0,.016) 14px);}}
  .tape-label-stripe{{position:absolute;top:0;left:0;right:0;height:5px;background:linear-gradient(90deg,var(--accent),var(--accent2));border-radius:8px 8px 0 0;}}
  .tape-label-np{{font-size:7px;font-weight:700;color:var(--accent);letter-spacing:4px;text-transform:uppercase;margin-top:2px;margin-bottom:1px;position:relative;z-index:1;}}
  .tape-label-title{{font-size:16px;font-weight:700;color:#111;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;position:relative;z-index:1;line-height:1.2;}}
  .tape-label-artist{{font-size:10px;font-weight:500;color:#444;margin-top:1px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;position:relative;z-index:1;}}

  /* pill bar — TOP of screen */
  .bottom-bar{{
    display:flex;
    padding:max(10px,var(--safe-t)) 12px 8px;
    gap:8px;flex-shrink:0;background:#111;
    border-bottom:1px solid #222;
  }}
  .bottom-pill{{
    flex:1;display:flex;align-items:center;justify-content:center;gap:5px;
    background:rgba(255,255,255,.05);border:1px solid #2a2a2a;
    border-radius:8px;padding:10px 4px;cursor:pointer;
    font-size:9px;font-weight:700;letter-spacing:1.2px;text-transform:uppercase;
    color:#555;transition:all .18s;user-select:none;-webkit-user-select:none;
  }}
  .bottom-pill.active{{background:rgba(232,0,58,.15);border-color:var(--accent);color:var(--accent)}}
  .bottom-pill svg{{width:11px;height:11px;fill:currentColor;flex-shrink:0}}

  .sheet-overlay{{
    position:fixed;inset:0;z-index:299;
    background:rgba(0,0,0,.75);
    opacity:0;pointer-events:none;
    transition:opacity .28s;
  }}
  .sheet-overlay.show{{opacity:1;pointer-events:auto}}
  /* Sheet is a full-screen overlay that slides up.
     It IS the scroll container — no inner div needed for scroll. */
  .sheet{{
    position:fixed;left:0;right:0;bottom:0;
    height:92%;
    border-radius:20px 20px 0 0;
    border-top:2px solid var(--accent);
    background:#1a1a1a;
    z-index:300;
    display:flex;flex-direction:column;
    overflow:hidden;
    transform:translateY(100%);
    transition:transform .28s cubic-bezier(.4,0,.2,1);
    will-change:transform;
    box-shadow:0 -12px 50px rgba(0,0,0,.9);
  }}
  .sheet.open{{transform:translateY(0)}}
  .sheet-scroll{{
    flex:1;min-height:0;
    overflow-y:auto;
    -webkit-overflow-scrolling:touch;
    padding-bottom:40px;
  }}
  .sheet-handle{{display:flex;justify-content:center;padding:10px 0 4px;flex-shrink:0;cursor:pointer}}
  .sheet-handle-bar{{width:36px;height:4px;background:#333;border-radius:2px}}
}}
@media(min-width:769px){{
  .cassette-card,.cassette-notch,.cassette-inner,.tape-window,.tape-label,
  .bottom-bar,.sheet,.sheet-overlay{{display:none !important}}
  .screw{{display:none !important}}
}}

/* ══════════════════ DESKTOP CASSETTE (tape-display) ══════════════════ */
.tape-display{{
  flex-shrink:0;
  padding:22px 22px 14px;
  background:var(--tb);
  border-bottom:3px solid var(--accent);
}}
.tape-label-area{{background:#f0ead8;border-radius:6px;padding:10px 12px 8px;position:relative;overflow:hidden;margin-top:14px}}
.tape-label-area::before{{content:'';position:absolute;inset:0;background:repeating-linear-gradient(45deg,transparent,transparent 4px,rgba(0,0,0,.025) 4px,rgba(0,0,0,.025) 8px)}}
.tape-label-stripe-d{{position:absolute;top:0;left:0;right:0;height:5px;background:linear-gradient(90deg,var(--accent),var(--accent2));border-radius:6px 6px 0 0}}
.tape-np-label{{font-size:8px;font-weight:700;color:var(--accent);letter-spacing:4px;text-transform:uppercase;margin-bottom:2px;position:relative;z-index:1}}
.tape-song-title{{font-size:16px;font-weight:700;color:#111;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;position:relative;z-index:1}}
.tape-song-artist{{font-size:11px;color:#555;margin-top:2px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;position:relative;z-index:1}}
.desktop-reels{{display:flex;justify-content:space-between;align-items:center;padding:0 10px}}
.desktop-reel{{
  width:64px;height:64px;border-radius:50%;
  background:radial-gradient(circle at 38% 35%,#3a3a3a,#111);
  border:3px solid #3a3a3a;
  position:relative;display:flex;align-items:center;justify-content:center;
  box-shadow:inset 0 0 14px rgba(0,0,0,.9),0 2px 8px rgba(0,0,0,.5);
}}
.desktop-reel::before{{content:'';position:absolute;width:36%;height:36%;border-radius:50%;background:#222;border:2px solid #444;z-index:2}}
.desktop-reel-spokes{{position:absolute;width:100%;height:100%;z-index:1}}
.desktop-reel-spokes::before,.desktop-reel-spokes::after,.desktop-reel-spokes span{{
  content:'';display:block;
  position:absolute;top:50%;left:50%;
  width:44%;height:2px;background:rgba(70,70,70,.8);
  transform-origin:0 50%;margin-top:-1px;border-radius:1px;
}}
.desktop-reel-spokes::before{{transform:rotate(0deg)}}
.desktop-reel-spokes::after{{transform:rotate(60deg)}}
.desktop-reel-spokes span{{transform:rotate(120deg)}}
.desktop-reel.spinning{{animation:spin 1.4s linear infinite}}
.tape-mid-text{{font-size:9px;font-weight:700;letter-spacing:5px;color:rgba(255,255,255,.15);text-transform:uppercase}}
@keyframes spin{{to{{transform:rotate(360deg)}}}}

/* ══════════════════ PROGRESS ══════════════════ */
.progress-area{{padding:10px 22px 6px;background:var(--tb);flex-shrink:0}}
@media(max-width:768px){{.progress-area{{background:var(--bg);padding:8px 16px 6px}}}}
.progress-bar{{width:100%;height:4px;background:rgba(255,255,255,.1);border-radius:2px;cursor:pointer}}
.progress-fill{{height:100%;background:linear-gradient(90deg,var(--accent),var(--accent2));border-radius:2px;width:0%;transition:width .4s linear;pointer-events:none}}
.time-row{{display:flex;justify-content:space-between;font-size:10px;font-weight:500;color:var(--ts);margin-top:4px;opacity:.6}}

/* ══════════════════ CONTROLS ══════════════════ */
.controls{{
  display:flex;align-items:center;justify-content:center;gap:8px;
  padding:10px 22px 12px;
  background:var(--tb);border-bottom:3px solid var(--accent);flex-shrink:0;
}}
@media(max-width:768px){{.controls{{background:var(--bg);border-bottom:1px solid var(--border);padding:8px 16px 10px}}}}
.ctrl-btn{{
  background:none;border:1.5px solid rgba(255,255,255,.12);color:var(--text);
  width:44px;height:44px;border-radius:5px;cursor:pointer;
  display:flex;align-items:center;justify-content:center;transition:all .15s;flex-shrink:0;
}}
.ctrl-btn:hover,.ctrl-btn:active{{background:rgba(232,0,58,.2);border-color:var(--accent);color:var(--accent)}}
.ctrl-btn.active-btn{{color:var(--accent);border-color:var(--accent)}}
.ctrl-btn.play-pause{{width:56px;height:56px;background:var(--accent);border-color:var(--accent);border-radius:5px}}
.ctrl-btn.play-pause:hover,.ctrl-btn.play-pause:active{{background:#ff0040}}
.ctrl-btn svg{{width:16px;height:16px;fill:currentColor}}
.ctrl-btn.play-pause svg{{width:22px;height:22px}}

/* ══════════════════ UPLOAD (desktop only) ══════════════════ */
.upload-area{{padding:10px 22px;background:var(--surface);border-bottom:1px solid var(--border);flex-shrink:0;display:flex;gap:8px}}
.upload-btn{{flex:1;display:flex;align-items:center;justify-content:center;gap:7px;background:rgba(255,255,255,.04);border:1px dashed var(--border);border-radius:5px;padding:9px 8px;cursor:pointer;font-size:10px;font-weight:700;color:var(--muted);letter-spacing:1.5px;text-transform:uppercase;transition:all .2s;white-space:nowrap}}
.upload-btn:hover,.upload-btn:active{{border-color:var(--accent);color:var(--accent)}}
.upload-btn input{{display:none}}
.ia-btn{{display:flex;align-items:center;justify-content:center;gap:7px;background:rgba(255,255,255,.04);border:1px dashed rgba(255,107,53,.4);border-radius:5px;padding:9px 8px;cursor:pointer;font-size:10px;font-weight:700;color:rgba(255,107,53,.7);letter-spacing:1.5px;text-transform:uppercase;transition:all .2s;white-space:nowrap;flex-shrink:0}}
.ia-btn:hover,.ia-btn:active{{border-color:var(--accent2);color:var(--accent2)}}

/* ══════════════════ SONG LIST ══════════════════ */
.song-panel-desktop{{display:flex;flex-direction:column;flex:1;overflow:hidden;min-height:0}}
.song-list-header{{padding:10px 22px 8px;display:flex;justify-content:space-between;align-items:center;flex-shrink:0;background:var(--surface)}}
.section-title{{font-size:10px;font-weight:700;letter-spacing:3px;color:var(--muted);text-transform:uppercase}}
.song-scroll{{flex:1;overflow-y:auto;min-height:0;-webkit-overflow-scrolling:touch;background:var(--surface)}}
.song-scroll::-webkit-scrollbar{{width:3px}}
.song-scroll::-webkit-scrollbar-thumb{{background:var(--border);border-radius:2px}}
.song-item{{display:flex;align-items:center;gap:10px;padding:9px 22px;cursor:pointer;border-bottom:1px solid rgba(255,255,255,.03);transition:background .1s;touch-action:manipulation}}
.song-item:hover{{background:rgba(255,255,255,.04)}}
.song-item:active{{background:rgba(255,255,255,.06)}}
.song-item.active{{background:rgba(232,0,58,.08);border-left:2px solid var(--accent)}}
.song-item.active .song-name{{color:var(--accent)}}
.song-num{{font-size:10px;font-weight:500;color:var(--muted);width:22px;text-align:right;flex-shrink:0}}
.song-info{{flex:1;min-width:0}}
.song-name{{font-size:12px;font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.song-artist-small{{font-size:10px;color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;margin-top:1px}}
.add-to-pl-btn{{width:24px;height:24px;border-radius:3px;border:none;background:none;color:var(--muted);font-size:16px;cursor:pointer;display:flex;align-items:center;justify-content:center;flex-shrink:0;opacity:0;transition:opacity .15s}}
.song-item:hover .add-to-pl-btn,.add-to-pl-btn{{opacity:1}}
.add-to-pl-btn:hover{{color:var(--accent)}}
/* shared sheet scroll */
.sheet-scroll::-webkit-scrollbar{{width:3px}}
.sheet-scroll::-webkit-scrollbar-thumb{{background:var(--border);border-radius:2px}}
.sheet-list-header{{padding:10px 20px 8px;display:flex;align-items:center;justify-content:space-between;flex-shrink:0;border-bottom:1px solid var(--border)}}

/* ══════════════════ SHEET UPLOAD BAR ══════════════════ */
.sheet-upload-bar{{padding:8px 14px;border-bottom:1px solid var(--border);flex-shrink:0;display:flex;gap:7px}}
.sheet-upload-btn{{flex:1;display:flex;align-items:center;justify-content:center;gap:6px;background:rgba(255,255,255,.04);border:1px dashed var(--border);border-radius:5px;padding:8px 6px;cursor:pointer;font-size:9px;font-weight:700;color:var(--muted);letter-spacing:1.2px;text-transform:uppercase;transition:all .2s;white-space:nowrap}}
.sheet-upload-btn:hover,.sheet-upload-btn:active{{border-color:var(--accent);color:var(--accent)}}
.sheet-upload-btn input{{display:none}}
.sheet-ia-btn{{display:flex;align-items:center;justify-content:center;gap:6px;background:rgba(255,255,255,.04);border:1px dashed rgba(255,107,53,.4);border-radius:5px;padding:8px 8px;cursor:pointer;font-size:9px;font-weight:700;color:rgba(255,107,53,.7);letter-spacing:1.2px;text-transform:uppercase;transition:all .2s;white-space:nowrap;flex-shrink:0}}
.sheet-ia-btn:hover,.sheet-ia-btn:active{{border-color:var(--accent2);color:var(--accent2)}}

/* ══════════════════ RIGHT PANEL / TAPES ══════════════════ */
.right-header{{padding:16px 24px 14px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between;flex-shrink:0}}
.brand{{font-size:24px;font-weight:700;letter-spacing:5px;text-transform:uppercase}}
.brand span{{color:var(--accent)}}
.new-playlist-btn{{background:none;border:1.5px solid var(--accent);color:var(--accent);font-family:var(--font);font-size:10px;font-weight:700;letter-spacing:2px;text-transform:uppercase;padding:7px 13px;border-radius:3px;cursor:pointer;transition:all .2s}}
.new-playlist-btn:hover{{background:var(--accent);color:white}}
.playlists-scroll{{flex:1;overflow-y:auto;min-height:0;padding:12px 20px;display:flex;flex-direction:column;gap:4px;-webkit-overflow-scrolling:touch}}
.playlists-scroll::-webkit-scrollbar{{width:3px}}
.playlists-scroll::-webkit-scrollbar-thumb{{background:var(--border)}}
.tape-card{{display:flex;align-items:stretch;border:1px solid var(--border);border-radius:5px;overflow:hidden;cursor:pointer;transition:all .2s;background:var(--card);touch-action:manipulation}}
.tape-card:hover{{border-color:var(--accent);transform:translateX(3px)}}
.tape-card.active-playlist{{border-color:var(--accent);background:rgba(232,0,58,.05)}}
.tape-card-spine{{width:7px;flex-shrink:0}}
.tape-card-body{{flex:1;padding:11px 14px;display:flex;align-items:center;justify-content:space-between;gap:10px;min-width:0}}
.tape-card-info{{min-width:0;flex:1;cursor:pointer}}
.tape-card-name{{font-size:13px;font-weight:700;letter-spacing:.5px;text-transform:uppercase;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.tape-card-meta{{font-size:10px;color:var(--muted);margin-top:2px;letter-spacing:.5px}}
.tape-card-actions{{display:flex;gap:5px;align-items:center;flex-shrink:0}}
.tape-action-btn{{background:none;border:1px solid var(--border);color:var(--muted);font-family:var(--font);font-size:10px;font-weight:600;letter-spacing:1px;text-transform:uppercase;padding:5px 9px;border-radius:3px;cursor:pointer;white-space:nowrap;transition:all .15s}}
.tape-action-btn:hover,.tape-action-btn:active{{border-color:var(--accent);color:var(--accent)}}
.tape-action-btn.play{{border-color:var(--accent);color:var(--accent)}}
.tape-action-btn.play:hover{{background:var(--accent);color:white}}
.reel-mini{{width:16px;height:16px;border-radius:50%;background:#111;border:2px solid #333;position:relative;flex-shrink:0}}
.reel-mini::before{{content:'';position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);width:5px;height:5px;border-radius:50%;background:#222;border:1px solid #444}}
.empty-state{{text-align:center;color:var(--muted);padding:40px 20px}}
.empty-state p{{font-size:13px;font-weight:700;letter-spacing:3px;text-transform:uppercase;margin-bottom:6px}}

/* ══════════════════ MODALS ══════════════════ */
.modal-overlay{{display:none;position:fixed;inset:0;background:rgba(0,0,0,.85);z-index:400;align-items:flex-end;justify-content:center}}
@media(min-width:769px){{.modal-overlay{{align-items:center}}}}
.modal-overlay.show{{display:flex}}
.modal{{background:var(--surface);border:1px solid var(--border);border-top:3px solid var(--accent);width:100%;max-width:500px;max-height:88vh;border-radius:16px 16px 0 0;overflow:hidden;display:flex;flex-direction:column}}
@media(min-width:769px){{.modal{{border-radius:6px}}}}
.modal-header{{padding:16px 20px 12px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between;flex-shrink:0}}
.modal-title{{font-size:13px;font-weight:700;letter-spacing:3px;text-transform:uppercase}}
.modal-close{{background:none;border:none;color:var(--muted);font-size:24px;cursor:pointer;line-height:1;padding:0 4px}}
.modal-body{{padding:16px 20px;overflow-y:auto;min-height:0;flex:1;-webkit-overflow-scrolling:touch}}
.modal-input{{width:100%;background:var(--bg);border:1px solid var(--border);color:var(--text);font-family:var(--font);font-size:14px;padding:10px 14px;border-radius:4px;margin-bottom:14px;outline:none}}
.modal-input:focus{{border-color:var(--accent)}}
.modal-song-item{{display:flex;align-items:center;gap:10px;padding:8px;border-radius:4px;cursor:pointer;margin-bottom:2px;font-size:12px}}
.modal-song-item:hover{{background:rgba(255,255,255,.04)}}
.modal-song-item.selected{{background:rgba(232,0,58,.1);color:var(--accent)}}
.modal-song-item input[type=checkbox]{{accent-color:var(--accent);width:15px;height:15px;flex-shrink:0}}
.modal-footer{{padding:12px 20px calc(12px + var(--safe-b));border-top:1px solid var(--border);display:flex;gap:10px;justify-content:flex-end;flex-shrink:0}}
.btn-cancel{{background:none;border:1px solid var(--border);color:var(--muted);font-family:var(--font);font-size:12px;font-weight:600;letter-spacing:2px;text-transform:uppercase;padding:9px 16px;border-radius:4px;cursor:pointer}}
.btn-save{{background:var(--accent);border:none;color:white;font-family:var(--font);font-size:12px;font-weight:600;letter-spacing:2px;text-transform:uppercase;padding:9px 16px;border-radius:4px;cursor:pointer}}
.btn-save:hover{{background:#ff0040}}
.sub-label{{font-size:10px;font-weight:700;letter-spacing:3px;text-transform:uppercase;color:var(--muted);margin-bottom:8px}}
.tape-colors{{display:flex;gap:8px;margin-bottom:16px;flex-wrap:wrap}}
.color-swatch{{width:26px;height:26px;border-radius:4px;cursor:pointer;border:3px solid transparent;transition:all .15s}}
.color-swatch.selected{{border-color:white;transform:scale(1.1)}}

/* IA progress */
.ia-progress-wrap{{margin-top:12px;display:none}}
.ia-progress-wrap.show{{display:block}}
.ia-progress-track{{height:3px;background:rgba(255,255,255,.1);border-radius:2px;overflow:hidden;margin-bottom:8px}}
.ia-progress-fill{{height:100%;background:linear-gradient(90deg,var(--accent),var(--accent2));width:0%;transition:width .4s;border-radius:2px}}
.ia-progress-msg{{font-size:11px;color:var(--ts);font-weight:500;min-height:16px;word-break:break-all}}
.ia-done-msg{{font-size:12px;font-weight:600;color:#00e676;margin-top:8px;display:none}}

/* picker */
.pl-picker-overlay{{display:none;position:fixed;inset:0;z-index:350}}
.pl-picker-overlay.show{{display:block}}
.pl-picker{{position:fixed;background:var(--surface);border:1px solid var(--border);border-top:2px solid var(--accent);border-radius:8px;width:200px;z-index:351;overflow:hidden;box-shadow:0 8px 32px rgba(0,0,0,.6)}}
.pl-picker-item{{padding:11px 14px;font-size:12px;font-weight:500;cursor:pointer;display:flex;align-items:center;gap:10px}}
.pl-picker-item:hover{{background:rgba(232,0,58,.12);color:var(--accent)}}

.toast{{position:fixed;bottom:calc(80px + var(--safe-b));left:50%;transform:translateX(-50%) translateY(10px);background:#1e1e1e;border:1px solid var(--border);border-left:3px solid var(--accent);color:var(--text);font-size:12px;font-weight:600;padding:10px 18px;border-radius:6px;z-index:500;opacity:0;transition:all .28s;white-space:nowrap;pointer-events:none;max-width:88vw;overflow:hidden;text-overflow:ellipsis}}
.toast.show{{opacity:1;transform:translateX(-50%) translateY(0)}}
</style>
</head>
<body>
<div class="app">

  <!-- ═══ PLAYER PANEL ═══ -->
  <div class="panel-player" id="panelPlayer">

    <!-- PILL BAR (mobile only) — physically first so it renders at top -->
    <div class="bottom-bar">
      <button class="bottom-pill" id="pillTracks" onclick="openSheet('tracks')">
        <svg viewBox="0 0 24 24"><path d="M12 3v10.55c-.59-.34-1.27-.55-2-.55-2.21 0-4 1.79-4 4s1.79 4 4 4 4-1.79 4-4V7h4V3h-6z"/></svg>Tracks
      </button>
      <button class="bottom-pill" id="pillTapes" onclick="openSheet('tapes')">
        <svg viewBox="0 0 24 24"><path d="M20 4H4c-1.1 0-2 .9-2 2v12c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2V6c0-1.1-.9-2-2-2zm0 14H4V6h16v12zM8 9c-1.1 0-2 .9-2 2s.9 2 2 2 2-.9 2-2-.9-2-2-2zm8 0c-1.1 0-2 .9-2 2s.9 2 2 2 2-.9 2-2-.9-2-2-2z"/></svg>Tapes
      </button>
      <button class="bottom-pill" id="pillSongs" onclick="openSheet('songs')">
        <svg viewBox="0 0 24 24"><path d="M15 6H3v2h12V6zm0 4H3v2h12v-2zM3 16h8v-2H3v2zM17 6v8.18c-.31-.11-.65-.18-1-.18-1.66 0-3 1.34-3 3s1.34 3 3 3 3-1.34 3-3V8h3V6h-5z"/></svg>
        <span id="pillSongsLabel">Songs</span>
      </button>
    </div>

    <!-- DESKTOP cassette -->
    <div class="tape-display" id="desktopCassette">
      <div class="desktop-reels">
        <div class="desktop-reel" id="dreel1"><div class="desktop-reel-spokes"><span></span></div></div>
        <div class="tape-mid-text">MIXTAPE</div>
        <div class="desktop-reel" id="dreel2"><div class="desktop-reel-spokes"><span></span></div></div>
      </div>
      <div class="tape-label-area">
        <div class="tape-label-stripe-d"></div>
        <div class="tape-np-label">Now Playing</div>
        <div class="tape-song-title" id="npTitleD">Select a Track</div>
        <div class="tape-song-artist" id="npArtistD">&nbsp;</div>
      </div>
    </div>

    <!-- MOBILE cassette — fills remaining space between safe-top and controls -->
    <div class="tape-display" id="mobileCassette">
      <div class="cassette-card">
        <div class="screw tl"></div><div class="screw tr"></div>
        <div class="screw bl"></div><div class="screw br"></div>
        <div class="cassette-inner">
          <div class="tape-window">
            <div class="reel" id="reel1"><div class="reel-spokes"><span></span></div></div>
            <div class="reel" id="reel2"><div class="reel-spokes"><span></span></div></div>
          </div>
          <div class="tape-label">
            <div class="tape-label-stripe"></div>
            <div class="tape-label-np">Now Playing</div>
            <div class="tape-label-title" id="npTitle">Select a Track</div>
            <div class="tape-label-artist" id="npArtist">&nbsp;</div>
          </div>
        </div>
      </div>
    </div>

    <!-- PROGRESS -->
    <div class="progress-area">
      <div class="progress-bar" id="progressBar"><div class="progress-fill" id="progressFill"></div></div>
      <div class="time-row"><span id="timeCurrent">0:00</span><span id="timeDuration">0:00</span></div>
    </div>

    <!-- CONTROLS -->
    <div class="controls">
      <button class="ctrl-btn" onclick="prevSong()"><svg viewBox="0 0 24 24"><path d="M6 6h2v12H6zm3.5 6 8.5 6V6z"/></svg></button>
      <button class="ctrl-btn play-pause" onclick="togglePlay()"><svg id="playIcon" viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg></button>
      <button class="ctrl-btn" onclick="nextSong()"><svg viewBox="0 0 24 24"><path d="M6 18l8.5-6L6 6v12zM16 6v12h2V6h-2z"/></svg></button>
      <button class="ctrl-btn" onclick="toggleShuffle()" id="shuffleBtn"><svg viewBox="0 0 24 24"><path d="M10.59 9.17L5.41 4 4 5.41l5.17 5.17 1.42-1.41zM14.5 4l2.04 2.04L4 18.59 5.41 20 17.96 7.46 20 9.5V4h-5.5zm.33 9.41l-1.41 1.41 3.13 3.13L14.5 20H20v-5.5l-2.04 2.04-3.13-3.13z"/></svg></button>
      <button class="ctrl-btn" onclick="toggleRepeat()" id="repeatBtn"><svg viewBox="0 0 24 24"><path d="M7 7h10v3l4-4-4-4v3H5v6h2V7zm10 10H7v-3l-4 4 4 4v-3h12v-6h-2v4z"/></svg></button>
    </div>

    <!-- UPLOAD (desktop only) -->
    <div class="upload-area">
      <label class="upload-btn" id="uploadLabel">
        <svg width="13" height="13" viewBox="0 0 24 24" fill="currentColor"><path d="M9 16h6v-6h4l-7-7-7 7h4v6zm-4 2h14v2H5v-2z"/></svg>Tracks
        <input type="file" accept=".mp3,audio/mpeg" multiple id="fileInput">
      </label>
      <label class="upload-btn" id="folderLabel">
        <svg width="13" height="13" viewBox="0 0 24 24" fill="currentColor"><path d="M10 4H4c-1.1 0-2 .9-2 2v12c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2V8c0-1.1-.9-2-2-2h-8l-2-2z"/></svg>Folder
        <input type="file" accept=".mp3,audio/mpeg" multiple id="folderInput" webkitdirectory>
      </label>
      <button class="ia-btn" onclick="openIAModal()">
        <svg width="13" height="13" viewBox="0 0 24 24" fill="currentColor"><path d="M19 9h-4V3H9v6H5l7 7 7-7zM5 18v2h14v-2H5z"/></svg>Archive
      </button>
    </div>

    <!-- SONG LIST (desktop only) -->
    <div class="song-panel-desktop">
      <div class="song-list-header">
        <span class="section-title" id="queueLabel">All Tracks</span>
      </div>
      <div class="song-scroll" id="songScroll"></div>
    </div>

  </div>

  <!-- ═══ RIGHT PANEL (desktop) ═══ -->
  <div class="panel-right" id="panelRight">
    <div class="right-header">
      <div class="brand">Mix<span>tape</span></div>
      <button class="new-playlist-btn" onclick="openNewPlaylistModal()">+ New Tape</button>
    </div>
    <div class="playlists-scroll" id="playlistsGrid"></div>
  </div>

  <!-- OVERLAY -->
  <div class="sheet-overlay" id="sheetOverlay" onclick="closeSheet()"></div>

  <!-- TRACKS sheet -->
  <div class="sheet" id="sheetTracks">
    <div class="sheet-handle" onclick="closeSheet()"><div class="sheet-handle-bar"></div></div>
    <div class="sheet-list-header">
      <span class="section-title" id="tracksSheetLabel">All Tracks</span>
    </div>
    <div class="sheet-upload-bar">
      <label class="sheet-upload-btn" id="sUploadLabel">
        <svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor"><path d="M9 16h6v-6h4l-7-7-7 7h4v6zm-4 2h14v2H5v-2z"/></svg>Add
        <input type="file" accept=".mp3,audio/mpeg" multiple id="sFileInput">
      </label>
      <label class="sheet-upload-btn" id="sFolderLabel">
        <svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor"><path d="M10 4H4c-1.1 0-2 .9-2 2v12c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2V8c0-1.1-.9-2-2-2h-8l-2-2z"/></svg>Folder&#8594;Tape
        <input type="file" accept=".mp3,audio/mpeg" multiple id="sFolderInput" webkitdirectory>
      </label>
      <button class="sheet-ia-btn" onclick="closeSheet();setTimeout(openIAModal,320)">
        <svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor"><path d="M19 9h-4V3H9v6H5l7 7 7-7zM5 18v2h14v-2H5z"/></svg>Archive
      </button>
    </div>
    <div class="sheet-scroll" id="tracksList"></div>
  </div>

  <!-- TAPES sheet -->
  <div class="sheet" id="sheetTapes">
    <div class="sheet-handle" onclick="closeSheet()"><div class="sheet-handle-bar"></div></div>
    <div class="sheet-list-header">
      <span class="section-title">Tapes</span>
      <button class="new-playlist-btn" style="font-size:9px;padding:5px 10px" onclick="openNewPlaylistModal()">+ New</button>
    </div>
    <div class="sheet-scroll" id="tapesSheetList"></div>
  </div>

  <!-- TAPE SONGS sheet -->
  <div class="sheet" id="sheetSongs">
    <div class="sheet-handle" onclick="closeSheet()"><div class="sheet-handle-bar"></div></div>
    <div class="sheet-list-header">
      <span class="section-title" id="songsSheetLabel">Songs</span>
      <button style="background:none;border:none;color:var(--muted);font-size:11px;font-weight:700;letter-spacing:1px;text-transform:uppercase;cursor:pointer" onclick="openSheet('tapes')">&larr; Tapes</button>
    </div>
    <div class="sheet-scroll" id="songsList"></div>
  </div>
</div>

<!-- MODALS -->
<div class="modal-overlay" id="iaModal">
  <div class="modal">
    <div class="modal-header"><span class="modal-title">&#8595; Archive.org Import</span><button class="modal-close" onclick="closeModal('iaModal')">&#215;</button></div>
    <div class="modal-body">
      <div class="sub-label" style="margin-bottom:6px">Archive.org URL</div>
      <input class="modal-input" type="url" id="iaUrl" placeholder="https://archive.org/compress/identifier/..." style="margin-bottom:8px">
      <div style="font-size:11px;color:var(--muted);margin-bottom:16px;line-height:1.6">Paste any archive.org link. MP3s downloaded one-by-one to keep iSH happy.</div>
      <div class="ia-progress-wrap" id="iaProgressWrap">
        <div class="ia-progress-track"><div class="ia-progress-fill" id="iaProgressFill"></div></div>
        <div class="ia-progress-msg" id="iaProgressMsg"></div>
        <div class="ia-done-msg" id="iaDoneMsg"></div>
      </div>
    </div>
    <div class="modal-footer">
      <button class="btn-cancel" onclick="closeModal('iaModal')">Close</button>
      <button class="btn-save" id="iaStartBtn" onclick="startIADownload()">Download</button>
    </div>
  </div>
</div>

<div class="modal-overlay" id="newPlaylistModal">
  <div class="modal">
    <div class="modal-header"><span class="modal-title">New Tape</span><button class="modal-close" onclick="closeModal('newPlaylistModal')">&#215;</button></div>
    <div class="modal-body">
      <input class="modal-input" type="text" id="newPlaylistName" placeholder="Tape name..." maxlength="40">
      <div class="sub-label">Spine Color</div><div class="tape-colors" id="tapeColors"></div>
      <div class="sub-label">Select Tracks</div>
      <input class="modal-input" type="text" id="newTrackSearch" placeholder="Search tracks..." oninput="filterModalSongs('modalSongList',this.value)" style="margin-bottom:10px">
      <div id="modalSongList"></div>
    </div>
    <div class="modal-footer"><button class="btn-cancel" onclick="closeModal('newPlaylistModal')">Cancel</button><button class="btn-save" onclick="saveNewPlaylist()">Save Tape</button></div>
  </div>
</div>

<div class="modal-overlay" id="editPlaylistModal">
  <div class="modal">
    <div class="modal-header"><span class="modal-title">Edit Tape</span><button class="modal-close" onclick="closeModal('editPlaylistModal')">&#215;</button></div>
    <div class="modal-body">
      <input class="modal-input" type="text" id="editPlaylistName" placeholder="Tape name..." maxlength="40">
      <div class="sub-label">Spine Color</div><div class="tape-colors" id="editTapeColors"></div>
      <div class="sub-label">Select Tracks</div>
      <input class="modal-input" type="text" id="editTrackSearch" placeholder="Search tracks..." oninput="filterModalSongs('editModalSongList',this.value)" style="margin-bottom:10px">
      <div id="editModalSongList"></div>
    </div>
    <div class="modal-footer"><button class="btn-cancel" onclick="closeModal('editPlaylistModal')">Cancel</button><button class="btn-save" onclick="saveEditPlaylist()">Update Tape</button></div>
  </div>
</div>

<div class="pl-picker-overlay" id="plPickerOverlay" onclick="closePlPicker()"></div>
<div class="pl-picker" id="plPicker" style="display:none"></div>
<audio id="player"></audio>
<div class="toast" id="toast"></div>

<script>
let ALL_SONGS={songs_json};
let PLAYLISTS={playlists_json};
let queue=[...ALL_SONGS],currentIndex=0,isShuffle=false,isRepeat=false;
let activePlaylistId=null,activeTapeSongsId=null,pickerSong=null;
const player=document.getElementById('player');
const COLORS=['#e8003a','#ff6b35','#ffd600','#00e676','#00b0ff','#7c4dff','#f50057','#1de9b6','#ff4081','#76ff03'];
const metaCache={{}};
const isMobile=()=>window.innerWidth<=768;

// ── sheets ─────────────────────────────────────────────────────────────────
let currentSheet=null;
function openSheet(which){{
  if(currentSheet===which){{closeSheet();return;}}
  if(currentSheet){{
    document.getElementById('sheet'+cap(currentSheet)).classList.remove('open');
    document.getElementById('pill'+cap(currentSheet)).classList.remove('active');
  }}
  currentSheet=which;
  const sheetEl=document.getElementById('sheet'+cap(which));
  sheetEl.classList.add('open');
  document.getElementById('pill'+cap(which)).classList.add('active');
  document.getElementById('sheetOverlay').classList.add('show');
  // lazy meta fetch for this sheet's scroll container
  const scroll=sheetEl.querySelector('.sheet-scroll');
  if(scroll)fetchVisibleMeta(scroll);
}}
function closeSheet(){{
  if(!currentSheet)return;
  document.getElementById('sheet'+cap(currentSheet)).classList.remove('open');
  document.getElementById('pill'+cap(currentSheet)).classList.remove('active');
  document.getElementById('sheetOverlay').classList.remove('show');
  currentSheet=null;
}}
function cap(s){{return s.charAt(0).toUpperCase()+s.slice(1)}}

// ── toast ──────────────────────────────────────────────────────────────────
function toast(msg){{
  const t=document.getElementById('toast');
  t.textContent=msg;t.classList.add('show');
  clearTimeout(t._t);t._t=setTimeout(()=>t.classList.remove('show'),3000);
}}

// ── meta ───────────────────────────────────────────────────────────────────
async function fetchMeta(song){{
  if(metaCache[song])return metaCache[song];
  try{{
    const r=await fetch('/meta/'+song.split('/').map(encodeURIComponent).join('/'));
    const d=await r.json();metaCache[song]=d;return d;
  }}catch{{return null;}}
}}

function setNowPlaying(song){{
  const fname=song?song.split('/').pop().replace(/\.mp3$/i,''):'Select a Track';
  ['npTitle','npTitleD'].forEach(id=>{{const el=document.getElementById(id);if(el)el.textContent=fname;}});
  ['npArtist','npArtistD'].forEach(id=>{{const el=document.getElementById(id);if(el)el.innerHTML='&nbsp;';}});
  if(!song)return;
  fetchMeta(song).then(m=>{{
    if(!m)return;
    if(m.title)['npTitle','npTitleD'].forEach(id=>{{const el=document.getElementById(id);if(el)el.textContent=m.title;}});
    if(m.artist)['npArtist','npArtistD'].forEach(id=>{{const el=document.getElementById(id);if(el)el.textContent=m.artist;}});
  }});
}}

// ── song list rendering ─────────────────────────────────────────────────────
function buildSongItems(container,songs,activeIdx,onClick,lazyMeta){{
  container.innerHTML='';
  if(!songs.length){{
    container.innerHTML='<div style="padding:20px;font-size:12px;color:var(--muted);text-align:center">No tracks yet</div>';
    return;
  }}
  songs.forEach((song,i)=>{{
    const fname=song.split('/').pop().replace(/\.mp3$/i,'');
    const d=document.createElement('div');
    d.className='song-item'+(i===activeIdx?' active':'');
    d.dataset.idx=i;
    d.dataset.song=song;
    d.innerHTML=`<span class="song-num">${{String(i+1).padStart(2,'0')}}</span><div class="song-info"><div class="song-name">${{fname}}</div><div class="song-artist-small"></div></div><button class="add-to-pl-btn" onclick="openPlPicker(event,'${{song}}')">+</button>`;
    d.addEventListener('click',e=>{{if(!e.target.closest('.add-to-pl-btn'))onClick(i);}});
    container.appendChild(d);
    // only fetch meta if not lazy — lazy containers fetch on openSheet
    if(!lazyMeta){{
      fetchMeta(song).then(m=>{{
        if(!m)return;
        const el=container.querySelector(`[data-song="${{song}}"]`);
        if(!el)return;
        if(m.title)el.querySelector('.song-name').textContent=m.title;
        if(m.artist)el.querySelector('.song-artist-small').textContent=m.artist;
      }});
    }}
  }});
}}

// fetch meta for all items in a container in batches of 50
async function fetchVisibleMeta(container){{
  const els=[...container.querySelectorAll('.song-item[data-song]')].filter(el=>!el.dataset.metaLoaded);
  if(!els.length)return;
  els.forEach(el=>el.dataset.metaLoaded='1');
  const songs=els.map(el=>el.dataset.song);
  // process in batches of 50 to avoid overloading iSH
  for(let i=0;i<songs.length;i+=50){{
    const batch=songs.slice(i,i+50);
    try{{
      const r=await fetch('/meta/batch',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{songs:batch}})}});
      const data=await r.json();
      batch.forEach(song=>{{
        const m=data[song];
        if(!m)return;
        const el=container.querySelector(`[data-song="${{song}}"]`);
        if(!el)return;
        if(m.title)el.querySelector('.song-name').textContent=m.title;
        if(m.artist)el.querySelector('.song-artist-small').textContent=m.artist;
        metaCache[song]=m;
      }});
    }}catch(e){{console.error('batch meta fail',e);}}
  }}
}}

function renderSongList(){{
  // desktop — fetch meta eagerly (small list visible immediately)
  buildSongItems(document.getElementById('songScroll'),queue,currentIndex,i=>playSong(i),false);
  // tracks sheet — lazy, meta loaded when sheet opens
  buildSongItems(document.getElementById('tracksList'),ALL_SONGS,
    ALL_SONGS.indexOf(queue[currentIndex]),
    i=>{{queue=[...ALL_SONGS];currentIndex=i;activePlaylistId=null;playSong(i);closeSheet();}},
    true);
  const lbl=document.getElementById('queueLabel');
  const lblS=document.getElementById('tracksSheetLabel');
  const name=activePlaylistId&&PLAYLISTS[activePlaylistId]?'\u25b6 '+PLAYLISTS[activePlaylistId].name:'All Tracks';
  if(lbl)lbl.textContent=name;
  if(lblS)lblS.textContent='All Tracks ('+ALL_SONGS.length+')';
}}

function renderTapesSheet(){{
  const g=document.getElementById('tapesSheetList');
  const ids=Object.keys(PLAYLISTS);
  if(!ids.length){{g.innerHTML='<div class="empty-state"><p>No Tapes Yet</p><small style="font-size:11px">Use Archive or Folder to import</small></div>';return;}}
  g.innerHTML='';
  ids.forEach(id=>{{
    const pl=PLAYLISTS[id],color=pl.color||'#e8003a',count=(pl.songs||[]).length;
    const c=document.createElement('div');
    c.className='tape-card'+(activePlaylistId===id?' active-playlist':'');
    c.innerHTML=`<div class="tape-card-spine" style="background:${{color}}"></div><div class="tape-card-body"><div class="tape-card-info" onclick="openTapeSongs('${{id}}')"><div class="tape-card-name">${{pl.name}}</div><div class="tape-card-meta">${{count}} track${{count!==1?'s':''}} \u203a</div></div><div class="tape-card-actions"><div style="display:flex;gap:4px"><div class="reel-mini"></div><div class="reel-mini"></div></div><button class="tape-action-btn play" onclick="playPlaylist('${{id}}');event.stopPropagation()">\u25b6 Play</button><button class="tape-action-btn" onclick="openEditPlaylistModal('${{id}}');event.stopPropagation()">Edit</button><button class="tape-action-btn" onclick="deleteTape('${{id}}');event.stopPropagation()">\u00d7</button></div></div>`;
    g.appendChild(c);
  }});
}}

function renderPlaylists(){{
  // desktop right panel
  const g=document.getElementById('playlistsGrid');
  const ids=Object.keys(PLAYLISTS);
  if(!ids.length){{g.innerHTML='<div class="empty-state"><p>No Tapes Yet</p></div>';return;}}
  g.innerHTML='';
  ids.forEach(id=>{{
    const pl=PLAYLISTS[id],color=pl.color||'#e8003a',count=(pl.songs||[]).length;
    const c=document.createElement('div');
    c.className='tape-card'+(activePlaylistId===id?' active-playlist':'');
    c.innerHTML=`<div class="tape-card-spine" style="background:${{color}}"></div><div class="tape-card-body"><div class="tape-card-info"><div class="tape-card-name">${{pl.name}}</div><div class="tape-card-meta">${{count}} track${{count!==1?'s':''}}</div></div><div class="tape-card-actions"><div style="display:flex;gap:4px"><div class="reel-mini"></div><div class="reel-mini"></div></div><button class="tape-action-btn play" onclick="playPlaylist('${{id}}')">\u25b6 Play</button><button class="tape-action-btn" onclick="openEditPlaylistModal('${{id}}')">Edit</button><button class="tape-action-btn" onclick="deleteTape('${{id}}')">\u00d7</button></div></div>`;
    g.appendChild(c);
  }});
  renderTapesSheet();
}}

// ── tape songs sheet ─────────────────────────────────────────────────────────
function openTapeSongs(id){{
  activeTapeSongsId=id;
  const pl=PLAYLISTS[id];if(!pl)return;
  document.getElementById('songsSheetLabel').textContent=pl.name+' ('+( pl.songs||[]).length+')';
  document.getElementById('pillSongsLabel').textContent=pl.name;
  const scroll=document.getElementById('songsList');
  buildSongItems(scroll,pl.songs||[],
    activePlaylistId===id?currentIndex:-1,
    i=>{{playPlaylist(id,i);closeSheet();}},
    true);
  fetchVisibleMeta(scroll);
  openSheet('songs');
}}

// ── playback ────────────────────────────────────────────────────────────────
function playPlaylist(id,startIdx){{
  activePlaylistId=id;
  const pl=PLAYLISTS[id];
  if(!pl?.songs?.length)return;
  queue=[...pl.songs];
  currentIndex=startIdx||0;
  const parts=queue[currentIndex].split('/').map(encodeURIComponent).join('/');
  player.src='/songs/'+parts;player.play();
  setNowPlaying(queue[currentIndex]);
  renderSongList();renderPlaylists();
}}
function playSong(i){{
  currentIndex=i;
  const parts=queue[i].split('/').map(encodeURIComponent).join('/');
  player.src='/songs/'+parts;player.play();
  setNowPlaying(queue[i]);renderSongList();
}}
function togglePlay(){{if(player.paused){{if(!player.src||player.src===location.href)playSong(0);else player.play();}}else player.pause();}}
function nextSong(){{playSong(isShuffle?Math.floor(Math.random()*queue.length):(currentIndex+1)%queue.length);}}
function prevSong(){{if(player.currentTime>3){{player.currentTime=0;return;}}playSong((currentIndex-1+queue.length)%queue.length);}}
function toggleShuffle(){{isShuffle=!isShuffle;document.getElementById('shuffleBtn').classList.toggle('active-btn',isShuffle);}}
function toggleRepeat(){{isRepeat=!isRepeat;player.loop=isRepeat;document.getElementById('repeatBtn').classList.toggle('active-btn',isRepeat);}}

function setReelSpin(on){{
  ['reel1','reel2','dreel1','dreel2'].forEach(id=>{{
    const el=document.getElementById(id);
    if(el)el.classList.toggle('spinning',on);
  }});
  const pi=document.getElementById('playIcon');
  if(pi)pi.querySelector('path').setAttribute('d',on?'M6 19h4V5H6v14zm8-14v14h4V5h-4z':'M8 5v14l11-7z');
}}
player.addEventListener('play',()=>setReelSpin(true));
player.addEventListener('pause',()=>setReelSpin(false));
player.addEventListener('ended',()=>{{if(!isRepeat)nextSong();}});
player.addEventListener('timeupdate',()=>{{
  if(!player.duration)return;
  document.getElementById('progressFill').style.width=(player.currentTime/player.duration*100)+'%';
  document.getElementById('timeCurrent').textContent=fmt(player.currentTime);
  document.getElementById('timeDuration').textContent=fmt(player.duration);
}});
document.getElementById('progressBar').addEventListener('click',e=>{{
  if(!player.duration)return;
  const r=e.currentTarget.getBoundingClientRect();
  player.currentTime=((e.clientX-r.left)/r.width)*player.duration;
}});
function fmt(s){{const m=Math.floor(s/60),sec=Math.floor(s%60);return m+':'+(sec<10?'0':'')+sec;}}

async function saveToServer(){{
  await fetch('/playlists',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(PLAYLISTS)}});
}}

// ── upload helpers ──────────────────────────────────────────────────────────
async function uploadFiles(files,onProgress){{
  const saved=[],existing=[];let failed=0;
  for(let i=0;i<files.length;i++){{
    onProgress(i+1,files.length);
    try{{
      const fd=new FormData();fd.append('file',files[i]);
      const res=await fetch('/upload',{{method:'POST',body:fd}});
      const data=await res.json();
      (data.songs||[]).forEach(s=>{{if(!ALL_SONGS.includes(s))ALL_SONGS.push(s);if(!saved.includes(s))saved.push(s);}});
      (data.existing||[]).forEach(s=>{{if(!ALL_SONGS.includes(s))ALL_SONGS.push(s);if(!existing.includes(s))existing.push(s);}});
    }}catch{{failed++;}}
  }}
  return{{saved,existing,all:[...saved,...existing],failed}};
}}

function makeUploadHandler(fileSel,lblEl,isFolderMode,onDone){{
  document.getElementById(fileSel).addEventListener('change',async function(){{
    if(!this.files.length)return;
    const files=Array.from(this.files).filter(f=>f.name.toLowerCase().endsWith('.mp3'));
    if(!files.length){{if(isFolderMode)toast('No MP3s in folder');this.value='';return;}}
    const lbl=document.getElementById(lblEl);const orig=lbl.innerHTML;
    const{{saved,existing,all,failed}}=await uploadFiles(files,(i,n)=>{{lbl.textContent=i+'/'+n;}});
    lbl.innerHTML=orig;this.value='';
    onDone({{saved,existing,all,failed,files}});
  }});
}}

function handleTracksUploadDone({{saved,existing,failed}}){{
  queue=[...ALL_SONGS];renderSongList();
  if(saved.length&&existing.length)toast('Added '+saved.length+' new, '+existing.length+' existed');
  else if(saved.length)toast('Added '+saved.length+' track'+(saved.length!==1?'s':''));
  else if(existing.length)toast('All already in library');
  if(failed)toast(failed+' failed');
}}

async function handleFolderUploadDone({{saved,existing,all,failed,files}}){{
  let folderName='New Tape';
  const firstPath=(files[0].webkitRelativePath||files[0].name);
  if(firstPath.includes('/'))folderName=firstPath.split('/')[0];
  if(all.length){{
    const id='pl_'+Date.now();
    PLAYLISTS[id]={{name:folderName,songs:all,color:COLORS[Math.floor(Math.random()*COLORS.length)]}};
    await saveToServer();queue=[...ALL_SONGS];renderSongList();renderPlaylists();
    let msg='Tape "'+folderName+'" \u2014 '+all.length+' track'+(all.length!==1?'s':'');
    if(existing.length)msg+=' ('+existing.length+' existed)';
    toast(msg);
  }}else toast('No tracks uploaded');
}}

makeUploadHandler('fileInput','uploadLabel',false,handleTracksUploadDone);
makeUploadHandler('folderInput','folderLabel',true,handleFolderUploadDone);
makeUploadHandler('sFileInput','sUploadLabel',false,handleTracksUploadDone);
makeUploadHandler('sFolderInput','sFolderLabel',true,handleFolderUploadDone);

// ── Internet Archive ─────────────────────────────────────────────────────────
function openIAModal(){{
  document.getElementById('iaUrl').value='';
  document.getElementById('iaProgressWrap').classList.remove('show');
  document.getElementById('iaDoneMsg').style.display='none';
  document.getElementById('iaProgressMsg').textContent='';
  document.getElementById('iaProgressFill').style.width='0%';
  document.getElementById('iaStartBtn').disabled=false;
  document.getElementById('iaStartBtn').textContent='Download';
  document.getElementById('iaModal').classList.add('show');
}}
async function startIADownload(){{
  const url=document.getElementById('iaUrl').value.trim();
  if(!url){{toast('Paste an archive.org URL first');return;}}
  document.getElementById('iaStartBtn').disabled=true;
  document.getElementById('iaStartBtn').textContent='Downloading...';
  document.getElementById('iaProgressWrap').classList.add('show');
  document.getElementById('iaDoneMsg').style.display='none';
  const res=await fetch('/ia/start',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{url}})}});
  const data=await res.json();
  if(data.error){{toast(data.error);document.getElementById('iaStartBtn').disabled=false;document.getElementById('iaStartBtn').textContent='Download';return;}}
  const es=new EventSource('/ia/progress/'+data.job_id);
  es.onmessage=e=>{{
    const job=JSON.parse(e.data);
    document.getElementById('iaProgressMsg').textContent=job.msg||'';
    if(job.total>0){{
      const pct=Math.round((job.progress||0)/job.total*100);
      document.getElementById('iaProgressFill').style.width=pct+'%';
    }}
    if(job.done){{
      es.close();
      document.getElementById('iaStartBtn').disabled=false;
      document.getElementById('iaStartBtn').textContent='Download';
      if(!job.error&&job.songs&&job.songs.length){{
        job.songs.forEach(s=>{{if(!ALL_SONGS.includes(s))ALL_SONGS.push(s);}});
        fetch('/playlists_get').then(r=>r.json()).then(pls=>{{
          PLAYLISTS=pls;queue=[...ALL_SONGS];
          renderSongList();renderPlaylists();
          document.getElementById('iaProgressFill').style.width='100%';
          const dm=document.getElementById('iaDoneMsg');
          dm.textContent='\u2713 '+job.msg;dm.style.display='block';
          toast(job.msg||'Download complete');
        }});
      }}else if(job.error){{toast(job.msg||'Download failed');}}
    }}
  }};
  es.onerror=()=>{{es.close();document.getElementById('iaStartBtn').disabled=false;document.getElementById('iaStartBtn').textContent='Download';}};
}}

// ── playlist CRUD ────────────────────────────────────────────────────────────
function filterModalSongs(cId,query){{
  const q=(query||'').toLowerCase();
  document.querySelectorAll('#'+cId+' .modal-song-item').forEach(el=>{{
    el.style.display=el.querySelector('label').textContent.toLowerCase().includes(q)?'':'none';
  }});
}}
function openNewPlaylistModal(){{
  document.getElementById('newPlaylistName').value='';
  document.getElementById('newTrackSearch').value='';
  renderSwatches('tapeColors',null);renderModalSongs('modalSongList',[],'new');
  document.getElementById('newPlaylistModal').classList.add('show');
}}
function openEditPlaylistModal(id){{
  const pl=PLAYLISTS[id];
  document.getElementById('editPlaylistName').value=pl.name;
  document.getElementById('editTrackSearch').value='';
  renderSwatches('editTapeColors',pl.color);renderModalSongs('editModalSongList',pl.songs||[],'edit');
  document.getElementById('editPlaylistModal').dataset.editId=id;
  document.getElementById('editPlaylistModal').classList.add('show');
}}
function closeModal(id){{document.getElementById(id).classList.remove('show');}}

function renderSwatches(cId,sel){{
  const c=document.getElementById(cId);c.innerHTML='';
  COLORS.forEach(color=>{{
    const s=document.createElement('div');
    s.className='color-swatch'+(color===(sel||COLORS[0])?' selected':'');
    s.style.background=color;s.dataset.color=color;
    s.onclick=()=>{{c.querySelectorAll('.color-swatch').forEach(x=>x.classList.remove('selected'));s.classList.add('selected');}};
    c.appendChild(s);
  }});
}}
function renderModalSongs(cId,sel,pfx){{
  const c=document.getElementById(cId);c.innerHTML='';
  ALL_SONGS.forEach((song,i)=>{{
    const name=song.split('/').pop().replace(/\.mp3$/i,''),checked=sel.includes(song);
    const d=document.createElement('div');d.className='modal-song-item'+(checked?' selected':'');
    d.innerHTML=`<input type="checkbox" id="${{pfx}}_s${{i}}" ${{checked?'checked':''}}><label for="${{pfx}}_s${{i}}" style="cursor:pointer;flex:1">${{name}}</label>`;
    d.querySelector('input').addEventListener('change',e=>d.classList.toggle('selected',e.target.checked));
    c.appendChild(d);
  }});
}}
function getSelectedSongs(cId){{return Array.from(document.querySelectorAll('#'+cId+' input[type=checkbox]:checked')).map(cb=>ALL_SONGS[parseInt(cb.id.split('_s')[1])]);}}
function getSelectedColor(cId){{return document.querySelector('#'+cId+' .color-swatch.selected')?.dataset.color||COLORS[0];}}

function saveNewPlaylist(){{
  const id='pl_'+Date.now();
  PLAYLISTS[id]={{name:document.getElementById('newPlaylistName').value.trim()||'Untitled Tape',songs:getSelectedSongs('modalSongList'),color:getSelectedColor('tapeColors')}};
  saveToServer();closeModal('newPlaylistModal');renderPlaylists();
}}
function saveEditPlaylist(){{
  const id=document.getElementById('editPlaylistModal').dataset.editId;
  PLAYLISTS[id]={{name:document.getElementById('editPlaylistName').value.trim()||'Untitled Tape',songs:getSelectedSongs('editModalSongList'),color:getSelectedColor('editTapeColors')}};
  saveToServer();closeModal('editPlaylistModal');renderPlaylists();
  if(activeTapeSongsId===id)openTapeSongs(id);
}}
function deleteTape(id){{
  delete PLAYLISTS[id];
  if(activePlaylistId===id){{activePlaylistId=null;queue=[...ALL_SONGS];renderSongList();}}
  if(activeTapeSongsId===id){{
    activeTapeSongsId=null;
    if(currentSheet==='songs')closeSheet();
  }}
  saveToServer();renderPlaylists();
}}

// ── + button picker ──────────────────────────────────────────────────────────
function openPlPicker(e,song){{
  e.stopPropagation();pickerSong=song;
  const picker=document.getElementById('plPicker'),ids=Object.keys(PLAYLISTS);
  picker.innerHTML=ids.length?ids.map(id=>`<div class="pl-picker-item" onclick="addSongToPlaylist('${{id}}')"><div style="width:9px;height:9px;border-radius:50%;background:${{PLAYLISTS[id].color||'#e8003a'}};flex-shrink:0"></div>${{PLAYLISTS[id].name}}</div>`).join(''):'<div class="pl-picker-item" style="color:var(--muted)">No tapes yet</div>';
  const r=e.target.getBoundingClientRect();
  picker.style.top=Math.min(r.bottom+6,window.innerHeight-160)+'px';
  picker.style.left=Math.max(4,Math.min(r.left-160,window.innerWidth-210))+'px';
  picker.style.display='block';
  document.getElementById('plPickerOverlay').classList.add('show');
}}
function closePlPicker(){{document.getElementById('plPicker').style.display='none';document.getElementById('plPickerOverlay').classList.remove('show');pickerSong=null;}}
function addSongToPlaylist(id){{
  if(!pickerSong)return;
  if(!PLAYLISTS[id].songs)PLAYLISTS[id].songs=[];
  if(!PLAYLISTS[id].songs.includes(pickerSong)){{PLAYLISTS[id].songs.push(pickerSong);saveToServer();renderPlaylists();toast('Added to '+PLAYLISTS[id].name);}}
  closePlPicker();
}}

// ── show right cassette on desktop, mobile on mobile ────────────────────────
function applyLayout(){{
  const mob=window.innerWidth<=768;
  document.getElementById('desktopCassette').style.display=mob?'none':'';
  document.getElementById('mobileCassette').style.display=mob?'':'none';
}}
window.addEventListener('resize',applyLayout);
applyLayout();

renderSongList();renderPlaylists();
</script>
</body></html>"""

@app.route("/songs/<path:filename>")
def songs(filename):
    return send_from_directory(UPLOAD_FOLDER,filename,mimetype='audio/mpeg')

@app.route("/upload",methods=["POST"])
def upload():
    saved=[];existing=[]
    for file in request.files.getlist("file"):
        if not file.filename or not file.filename.lower().endswith(".mp3"): continue
        safe=os.path.basename(file.filename)
        if not safe: continue
        path=os.path.join(UPLOAD_FOLDER,safe)
        if os.path.exists(path): existing.append(safe)
        else: file.save(path);saved.append(safe)
    return jsonify({"ok":True,"songs":saved,"existing":existing})

@app.route("/playlists",methods=["POST"])
def update_playlists():
    save_playlists(request.get_json());return jsonify({"ok":True})

@app.route("/playlists_get")
def get_playlists():
    return jsonify(load_playlists())

@app.route("/meta/batch",methods=["POST"])
def meta_batch():
    songs=request.get_json().get("songs",[])
    result={}
    for song in songs[:50]:
        fp=os.path.join(UPLOAD_FOLDER,song)
        if os.path.exists(fp):
            artist,title=read_id3_text(fp)
            result[song]={"artist":artist,"title":title}
    return jsonify(result)

@app.route("/manifest.json")
def manifest():
    data={"name":"Mixtape","short_name":"Mixtape","start_url":"/","display":"standalone","background_color":"#0a0a0a","theme_color":"#e8003a","icons":[{"src":"/icon.png","sizes":"192x192","type":"image/png"},{"src":"/icon.png","sizes":"512x512","type":"image/png"}]}
    return jsonify(data)

@app.route("/sw.js")
def sw():
    if os.path.exists('sw.js'): return send_from_directory('.','sw.js')
    return app.response_class("self.addEventListener('fetch',e=>e.respondWith(fetch(e.request)))",mimetype='application/javascript')

@app.route("/icon.png")
def icon():
    if os.path.exists('icon.png'): return send_from_directory('.','icon.png')
    return '',404

log=logging.getLogger('werkzeug');log.setLevel(logging.ERROR)
try:
    s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);s.connect(("8.8.8.8",80));ip=s.getsockname()[0];s.close()
except:
    ip="localhost"
print(f"\n\033[1;31m  Mixtape \u2192 http://{ip}:8000\033[0m\n")
app.run(host="0.0.0.0",port=8000)