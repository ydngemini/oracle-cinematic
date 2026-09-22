#!/usr/bin/env python3
"""Exercise the served property sheet and real WebGL renderer with synthetic API/media fixtures."""

import argparse
import json
import struct
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, expect, sync_playwright


def mesh_fixture():
    positions = [-1, -1, -1, 1, -1, -1, 1, 1, -1, -1, 1, -1,
                 -1, -1, 1, 1, -1, 1, 1, 1, 1, -1, 1, 1]
    indices = [0, 2, 1, 0, 3, 2, 4, 5, 6, 4, 6, 7, 0, 1, 5, 0, 5, 4,
               3, 7, 6, 3, 6, 2, 0, 4, 7, 0, 7, 3, 1, 2, 6, 1, 6, 5]
    binary = struct.pack('<24f36H', *positions, *indices)
    model = {
        'asset': {'version': '2.0'}, 'scene': 0, 'scenes': [{'nodes': [0]}],
        'nodes': [{'mesh': 0}],
        'meshes': [{'primitives': [{'attributes': {'POSITION': 0}, 'indices': 1, 'material': 0}]}],
        'materials': [{'doubleSided': True, 'pbrMetallicRoughness': {
            'baseColorFactor': [0.2, 0.7, 0.8, 1], 'metallicFactor': 0, 'roughnessFactor': 0.8}}],
        'buffers': [{'byteLength': len(binary)}],
        'bufferViews': [{'buffer': 0, 'byteLength': 96, 'target': 34962},
                        {'buffer': 0, 'byteOffset': 96, 'byteLength': 72, 'target': 34963}],
        'accessors': [{'bufferView': 0, 'componentType': 5126, 'count': 8, 'type': 'VEC3',
                       'min': [-1, -1, -1], 'max': [1, 1, 1]},
                      {'bufferView': 1, 'componentType': 5123, 'count': 36, 'type': 'SCALAR'}],
    }
    encoded = json.dumps(model).encode()
    encoded += b' ' * (-len(encoded) % 4)
    return (struct.pack('<III', 0x46546C67, 2, 28 + len(encoded) + len(binary))
            + struct.pack('<II', len(encoded), 0x4E4F534A) + encoded
            + struct.pack('<II', len(binary), 0x004E4942) + binary)


def run_case(browser, base_url, artifacts, width, reduced=False, low_budget=False):
    print(f'Opening {width}px, reduced={reduced}, low_budget={low_budget}', flush=True)
    context = browser.new_context(viewport={'width': width, 'height': 900 if width > 719 else 740},
                                  reduced_motion='reduce' if reduced else 'no-preference',
                                  service_workers='block')
    context.add_init_script("""
        localStorage.setItem('oracle_product_tour_v1', 'done');
        localStorage.setItem('oracle_active_states', '["DE"]');
        sessionStorage.setItem('oracle_onboarding_dismissed', '1');
    """)
    context.add_init_script(f"""
        Object.defineProperty(navigator, 'hardwareConcurrency', {{get: () => {2 if low_budget else 8}}});
        Object.defineProperty(navigator, 'deviceMemory', {{get: () => 8}});
    """)
    page = context.new_page()
    page.set_default_timeout(15000)
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('console', lambda message: print(message.text, flush=True) if message.type == 'error' else None)
    media_requests = []
    mode = {'value': 'capture'}
    pano = {'scene_id': 'kitchen', 'url': '/api/media/morph-pano.png', 'label': 'Kitchen',
            'is_this_property': True, 'neighbours': [], 'floor_index': 0}
    media = mesh_fixture()

    def respond(route):
        path = urlparse(route.request.url).path
        if not path.startswith(('/api/', '/auth/', '/billing/')):
            route.continue_()
            return
        if route.request.method != 'GET':
            route.fulfill(status=405, json={'detail': 'Read-only browser test'})
            return
        if path.startswith('/api/media/'):
            media_requests.append(path)
            if path.endswith('.png'):
                route.fulfill(content_type='image/png', path='oracle-app/public/oracle-512.png')
            else:
                route.fulfill(content_type='application/octet-stream', body=media)
            return
        payload = {}
        if path == '/auth/session':
            payload = {'authenticated': True, 'role': 'agent'}
        elif path == '/auth/policy-acceptance':
            payload = {'required': False, 'account_security_required': False}
        elif path.startswith('/billing/status/'):
            payload = {'active': True, 'status': 'active', 'plan': 'test'}
        elif path.endswith('/dossier'):
            payload = {'id': 'morph-test', 'parcel_id': 'morph-test', 'state': 'DE', 'interactions': [],
                       'dossier_status': 'ready', 'payload': {'address': '12 Test Avenue',
                                                            'city': 'Dover', 'zoning_code': 'R-1'}}
        elif path == '/api/crm/property-tour':
            if mode['value'] == 'error':
                route.fulfill(status=403, json={'detail': 'Fixture: unavailable tour'})
                return
            payload = {'splat_url': '/api/media/morph-test.glb', 'splat_format': '.glb',
                       'splat_scene': {'denseBounds': {'min': [-1, -1, -1], 'max': [1, 1, 1]}},
                       'is_this_property': True, 'disclosure': 'Synthetic geometry for browser verification.',
                       'pano_scenes': [pano, {**pano, 'scene_id': 'entry', 'label': 'Entry'}],
                       'pano_scene_count': 2, 'photo_count': 0,
                       'tourpoints': [{'scene_id': 'kitchen', 'label': 'Kitchen'},
                                      {'scene_id': 'entry', 'label': 'Entry'}]}
            if mode['value'] == 'empty':
                payload = {'splat_url': None, 'pano_scenes': [], 'pano_scene_count': 0, 'photo_count': 0}
        elif path == '/api/ai/chat/messages':
            payload = {'messages': []}
        route.fulfill(json=payload)

    for pattern in ('**/api/**', '**/auth/**', '**/billing/**'):
        context.route(pattern, respond)
    context.route_web_socket('**/*', lambda socket: None)
    print('Loading property route', flush=True)
    page.goto(f'{base_url}/property/morph-test', wait_until='domcontentloaded')
    try:
        page.wait_for_load_state('networkidle', timeout=8000)
    except PlaywrightTimeoutError:
        print('Network still active; checking the rendered sheet', flush=True)
    print('Property route loaded', flush=True)
    sheet = page.get_by_role('dialog', name='Property — 12 Test Avenue')
    expect(sheet).to_be_visible()
    opener = sheet.get_by_role('button', name='Explore in 3D', exact=True)
    expect(opener).to_be_enabled()
    note = sheet.get_by_placeholder('R-2', exact=True)
    expect(note).to_be_visible()
    note.fill('Unsaved zoning')
    scroller = sheet.locator('[class*="body_"]').first
    scroller.evaluate('(node) => { node.scrollTop = 160; window.savedDossier = node; }')
    previous_scroll = scroller.evaluate('(node) => node.scrollTop')
    opener.focus()
    label = f'{width}-{"reduced" if reduced else "low-budget" if low_budget else "motion"}'
    page.screenshot(path=str(artifacts / f'{label}-property.png'))
    widths = page.evaluate("""async () => {
        const sheet = document.querySelector('section[role="dialog"]');
        const widths = [sheet.getBoundingClientRect().width];
        [...sheet.querySelectorAll('button')].find(node => node.textContent === 'Explore in 3D').click();
        for (let frame = 0; frame < 35; frame++) {
            await new Promise(requestAnimationFrame);
            widths.push(sheet.getBoundingClientRect().width);
        }
        return widths;
    }""")
    print('Sheet expanded; waiting for the renderer', flush=True)
    expect(page).to_have_url(f'{base_url}/property/morph-test?tour=3d')
    expect(sheet.get_by_role('group', name='Camera mode')).to_be_visible(timeout=45000)
    back = sheet.get_by_role('button', name='Back to property', exact=True)
    expect(back).to_be_focused()
    expect(page.get_by_role('dialog')).to_have_count(1)
    expect(note).to_be_hidden()
    bounds = sheet.bounding_box()
    assert abs(bounds['width'] - width) < 2, bounds
    canvas = sheet.locator('canvas')
    canvas_bounds = canvas.bounding_box()
    assert abs(canvas_bounds['width'] - width) < 2, canvas_bounds
    assert canvas_bounds['height'] > 350, canvas_bounds
    assert canvas_bounds['y'] >= back.bounding_box()['y'] + back.bounding_box()['height']
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    if width > 719 and not reduced and not low_budget:
        assert any(602 < value < width - 2 for value in widths), widths
    if reduced or low_budget:
        assert all(abs(value - width) < 2 or abs(value - widths[0]) < 2 for value in widths), widths
    page.screenshot(path=str(artifacts / f'{label}-tour.png'))
    assert '/api/media/morph-test.glb' in media_requests
    page.keyboard.press('Shift+Tab')
    assert sheet.evaluate('(node) => node.contains(document.activeElement)')
    page.keyboard.press('Tab')
    expect(back).to_be_focused()
    page.keyboard.press('Escape')
    expect(opener).to_be_visible()
    expect(opener).to_be_focused()
    expect(note).to_have_value('Unsaved zoning')
    expect(sheet.locator('canvas')).to_have_count(0)
    assert scroller.evaluate('(node) => node === window.savedDossier')
    assert abs(scroller.evaluate('(node) => node.scrollTop') - previous_scroll) < 2
    page.go_forward()
    expect(back).to_be_visible()
    expect(sheet.get_by_role('group', name='Camera mode')).to_be_visible()
    sheet.get_by_role('button', name='360° walkthrough', exact=True).click()
    expect(sheet.get_by_role('navigation', name='Guided tour')).to_be_visible()
    sheet.get_by_role('button', name='Start the guided tour', exact=True).click()
    expect(sheet.get_by_text('Kitchen · 1 of 2', exact=True)).to_be_visible()
    page.go_back()
    expect(opener).to_be_visible()
    expect(note).to_have_value('Unsaved zoning')
    assert scroller.evaluate('(node) => node === window.savedDossier')
    page.goto(f'{base_url}/property/morph-test?tour=3d', wait_until='networkidle')
    expect(back).to_be_visible()
    back.click()
    expect(page).to_have_url(f'{base_url}/property/morph-test')
    expect(opener).to_be_visible()
    assert sheet.evaluate('(node) => node.contains(document.activeElement)')
    mode['value'] = 'empty'
    page.reload(wait_until='networkidle')
    expect(sheet.get_by_role('button', name='3D tour not captured yet')).to_be_disabled()
    mode['value'] = 'error'
    page.goto(f'{base_url}/property/morph-test?tour=3d', wait_until='networkidle')
    expect(sheet.get_by_role('alert')).to_contain_text('could not be loaded')
    back.click()
    expect(sheet.get_by_role('button', name='Tour status unavailable')).to_be_disabled()
    assert not errors, errors
    result = {'case': label, 'canvas': canvas_bounds, 'animation_samples': widths, 'errors': errors}
    context.close()
    print(json.dumps(result), flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='http://127.0.0.1:5173')
    parser.add_argument('--artifacts', type=Path, default=Path('/tmp/neoh-property-morph'))
    parser.add_argument('--desktop-only', action='store_true')
    args = parser.parse_args()
    args.artifacts.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=['--enable-unsafe-swiftshader'])
        cases = [(1440, False, False)] if args.desktop_only else [
            (1440, False, False), (1024, False, False), (768, False, False),
            (320, False, False), (390, True, False), (1440, False, True),
        ]
        try:
            results = [run_case(browser, args.base_url, args.artifacts, *case) for case in cases]
            (args.artifacts / 'results.json').write_text(json.dumps(results, indent=2))
        finally:
            browser.close()


if __name__ == '__main__':
    main()
