#!/usr/bin/env python3

import base64
import collections
import io
import itertools
import json
import logging
import random
import unittest
import unittest.mock

import requests

from PyPDF2 import PdfFileMerger
from flask import Flask, Response, request

__doc__ = '''
Generates a file that can be used for learning how to write a specific
Chinese character, when printed.

For a description of the deployment, look at the Dockerfile.

Requires downloading the following files:

    https://raw.githubusercontent.com/skishore/makemeahanzi/master/graphics.txt
    https://raw.githubusercontent.com/skishore/makemeahanzi/master/dictionary.txt
'''

__TODO__ = '''
*** TODO ***

Bugs:

    * figure out zooming in previews, get rid of two preview modes

UX:

    * add print margins?
    * progress reporting?
    * more options: how many repetitions of each stroke, how many characters
      does it take to switch to random mode?
    * custom titles
    * make frontend cuter

Learning:

    * repetition mode: show two strokes at once?
    * group using this pattern: A, B, AB, C, D, CD, ABCD, ...

Testing and refactoring:

    * make all html views validate
    * better error reporting
    * selenium tests
    * unit tests

------------------------------------------------------------------------------
'''

app = Flask(__name__)


PAGE_SIZE = (200, 300)
LOGGER = logging.getLogger(__name__)
CHUNK_SIZE = 4
LINE_THICK = 30


def load_strokes_db(graphics_txt_path):
    ret = {}
    with open(graphics_txt_path, 'r', encoding='utf8') as f:
        for line in f:
            x = json.loads(line)
            ret[x['character']] = x['strokes']
    return ret


def load_dictionary(dictionary_txt_path):
    d = {}
    with open(dictionary_txt_path, 'r') as f:
        for line in f:
            j = json.loads(line)
            if j['pinyin']:
                d[j['character']] = j['pinyin'][0]
    return d


STROKES_DB = load_strokes_db('graphics.txt')
PINYIN_DB = load_dictionary('dictionary.txt')


class Tile:
    """Class responsible for preparing SVG code describing a single tine.

    It's used for storage of data related to rendering - i.e. group it belongs
    to. I could probably get away with dict + function but it'd be uglier.

    Also, set_dimensions had to be deferred because gen_images_iter can't know
    it."""

    SVG_HEADER = '''<svg x="%(x)d" y="%(y)d" width="%(size)d"
        height="%(size)d"><svg version="1.1" viewBox="0 0 1024 1024"
        xmlns="http://www.w3.org/2000/svg">'''

    PREAMBLE = '''
        <g stroke="black" stroke-width="2" transform="scale(4, 4)">
            <line x1="0" y1="0" x2="0" y2="256"
                stroke-width="%(leftline_width)d"></line>
            <line x1="0" y1="0" x2="256" y2="256"></line>
            <line x1="256" y1="0" x2="0" y2="256"></line>
            <line x1="256" y1="0" x2="0" y2="0"
                stroke-width="%(topline_width)d"></line>
            <line x1="256" y1="0" x2="256" y2="256"></line>
            <line x1="128" y1="0" x2="128" y2="256"></line>
            <line x1="0" y1="128" x2="256" y2="128"></line>
            <line x1="0" y1="256" x2="256" y2="256"></line>
        </g>
        <g transform="scale(1, -1) translate(0, -900)">
    '''

    PATH_TPL = '''<path d="%s" stroke="black" stroke-width="%d"
        fill="white"></path>'''

    FOOTER = '''</g></svg></svg>'''

    def __init__(self, C, chunk, strokes, img_num, skip_strokes, stop_at,
                 add_pinyin=True, skip_in_header=False):

        self.C = C
        self.chunk = chunk
        self.strokes = strokes
        self.img_num = img_num
        self.skip_strokes = skip_strokes
        self.stop_at = stop_at
        self.add_pinyin = add_pinyin
        self.skip_in_header = skip_in_header
        self.x = None
        self.y = None
        self.size = None
        self.leftline_width = 10
        self.topline_width = 10

    def set_dimensions(self, x, y, size):
        self.x = x
        self.y = y
        self.size = size

    def render(self):

        if not all([self.size, self.y, self.size]):
            raise RuntimeError("Call set_dimensions first!")

        add_text = PINYIN_DB[self.C] if self.add_pinyin else ''
        add_text_svg = ('''<text x="50" y="950"
            font-size="300px">%s</text>''' % add_text)
        header_args = {'x': self.x, 'y': self.y, 'size': self.size}
        preamble_args = {'leftline_width': self.leftline_width,
                         'topline_width': self.topline_width}

        with io.StringIO() as f:

            f.write(''.join([self.SVG_HEADER % header_args, add_text_svg,
                             self.PREAMBLE % preamble_args]))
            for n, stroke in enumerate(self.strokes):
                if n < self.skip_strokes or n >= self.stop_at:
                    continue
                # IMHO this can safely be hardcoded because it's relative
                # to this image
                line_size = (20 if n - 1 < self.img_num else 10)
                f.write(self.PATH_TPL % (stroke, line_size))
            f.write(self.FOOTER)
            return f.getvalue()


def grouper(iterable, n):
    while True:
        yield itertools.chain([next(iterable)],
                              itertools.islice(iterable, n-1))


def gen_images(input_characters, num_repeats):
    """This is where the learning logic sits.

    We iterate over input_characters grouped in groups and by each stroke of
    each character, generating num_repeats of titles of various types. At
    the end, we optionally randomly ask the user to write those based on
    pinyin."""

    # if we're not really repeating characters, chunks are meaningless
    if num_repeats == 0:
        chunk_size = 1
    else:
        chunk_size = CHUNK_SIZE

    for chunk_iter in grouper(iter(input_characters), chunk_size):
        chunk = list(chunk_iter)
        for C in chunk:
            strokes = STROKES_DB[C]
            for n in range(len(strokes)):

                if num_repeats == 0:
                    yield Tile(C, chunk, strokes, n, 0, 99, False)
                    continue

                # draw n-th stroke alone
                for j in range(num_repeats):
                    # we only want to print pinyin on first repetition of first
                    # stroke - this is when it's most likely its text won't
                    # overlap at the bottom of the tile
                    add_text = n == 0 and j == 0
                    yield Tile(C, chunk, strokes, n, 0, n + 1, add_text)

                # whole character, highlight n-th stroke
                for _ in range(num_repeats):
                    yield Tile(C, chunk, strokes, n, 0, 99, False)

                # draw n-th stroke into context
                for _ in range(num_repeats):
                    yield Tile(C, chunk, strokes, n, n + 1, 99, False)

        repeats = chunk * num_repeats * 2
        random.shuffle(repeats)
        for C in repeats:
            yield Tile(C, chunk, [], 0, 0, 0, skip_in_header=True)


class Header:
    """The responsibility of this class is to manage the header - i.e. make
    sure it's split into two lines."""

    def __init__(self):
        self.header = ''
        self.chars_drawn = []

    def observe_char(self, C):
        if C in self.chars_drawn:
            return
        self.chars_drawn.append(C)
        if self.header:
            self.header += ', '
        # header[1:] was chosen so that we don't catch first
        # tspan.
        if len(self.header) > 75 and '<tspan' not in self.header[1:]:
            self.header += '</tspan><tspan x="0" dy="1.2em">'
        self.header += '%s (%s)' % (C, PINYIN_DB[C])

    def get_text(self, page_drawn):
        return '''<text x="0" y="5" font-size="5px"><tspan x="0" dy="0em"
            >%d: %s</tspan></text>''' % (page_drawn, self.header)


class Page:

    HEADER_SINGLE = '''<svg width="100%%" height="100%%" viewBox="0 0 %d %d"
    version="1.1" xmlns="http://www.w3.org/2000/svg"
    xmlns:xlink="http://www.w3.org/1999/xlink">''' % PAGE_SIZE

    FOOTER_SINGLE = '</svg>'

    def __init__(self, page_drawn, tile_size, gen_images_iter):
        self.page_drawn = page_drawn
        self.tiles_by_pos = collections.defaultdict(dict)
        self.hdr = Header()
        self.tile_size = tile_size
        self.gen_images_iter = gen_images_iter
        self.f = io.StringIO()
        self.rendered = ''

    def gen_positions(self):

        num_per_row = PAGE_SIZE[0] // self.tile_size
        num_rows = PAGE_SIZE[1] // self.tile_size

        for i in range(num_per_row * (num_rows - 1)):

            row_num = i % num_per_row
            col_num = i // num_per_row

            if ((i // num_per_row) + 3) > num_rows:
                # this page is full, move on to generating another
                break

            yield row_num, col_num

    def maybe_draw_border(self, tile, row_num, col_num):
        if row_num > 0 and (self.tiles_by_pos[row_num - 1][col_num].chunk
                            != tile.chunk):
            tile.leftline_width = LINE_THICK
        if col_num > 0 and (self.tiles_by_pos[row_num][col_num - 1].chunk
                            != tile.chunk):
            tile.topline_width = LINE_THICK

    def write_tiles(self, f):

        for row_num, col_num in self.gen_positions():

            x = row_num * self.tile_size
            y = (col_num + 1) * self.tile_size

            tile = next(self.gen_images_iter)
            self.tiles_by_pos[row_num][col_num] = tile
            tile.set_dimensions(x, y, self.tile_size)

            if not tile.skip_in_header:
                self.hdr.observe_char(tile.C)

            self.maybe_draw_border(tile, row_num, col_num)

            f.write(tile.render())

        return True

    def prepare(self):
        """Renders the page and returns a boolean that tells whether it was
        the last one."""

        self.f.write(self.HEADER_SINGLE)
        try:
            self.write_tiles(self.f)
        finally:
            # in the last page, we will get StopIteration. Regardless of it,
            # We need to finalize rendering.
            self.f.write(self.hdr.get_text(self.page_drawn))
            self.f.write(self.FOOTER_SINGLE)


def gen_svgs(size, gen_images_iter):

    pages = []
    page_drawn = 0
    while True:
        page_drawn += 1
        page = Page(page_drawn, size, gen_images_iter)
        pages.append(page)
        try:
            page.prepare()
        except StopIteration:
            break
    return pages


def gen_pdf(svg_code):
    data_b64 = base64.b64encode(svg_code.encode('utf8')).decode('ascii')
    datauri = 'data:image/svg+xml;base64,' + data_b64
    resp = requests.post('http://html2pdf:5000/html2pdf', {'url': datauri})
    return resp.content


def gen_pdfs(pages):

    merger = PdfFileMerger()
    pdf_files = []
    try:
        for page in pages:
            pdf = gen_pdf(page.f.getvalue())
            pdf_f = io.BytesIO(pdf)
            pdf_files.append(pdf_f)
            merger.append(pdf_f)

        with io.BytesIO() as fout:
            merger.write(fout)
            return fout.getvalue()
    finally:
        for pdf_f in pdf_files:
            pdf_f.close()


def gen_html(pages, small=True):
    # just put together the stream of SVG images
    body = ['<body>']
    for page in pages:
        svg_code = page.f.getvalue()
        if small:
            body.append(svg_code)
            continue
        # The following zooms the images in, but doesn't allow zooming out
        data_b64 = base64.b64encode(svg_code.encode('utf8')).decode('ascii')
        datauri = 'data:image/svg+xml;base64,%s' % data_b64
        body.append('<img src="%s" />' % datauri)
    return ''.join(body)


def draw(input_characters, size, num_repeats, action):

    LOGGER.info('Generating SVG...')
    gen_images_iter = iter(gen_images(input_characters, num_repeats))
    pages = gen_svgs(size, gen_images_iter)
    LOGGER.error('Generating pdfs...')

    if action == 'generate':
        return [gen_pdfs(pages)], {'mimetype': 'application/pdf'}
    if action == 'preview_small':
        return [gen_html(pages)], {'mimetype': 'text/html'}
    if action == 'preview_large':
        return [gen_html(pages, False)], {'mimetype': 'text/html'}
    body = '<h1>Invalid action: %r</h1>' % action
    return [body], {'mimetype': 'text/html', 'status': 400}


@app.route('/gen_strokes', methods=['POST'])
def gen_strokes():
    size = int(request.form.get('size') or 10)
    num_repetitions = int(request.form.get('nr') or 3)

    if 'chars' not in request.form:
        resp_kwargs = {'status': 400, 'mimetype': 'text/html'}
        return Response('<h1>Missing "chars" argument.</h1>', **resp_kwargs)
    C = request.form['chars']

    action = request.form.get('action') or 'preview'
    try:
        resp_args, resp_kwargs = draw(C, size, num_repetitions, action)
    except KeyError as e:
        resp_args = ['<h1>Unknown character: %r</h1>' % e.args[0]]
        resp_kwargs = {'status': 400, 'mimetype': 'text/html'}
    return Response(*resp_args, **resp_kwargs)


@app.route('/')
def index():
    return '''<!DOCTYPE HTML><html><body>
        <form action="/gen_strokes" method="post">
        <p>Characters: <input type="text" name="chars" value="你好"/></p>
        <p>Size: <input type="text" name="size" value="15"/></p>
        <p>Number of repetitions. 0 means "no repetitions"; useful if you're
            just trying to quickly get familiar with many characters:
            <input type="text" name="nr" value="1"/></p>
        <button type="submit" value="generate"
            name="action">Generate (PDF, slow)</button>
        <button type="submit" value="preview_small"
            name="action">Preview (SVG, zoomed out)</button>
        <button type="submit" value="preview_large"
            name="action">Preview (SVG, zoomed in)</button>
    </form>
    '''


def MINIMAL_PDF_MOCK(*_, **__):
    return base64.b64decode(b'''
        JVBERi0xLjEKJcKlwrHDqwoKMSAwIG9iagogIDw8IC9UeXBlIC9DYXRhbG9n
        CiAgICAgL1BhZ2VzIDIgMCBSCiAgPj4KZW5kb2JqCgoyIDAgb2JqCiAgPDwg
        L1R5cGUgL1BhZ2VzCiAgICAgL0tpZHMgWzMgMCBSXQogICAgIC9Db3VudCAx
        CiAgICAgL01lZGlhQm94IFswIDAgMzAwIDE0NF0KICA+PgplbmRvYmoKCjMg
        MCBvYmoKICA8PCAgL1R5cGUgL1BhZ2UKICAgICAgL1BhcmVudCAyIDAgUgog
        ICAgICAvUmVzb3VyY2VzCiAgICAgICA8PCAvRm9udAogICAgICAgICAgIDw8
        IC9GMQogICAgICAgICAgICAgICA8PCAvVHlwZSAvRm9udAogICAgICAgICAg
        ICAgICAgICAvU3VidHlwZSAvVHlwZTEKICAgICAgICAgICAgICAgICAgL0Jh
        c2VGb250IC9UaW1lcy1Sb21hbgogICAgICAgICAgICAgICA+PgogICAgICAg
        ICAgID4+CiAgICAgICA+PgogICAgICAvQ29udGVudHMgNCAwIFIKICA+Pgpl
        bmRvYmoKCjQgMCBvYmoKICA8PCAvTGVuZ3RoIDU1ID4+CnN0cmVhbQogIEJU
        CiAgICAvRjEgMTggVGYKICAgIDAgMCBUZAogICAgKEhlbGxvIFdvcmxkKSBU
        agogIEVUCmVuZHN0cmVhbQplbmRvYmoKCnhyZWYKMCA1CjAwMDAwMDAwMDAg
        NjU1MzUgZiAKMDAwMDAwMDAxOCAwMDAwMCBuIAowMDAwMDAwMDc3IDAwMDAw
        IG4gCjAwMDAwMDAxNzggMDAwMDAgbiAKMDAwMDAwMDQ1NyAwMDAwMCBuIAp0
        cmFpbGVyCiAgPDwgIC9Sb290IDEgMCBSCiAgICAgIC9TaXplIDUKICA+Pgpz
        dGFydHhyZWYKNTY1CiUlRU9GCg==''')


# I was too lazy to write regular unit tests and this is already pretty fast
# and gives decent coverage, so some red flags will be caught:
class SystemTests(unittest.TestCase):

    def setUp(self):
        app.testing = True
        self.app = app.test_client()

    def test_get_index(self):
        rv = self.app.get('/')
        self.assertEqual(rv.status, '200 OK')

    def test_fivedigits_smallpreview(self):
        data = {'size': 12, 'nr': 1, 'action': 'preview_small',
                'chars': '一二三四五'}
        rv = self.app.post('/gen_strokes', data=data)
        self.assertEqual(rv.status, '200 OK')

    def test_fivedigits_smallpreview_norepeats(self):
        data = {'size': 12, 'nr': 0, 'action': 'preview_small',
                'chars': '一二三四五'}
        rv = self.app.post('/gen_strokes', data=data)
        self.assertEqual(rv.status, '200 OK')

    def test_fivedigits_bigpreview(self):
        data = {'size': 12, 'nr': 1, 'action': 'preview_large',
                'chars': '一二三四五'}
        rv = self.app.post('/gen_strokes', data=data)
        self.assertEqual(rv.status, '200 OK')

    def test_xiexie_multipage(self):
        data = {'size': 30, 'nr': 10, 'action': 'preview_small',
                'chars': '谢'}
        rv = self.app.post('/gen_strokes', data=data)
        self.assertEqual(rv.status, '200 OK')

    def test_invalid_action_signals_error(self):
        data = {'size': 12, 'nr': 1, 'action': 'invalid',
                'chars': '一二三四五'}
        rv = self.app.post('/gen_strokes', data=data)
        self.assertNotEqual(rv.status, '200 OK')

    def test_multiline_header(self):
        data = {'size': 12, 'nr': 1, 'action': 'preview_small',
                'chars': '一七三上下不东个中么九习书买了二五些京亮人什'}
        rv = self.app.post('/gen_strokes', data=data)
        self.assertEqual(rv.status, '200 OK')

    @unittest.mock.patch.dict(globals(), {'gen_pdf': MINIMAL_PDF_MOCK})
    def test_gen_pdf(self):
        data = {'size': 12, 'nr': 1, 'action': 'generate',
                'chars': '一二三四五'}
        rv = self.app.post('/gen_strokes', data=data)
        self.assertEqual(rv.status, '200 OK')
