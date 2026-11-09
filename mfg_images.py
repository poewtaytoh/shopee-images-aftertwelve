#!/usr/bin/env python3
"""
mfg_images.py
Downloads product photos from MFG Dropship product pages, converts them to
Shopee-ready JPG (max 2 MB), names them predictably, and writes a manifest that
contains the public link of every image (for GitHub Pages).

SETUP (once, in Terminal):
    pip3 install pillow

USAGE:
    1. Put product URLs in a text file, one per line (urls.txt).
    2. Run:
         python3 mfg_images.py urls.txt --base https://YOURNAME.github.io/shopee-images
    3. Upload the "docs" folder to your GitHub repository (GitHub Desktop: commit + push).
       In the repo: Settings > Pages > Deploy from branch > main, folder /docs.

OUTPUT (inside docs/):
    images/<product id>/<product id>_01.jpg      01 = cover, 02..09 = item images 1..8
    images/<product id>/<product id>_v_<color>.jpg   photo of a color (only if not in the 9)
    images/<product id>/manifest.csv             one product
    manifest.csv                                 all products (paste into your Sheet)

Files that already exist are skipped, so you can re-run it after adding new URLs.
"""
import argparse
import csv
import html as htmllib
import io
import os
import re
import sys
import time
import urllib.request
from collections import Counter

from PIL import Image

UA = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 '
      '(KHTML, like Gecko) Version/17.0 Safari/605.1.15')
IMG_RE = re.compile(
    r'https?://[^\s"\'<>()]+/wholesale/production/product/[^\s"\'<>()]+?\.(?:webp|jpe?g|png)', re.I)
STOCK_RE = re.compile(r'Stock\s+Tersedia\s*:?\s*(\d+)', re.I)
SOLD_RE = re.compile(r'Sold\s*Out', re.I)  # sold-out colors show this instead of a stock line


def fetch(url):
    req = urllib.request.Request(url, headers={'User-Agent': UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def meta(page, prop):
    esc = re.escape(prop)
    m = (re.search(r'<meta[^>]+(?:property|name)=["\']' + esc + r'["\'][^>]*content=["\']([^"\']*)["\']', page, re.I)
         or re.search(r'<meta[^>]+content=["\']([^"\']*)["\'][^>]*(?:property|name)=["\']' + esc + r'["\']', page, re.I))
    return htmllib.unescape(m.group(1)) if m else None


def slug(text):
    return re.sub(r'[^a-z0-9]+', '-', text.lower()).strip('-') or 'x'


def folder_of(url):
    return url.rsplit('/', 1)[0]


def parse_page(page, url):
    """Returns product id, name, gallery image urls (in order), variants [(color, image url)]."""
    pid = meta(page, 'product:retailer_item_id') or slug(url.rstrip('/').rsplit('/', 1)[-1])
    name = meta(page, 'og:title') or pid

    matches = [(m.start(), m.end(), m.group(0)) for m in IMG_RE.finditer(page)]
    if not matches:
        raise RuntimeError('no product images found on the page')

    # The product's own folder = the folder most images live in (ignores "related products")
    main_folder = Counter(folder_of(u) for _, _, u in matches).most_common(1)[0][0]
    own = [(s, e, u) for s, e, u in matches if folder_of(u) == main_folder]

    gallery = []
    for _, _, u in own:
        if u not in gallery:
            gallery.append(u)

    # Variants: each "Stock Tersedia: N" is preceded by the color's image and name
    variants = []
    markers = sorted(list(STOCK_RE.finditer(page)) + list(SOLD_RE.finditer(page)), key=lambda m: m.start())
    for sm in markers:
        before = [x for x in own if x[1] <= sm.start()]
        if not before:
            continue
        img_end, img_url = before[-1][1], before[-1][2]
        seg = re.sub(r'<[^>]+>', '\n', page[img_end:sm.start()])
        lines = [re.sub(r'\s+', ' ', htmllib.unescape(l)).strip() for l in seg.split('\n')]
        lines = [l for l in lines if l]
        if not lines:
            continue
        if SOLD_RE.match(sm.group(0)) and not STOCK_RE.match(sm.group(0)):
            if len(lines) > 3 or len(lines[-1]) > 40:  # "Sold Out" text somewhere unrelated
                continue
        variants.append((lines[-1], img_url))
    return pid, name, gallery, variants


def to_jpeg(data, dest, max_side=2000, max_bytes=1_900_000):
    im = Image.open(io.BytesIO(data))
    if im.mode in ('RGBA', 'LA') or (im.mode == 'P' and 'transparency' in im.info):
        im = im.convert('RGBA')
        bg = Image.new('RGB', im.size, (255, 255, 255))
        bg.paste(im, mask=im.split()[3])
        im = bg
    else:
        im = im.convert('RGB')
    if max(im.size) > max_side:
        im.thumbnail((max_side, max_side), Image.LANCZOS)
    buf = io.BytesIO()
    for q in (90, 85, 80, 75, 70, 60):
        buf = io.BytesIO()
        im.save(buf, 'JPEG', quality=q, optimize=True)
        if buf.tell() <= max_bytes:
            break
    with open(dest, 'wb') as f:
        f.write(buf.getvalue())
    return im.size, buf.tell()


def process_product(url, out_dir, base, max_images, delay):
    page = fetch(url).decode('utf-8', errors='replace')
    pid, name, gallery, variants = parse_page(page, url)

    pdir = os.path.join(out_dir, 'images', pid)
    os.makedirs(pdir, exist_ok=True)

    selected = gallery[:max_images]
    filenames = {}  # source url -> file name
    rows = []       # role, color, filename, source
    for i, u in enumerate(selected, start=1):
        fn = '%s_%02d.jpg' % (pid, i)
        filenames[u] = fn
        rows.append(('cover' if i == 1 else 'item_%d' % (i - 1), '', fn, u))
    for color, u in variants:
        if u not in filenames:
            filenames[u] = '%s_v_%s.jpg' % (pid, slug(color))
        rows.append(('variant', color, filenames[u], u))

    done = set()
    new_files = 0
    for _, _, fn, u in rows:
        if fn in done:
            continue
        done.add(fn)
        dest = os.path.join(pdir, fn)
        if os.path.exists(dest):
            continue
        size, nbytes = to_jpeg(fetch(u), dest)
        new_files += 1
        print('    %s  %dx%d  %d KB' % (fn, size[0], size[1], nbytes // 1024))
        time.sleep(delay)

    with open(os.path.join(pdir, 'manifest.csv'), 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['product_id', 'product_name', 'role', 'color', 'filename', 'public_url', 'source_url'])
        for role, color, fn, u in rows:
            pub = '%s/images/%s/%s' % (base.rstrip('/'), pid, fn) if base else ''
            w.writerow([pid, name, role, color, fn, pub, u])
    return pid, name, len(selected), len(variants), new_files


def build_master(out_dir):
    master = os.path.join(out_dir, 'manifest.csv')
    img_root = os.path.join(out_dir, 'images')
    rows = []
    for pid in sorted(os.listdir(img_root)):
        p = os.path.join(img_root, pid, 'manifest.csv')
        if os.path.isfile(p):
            with open(p, newline='', encoding='utf-8') as f:
                r = list(csv.reader(f))
            header, rows = r[0], rows + r[1:]
    with open(master, 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    return master, len(rows)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('urls_file', help='text file with one product URL per line')
    ap.add_argument('--base', default='', help='public base URL, e.g. https://name.github.io/shopee-images')
    ap.add_argument('--out', default='docs', help='output folder (default: docs)')
    ap.add_argument('--max-images', type=int, default=9, help='cover + item images per product (Shopee max 9)')
    ap.add_argument('--delay', type=float, default=0.5, help='seconds between downloads')
    args = ap.parse_args()

    with open(args.urls_file, encoding='utf-8') as f:
        urls = [l.strip() for l in f if l.strip().startswith('http')]
    if not urls:
        sys.exit('No URLs found in %s' % args.urls_file)
    if not args.base:
        print('Note: no --base given, so the manifest will have empty public_url columns.\n')

    ok = failed = 0
    for n, url in enumerate(urls, start=1):
        print('[%d/%d] %s' % (n, len(urls), url))
        try:
            pid, name, ng, nv, nf = process_product(url, args.out, args.base, args.max_images, args.delay)
            print('  OK  %s (%s): %d gallery images, %d color photos, %d new files' % (pid, name, ng, nv, nf))
            ok += 1
        except Exception as e:  # keep going with the other products
            print('  FAILED: %s' % e)
            failed += 1
        time.sleep(args.delay)

    master, count = build_master(args.out)
    print('\nDone. %d ok, %d failed. Manifest: %s (%d rows)' % (ok, failed, master, count))


if __name__ == '__main__':
    main()
