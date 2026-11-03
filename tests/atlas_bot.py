"""Atlas test bot.

Runs on GitHub (see .github/workflows/atlas-bot.yml) and writes a report to the job summary:
  1. Planet packs: every file in index.json opens, its grids and images decode, sizes.
  2. The page: loads, switches between the Solar System view and the globe with the buttons and double-click,
     checks that scrolling never switches views, that the two views agree on the camera at every switch,
     that the page doesn't jump, and that there are no script errors. Screenshots are saved.
  3. Live data: whether OpenSky (flights) and CelesTrak (satellites) answer, and whether they let a web page
     read the answer (the CORS header). This part is a report only; it never fails the run.
Exit code 1 if part 1 or 2 finds a problem.
"""
import asyncio, base64, json, os, subprocess, sys, time, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / 'bot-output'; OUT.mkdir(exist_ok=True)
PORT = 8765
LOCAL_LIBS = os.environ.get('ATLAS_LOCAL_LIBS')   # for running offline: a folder with three.min.js and OrbitControls.js
ORIGIN = 'https://tbirdpower3000.github.io'
problems, notes, rows = [], [], []

def problem(msg): problems.append(msg); print('PROBLEM:', msg, flush=True)

# ---------------------------------------------------------------- 1. packs
def check_packs():
    ix = json.loads((ROOT / 'index.json').read_text())
    total = 0
    for e in ix:
        f = ROOT / Path(e['file']).name
        if not f.exists(): problem(f"{e['id']}: file {f.name} is missing"); continue
        mb = f.stat().st_size / 1e6; total += mb
        try: d = json.loads(f.read_text())
        except Exception as x: problem(f"{e['id']}: not valid JSON ({x})"); continue
        bad = []
        for k in ('id', 'name', 'radiusKm'):
            if k not in d: bad.append(f'no {k}')
        for g in d.get('grids', []):
            r, c = g.get('rows'), g.get('cols')
            try:
                n = len(base64.b64decode(g.get('b64') or g.get('data') or ''))
                if not r or not c or c != 2 * (r - 1) or n != r * c * 2: bad.append(f"grid {g.get('id')} is {r}x{c} with {n} bytes")
            except Exception as x: bad.append(f"grid {g.get('id')} won't decode ({x})")
        for im in d.get('images', []):
            s = im.get('src', '')
            if not s.startswith('data:image/') or len(s) < 1000: bad.append(f"image {im.get('id')} looks empty")
        layers = len(d.get('grids', [])) + len(d.get('images', []))
        if not layers: bad.append('no layers')
        if bad: problem(f"{e['id']}: " + '; '.join(bad))
        rows.append((e['name'], f'{mb:.1f} MB', layers, 'ok' if not bad else 'problem'))
    notes.append(f'{len(ix)} packs, {total:.0f} MB in all.')

# ---------------------------------------------------------------- 2. the page
REC = '''window.BOT={log:[],last:null,n:{}};
(function rec(){requestAnimationFrame(rec);const V=window.atlasViews;if(!V||!V.map||!window.atlasMapDbg)return;
 const s=window.atlasDive(),g=V.globe.cam,m=V.map.cam,gl=g.position.length(),ang=s==='map'?g.quaternion.angleTo(m.quaternion)*57.2958:0;
 const bx=document.getElementById(s==='landed'?'stage':'mapStage').getBoundingClientRect(),px=bx.height/2/Math.tan(g.fov*Math.PI/360)/gl;
 const e={s,gl,px,cy:bx.top+bx.height/2};const L=BOT.last;
 if(L&&L.s!==e.s)BOT.log.push({from:L.s,to:e.s,px0:L.px,px1:e.px,cy0:L.cy,cy1:e.cy,ang:e.ang});
 if(s==='map'&&ang>.5){BOT.n.ang=(BOT.n.ang||0)+1;}
 BOT.last=e;})();'''

async def page_bot():
    from playwright.async_api import async_playwright
    srv = subprocess.Popen([sys.executable, '-m', 'http.server', str(PORT), '--bind', '127.0.0.1'], cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1)
    try:
        async with async_playwright() as p:
            b = await p.chromium.launch(args=['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist'],
                                        executable_path=os.environ.get('ATLAS_CHROME') or None)
            pg = await b.new_page(viewport={'width': 1100, 'height': 800})
            if LOCAL_LIBS:
                async def route(r):
                    u = r.request.url
                    for k in ('three.min.js', 'OrbitControls.js'):
                        if u.endswith(k): return await r.fulfill(path=os.path.join(LOCAL_LIBS, k), content_type='application/javascript')
                    return await (r.continue_() if '127.0.0.1' in u else r.abort())
                await pg.route('**/*', route)
            errs = []; pg.on('pageerror', lambda e: errs.append(str(e)))
            t0 = time.time()
            await pg.goto(f'http://127.0.0.1:{PORT}/index.html')
            await pg.wait_for_function('document.getElementById("busy").hidden', timeout=90000)
            notes.append(f'Page ready in {time.time() - t0:.1f} s (software graphics, so slower than a real computer).')
            await pg.evaluate('document.getElementById("mapStage").scrollIntoView({block:"start"})'); await pg.evaluate(REC)
            ms = await pg.query_selector('#mapStage'); bx = await ms.bounding_box(); cx, cy = bx['x'] + bx['width'] / 2, bx['y'] + bx['height'] / 2
            st = lambda: pg.evaluate('window.atlasDive()')
            async def until(want, secs=20):
                t = time.time()
                while time.time() - t < secs:
                    if await st() == want: return round(time.time() - t, 1)
                    await pg.wait_for_timeout(200)
                return None
            async def step(name, ok, detail=''):
                print(('ok   ' if ok else 'FAIL ') + name, detail, flush=True)
                if not ok: problem(f'{name} {detail}'.strip())
            async def label(n): await pg.evaluate(f'[...document.querySelectorAll("#mapLabels span")].find(s=>s.textContent==={json.dumps(n)}).click()')
            await label('Mars'); t = await until('map'); await step('Click Mars: its globe is drawn in the Solar System view', t is not None, f'({t} s)')
            await pg.click('#mapGo'); t = await until('landed'); await step('Land on globe switches to the globe', t is not None, f'({t} s)')
            await (await pg.query_selector('#stage')).screenshot(path=str(OUT / 'mars-landed.png'))
            sb = await (await pg.query_selector('#stage')).bounding_box(); await pg.mouse.move(sb['x'] + sb['width'] / 2, sb['y'] + sb['height'] / 2)
            for _ in range(20): await pg.mouse.wheel(0, 400); await pg.wait_for_timeout(120)
            await step('Scrolling out on the globe stays on the globe', await st() == 'landed')
            await pg.click('#toMap'); t = await until('map'); await step('↑ Solar System switches back', t is not None, f'({t} s)')
            await pg.mouse.move(cx, cy)
            for _ in range(30): await pg.mouse.wheel(0, -300); await pg.wait_for_timeout(120)
            await step('Scrolling in on the Solar System view stays there', await st() == 'map')
            await pg.evaluate('document.querySelector("#bodies button[data-id=earth]").click()')
            await pg.evaluate('document.getElementById("mapStage").scrollIntoView({block:"start"})')
            t = await until('map', 30); await step('Choosing Earth from the list shows it in the Solar System view', t is not None, f'({t} s)')
            bx = await ms.bounding_box(); await pg.mouse.dblclick(bx['x'] + bx['width'] / 2, bx['y'] + bx['height'] / 2)
            t = await until('landed', 30); await step('Double-click Earth lands on it', t is not None, f'({t} s)')
            await (await pg.query_selector('#stage')).screenshot(path=str(OUT / 'earth-landed.png'))
            # Earth: live switches present, real-colour start layer
            await step('Earth shows the live-data switches', await pg.evaluate('!document.getElementById("liveT").hidden'))
            cap = await pg.inner_text('#cap'); await step('Earth opens on its colour imagery', 'Visible colour' in cap, f'({cap})')
            log = await pg.evaluate('BOT.log'); n = await pg.evaluate('BOT.n')
            for sw in log:
                if sw['from'] == 'map' and sw['to'] == 'landed' or sw['from'] == 'landed' and sw['to'] == 'map':
                    size = abs(sw['px1'] / max(1e-6, sw['px0']) - 1); move = abs(sw['cy1'] - sw['cy0'])
                    await step(f"Switch {sw['from']} → {sw['to']}: planet keeps its size and place", size < .08 and move < 4, f'(size change {size * 100:.0f}%, moved {move:.0f} px)')
            await step('The two views always agree on the camera angle', not n.get('ang'), f"({n.get('ang', 0)} frames off)")
            await step('No script errors', not errs, '; '.join(errs[:3]))
            await b.close()
    finally:
        srv.terminate()

# ---------------------------------------------------------------- 3. live data
def probe(name, url):
    req = urllib.request.Request(url, headers={'Origin': ORIGIN, 'User-Agent': 'planet-packs-sol atlas bot'})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            acao = r.headers.get('Access-Control-Allow-Origin'); return (name, r.status, acao or 'none')
    except urllib.error.HTTPError as x: return (name, x.code, x.headers.get('Access-Control-Allow-Origin') or 'none')
    except Exception as x: return (name, 'no answer', str(x)[:60])

def live():
    out = [probe('OpenSky flights', 'https://opensky-network.org/api/states/all?lamin=45.8&lomin=5.9&lamax=47.8&lomax=10.5'),   # a small box: 1 credit
           probe('CelesTrak satellites', 'https://celestrak.org/NORAD/elements/gp.php?CATNR=25544&FORMAT=TLE')]                    # one satellite, the ISS
    return out

def report(live_rows):
    lines = ['# Atlas test bot', '', '**' + ('All checks passed.' if not problems else f'{len(problems)} problem(s) found.') + '**', '']
    if problems: lines += ['## Problems', ''] + [f'- {p}' for p in problems] + ['']
    lines += ['## Notes', ''] + [f'- {n}' for n in notes] + ['']
    lines += ['## Live data (a web page can read it only if the CORS header allows it)', '', '| Source | Answer | CORS header |', '|---|---|---|']
    lines += [f'| {a} | {b} | {c} |' for a, b, c in live_rows]
    lines += ['', '## Planet packs', '', '| World | Size | Layers | Check |', '|---|---|---|---|'] + [f'| {a} | {b} | {c} | {d} |' for a, b, c, d in rows]
    text = '\n'.join(lines) + '\n'
    (OUT / 'report.md').write_text(text)
    if os.environ.get('GITHUB_STEP_SUMMARY'): open(os.environ['GITHUB_STEP_SUMMARY'], 'a').write(text)
    print(text)

if __name__ == '__main__':
    check_packs()
    try: asyncio.run(page_bot())
    except Exception as x: problem(f'The page test stopped early: {x}')
    lr = live() if not os.environ.get('ATLAS_SKIP_LIVE') else [('live checks', 'skipped', '')]
    report(lr)
    sys.exit(1 if problems else 0)
