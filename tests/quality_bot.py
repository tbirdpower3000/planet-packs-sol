"""Quality-control bot: has the picture got worse?

Photographs worlds in two versions of the atlas under identical conditions (same clock, same camera view, same layer,
labels and markers hidden), measures each picture, and flags anything that got darker, blurrier, flatter or duller.
Writes a side-by-side image per world and layer and a report table.

  python tests/quality_bot.py --new . --old base            # compare this checkout with an older one
  python tests/quality_bot.py --new .                        # just measure (no comparison)

Exit code 1 if anything got clearly worse.
"""
import argparse, asyncio, json, os, subprocess, sys, time
from pathlib import Path
import numpy as np
from PIL import Image, ImageDraw

ap = argparse.ArgumentParser()
ap.add_argument('--new', default='.'); ap.add_argument('--old', default=None)
ap.add_argument('--worlds', default='earth,moon,mars,jupiter,saturn')
ap.add_argument('--clock', default='2026-03-20T11:00:00Z', help='the moment both versions are shown at (sunlit side faces the chosen view)')
ap.add_argument('--out', default='bot-output/quality')
a = ap.parse_args()
OUT = Path(a.out); OUT.mkdir(parents=True, exist_ok=True)
LOCAL_LIBS = os.environ.get('ATLAS_LOCAL_LIBS'); CHROME = os.environ.get('ATLAS_CHROME') or None
CLOCK_MS = int(np.datetime64(a.clock.rstrip('Z'), 'ms').astype('int64'))
# freeze the page's clock so both versions show the same date, time and lighting
FREEZE = f'''(()=>{{const T={CLOCK_MS},D=Date;window.Date=class extends D{{constructor(...x){{super(...(x.length?x:[T]))}}static now(){{return T}}}};}})();'''

def measure(img):
    a = np.asarray(img.convert('RGB'), float) / 255; g = a @ [.299, .587, .114]
    disk = g > .02                                      # the planet, not the black sky
    if disk.sum() < 500: return dict(cover=0, bright=0, contrast=0, sharp=0, colour=0)
    lap = np.abs(4 * g[1:-1, 1:-1] - g[:-2, 1:-1] - g[2:, 1:-1] - g[1:-1, :-2] - g[1:-1, 2:])
    inner = disk[1:-1, 1:-1] & disk[:-2, 1:-1] & disk[2:, 1:-1] & disk[1:-1, :-2] & disk[1:-1, 2:]
    rg, yb = a[..., 0] - a[..., 1], (a[..., 0] + a[..., 1]) / 2 - a[..., 2]
    return dict(cover=float(disk.mean()), bright=float(g[disk].mean()), contrast=float(g[disk].std()),
                sharp=float(lap[inner].mean() * 100), colour=float(np.hypot(rg[disk].std(), yb[disk].std())))

async def shoot(root, port, tag):
    from playwright.async_api import async_playwright
    srv = subprocess.Popen([sys.executable, '-m', 'http.server', str(port), '--bind', '127.0.0.1'], cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1); shots = {}
    try:
        async with async_playwright() as p:
            b = await p.chromium.launch(args=['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist'], executable_path=CHROME)
            for w in a.worlds.split(','):
                pg = await b.new_page(viewport={'width': 1100, 'height': 860}); await pg.add_init_script(FREEZE)
                if LOCAL_LIBS:
                    async def route(r):
                        u = r.request.url
                        for k in ('three.min.js', 'OrbitControls.js'):
                            if u.endswith(k): return await r.fulfill(path=os.path.join(LOCAL_LIBS, k), content_type='application/javascript')
                        return await (r.continue_() if '127.0.0.1' in u else r.abort())
                    await pg.route('**/*', route)
                try:
                    await pg.goto(f'http://127.0.0.1:{port}/index.html#{w}')
                    await pg.wait_for_function(f'document.getElementById("busy").hidden&&document.getElementById("bName")&&document.getElementById("bName").textContent.toLowerCase()==={json.dumps(w)}', timeout=60000)
                except Exception as x:
                    print(f'{tag} {w}: did not open ({x})', flush=True); await pg.close(); continue
                # pause the clock if the page has a pause button, hide every label, grid and marker, face the sunlit side
                await pg.evaluate('''()=>{const z=document.querySelector('#warp button[data-w="0"]');if(z)z.click();
                  for(const id of ['gridT','featT','namesT','axisT'])(e=>{if(e&&e.checked)e.click();})(document.getElementById(id));
                  document.querySelectorAll('#catKeys input').forEach(i=>{if(i.checked)i.click();});
                  const s=document.createElement('style');s.textContent='#labels,#toMap,#cap,#busy,#handMsg{display:none!important}';document.head.appendChild(s);
                  document.getElementById('stage').scrollIntoView({block:'center'});}''')
                await pg.wait_for_timeout(500)
                start = await pg.evaluate("(document.querySelector('#layerSeg button[aria-pressed=true]')||{}).dataset?.id||null")
                layers = await pg.evaluate("[...document.querySelectorAll('#layerSeg button')].map(b=>[b.dataset.id,b.textContent.trim()])")
                # the view whose middle is nearest the sunlit point at this clock
                vb = await pg.evaluate("document.querySelectorAll('#viewBtns button').length")
                if vb: await pg.evaluate(f"document.querySelectorAll('#viewBtns button')[{1 if vb > 1 else 0}].click()")
                await pg.wait_for_timeout(2600)
                for lid, label in layers:
                    await pg.evaluate(f"document.querySelector('#layerSeg button[data-id=\"{lid}\"]').click()")
                    await pg.wait_for_timeout(1800)
                    f = OUT / f'{tag}-{w}-{lid}.png'; await (await pg.query_selector('#globe canvas')).screenshot(path=str(f))
                    shots[(w, lid)] = dict(file=f, label=label, start=lid == start)
                print(f'{tag} {w}: {len(layers)} layers, opens on {start}', flush=True)
                await pg.close()
            await b.close()
    finally: srv.terminate()
    return shots

def side(old, new, f, title):
    A, B = Image.open(old).convert('RGB'), Image.open(new).convert('RGB'); h = 360
    A = A.resize((int(A.width * h / A.height), h)); B = B.resize((int(B.width * h / B.height), h))
    c = Image.new('RGB', (A.width + B.width + 10, h + 26), (20, 20, 24)); c.paste(A, (0, 26)); c.paste(B, (A.width + 10, 26))
    d = ImageDraw.Draw(c); d.text((6, 6), 'before: ' + title, fill=(220, 220, 220)); d.text((A.width + 16, 6), 'now', fill=(220, 220, 220)); c.save(f)

# how much worse counts as a problem
LIMITS = dict(bright=.80, contrast=.80, sharp=.80, colour=.75)
NAMES = dict(bright='darker', contrast='flatter', sharp='blurrier', colour='duller')

def main():
    new = asyncio.run(shoot(a.new, 8791, 'now'))
    old = asyncio.run(shoot(a.old, 8792, 'before')) if a.old else {}
    rows, worse = [], []
    for (w, lid), n in sorted(new.items()):
        mn = measure(Image.open(n['file'])); o = old.get((w, lid)); mo = measure(Image.open(o['file'])) if o else None
        flags = []
        if mo and mo['cover'] > .05:
            for k, lim in LIMITS.items():
                if mo[k] > 1e-6 and mn[k] / mo[k] < lim: flags.append(f"{NAMES[k]} ({mn[k] / mo[k] * 100 - 100:+.0f}%)")
            side(o['file'], n['file'], OUT / f'compare-{w}-{lid}.png', f"{w} · {n['label']}")
        if flags: worse.append(f"{w.title()} · {n['label']}: " + ', '.join(flags))
        rows.append((w.title(), n['label'] + (' (opens here)' if n['start'] else ''), mn, mo, flags))
    # what each world opens on, before and now
    opens = []
    for w in a.worlds.split(','):
        sn = next((v['label'] for (ww, _), v in new.items() if ww == w and v['start']), None)
        so = next((v['label'] for (ww, _), v in old.items() if ww == w and v['start']), None)
        if old and sn != so: opens.append(f'{w.title()} now opens on **{sn}** (before: {so}).')
    f = lambda m, k: f'{m[k]:.3f}' if m else '–'
    L = ['# Quality-control bot', '', f'Clock frozen at {a.clock}; labels, grids and markers hidden; same camera view in both versions.', '',
         '**' + (f'{len(worse)} picture(s) got worse.' if worse else ('Nothing got worse.' if old else 'Measured (no older version to compare with).')) + '**', '']
    if worse: L += ['## Got worse', ''] + [f'- {x}' for x in worse] + ['']
    if opens: L += ['## Opening view changed', ''] + [f'- {x}' for x in opens] + ['']
    L += ['## Measurements (now / before)', '', '| World | Layer | Brightness | Contrast | Sharpness | Colour | Flags |', '|---|---|---|---|---|---|---|']
    for w, l, mn, mo, fl in rows:
        L.append(f"| {w} | {l} | {f(mn,'bright')} / {f(mo,'bright')} | {f(mn,'contrast')} / {f(mo,'contrast')} | {f(mn,'sharp')} / {f(mo,'sharp')} | {f(mn,'colour')} / {f(mo,'colour')} | {'; '.join(fl) or 'ok'} |")
    text = '\n'.join(L) + '\n'; (OUT / 'report.md').write_text(text)
    if os.environ.get('GITHUB_STEP_SUMMARY'): open(os.environ['GITHUB_STEP_SUMMARY'], 'a').write(text)
    print(text); sys.exit(1 if worse else 0)

main()
